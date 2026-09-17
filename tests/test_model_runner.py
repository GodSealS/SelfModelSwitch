import importlib.util
import json
from pathlib import Path
import subprocess

import pytest
import signal

from model_scheduler.control_protocol_v1 import Fence
from model_scheduler.contracts_v2 import GGUF_PROFILE, HF_SHARDED_PROFILE, parse_deployment
from model_scheduler.model_runner import RunnerError, SupervisedLaunch, docker_run_argv, docker_stop_argv, require_container_identity, require_manifest_config_digest, require_storage_ready, run_child_with_signal_forwarding
from model_scheduler.ports_v3 import stopped_is_proven
from model_scheduler.runtime_profiles import LaunchRenderError, render_container_launch
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


def _fence(operation_id: str = "op-1") -> Fence:
    return Fence(
        boot_id="boot-1",
        model_id="qwen25vl-7b-q4",
        generation=1,
        operation_id=operation_id,
        execution_id=None,
        attempt=None,
    )


class Child:
    """A fake supervised launcher child: pid, optional hang, one exit code."""

    def __init__(self, pid: int, exit_code: int = 0, hangs: bool = False) -> None:
        self.pid = pid
        self.exit_code = exit_code
        self.hangs = hangs
        self.waited: list[float | None] = []

    def wait(self, timeout: float | None = None) -> int:
        self.waited.append(timeout)
        if self.hangs:
            raise subprocess.TimeoutExpired(["docker", "run"], timeout)
        return self.exit_code


def test_supervised_launch_records_pid_process_group_and_fence() -> None:
    child = Child(pid=4242)
    seen: list[tuple[list[str], dict]] = []

    def fake_popen(argv: list[str], **kwargs: object) -> Child:
        seen.append((argv, kwargs))
        return child

    launch = SupervisedLaunch(["docker", "run", "--name", "sms-lab-m1"], _fence(), popen=fake_popen)
    operation = launch.start()

    assert operation.state == "starting"
    assert operation.is_terminal is False
    assert operation.terminal_at_monotonic is None
    assert operation.pid == 4242
    assert operation.process_group_id == 4242
    assert operation.fence.operation_id == "op-1"
    assert seen == [(["docker", "run", "--name", "sms-lab-m1"], {"start_new_session": True})]

    assert launch.wait(timeout=30) == 0
    assert launch.operation.state == "completed"
    assert launch.operation.is_terminal is True
    assert launch.operation.terminal_at_monotonic is not None
    assert child.waited == [30]


def test_supervised_launch_timeout_never_proves_the_instance_stopped() -> None:
    child = Child(pid=4243, hangs=True)
    launch = SupervisedLaunch(["docker", "run"], _fence(), popen=lambda argv, **_: child)
    launch.start()

    assert launch.wait(timeout=0.01) is None
    assert launch.operation.state == "starting"
    assert stopped_is_proven(
        container_absent=True,
        launch_operation_terminal=launch.operation.is_terminal,
        subprocess_exited=True,
        port_listening=False,
    ) is False

    child.hangs = False
    assert launch.wait(timeout=30) == 0
    assert stopped_is_proven(
        container_absent=True,
        launch_operation_terminal=launch.operation.is_terminal,
        subprocess_exited=True,
        port_listening=False,
    ) is True


def test_supervised_launch_marks_a_nonzero_launcher_exit_failed() -> None:
    launch = SupervisedLaunch(["docker", "run"], _fence(), popen=lambda argv, **_: Child(pid=7, exit_code=125))
    launch.start()

    assert launch.wait() == 125
    assert launch.operation.state == "failed"
    assert launch.operation.is_terminal is True


def test_supervised_launch_signals_the_recorded_process_group() -> None:
    child = Child(pid=5150)
    signals: list[tuple[int, int]] = []
    launch = SupervisedLaunch(
        ["docker", "run"],
        _fence(),
        popen=lambda argv, **_: child,
        killpg=lambda pgid, signum: signals.append((pgid, signum)),
    )
    with pytest.raises(RunnerError, match="not started"):
        launch.terminate()

    launch.start()
    launch.terminate()
    assert signals == [(5150, signal.SIGTERM)]
    assert launch.operation.state == "starting"


def test_supervised_launch_refuses_a_failed_start_and_a_second_start() -> None:
    def failing_popen(argv: list[str], **kwargs: object) -> Child:
        raise OSError("docker not found")

    failed = SupervisedLaunch(["docker", "run"], _fence(), popen=failing_popen)
    with pytest.raises(RunnerError, match="cannot start"):
        failed.start()

    launch = SupervisedLaunch(["docker", "run"], _fence(), popen=lambda argv, **_: Child(pid=9))
    launch.start()
    with pytest.raises(RunnerError, match="already started"):
        launch.start()


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


M00_RUNTIME = "llama-cpp-cuda-sm87-4bc272f"
ALT_RUNTIME = "llama-cpp-cuda-sm87-alt"
M00_IMAGE = "ghcr.io/example/llama-cuda@sha256:" + "a" * 64
ALT_IMAGE = "ghcr.io/example/llama-cuda-next@sha256:" + "9" * 64
MODEL_DIRECTORY = "/media/jtzn/sandisk-ext4/models"
MODEL_ASSET = "qwen25vl-7b-q4/Qwen_Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf"
PROJECTOR_ASSET = "qwen25vl-7b-q4/mmproj-Qwen_Qwen2.5-VL-7B-Instruct-bf16.gguf"
# The M00 runtime flags (plan/m00-envelope.md section 9.2): the first profile must
# reproduce these values for the same envelope, with --load-mode fixed to auto.
M00_FLAGS = [
    "--load-mode",
    "--parallel",
    "--kv-unified-per-slot",
    "--image-max-tokens",
    "--n-gpu-layers",
    "--flash-attn",
    "--no-warmup",
    "--no-webui",
    "--host",
    "--port",
]


def _registration(*, startup_args=None, measured=True, capabilities=("chat", "vision"), port=18081, runtime_id=M00_RUNTIME) -> object:
    runtime = {
        "runtime_id": M00_RUNTIME,
        "profile_id": GGUF_PROFILE,
        "image_digest": M00_IMAGE,
        "adapter_sha256": "b" * 64,
        "lock_sha256": "c" * 64,
        "startup_args": M00_FLAGS if startup_args is None else startup_args,
    }
    alt = {**runtime, "runtime_id": ALT_RUNTIME, "image_digest": ALT_IMAGE}
    vision = "vision" in capabilities
    assets = [{"role": "model", "path": MODEL_ASSET, "sha256": "3f" + "0" * 62, "size_bytes": 4683072320}]
    if vision:
        assets.append({"role": "projector", "path": PROJECTOR_ASSET, "sha256": "d1" + "0" * 62, "size_bytes": 1354162912})
    return parse_deployment(
        {
            "schema_version": 2,
            "runtimes": [runtime, alt],
            "models": [
                {
                    "model_id": "qwen25vl-7b-q4",
                    "runtime_id": runtime_id,
                    "capabilities": list(capabilities),
                    "assets": assets,
                    "port": port,
                    "envelope": {
                        "ctx_size": 32768,
                        "max_input_tokens": 28672,
                        "max_output_tokens": 4096,
                        "max_parallel": 2,
                        "max_image_tokens": 1280 if vision else 0,
                        "max_image_edge_pixels": 1024 if vision else 0,
                        "max_images": 1 if vision else 0,
                    },
                    "timeout_seconds": 3600,
                    "reserved_bytes": 6106148045,
                    "measured": measured,
                    "measurement_ref": "e" * 64 if measured else None,
                    "physical_resident_peak_bytes": 5_000_000_000 if measured else None,
                }
            ],
        }
    )


def _render(
    *,
    startup_args=None,
    measured=True,
    capabilities=("chat", "vision"),
    port=18081,
    runtime_id=M00_RUNTIME,
    deployment_id="lab-orin",
    model_directory=MODEL_DIRECTORY,
    config_sha256="d" * 64,
    mode="production",
    temporary_budget_bytes=None,
):
    return render_container_launch(
        _registration(
            startup_args=startup_args, measured=measured, capabilities=capabilities, port=port, runtime_id=runtime_id
        ),
        "qwen25vl-7b-q4",
        deployment_id=deployment_id,
        model_directory=model_directory,
        config_sha256=config_sha256,
        mode=mode,
        temporary_budget_bytes=temporary_budget_bytes,
    )


def test_profile_render_reproduces_the_m00_server_arguments() -> None:
    launch = _render()

    assert launch.server_args == (
        "--model", f"/models/{MODEL_ASSET}",
        "--mmproj", f"/models/{PROJECTOR_ASSET}",
        "--parallel", "2",
        "--kv-unified-per-slot", "32768",
        "--image-max-tokens", "1280",
        "--n-gpu-layers", "99",
        "--flash-attn", "auto",
        "--no-warmup",
        "--no-webui",
        "--load-mode", "auto",
        "--host", "0.0.0.0",
        "--port", "8080",
    )
    assert launch.argv[:6] == ("docker", "run", "--name", "sms-lab-orin-qwen25vl-7b-q4", "--init", "--rm")
    assert "--restart=no" in launch.argv
    assert M00_IMAGE in launch.argv
    # The M00 runtime has no --no-mmap; the profile fixes --load-mode auto instead.
    assert "--no-mmap" not in launch.argv


def test_profile_render_keeps_the_registered_port_and_read_only_assets() -> None:
    launch = _render(port=10077)

    assert launch.port == 10077
    assert "127.0.0.1:10077:8080" in launch.argv
    assert [token for token in launch.argv if token == "--mount"] == ["--mount"]
    assert f"type=bind,src={MODEL_DIRECTORY},dst=/models,readonly" in launch.argv
    assert "--entrypoint" not in launch.argv
    assert "--privileged" not in launch.argv
    assert "--volume" not in launch.argv
    assert not any("docker.sock" in token for token in launch.argv)
    assert launch.container_name == "sms-lab-orin-qwen25vl-7b-q4"


def test_profile_render_refuses_flags_without_a_value_source() -> None:
    with pytest.raises(LaunchRenderError, match="no value source"):
        _render(startup_args=[*M00_FLAGS, "--threads"])
    with pytest.raises(LaunchRenderError, match="--parallel"):
        _render(startup_args=["--host", "--port", "--kv-unified-per-slot", "--image-max-tokens"])
    with pytest.raises(LaunchRenderError, match="--image-max-tokens"):
        _render(capabilities=("chat",))


def test_profile_render_refuses_capabilities_the_profile_cannot_render() -> None:
    with pytest.raises(LaunchRenderError, match="--embedding"):
        _render(capabilities=("embeddings",))
    with pytest.raises(LaunchRenderError, match="--embedding"):
        _render(capabilities=("rerank",))


def test_profile_render_requires_measurement_for_production_and_a_budget_for_lab() -> None:
    with pytest.raises(LaunchRenderError, match="measured"):
        _render(mode="production", measured=False)
    with pytest.raises(LaunchRenderError, match="temporary budget"):
        _render(mode="production", temporary_budget_bytes=16_000_000_000)
    with pytest.raises(LaunchRenderError, match="temporary budget"):
        _render(mode="lab", measured=False)

    launch = _render(mode="lab", measured=False, temporary_budget_bytes=16_000_000_000)
    assert launch.mode == "lab"
    assert launch.is_lab is True
    assert launch.labels["io.self-model-switch.mode"] == "lab"
    assert launch.labels["io.self-model-switch.config-sha256"] == "d" * 64

    with pytest.raises(LaunchRenderError, match="mode"):
        _render(mode="dev")


def test_profile_render_refuses_a_registration_only_profile() -> None:
    registration = parse_deployment(
        {
            "schema_version": 2,
            "runtimes": [
                {
                    "runtime_id": "hf-transformers-cpu",
                    "profile_id": HF_SHARDED_PROFILE,
                    "image_digest": "ghcr.io/example/hf-serve@sha256:" + "f" * 64,
                    "adapter_sha256": "b" * 64,
                    "lock_sha256": "c" * 64,
                    "startup_args": ["--host", "--port"],
                }
            ],
            "models": [
                {
                    "model_id": "hf-sharded-model",
                    "runtime_id": "hf-transformers-cpu",
                    "capabilities": ["chat"],
                    "assets": [
                        {"role": "model", "path": "hf/shard-00001.safetensors", "sha256": "1" * 64, "size_bytes": 1000},
                        {"role": "model", "path": "hf/shard-00002.safetensors", "sha256": "2" * 64, "size_bytes": 1000},
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
                }
            ],
        }
    )

    with pytest.raises(LaunchRenderError, match="registration-only"):
        render_container_launch(
            registration,
            "hf-sharded-model",
            deployment_id="lab-orin",
            model_directory=MODEL_DIRECTORY,
            config_sha256="d" * 64,
            mode="lab",
            temporary_budget_bytes=16_000_000_000,
        )


def test_profile_render_refuses_unsafe_model_directories_and_bad_identity() -> None:
    with pytest.raises(LaunchRenderError, match="model directory"):
        _render(model_directory="models")
    with pytest.raises(LaunchRenderError, match="model directory"):
        _render(model_directory="/")
    with pytest.raises(LaunchRenderError, match="model directory"):
        _render(model_directory="/models,readonly,dst=/")
    with pytest.raises(LaunchRenderError, match="deployment"):
        _render(deployment_id="Lab Orin")
    with pytest.raises(LaunchRenderError, match="config"):
        _render(config_sha256="not-a-digest")


def test_profile_render_uses_the_runtime_the_model_is_bound_to() -> None:
    launch = _render(runtime_id=ALT_RUNTIME)

    assert launch.runtime_id == ALT_RUNTIME
    assert launch.labels["io.self-model-switch.runtime"] == ALT_RUNTIME
    assert launch.labels["io.self-model-switch.profile"] == GGUF_PROFILE
    assert ALT_IMAGE in launch.argv
    assert M00_IMAGE not in launch.argv
