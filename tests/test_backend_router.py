from __future__ import annotations

import pytest

from model_scheduler.backend_router import BackendRouter, BackendRouterError
from model_scheduler.contracts_v2 import GGUF_PROFILE, HF_SHARDED_PROFILE, parse_deployment

GGUF_RUNTIME = "llama-cpp-cuda-sm87-4bc272f"
GGUF_ALT_RUNTIME = "llama-cpp-cuda-sm87-alt"
HF_RUNTIME = "hf-transformers-cpu"


def _runtimes() -> list[dict]:
    return [
        {
            "runtime_id": GGUF_RUNTIME,
            "profile_id": GGUF_PROFILE,
            "image_digest": "ghcr.io/example/llama-cuda@sha256:" + "a" * 64,
            "adapter_sha256": "b" * 64,
            "lock_sha256": "c" * 64,
            "startup_args": ["--parallel", "--kv-unified-per-slot", "--image-max-tokens", "--no-warmup"],
        },
        {
            "runtime_id": GGUF_ALT_RUNTIME,
            "profile_id": GGUF_PROFILE,
            "image_digest": "ghcr.io/example/llama-cuda-next@sha256:" + "9" * 64,
            "adapter_sha256": "b" * 64,
            "lock_sha256": "c" * 64,
            "startup_args": ["--parallel", "--kv-unified-per-slot"],
        },
        {
            "runtime_id": HF_RUNTIME,
            "profile_id": HF_SHARDED_PROFILE,
            "image_digest": "ghcr.io/example/hf-serve@sha256:" + "f" * 64,
            "adapter_sha256": "b" * 64,
            "lock_sha256": "c" * 64,
            "startup_args": ["--host", "--port"],
        },
    ]


def _deployment() -> dict:
    """The M00 registration plus a registration-only sharded runtime (P01/P06)."""
    return {
        "schema_version": 2,
        "runtimes": _runtimes(),
        "models": [
            {
                "model_id": "qwen25vl-7b-q4",
                "runtime_id": GGUF_RUNTIME,
                "capabilities": ["chat", "vision"],
                "assets": [
                    {
                        "role": "model",
                        "path": "qwen25vl-7b-q4/model.gguf",
                        "sha256": "3f" + "0" * 62,
                        "size_bytes": 4683072320,
                    },
                    {
                        "role": "projector",
                        "path": "qwen25vl-7b-q4/mmproj.gguf",
                        "sha256": "d1" + "0" * 62,
                        "size_bytes": 1354162912,
                    },
                ],
                "port": 18081,
                "envelope": {
                    "ctx_size": 32768,
                    "max_input_tokens": 28672,
                    "max_output_tokens": 4096,
                    "max_parallel": 2,
                    "max_image_tokens": 1280,
                    "max_image_edge_pixels": 1024,
                    "max_images": 1,
                },
                "timeout_seconds": 3600,
                "reserved_bytes": 6106148045,
                "measured": True,
                "measurement_ref": "e" * 64,
                "physical_resident_peak_bytes": 5_000_000_000,
            },
            {
                "model_id": "hf-sharded-model",
                "runtime_id": HF_RUNTIME,
                "capabilities": ["chat"],
                "assets": [
                    {"role": "model", "path": "hf/shard-00001.safetensors", "sha256": "1" * 64, "size_bytes": 1000},
                    {"role": "model", "path": "hf/shard-00002.safetensors", "sha256": "2" * 64, "size_bytes": 1000},
                    {"role": "tokenizer", "path": "hf/tokenizer.json", "sha256": "4" * 64, "size_bytes": 10},
                ],
                "port": 18082,
                "envelope": {
                    "ctx_size": 4096,
                    "max_input_tokens": 3072,
                    "max_output_tokens": 1024,
                    "max_parallel": 1,
                    "max_image_tokens": 0,
                    "max_image_edge_pixels": 0,
                    "max_images": 0,
                },
                "timeout_seconds": 600,
                "reserved_bytes": 1000,
                "measured": False,
                "measurement_ref": None,
                "physical_resident_peak_bytes": None,
            },
        ],
    }


def _router() -> BackendRouter:
    deployment = parse_deployment(_deployment())
    return BackendRouter(
        runtimes={runtime.runtime_id: runtime for runtime in deployment.runtimes},
        models={model.model_id: model for model in deployment.models},
    )


class Adapter:
    """A fake backend adapter; only the port methods are part of the contract."""

    def __init__(self, declared_runtime_id: str | None = None) -> None:
        if declared_runtime_id is not None:
            self.runtime_id = declared_runtime_id

    async def load(self, spec, fence, deadline): raise AssertionError("not used")
    async def execute(self, request, fence, deadline): raise AssertionError("not used")
    async def cancel(self, handle, deadline): raise AssertionError("not used")
    async def stop(self, identity, fence, deadline): raise AssertionError("not used")


class Incomplete:
    async def load(self, spec, fence, deadline): raise AssertionError("not used")
    async def execute(self, request, fence, deadline): raise AssertionError("not used")
    async def cancel(self, handle, deadline): raise AssertionError("not used")


def test_router_selects_the_adapter_bound_to_the_model_runtime() -> None:
    router = _router()
    adapter = Adapter()
    router.register(GGUF_RUNTIME, adapter)

    assert router.backend_for("qwen25vl-7b-q4") is adapter
    assert router.runtime_spec("qwen25vl-7b-q4").runtime_id == GGUF_RUNTIME
    assert router.bindings[GGUF_RUNTIME].profile_id == GGUF_PROFILE
    assert router.bindings[GGUF_RUNTIME].image_digest.endswith("a" * 64)


def test_router_never_falls_back_to_another_runtime_adapter() -> None:
    router = _router()
    router.register(GGUF_RUNTIME, Adapter())

    with pytest.raises(BackendRouterError, match="no adapter for runtime"):
        router.backend_for("hf-sharded-model")
    with pytest.raises(BackendRouterError, match="unknown model"):
        router.backend_for("missing-model")


def test_router_refuses_registration_only_profiles_and_unknown_runtimes() -> None:
    router = _router()

    with pytest.raises(BackendRouterError, match="registration-only"):
        router.register(HF_RUNTIME, Adapter())
    with pytest.raises(BackendRouterError, match="unknown runtime"):
        router.register("not-registered", Adapter())
    assert router.bindings == {}


def test_router_refuses_an_adapter_that_declares_another_runtime_identity() -> None:
    router = _router()

    with pytest.raises(BackendRouterError, match="runtime identity"):
        router.register(GGUF_RUNTIME, Adapter(declared_runtime_id="some-other-runtime"))
    assert router.bindings == {}


def test_router_refuses_a_duplicate_runtime_registration() -> None:
    router = _router()
    adapter = Adapter()
    router.register(GGUF_RUNTIME, adapter)

    with pytest.raises(BackendRouterError, match="already registered"):
        router.register(GGUF_RUNTIME, Adapter())
    assert router.backend_for("qwen25vl-7b-q4") is adapter


def test_router_refuses_to_reuse_one_adapter_for_two_runtime_identities() -> None:
    router = _router()
    adapter = Adapter()
    router.register(GGUF_RUNTIME, adapter)

    with pytest.raises(BackendRouterError, match="bound to runtime"):
        router.register(GGUF_ALT_RUNTIME, adapter)
    assert set(router.bindings) == {GGUF_RUNTIME}


def test_router_refuses_an_adapter_that_does_not_satisfy_the_backend_port() -> None:
    router = _router()

    with pytest.raises(BackendRouterError, match="BackendPort"):
        router.register(GGUF_RUNTIME, Incomplete())
    assert router.bindings == {}
