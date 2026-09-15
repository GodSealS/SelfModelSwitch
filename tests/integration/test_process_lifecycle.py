from __future__ import annotations

import asyncio
import json

import pytest

from model_scheduler.backend_control import LlamaSwapBackend, ManagedModel
from model_scheduler.contracts import Presence
from model_scheduler.process_observer import ProcessObserver


class RecordingControl:
    def __init__(self) -> None:
        self.loads: list[str] = []
        self.unloads: list[str] = []

    async def load(self, model_id: str) -> None:
        self.loads.append(model_id)

    async def unload(self, model_id: str) -> None:
        self.unloads.append(model_id)


async def _health_server() -> tuple[asyncio.AbstractServer, int]:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


@pytest.mark.asyncio
async def test_lost_model_health_socket_never_causes_automatic_load() -> None:
    server, port = await _health_server()
    deployment_id = "thor-local"
    image = "repo/image@sha256:" + "a" * 64
    config_sha256 = "b" * 64

    def inspect(_: list[str]) -> str:
        return json.dumps([
            {
                "Id": "container-id",
                "Config": {
                    "Image": image,
                    "Labels": {
                        "io.self-model-switch.deployment": deployment_id,
                        "io.self-model-switch.model": "qwen-small",
                        "io.self-model-switch.config-sha256": config_sha256,
                    },
                },
                "State": {"Running": True, "StartedAt": "2026-09-16T00:00:00Z"},
            }
        ])

    control = RecordingControl()
    observer = ProcessObserver(deployment_id, image, config_sha256, inspect=inspect)
    backend = LlamaSwapBackend(control, observer, {"qwen-small": ManagedModel("sms-thor-local-qwen-small", port)})
    try:
        assert (await backend.observe("qwen-small")).presence is Presence.RUNNING
        server.close()
        await server.wait_closed()
        assert (await backend.observe("qwen-small")).presence is Presence.UNKNOWN
    finally:
        server.close()
        await server.wait_closed()

    assert control.loads == []
    assert control.unloads == []
