"""Narrow Docker process evidence adapter; it never scans or mutates containers."""
from __future__ import annotations

import json
import socket
import subprocess
from time import monotonic
from typing import Callable
from urllib.error import URLError
from urllib.request import Request, urlopen

from .contracts import Observation, Presence


class ProcessObserver:
    def __init__(self, deployment_id: str, image_digest: str, config_sha256: str, *, inspect: Callable[[list[str]], str] | None = None, port_open: Callable[[int], bool] | None = None, health: Callable[[int], bool] | None = None):
        self.deployment_id, self.image_digest, self.config_sha256 = deployment_id, image_digest, config_sha256
        self._inspect = inspect or self._docker_inspect
        self._port_open = port_open or self._port_is_open
        self._health = health or self._health_endpoint

    @staticmethod
    def _docker_inspect(args: list[str]) -> str:
        return subprocess.check_output(args, text=True, timeout=5)

    @staticmethod
    def _port_is_open(port: int) -> bool:
        with socket.socket() as sock:
            sock.settimeout(0.5)
            return sock.connect_ex(("127.0.0.1", port)) == 0

    @staticmethod
    def _health_endpoint(port: int) -> bool:
        """Probe only the model's fixed loopback health endpoint."""
        try:
            request = Request(f"http://127.0.0.1:{port}/health", method="GET")
            with urlopen(request, timeout=0.5) as response:  # noqa: S310 - fixed loopback URL
                return response.status == 200
        except (OSError, URLError):
            return False

    def observe(self, model_id: str, container_name: str, port: int) -> Observation:
        try:
            payload = json.loads(self._inspect(["docker", "inspect", container_name]))
        except (OSError, ValueError, subprocess.SubprocessError):
            return Observation(Presence.UNKNOWN, None, False, monotonic(), "docker_unavailable")
        if not isinstance(payload, list):
            return Observation(Presence.UNKNOWN, None, False, monotonic(), "inspect_protocol_error")
        if not payload:
            return Observation(Presence.STOPPED if not self._port_open(port) else Presence.UNKNOWN, None, False, monotonic(), None)
        if len(payload) != 1 or not isinstance(payload[0], dict):
            return Observation(Presence.UNKNOWN, None, False, monotonic(), "inspect_protocol_error")
        item = payload[0]
        labels = item.get("Config", {}).get("Labels", {})
        state = item.get("State", {})
        identity_ok = labels == {"io.self-model-switch.deployment": self.deployment_id, "io.self-model-switch.model": model_id, "io.self-model-switch.config-sha256": self.config_sha256}
        image_ok = item.get("Config", {}).get("Image") == self.image_digest
        running = state.get("Running") is True
        instance = f"{item.get('Id')}:{state.get('StartedAt')}" if item.get("Id") and state.get("StartedAt") else None
        if identity_ok and image_ok and running and self._port_open(port) and self._health(port):
            return Observation(Presence.RUNNING, instance, True, monotonic(), None)
        return Observation(Presence.UNKNOWN, instance, False, monotonic(), "container_identity_or_health_mismatch")
