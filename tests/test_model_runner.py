import importlib.util
import json
from pathlib import Path

import pytest
import signal

from model_scheduler.model_runner import RunnerError, docker_run_argv, docker_stop_argv, require_container_identity, require_manifest_config_digest, require_storage_ready, run_child_with_signal_forwarding
from model_scheduler.storage_monitor import StorageSnapshot


def _deploy_runner_module():
    path = Path(__file__).resolve().parent.parent / "deploy" / "model-runner.py"
    spec = importlib.util.spec_from_file_location("deploy_model_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runner_only_stops_exact_manifest_container() -> None:
    assert docker_stop_argv("sms-thor-local-qwen-small", "sms-thor-local-qwen-small") == ["docker", "stop", "--time", "30", "sms-thor-local-qwen-small"]
    assert docker_stop_argv("other", "sms-thor-local-qwen-small") is None


def test_docker_run_argv_is_manifest_derived_and_read_only() -> None:
    manifest = {"deployment_id": "thor-local", "image": "repo/image@sha256:" + "c" * 64, "models": {"qwen-small": {"container_name": "sms-thor-local-qwen-small", "file": "qwen-small.gguf", "context_size": 2048, "parallel": 1}}}
    argv = docker_run_argv(manifest, "qwen-small", "d" * 64)
    assert argv[:6] == ["docker", "run", "--name", "sms-thor-local-qwen-small", "--init", "--rm"]
    assert "type=bind,src=/mnt/model-ssd/models,dst=/models,readonly" in argv
    assert "127.0.0.1:10003:8080" in argv
    assert argv[-7:] == ["--parallel", "1", "--n-gpu-layers", "99", "--fit", "off", "--jinja"]


def test_docker_run_argv_rejects_a_manifest_name_mismatch() -> None:
    manifest = {"deployment_id": "thor-local", "image": "repo/image@sha256:" + "c" * 64, "models": {"qwen-small": {"container_name": "other", "file": "qwen-small.gguf", "context_size": 2048, "parallel": 1}}}
    with pytest.raises(RunnerError, match="container"):
        docker_run_argv(manifest, "qwen-small", "d" * 64)


def test_runner_forwards_service_termination_to_its_own_docker_child_and_waits() -> None:
    handlers = {}

    class Child:
        sent: list[int] = []
        waited = False

        def send_signal(self, signum: int) -> None:
            self.sent.append(signum)

        def wait(self) -> int:
            self.waited = True
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            return 143

    child = Child()

    def set_handler(signum, handler):
        previous = handlers.get(signum)
        handlers[signum] = handler
        return previous

    assert run_child_with_signal_forwarding(["docker", "run"], popen=lambda _: child, set_handler=set_handler) == 143
    assert child.waited is True
    assert child.sent == [signal.SIGTERM]


def test_runner_requires_a_verified_storage_snapshot_before_docker_start() -> None:
    class Storage:
        def __init__(self, ready: bool) -> None:
            self.ready = ready
            self.seen = None

        def check(self, models):
            self.seen = models
            return StorageSnapshot(self.ready, None if self.ready else "model_hash_mismatch", 0, {})

    models = {"qwen-small": object()}
    storage = Storage(True)
    require_storage_ready(storage, models)
    assert storage.seen is models
    with pytest.raises(RunnerError, match="storage verification"):
        require_storage_ready(Storage(False), models)


def test_runner_refuses_to_label_a_container_with_a_config_not_bound_by_manifest() -> None:
    manifest = {"config_sha256": "a" * 64}
    require_manifest_config_digest(manifest, "a" * 64)
    with pytest.raises(RunnerError, match="config digest"):
        require_manifest_config_digest(manifest, "b" * 64)
    with pytest.raises(RunnerError, match="config digest"):
        require_manifest_config_digest({}, "a" * 64)


def test_runner_requires_all_three_manifest_identity_labels_before_stop() -> None:
    labels = {
        "io.self-model-switch.deployment": "thor-local",
        "io.self-model-switch.model": "qwen-small",
        "io.self-model-switch.config-sha256": "a" * 64,
    }
    require_container_identity(labels, "thor-local", "qwen-small", "a" * 64)
    labels["io.self-model-switch.config-sha256"] = "b" * 64
    with pytest.raises(RunnerError, match="container identity"):
        require_container_identity(labels, "thor-local", "qwen-small", "a" * 64)


def test_deploy_runner_does_not_stop_a_container_with_a_stale_config_label(monkeypatch) -> None:
    runner = _deploy_runner_module()
    calls: list[list[str]] = []
    record = {
        "Config": {
            "Labels": {
                "io.self-model-switch.deployment": "thor-local",
                "io.self-model-switch.model": "qwen-small",
                "io.self-model-switch.config-sha256": "b" * 64,
            }
        }
    }

    class Result:
        returncode = 0
        stdout = json.dumps([record])

    def fake_run(argv: list[str], **_: object) -> Result:
        calls.append(argv)
        return Result()

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    with pytest.raises(RunnerError, match="container identity"):
        runner._stop("sms-thor-local-qwen-small", "thor-local", "qwen-small", "a" * 64)
    assert calls == [["docker", "inspect", "sms-thor-local-qwen-small"]]
