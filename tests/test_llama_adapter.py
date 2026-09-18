"""llama.cpp capability adapter tests (M04/P15, C06).

These tests pin the first real BackendPort adapter: it consumes the registered
chat/vision/embeddings/rerank protocols, refuses to dispatch when token or
image counting fails, never follows redirects, and never treats an HTTP 200 as
device quiescence. llama-swap stays on the lifecycle boundary only.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import struct
import zlib
from pathlib import Path

import httpx
import pytest

from model_scheduler.backend_router import BackendRouter, BackendRouterError
from model_scheduler.contracts_v2 import GGUF_PROFILE, HF_SHARDED_PROFILE, parse_deployment
from model_scheduler.control_protocol_v1 import Fence, InstanceIdentity
from model_scheduler.ports_v3 import (
    BackendPort,
    CancelAck,
    ExecutionHandle,
    ExecutionRequest,
    Observation,
    StopAck,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "llama_cpp_v1.json"
REQUIREMENTS = Path(__file__).resolve().parent.parent / "requirements.in"
REQUIREMENTS_LOCK = Path(__file__).resolve().parent.parent / "requirements.lock"

GGUF_RUNTIME = "llama-cpp-cuda-sm87-4bc272f"


def _encode_png(width: int, height: int) -> bytes:
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        for x in range(width):
            raw.extend((x & 0xFF, y & 0xFF, (x + y) & 0xFF))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)

    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b"")


def _data_url(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def _deployment() -> dict:
    return {
        "schema_version": 2,
        "runtimes": [
            {
                "runtime_id": GGUF_RUNTIME,
                "profile_id": GGUF_PROFILE,
                "image_digest": "ghcr.io/example/llama-cuda@sha256:" + "a" * 64,
                "adapter_sha256": "b" * 64,
                "lock_sha256": "c" * 64,
                "startup_args": ["--parallel", "--kv-unified-per-slot", "--image-max-tokens", "--no-warmup"],
            },
            {
                "runtime_id": "hf-transformers-cpu",
                "profile_id": HF_SHARDED_PROFILE,
                "image_digest": "ghcr.io/example/hf-serve@sha256:" + "f" * 64,
                "adapter_sha256": "b" * 64,
                "lock_sha256": "c" * 64,
                "startup_args": ["--host", "--port"],
            },
        ],
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
                "runtime_id": "hf-transformers-cpu",
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


def _parsed():
    return parse_deployment(_deployment())


def _runtime():
    return {item.runtime_id: item for item in _parsed().runtimes}[GGUF_RUNTIME]


def _model():
    return {item.model_id: item for item in _parsed().models}["qwen25vl-7b-q4"]


def _fence(**overrides) -> Fence:
    values = {
        "boot_id": "boot-0001",
        "model_id": "qwen25vl-7b-q4",
        "generation": 3,
        "operation_id": "op-0001",
        "execution_id": "e-1",
        "attempt": 1,
    }
    values.update(overrides)
    return Fence(**values)


def _identity() -> InstanceIdentity:
    return InstanceIdentity(
        container_id="c-abc",
        started_at="2026-09-17T05:00:00Z",
        deployment_id="orin-local",
        model_id="qwen25vl-7b-q4",
        runtime_id=GGUF_RUNTIME,
        candidate_digest="b" * 64,
        image_digest="ghcr.io/example/llama-cuda@sha256:" + "c" * 64,
    )


def _chat_ok() -> dict:
    return {
        "id": "chatcmpl-1",
        "model": "qwen25vl-7b-q4",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5},
    }


class RecordingTransport(httpx.AsyncBaseTransport):
    """Scripted llama-server: records every request and never follows anything."""

    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        handler = self.routes.get(request.url.path)
        if handler is None:
            return httpx.Response(404, json={"error": "missing"})
        if callable(handler):
            return handler(request)
        status, payload = handler
        if isinstance(payload, bytes):
            return httpx.Response(status, content=payload)
        if isinstance(payload, httpx.Response):
            return payload
        return httpx.Response(status, json=payload)


def _tokenize_by_words(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    content = body["content"]
    tokens = list(range(max(1, len(content.split()))))
    return httpx.Response(200, json={"tokens": tokens})


def _apply_template(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    messages = body["messages"]
    parts = []
    for message in messages:
        content = message.get("content", "")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
    return httpx.Response(200, json={"prompt": " ".join(parts)})


def _client(routes: dict[str, object], *, follow_redirects: bool = False) -> tuple[httpx.AsyncClient, RecordingTransport]:
    transport = RecordingTransport(routes)
    client = httpx.AsyncClient(transport=transport, follow_redirects=follow_redirects, base_url="http://127.0.0.1:18081")
    return client, transport


def _adapter(client: httpx.AsyncClient, **overrides):
    from model_scheduler.adapters.llama_cpp import LlamaCppAdapter

    values = {
        "runtime": _runtime(),
        "model": _model(),
        "inference_base_url": "http://127.0.0.1:18081",
        "client": client,
        "identity": _identity(),
        "fixture_path": FIXTURE,
    }
    values.update(overrides)
    return LlamaCppAdapter(**values)


def _deadline() -> float:
    return asyncio.get_running_loop().time() + 10


def test_fixture_pins_the_m00_protocol_and_refuses_to_treat_http_as_quiescent() -> None:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert data["profile_id"] == GGUF_PROFILE
    assert data["http"]["follow_redirects"] is False
    assert data["endpoints"]["chat"]["path"] == "/v1/chat/completions"
    assert data["endpoints"]["tokenize"]["path"] == "/tokenize"
    assert data["endpoints"]["apply_template"]["required_for_chat_counting"] is True
    assert data["slot_protocol"]["trusted_for_device_quiescence"] is False
    assert data["slot_protocol"]["fallback_termination"] == "independent_STOPPED"
    assert data["slot_protocol"]["stop_reload_cost"]["measured_cold_load_seconds"] == 18.07
    assert data["lifecycle_boundary"]["llama_swap"] == "load and unload only"
    digest = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    assert len(digest) == 64


def test_scheduler_lockfiles_do_not_pull_torch_or_ort() -> None:
    declared = REQUIREMENTS.read_text(encoding="utf-8").lower()
    locked = REQUIREMENTS_LOCK.read_text(encoding="utf-8").lower()
    for name in ("torch", "onnxruntime", "onnxruntime-gpu"):
        assert name not in declared
        assert not any(line.startswith(name + "==") or line.startswith(name + "[") for line in locked.splitlines())


@pytest.mark.asyncio
async def test_adapter_satisfies_backend_port_and_consumes_chat_messages() -> None:
    seen: list[dict] = []

    def chat(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=_chat_ok())

    client, transport = _client(
        {
            "/apply-template": _apply_template,
            "/tokenize": _tokenize_by_words,
            "/v1/chat/completions": chat,
            "/slots": (200, [{"id": 0, "is_processing": False, "n_ctx": 32768}]),
        }
    )
    adapter = _adapter(client)
    assert isinstance(adapter, BackendPort)
    handle = await adapter.execute(
        ExecutionRequest(
            execution_id="e-1",
            operation="chat",
            inline_input={"messages": [{"role": "user", "content": "hello there"}]},
            parameters={"max_tokens": 16},
        ),
        _fence(),
        _deadline(),
    )
    assert isinstance(handle, ExecutionHandle)
    assert handle.execution_id == "e-1"
    assert handle.instance.container_id == "c-abc"
    assert seen[0]["messages"][0]["content"] == "hello there"
    assert seen[0]["max_tokens"] == 16
    assert adapter.claims_device_quiescence() is False
    paths = [str(request.url.path) for request in transport.requests]
    assert "/apply-template" in paths
    assert "/tokenize" in paths
    assert "/v1/chat/completions" in paths
    await client.aclose()


@pytest.mark.asyncio
async def test_token_counting_failure_refuses_dispatch() -> None:
    dispatched: list[str] = []

    def explode(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "tokenizer down"})

    def chat(request: httpx.Request) -> httpx.Response:
        dispatched.append(str(request.url.path))
        return httpx.Response(200, json=_chat_ok())

    client, _transport = _client({"/apply-template": explode, "/tokenize": explode, "/v1/chat/completions": chat})
    adapter = _adapter(client)
    with pytest.raises(Exception, match="envelope|count|tokenize|template") as error:
        await adapter.execute(
            ExecutionRequest(
                execution_id="e-1",
                operation="chat",
                inline_input={"messages": [{"role": "user", "content": "hello"}]},
            ),
            _fence(),
            _deadline(),
        )
    assert dispatched == []
    assert getattr(error.value, "code", "envelope_exceeded") in {"envelope_exceeded", "backend_failed", "contract_violation"}
    await client.aclose()


@pytest.mark.asyncio
async def test_over_envelope_input_is_rejected_before_the_chat_path() -> None:
    dispatched: list[str] = []

    def chat(request: httpx.Request) -> httpx.Response:
        dispatched.append("chat")
        return httpx.Response(200, json=_chat_ok())

    client, transport = _client(
        {
            "/apply-template": _apply_template,
            "/tokenize": _tokenize_by_words,
            "/v1/chat/completions": chat,
        }
    )
    adapter = _adapter(client)
    with pytest.raises(Exception, match="envelope"):
        await adapter.execute(
            ExecutionRequest(
                execution_id="e-1",
                operation="chat",
                inline_input={"messages": [{"role": "user", "content": "word " * 30000}]},
            ),
            _fence(),
            _deadline(),
        )
    assert dispatched == []
    assert all(request.url.path != "/v1/chat/completions" for request in transport.requests)
    await client.aclose()


@pytest.mark.asyncio
async def test_remote_image_url_is_rejected_and_not_fetched() -> None:
    client, transport = _client(
        {
            "/apply-template": _apply_template,
            "/tokenize": _tokenize_by_words,
            "/v1/chat/completions": (200, _chat_ok()),
        }
    )
    adapter = _adapter(client)
    with pytest.raises(Exception, match="url|image|envelope|contract"):
        await adapter.execute(
            ExecutionRequest(
                execution_id="e-1",
                operation="vision",
                inline_input={
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "what is this"},
                                {"type": "image_url", "image_url": {"url": "https://evil.example/x.png"}},
                            ],
                        }
                    ]
                },
            ),
            _fence(),
            _deadline(),
        )
    hosts = {request.url.host for request in transport.requests}
    assert "evil.example" not in hosts
    assert all(request.url.path != "/v1/chat/completions" for request in transport.requests)
    await client.aclose()


@pytest.mark.asyncio
async def test_image_edge_and_count_limits_are_enforced_before_dispatch() -> None:
    client, transport = _client(
        {
            "/apply-template": _apply_template,
            "/tokenize": _tokenize_by_words,
            "/v1/chat/completions": (200, _chat_ok()),
        }
    )
    adapter = _adapter(client)
    oversized = _data_url(_encode_png(1025, 32))
    with pytest.raises(Exception, match="envelope|image"):
        await adapter.execute(
            ExecutionRequest(
                execution_id="e-1",
                operation="vision",
                inline_input={
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "look"},
                                {"type": "image_url", "image_url": {"url": oversized}},
                            ],
                        }
                    ]
                },
            ),
            _fence(),
            _deadline(),
        )
    two = _data_url(_encode_png(32, 32))
    with pytest.raises(Exception, match="envelope|image"):
        await adapter.execute(
            ExecutionRequest(
                execution_id="e-2",
                operation="vision",
                inline_input={
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "look"},
                                {"type": "image_url", "image_url": {"url": two}},
                                {"type": "image_url", "image_url": {"url": two}},
                            ],
                        }
                    ]
                },
            ),
            _fence(),
            _deadline(),
        )
    assert all(request.url.path != "/v1/chat/completions" for request in transport.requests)
    await client.aclose()


@pytest.mark.asyncio
async def test_undetermined_image_tokens_are_charged_at_the_registered_worst_case() -> None:
    seen: list[int] = []

    def tokenize(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        # 28600 text tokens so that +1280 worst-case image tokens exceeds 28672.
        n = 28600 if "look" in body["content"] else 1
        return httpx.Response(200, json={"tokens": list(range(n))})

    client, transport = _client(
        {
            "/apply-template": _apply_template,
            "/tokenize": tokenize,
            "/v1/chat/completions": lambda request: seen.append(1) or httpx.Response(200, json=_chat_ok()),
        }
    )
    adapter = _adapter(client)
    with pytest.raises(Exception, match="envelope"):
        await adapter.execute(
            ExecutionRequest(
                execution_id="e-1",
                operation="vision",
                inline_input={
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "look"},
                                {"type": "image_url", "image_url": {"url": _data_url(_encode_png(64, 64))}},
                            ],
                        }
                    ]
                },
            ),
            _fence(),
            _deadline(),
        )
    assert seen == []
    assert all(request.url.path != "/v1/chat/completions" for request in transport.requests)
    await client.aclose()


@pytest.mark.asyncio
async def test_non_finite_and_malformed_outputs_are_rejected() -> None:
    def embeddings(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"data":[{"index":0,"embedding":[1.0,NaN]}]}')

    def chat(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "x", "model": "qwen25vl-7b-q4"})

    client, _transport = _client(
        {
            "/apply-template": _apply_template,
            "/tokenize": _tokenize_by_words,
            "/v1/embeddings": embeddings,
            "/v1/chat/completions": chat,
        }
    )
    adapter = _adapter(client)
    with pytest.raises(Exception, match="output|finite|shape|protocol"):
        await adapter.execute(
            ExecutionRequest(execution_id="e-1", operation="chat", inline_input={"messages": [{"role": "user", "content": "hi"}]}),
            _fence(),
            _deadline(),
        )

    embed_spec = parse_deployment(_deployment()).models[0]
    embed_only = type(embed_spec)(
        **{
            **embed_spec.__dict__,
            "capabilities": ("embeddings",),
        }
    )
    embed_adapter = _adapter(client, model=embed_only)
    with pytest.raises(Exception, match="output|finite|shape|protocol"):
        await embed_adapter.execute(
            ExecutionRequest(execution_id="e-2", operation="embeddings", inline_input={"input": "hello"}),
            _fence(),
            _deadline(),
        )
    await client.aclose()


@pytest.mark.asyncio
async def test_redirects_are_not_followed_to_an_external_url() -> None:
    def bounce(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example/steal"})

    client, transport = _client(
        {
            "/apply-template": _apply_template,
            "/tokenize": _tokenize_by_words,
            "/v1/chat/completions": bounce,
        }
    )
    adapter = _adapter(client)
    with pytest.raises(Exception, match="redirect|upstream|protocol|backend"):
        await adapter.execute(
            ExecutionRequest(
                execution_id="e-1",
                operation="chat",
                inline_input={"messages": [{"role": "user", "content": "hello"}]},
            ),
            _fence(),
            _deadline(),
        )
    hosts = {request.url.host for request in transport.requests}
    schemes = {request.url.scheme for request in transport.requests}
    assert "evil.example" not in hosts
    assert schemes <= {"http"}
    await client.aclose()


@pytest.mark.asyncio
async def test_http_completion_and_idle_slot_do_not_claim_device_quiescence() -> None:
    client, _transport = _client(
        {
            "/apply-template": _apply_template,
            "/tokenize": _tokenize_by_words,
            "/v1/chat/completions": (200, _chat_ok()),
            "/slots": (200, [{"id": 0, "is_processing": False, "id_task": -1, "n_ctx": 32768}]),
        }
    )
    adapter = _adapter(client)
    await adapter.execute(
        ExecutionRequest(
            execution_id="e-1",
            operation="chat",
            inline_input={"messages": [{"role": "user", "content": "hello"}]},
        ),
        _fence(),
        _deadline(),
    )
    assert adapter.slot_protocol["trusted_for_device_quiescence"] is False
    assert adapter.slot_protocol["fallback_termination"] == "independent_STOPPED"
    assert adapter.last_http_complete is True
    assert adapter.last_slot_idle is True
    assert adapter.claims_device_quiescence() is False
    assert adapter.stop_reload_cost["measured_cold_load_seconds"] == 18.07
    await client.aclose()


@pytest.mark.asyncio
async def test_unknown_and_registration_only_profiles_are_refused() -> None:
    from model_scheduler.adapters.llama_cpp import AdapterError, LlamaCppAdapter

    client, _transport = _client({})
    hf = {item.runtime_id: item for item in _parsed().runtimes}["hf-transformers-cpu"]
    hf_model = {item.model_id: item for item in _parsed().models}["hf-sharded-model"]
    with pytest.raises(AdapterError, match="profile"):
        LlamaCppAdapter(
            runtime=hf,
            model=hf_model,
            inference_base_url="http://127.0.0.1:18082",
            client=client,
            identity=_identity(),
            fixture_path=FIXTURE,
        )
    await client.aclose()


def test_router_refuses_a_llama_adapter_on_a_runtime_without_this_profile() -> None:
    class Claimant:
        runtime_id = GGUF_RUNTIME
        profile_id = "onnx-runtime-v9"

        async def load(self, spec, fence, deadline):
            raise AssertionError("not used")

        async def execute(self, request, fence, deadline):
            raise AssertionError("not used")

        async def cancel(self, handle, deadline):
            raise AssertionError("not used")

        async def stop(self, identity, fence, deadline):
            raise AssertionError("not used")

    deployment = _parsed()
    router = BackendRouter(
        runtimes={item.runtime_id: item for item in deployment.runtimes},
        models={item.model_id: item for item in deployment.models},
    )
    with pytest.raises(BackendRouterError, match="profile"):
        router.register(GGUF_RUNTIME, Claimant())
    assert router.bindings == {}


def test_adapter_has_no_business_job_or_stage_surface() -> None:
    from model_scheduler.adapters import llama_cpp as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    for forbidden in ("job_id", "stage_id", "pipeline", "film", "subtitle"):
        assert forbidden not in source
    assert not hasattr(module.LlamaCppAdapter, "submit_job")


@pytest.mark.asyncio
async def test_load_uses_lifecycle_control_and_requires_more_than_http_ok() -> None:
    loaded: list[str] = []

    class Control:
        async def load(self, model_id: str) -> None:
            loaded.append(model_id)

        async def unload(self, model_id: str) -> None:
            loaded.append(f"unload:{model_id}")

    client, _transport = _client(
        {
            "/health": (200, {"status": "ok"}),
            "/slots": (200, [{"id": 0, "is_processing": False, "n_ctx": 32768}, {"id": 1, "is_processing": False, "n_ctx": 32768}]),
        }
    )
    adapter = _adapter(client, control=Control(), identity=None)
    observation = await adapter.load(_model(), _fence(execution_id=None, attempt=None), _deadline())
    assert loaded == ["qwen25vl-7b-q4"]
    assert isinstance(observation, Observation)
    assert observation.state == "running"
    assert observation.port_state == "listening"
    stop = await adapter.stop(_identity(), _fence(execution_id=None, attempt=None), _deadline())
    assert isinstance(stop, StopAck) and stop.accepted is True
    assert loaded[-1] == "unload:qwen25vl-7b-q4"
    await client.aclose()


@pytest.mark.asyncio
async def test_cancel_is_an_ack_not_a_stop_proof() -> None:
    client, _transport = _client({})
    adapter = _adapter(client)
    ack = await adapter.cancel(ExecutionHandle(execution_id="e-1", instance=_identity()), _deadline())
    assert isinstance(ack, CancelAck)
    assert ack.execution_id == "e-1"
    await client.aclose()
