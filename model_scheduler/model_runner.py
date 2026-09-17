"""Safe Docker argv construction for manifest-managed model containers."""
from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from typing import Any, Callable, Mapping

from .control_protocol_v1 import Fence
from .ports_v3 import LaunchOperation


class RunnerError(ValueError):
    pass


_PORTS = {"embedding": 10001, "reranker": 10002, "qwen-small": 10003, "qwen-large": 10004}
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def run_child_with_signal_forwarding(
    argv: list[str],
    *,
    popen: Callable[[list[str]], Any] = subprocess.Popen,
    set_handler: Callable[[int, Any], Any] = signal.signal,
) -> int:
    """Wait for one argv-only child while relaying service stop signals to it."""
    child = popen(argv)
    previous: dict[int, Any] = {}

    def forward(signum: int, _frame: Any) -> None:
        child.send_signal(signum)

    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = set_handler(signum, forward)
        return child.wait()
    finally:
        for signum, handler in previous.items():
            set_handler(signum, handler)


class SupervisedLaunch:
    """One supervised launcher child whose state is an observable LaunchOperation.

    The operation stays `starting` until the launcher process itself exits: an
    elapsed timeout never makes it terminal, so `stopped_is_proven` cannot be
    satisfied while the launcher is still running (plan/08-execution-plan.md C03).
    """

    def __init__(
        self,
        argv: list[str],
        fence: Fence,
        *,
        popen: Callable[..., Any] = subprocess.Popen,
        monotonic: Callable[[], float] = time.monotonic,
        start_new_session: bool = True,
        getpgid: Callable[[int], int] = os.getpgid,
        killpg: Callable[[int, int], Any] = os.killpg,
    ) -> None:
        if not isinstance(fence, Fence):
            raise RunnerError("a supervised launch requires a fence")
        self._argv = list(argv)
        self._fence = fence
        self._popen = popen
        self._monotonic = monotonic
        self._start_new_session = start_new_session
        self._getpgid = getpgid
        self._killpg = killpg
        self._child: Any = None
        self._operation: LaunchOperation | None = None

    @property
    def operation(self) -> LaunchOperation:
        if self._operation is None:
            raise RunnerError("the supervised launch is not started")
        return self._operation

    def start(self) -> LaunchOperation:
        """Start the launcher once and record pid, process group and fence."""
        if self._child is not None:
            raise RunnerError("the supervised launch is already started")
        try:
            child = self._popen(self._argv, start_new_session=self._start_new_session)
        except OSError as exc:
            raise RunnerError(f"cannot start the supervised launcher: {exc}") from exc
        pid = getattr(child, "pid", None)
        if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1:
            raise RunnerError("the supervised launcher reported no process id")
        self._child = child
        process_group_id = pid if self._start_new_session else self._query_process_group(pid)
        self._operation = LaunchOperation(
            operation_id=self._fence.operation_id,
            fence=self._fence,
            started_at_monotonic=self._monotonic(),
            state="starting",
            pid=pid,
            process_group_id=process_group_id,
        )
        return self._operation

    def wait(self, timeout: float | None = None) -> int | None:
        """Wait for the launcher; a timeout returns None and proves nothing."""
        if self._child is None:
            raise RunnerError("the supervised launch is not started")
        try:
            exit_code = self._child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            raise RunnerError("the supervised launcher reported no exit code")
        if not self.operation.is_terminal:
            self._operation = LaunchOperation(
                operation_id=self._fence.operation_id,
                fence=self._fence,
                started_at_monotonic=self.operation.started_at_monotonic,
                state="completed" if exit_code == 0 else "failed",
                pid=self.operation.pid,
                process_group_id=self.operation.process_group_id,
                terminal_at_monotonic=self._monotonic(),
            )
        return exit_code

    def signal_group(self, signum: int) -> None:
        """Signal the recorded process group so a launcher cannot leave children behind."""
        operation = self.operation
        if operation.process_group_id is None:
            raise RunnerError("the supervised launch has no process group to signal")
        try:
            self._killpg(operation.process_group_id, signum)
        except OSError as exc:
            raise RunnerError(f"cannot signal the launch process group: {exc}") from exc

    def terminate(self) -> None:
        self.signal_group(signal.SIGTERM)

    def _query_process_group(self, pid: int) -> int | None:
        try:
            return self._getpgid(pid)
        except OSError:
            return None


def require_storage_ready(storage: Any, models: Mapping[str, Any]) -> None:
    """Refuse to start Docker unless the SSD and every declared model verify."""
    snapshot = storage.check(models)
    if getattr(snapshot, "ready", None) is not True:
        raise RunnerError("storage verification failed")


def require_manifest_config_digest(manifest: Mapping[str, Any], config_digest: str) -> None:
    """Bind the Docker identity label to the exact rendered scheduler config."""
    if not _HASH.fullmatch(config_digest) or manifest.get("config_sha256") != config_digest:
        raise RunnerError("manifest config digest mismatch")


def require_container_identity(labels: Mapping[str, Any], deployment_id: str, model_id: str, config_sha256: str) -> None:
    expected = {
        "io.self-model-switch.deployment": deployment_id,
        "io.self-model-switch.model": model_id,
        "io.self-model-switch.config-sha256": config_sha256,
    }
    if any(labels.get(key) != value for key, value in expected.items()):
        raise RunnerError("container identity mismatch")


def docker_stop_argv(requested_name: str, manifest_name: str) -> list[str] | None:
    if requested_name != manifest_name or not manifest_name.startswith("sms-"):
        return None
    return ["docker", "stop", "--time", "30", manifest_name]


def docker_run_argv(manifest: Mapping[str, Any], model_id: str, config_sha256: str) -> list[str]:
    """Build the exact argv for one declared model; never accept shell fragments."""
    if model_id not in _PORTS or not _HASH.fullmatch(config_sha256):
        raise RunnerError("invalid model or config digest")
    deployment_id, image, models = manifest.get("deployment_id"), manifest.get("image"), manifest.get("models")
    if not isinstance(deployment_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", deployment_id):
        raise RunnerError("invalid deployment")
    if not isinstance(image, str) or not re.fullmatch(r"[^@]+@sha256:[0-9a-f]{64}", image) or not isinstance(models, Mapping):
        raise RunnerError("invalid image or models")
    model = models.get(model_id)
    if not isinstance(model, Mapping):
        raise RunnerError("unknown model")
    name, filename, context, parallel = (model.get(key) for key in ("container_name", "file", "context_size", "parallel"))
    expected_name = f"sms-{deployment_id}-{model_id}"
    if name != expected_name:
        raise RunnerError("invalid container name")
    if not isinstance(filename, str) or "/" in filename or filename in {"", ".", ".."}:
        raise RunnerError("invalid model file")
    if type(context) is not int or context <= 0 or type(parallel) is not int or not 1 <= parallel <= 16 or context % parallel:
        raise RunnerError("invalid model sizing")
    argv = ["docker", "run", "--name", name, "--init", "--rm", "--restart=no", "--gpus", "all", "--label", f"io.self-model-switch.deployment={deployment_id}", "--label", f"io.self-model-switch.model={model_id}", "--label", f"io.self-model-switch.config-sha256={config_sha256}", "--publish", f"127.0.0.1:{_PORTS[model_id]}:8080", "--mount", "type=bind,src=/mnt/model-ssd/models,dst=/models,readonly", image, "--model", f"/models/{filename}", "--alias", model_id, "--host", "0.0.0.0", "--port", "8080", "--ctx-size", str(context), "--batch-size", "512", "--ubatch-size", "128", "--cache-type-k", "f16", "--cache-type-v", "f16"]
    if model_id == "embedding":
        argv.extend(["--embedding", "--pooling", "mean"])
    elif model_id == "reranker":
        argv.extend(["--embedding", "--pooling", "rank"])
    argv.extend(["--parallel", str(parallel), "--n-gpu-layers", "99", "--fit", "off"])
    if model_id.startswith("qwen-"):
        argv.append("--jinja")
    return argv
