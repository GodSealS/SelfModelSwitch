"""Narrow implementation behind the root-owned no-argument recovery helper."""
from __future__ import annotations

import json
from pathlib import Path
import socket
import subprocess
from typing import Any, Callable


class RecoveryHelper:
    _PORTS = {"embedding": 10001, "reranker": 10002, "qwen-small": 10003, "qwen-large": 10004}

    def __init__(self, manifest: str | Path, *, runner: Callable[[list[str]], str] | None = None, inspector: Callable[[str], dict[str, Any] | None] | None = None, port_open: Callable[[int], bool] | None = None, control_stopped: Callable[[], bool] | None = None):
        self.manifest = Path(manifest)
        self.runner = runner or self._run
        self.inspector = inspector or self._inspect
        self.port_open = port_open or self._port_open
        self.control_stopped = control_stopped or self._control_stopped

    @staticmethod
    def _run(argv: list[str]) -> str:
        return subprocess.check_output(argv, text=True, timeout=90)

    @staticmethod
    def _inspect(container_name: str) -> dict[str, Any] | None:
        try:
            payload = json.loads(subprocess.check_output(["docker", "inspect", container_name], text=True, timeout=10))
        except subprocess.CalledProcessError:
            return None
        if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
            raise ValueError("invalid docker inspect response")
        return payload[0]

    @staticmethod
    def _port_open(port: int) -> bool:
        with socket.socket() as sock:
            sock.settimeout(0.5)
            return sock.connect_ex(("127.0.0.1", port)) == 0

    @staticmethod
    def _control_stopped() -> bool:
        """Require both unit inactivity and an empty service cgroup."""
        try:
            active = subprocess.check_output(
                ["systemctl", "show", "--property=ActiveState", "--value", "llama-swap.service"],
                text=True,
                timeout=10,
            ).strip()
            group = subprocess.check_output(
                ["systemctl", "show", "--property=ControlGroup", "--value", "llama-swap.service"],
                text=True,
                timeout=10,
            ).strip()
            if active != "inactive" or not group.startswith("/"):
                return False
            return not (Path("/sys/fs/cgroup") / group.lstrip("/") / "cgroup.procs").read_text(encoding="utf-8").strip()
        except (OSError, subprocess.SubprocessError):
            return False

    def recover(self) -> dict[str, object]:
        try:
            data = json.loads(self.manifest.read_text(encoding="utf-8"))
            deployment_id, models = data["deployment_id"], data["models"]
            if not isinstance(deployment_id, str) or not isinstance(models, dict) or set(models) != set(self._PORTS):
                raise ValueError("invalid manifest")
            for model_id, model in models.items():
                if not isinstance(model, dict) or model.get("container_name") != f"sms-{deployment_id}-{model_id}":
                    raise ValueError("unsafe container name")
            self.runner(["systemctl", "stop", "llama-swap.service"])
            if not self.control_stopped():
                raise ValueError("control_plane_still_running")
            stopped = []
            for model_id in sorted(models):
                name = models[model_id].get("container_name")
                if not isinstance(name, str):
                    raise ValueError("unsafe container name")
                record = self.inspector(name)
                if record is not None:
                    labels = record.get("Config", {}).get("Labels", {})
                    if labels.get("io.self-model-switch.deployment") != deployment_id or labels.get("io.self-model-switch.model") != model_id:
                        raise ValueError("container label mismatch")
                    self.runner(["docker", "stop", "--time", "30", name])
                    after = self.inspector(name)
                    if after is not None and after.get("State", {}).get("Running") is True:
                        raise ValueError("container remains running")
                if self.port_open(self._PORTS[model_id]):
                    raise ValueError("container port remains open")
                stopped.append(model_id)
            self.runner(["systemctl", "start", "llama-swap.service"])
            return {"ok": True, "phase": "complete", "error_code": None, "stopped_models": stopped}
        except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.SubprocessError):
            return {"ok": False, "phase": "failed", "error_code": "control_recovery_failed", "stopped_models": []}
