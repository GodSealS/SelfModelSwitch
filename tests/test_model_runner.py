import pytest
import signal

from model_scheduler.model_runner import RunnerError, docker_run_argv, docker_stop_argv, run_child_with_signal_forwarding


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
