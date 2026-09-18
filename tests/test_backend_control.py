from __future__ import annotations

import pytest
import asyncio
from datetime import datetime, timezone

from model_scheduler import ports_v3 as pv
from model_scheduler.backend_control import LlamaSwapBackend, ManagedLifecycle, ManagedModel
from model_scheduler.contracts import Observation, Operation, Presence
from model_scheduler.control_protocol_v1 import Fence, InstanceIdentity
from model_scheduler.ports_v3 import StopAck


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


# -- P16: the v3 managed lifecycle bridge -------------------------------------------------

UTC = timezone.utc
INSTANCE = InstanceIdentity(
    container_id="c-managed",
    started_at="2026-09-18T05:00:00Z",
    deployment_id="orin-lab",
    model_id="chat",
    runtime_id="llama-cpp",
    candidate_digest="b" * 64,
    image_digest="repo/llama@sha256:" + "c" * 64,
)


def v3_observation(state: str, instance: InstanceIdentity | None = None) -> pv.Observation:
    return pv.Observation(
        state=state,
        sampled_at_monotonic=0.0,
        sampled_at_utc=datetime(2026, 9, 18, 5, 0, tzinfo=UTC),
        port_state="listening" if state == pv.RUNNING else "closed",
        subprocess_state="running" if state == pv.RUNNING else "exited",
        instance=instance,
        launch_operation=None,
    )


class ManagedFakeAdapter:
    """The v3 BackendPort surface the lifecycle bridge depends on (load/stop only here)."""

    def __init__(self, *, load_state: str = pv.RUNNING) -> None:
        self.load_state = load_state
        self.loads: list[Fence] = []
        self.stops: list[InstanceIdentity] = []

    def claims_device_quiescence(self) -> bool:
        return False

    async def load(self, spec, fence: Fence, deadline: float) -> pv.Observation:
        self.loads.append(fence)
        return v3_observation(self.load_state, INSTANCE if self.load_state == pv.RUNNING else None)

    async def stop(self, identity: InstanceIdentity, fence: Fence, deadline: float) -> StopAck:
        self.stops.append(identity)
        return StopAck(accepted=True)

    async def execute(self, request, fence, deadline):  # pragma: no cover
        raise AssertionError("lifecycle never executes")

    async def cancel(self, handle, deadline):  # pragma: no cover
        raise AssertionError("lifecycle never cancels")


class ScriptedObserver:
    """A ports_v3.ObserverPort returning scripted observations (last entry repeats)."""

    def __init__(self, script: list[pv.Observation]) -> None:
        self.script = list(script)
        self.targets: list[pv.ObservationTarget] = []

    async def observe(self, target: pv.ObservationTarget, deadline: float) -> pv.Observation:
        self.targets.append(target)
        return self.script.pop(0) if len(self.script) > 1 else self.script[0]


class BrokenObserver:
    async def observe(self, target, deadline):
        raise RuntimeError("docker unreachable")


def lifecycle(adapter: ManagedFakeAdapter, observer) -> ManagedLifecycle:
    return ManagedLifecycle(
        boot_id="boot-1", deployment_id="orin-lab",
        specs={"chat": object()}, adapter_for=lambda model_id: adapter,
        observers={"chat": observer}, poll_seconds=0.01,
    )


@pytest.mark.asyncio
async def test_managed_load_writes_back_the_independently_observed_instance_and_fence() -> None:
    adapter = ManagedFakeAdapter()
    bridge = lifecycle(adapter, ScriptedObserver([v3_observation(pv.RUNNING, INSTANCE)]))

    result = await bridge.load(Operation("op-1", "chat", 3, 0), deadline())

    assert result.presence is Presence.RUNNING and result.healthy is True
    assert bridge.instance("chat") == INSTANCE
    # every lifecycle action carries a complete non-execution fence matching the book's operation
    assert adapter.loads == [Fence("boot-1", "chat", 3, "op-1", None, None)]


@pytest.mark.asyncio
async def test_managed_load_stays_unknown_when_the_observation_cannot_verify_the_instance() -> None:
    adapter = ManagedFakeAdapter()
    bridge = lifecycle(adapter, ScriptedObserver([v3_observation(pv.UNKNOWN)]))

    result = await bridge.load(Operation("op-1", "chat", 1, 0), deadline())

    assert result.presence is Presence.UNKNOWN
    assert bridge.instance("chat") is None


@pytest.mark.asyncio
async def test_managed_stop_polls_the_four_facts_and_only_then_clears_the_instance() -> None:
    adapter = ManagedFakeAdapter()
    observer = ScriptedObserver([v3_observation(pv.RUNNING, INSTANCE),  # the load verification
                                 v3_observation(pv.UNKNOWN, None),       # stop still in flight
                                 v3_observation(pv.STOPPED, INSTANCE)])  # the four facts land
    bridge = lifecycle(adapter, observer)
    await bridge.load(Operation("op-1", "chat", 1, 0), deadline())

    result = await bridge.stop(Operation("op-2", "chat", 1, 0), deadline())

    assert adapter.stops == [INSTANCE]  # StopAck goes out for the verified instance only
    assert result.presence is Presence.STOPPED
    assert bridge.instance("chat") is None


@pytest.mark.asyncio
async def test_a_stop_ack_with_a_still_running_container_does_not_release() -> None:
    adapter = ManagedFakeAdapter()
    observer = ScriptedObserver([v3_observation(pv.RUNNING, INSTANCE)])
    bridge = lifecycle(adapter, observer)
    await bridge.load(Operation("op-1", "chat", 1, 0), deadline())

    result = await bridge.stop(Operation("op-2", "chat", 1, 0), deadline())

    assert result.presence is Presence.RUNNING  # the book must not move before the facts land
    assert bridge.instance("chat") == INSTANCE


@pytest.mark.asyncio
async def test_a_docker_outage_makes_the_stop_unprovable_not_successful() -> None:
    adapter = ManagedFakeAdapter()
    bridge = lifecycle(adapter, BrokenObserver())
    await bridge.load(Operation("op-1", "chat", 1, 0), deadline())  # load verify also fails → still UNKNOWN
    assert bridge.instance("chat") is None

    result = await bridge.stop(Operation("op-2", "chat", 1, 0), deadline())

    assert result.presence is Presence.UNKNOWN  # never RUNNING, never a fake STOPPED
    assert result.detail_code == "stop_unverified"
