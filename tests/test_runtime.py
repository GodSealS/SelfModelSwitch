from __future__ import annotations

from contextlib import nullcontext
import hashlib
import json
import math
from pathlib import Path
import sys
from types import ModuleType

import pytest

from model_scheduler.config import load_config
from model_scheduler.control_recovery import ControlRecoveryClient
from model_scheduler.contracts import MemorySample
from model_scheduler.contracts_v2 import DeploymentSpec, Envelope, RuntimeSpec
from model_scheduler.contracts_v2 import ModelSpec as RegisteredModelSpec
from model_scheduler.llama_swap_client import ControlRequest, LlamaSwapControlContract
from model_scheduler.runtime import RuntimeCompositionError, build_backend, build_scheduler, ledger_specs_from
from app import create_app
from run import main



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


def _registered_model(model_id: str, runtime_id: str = "llama-cpp-gguf-v1", *, port: int = 18081) -> RegisteredModelSpec:
    return RegisteredModelSpec(
        model_id=model_id,
        runtime_id=runtime_id,
        capabilities=("chat",),
        assets=(),
        port=port,
        envelope=Envelope(4096, 2048, 512, 1, 0, 0, 0),
        timeout_seconds=600,
        reserved_bytes=1_150,
        measured=True,
        measurement_ref="a" * 64,
        physical_resident_peak_bytes=2_000,
    )


def _registration(*models: RegisteredModelSpec) -> DeploymentSpec:
    return DeploymentSpec(
        runtimes=(
            RuntimeSpec(
                runtime_id="llama-cpp-gguf-v1",
                profile_id="gguf-v1",
                image_digest="sha256:" + "b" * 64,
                adapter_sha256="c" * 64,
                lock_sha256="d" * 64,
                startup_args=(),
            ),
        ),
        models=models,
    )


def test_ledger_specs_come_from_exactly_one_registration_source() -> None:
    config = load_config(Path(__file__).resolve().parent.parent / "config.yaml")
    registration = _registration(_registered_model("lab-a"))

    assert set(ledger_specs_from(config=config)) == set(config.models)
    assert set(ledger_specs_from(registration=registration)) == {"lab-a"}
    with pytest.raises(ValueError):
        ledger_specs_from()
    with pytest.raises(ValueError):
        ledger_specs_from(config=config, registration=registration)


def test_ledger_specs_from_a_v2_registration_reject_duplicates_and_unknown_runtimes() -> None:
    duplicated = _registration(_registered_model("lab-a"), _registered_model("lab-a", port=18082))
    with pytest.raises(ValueError, match="duplicate"):
        ledger_specs_from(registration=duplicated)

    unknown_runtime = _registration(_registered_model("lab-a", runtime_id="hf-sharded-v1"))
    with pytest.raises(ValueError, match="unknown runtimes"):
        ledger_specs_from(registration=unknown_runtime)


def test_build_scheduler_accounts_the_v1_legacy_margin_exactly_once() -> None:
    config = load_config(Path(__file__).resolve().parent.parent / "config.yaml")
    scheduler = build_scheduler(config, Backend(), resources=Resources(), storage_guard=lambda: True)
    margin = config.scheduler.resource_safety_margin

    assert scheduler.book.physical_enforced is False
    for model_id, spec in scheduler.book.specs.items():
        assert scheduler.book.required(model_id) == math.ceil(spec.reserved_bytes * (1 + margin)), model_id
        assert scheduler.book.ledger_spec(model_id).physical_reserved_bytes is None
    assert scheduler.book.committed == 0  # the legacy bootstrap follows an observed stop, so nothing is reserved


def test_app_factory_composes_runtime_when_control_backend_is_injected() -> None:
    app = create_app(backend=Backend(), resources=Resources(), storage_guard=lambda: True)
    assert app.state.scheduler.book.specs["embedding"].preload is True
    assert app.state.gateway is not None


def _control_contract() -> LlamaSwapControlContract:
    return LlamaSwapControlContract(
        running_parser=lambda _: [],
        load_request=lambda model_id: ControlRequest("GET", "/fixed/load", params={"model": model_id}),
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


def test_process_entrypoint_refuses_to_start_without_the_pinned_control_contract(monkeypatch, capsys) -> None:
    """P06a pins the contract, so the fail-closed case is a contract that cannot be imported."""
    config_path = Path(__file__).resolve().parent.parent / "config.yaml"
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def refusing_import(name, *args, **kwargs):
        if name == "model_scheduler.llama_swap_contract":
            raise ImportError("no pinned contract in this deployment")
        return real_import(name, *args, **kwargs)

    def should_not_run(*_args, **_kwargs):
        raise AssertionError("uvicorn must not start without the control contract")

    monkeypatch.setattr("builtins.__import__", refusing_import)
    monkeypatch.setattr("run.uvicorn.run", should_not_run)
    assert main(["--config", str(config_path)]) == 78
    assert "fixed llama-swap control contract" in capsys.readouterr().err


def test_pinned_control_contract_matches_the_shipped_fixture() -> None:
    from model_scheduler.llama_swap_contract import CONTROL_CONTRACT

    assert CONTROL_CONTRACT.load_request("qwen25vl-7b-q4").path == "/upstream/qwen25vl-7b-q4/health"
    assert CONTROL_CONTRACT.unload_path == "/api/models/unload/{model_id}"


def test_process_entrypoint_rejects_a_configuration_changed_while_loading(monkeypatch, tmp_path, capsys) -> None:
    source = Path(__file__).resolve().parent.parent / "config.yaml"
    config_path = tmp_path / "config.yaml"
    config_path.write_bytes(source.read_bytes())
    real_load = load_config

    def changing_load(path):
        result = real_load(path)
        config_path.write_bytes(config_path.read_bytes() + b"\n")
        return result

    monkeypatch.setattr("run.load_config", changing_load)
    assert main(["--config", str(config_path), "--check-config"]) == 78
    assert "configuration changed while loading" in capsys.readouterr().err


def test_process_entrypoint_builds_and_injects_a_manifest_bound_backend(monkeypatch, tmp_path) -> None:
    config_path = Path(__file__).resolve().parent.parent / "config.yaml"
    config = load_config(config_path)
    digest = hashlib.sha256(config_path.read_bytes()).hexdigest()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest(config, digest)))
    contract = _control_contract()
    module = ModuleType("model_scheduler.llama_swap_contract")
    module.CONTROL_CONTRACT = contract
    seen = {}

    def app_factory(_path, *, backend, config):
        seen["backend"] = backend
        seen["config"] = config
        return object()

    monkeypatch.setitem(sys.modules, "model_scheduler.llama_swap_contract", module)
    monkeypatch.setattr("run._MANIFEST_PATH", manifest_path)
    monkeypatch.setattr("run.acquire", lambda _: nullcontext())
    monkeypatch.setattr("run.create_app", app_factory)
    monkeypatch.setattr("run.uvicorn.run", lambda app, **_: seen.setdefault("app", app))

    assert main(["--config", str(config_path)]) == 0
    assert seen["backend"].control.contract is contract
    assert seen["backend"].observer.config_sha256 == digest
    assert seen["config"] == config
    assert seen["app"] is not None
