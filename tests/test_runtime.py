from __future__ import annotations

from pathlib import Path

import pytest

from model_scheduler.config import load_config
from model_scheduler.control_recovery import ControlRecoveryClient
from model_scheduler.contracts import MemorySample
from model_scheduler.llama_swap_client import LlamaSwapControlContract
from model_scheduler.runtime import RuntimeCompositionError, build_backend, build_scheduler
from app import create_app



class Resources:
    def snapshot_now(self): return MemorySample(64 * 1024**3, 60 * 1024**3, 0)
    async def snapshot(self): return self.snapshot_now()


class Backend:
    async def load(self, operation, deadline): raise AssertionError
    async def stop(self, operation, deadline): raise AssertionError


def test_runtime_builder_wires_strict_config_to_single_scheduler_book(tmp_path) -> None:
    config = load_config(Path(__file__).resolve().parent.parent / "config.yaml")
    scheduler = build_scheduler(config, Backend(), resources=Resources(), storage_guard=lambda: True)
    assert scheduler.book.specs["embedding"].preload is True
    assert scheduler.queue_capacity == config.scheduler.queue_capacity
    assert scheduler.book.model_budget == 64 * 1024**3 - config.resources.system_reserve_bytes - config.scheduler.min_free_memory_bytes


def test_runtime_builder_defaults_to_a_fail_closed_storage_guard() -> None:
    config = load_config(Path(__file__).resolve().parent.parent / "config.yaml")
    scheduler = build_scheduler(config, Backend(), resources=Resources())
    assert scheduler.admission_guard is not None
    assert isinstance(scheduler.recovery, ControlRecoveryClient)


def test_app_factory_composes_runtime_when_control_backend_is_injected() -> None:
    app = create_app(backend=Backend(), resources=Resources(), storage_guard=lambda: True)
    assert app.state.scheduler.book.specs["embedding"].preload is True
    assert app.state.gateway is not None


def _control_contract() -> LlamaSwapControlContract:
    return LlamaSwapControlContract(
        running_parser=lambda _: [],
        load_path="/fixed/load",
        unload_path="/fixed/unload/{model_id}",
        validate_load_response=lambda _: None,
        validate_unload_response=lambda _: None,
    )


def _manifest(config, digest: str) -> dict:
    return {
        "deployment_id": "lab",
        "image": "repo/image@sha256:" + "a" * 64,
        "config_sha256": digest,
        "models": {
            model_id: {
                "container_name": model.container_name,
                "file": model.file,
                "sha256": model.sha256,
            }
            for model_id, model in config.models.items()
        },
    }


def test_runtime_backend_is_composed_only_from_matching_manifest_identity() -> None:
    config = load_config(Path(__file__).resolve().parent.parent / "config.yaml")
    digest = "b" * 64
    contract = _control_contract()
    backend = build_backend(config, _manifest(config, digest), digest, contract)

    assert backend.control.contract is contract
    assert backend.observer.deployment_id == "lab"
    assert backend.observer.image_digest.endswith("a" * 64)
    assert backend.models["qwen-small"].container_name == "sms-lab-qwen-small"
    assert backend.models["qwen-small"].port == 10003


def test_runtime_backend_refuses_manifest_config_or_container_identity_mismatches() -> None:
    config = load_config(Path(__file__).resolve().parent.parent / "config.yaml")
    digest = "b" * 64
    manifest = _manifest(config, digest)
    manifest["models"]["qwen-small"]["container_name"] = "sms-other-qwen-small"

    with pytest.raises(RuntimeCompositionError, match="manifest/config"):
        build_backend(config, manifest, digest, _control_contract())

    manifest = _manifest(config, "c" * 64)
    with pytest.raises(RuntimeCompositionError, match="runtime manifest identity"):
        build_backend(config, manifest, digest, _control_contract())
