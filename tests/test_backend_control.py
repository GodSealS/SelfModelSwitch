from __future__ import annotations

import pytest
import asyncio

from model_scheduler.backend_control import LlamaSwapBackend, ManagedModel
from model_scheduler.contracts import Observation, Operation, Presence


def deadline() -> float:
    return asyncio.get_running_loop().time() + 1


class Client:
    def __init__(self): self.loaded: list[str] = []; self.unloaded: list[str] = []
    async def load(self, model_id): self.loaded.append(model_id)
    async def unload(self, model_id): self.unloaded.append(model_id)


class Observer:
    def __init__(self, observation): self.observation = observation; self.calls = []
    def observe(self, model_id, container_name, port):
        self.calls.append((model_id, container_name, port)); return self.observation


@pytest.mark.asyncio
async def test_load_requires_matching_running_health_observation() -> None:
    client = Client(); observer = Observer(Observation(Presence.RUNNING, "id", True, 0))
    backend = LlamaSwapBackend(client, observer, {"chat": ManagedModel("sms-test-chat", 10003)})
    result = await backend.load(Operation("op", "chat", 1, 0), deadline())
    assert result.presence is Presence.RUNNING
    assert client.loaded == ["chat"]


@pytest.mark.asyncio
async def test_unload_http_success_without_stop_evidence_does_not_release_budget() -> None:
    client = Client(); observer = Observer(Observation(Presence.RUNNING, "id", True, 0))
    backend = LlamaSwapBackend(client, observer, {"chat": ManagedModel("sms-test-chat", 10003)})
    result = await backend.stop(Operation("op", "chat", 1, 0), deadline())
    assert result.presence is Presence.RUNNING
    assert client.unloaded == ["chat"]


@pytest.mark.asyncio
async def test_control_transport_error_becomes_unknown_evidence() -> None:
    class BrokenClient(Client):
        async def load(self, model_id): raise RuntimeError("network")
    backend = LlamaSwapBackend(BrokenClient(), Observer(Observation(Presence.RUNNING, "id", True, 0)), {"chat": ManagedModel("sms-test-chat", 10003)})
    result = await backend.load(Operation("op", "chat", 1, 0), deadline())
    assert result.presence is Presence.UNKNOWN
    assert result.detail_code == "control_load_failed"
