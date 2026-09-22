from __future__ import annotations

import pytest
import asyncio
from datetime import datetime, timezone

from model_scheduler import ports_v3 as pv
from model_scheduler.backend_control import LlamaSwapBackend, ManagedLifecycle, ManagedModel
from model_scheduler.contracts import Capability, MemorySample, ModelSpec, Observation, Operation, Presence
from model_scheduler.control_protocol_v1 import Fence, InstanceIdentity
from model_scheduler.model_registry import Book
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


def v3_observation(state: str, instance: InstanceIdentity | None = None, *, sampled_at: float = 0.0,
                   launch_resolved: bool = True) -> pv.Observation:
    return pv.Observation(
        state=state,
        sampled_at_monotonic=sampled_at,
        sampled_at_utc=datetime(2026, 9, 18, 5, 0, tzinfo=UTC),
        port_state="listening" if state == pv.RUNNING else "closed",
        subprocess_state="running" if state == pv.RUNNING else "exited",
        instance=instance,
        launch_operation=None,
        launch_resolved=launch_resolved,
    )


class ManagedFakeAdapter:
    """The v3 BackendPort surface the lifecycle bridge depends on (load/stop only here)."""

    def __init__(self, *, load_state: str = pv.RUNNING) -> None:
        self.load_state = load_state
        self.loads: list[Fence] = []
        self.stops: list[InstanceIdentity] = []
        self.releases: list[str] = []
        self.release_deadlines: list[float | None] = []

    def claims_device_quiescence(self) -> bool:
        return False

    async def load(self, spec, fence: Fence, deadline: float) -> pv.Observation:
        self.loads.append(fence)
        return v3_observation(self.load_state, INSTANCE if self.load_state == pv.RUNNING else None)

    async def stop(self, identity: InstanceIdentity, fence: Fence, deadline: float) -> StopAck:
        self.stops.append(identity)
        return StopAck(accepted=True)

    async def release(self, model_id: str, deadline: float | None = None) -> bool:
        self.releases.append(model_id)
        self.release_deadlines.append(deadline)
        return True

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


class Clock:
    """A controllable monotonic source; the bridge must never read a real one in tests."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


TEST_POLICY = pv.LifecyclePolicy(verify_window_seconds=5.0, poll_seconds=0.5,
                                 observation_timeout_seconds=1.0, observation_max_age_seconds=1.0,
                                 recovery_seconds=5.0)


def make_book() -> Book:
    spec = ModelSpec("chat", "http://127.0.0.1:10001", frozenset({Capability.CHAT}), 100)
    book = Book({"chat": spec}, model_budget=1_000, free_floor=20, margin=0)
    book.bootstrap_stopped("chat")
    return book


def accepted(book: Book) -> Operation:
    """Put the book in READY with the identity the observer is about to witness."""
    operation = book.begin_load("chat", MemorySample(1_000, 900, 0.0), 0.0)
    book.loaded(operation, 0.0, instance=INSTANCE)
    return operation


def lifecycle(adapter: ManagedFakeAdapter, observer, *, book: Book | None = None,
              clock: Clock | None = None, policy: pv.LifecyclePolicy | None = None) -> ManagedLifecycle:
    registry = book if book is not None else make_book()
    current = clock or Clock()

    async def advance(seconds: float) -> None:
        # Fake time: a poll that never advanced the clock would spin forever.
        current.now += seconds
        await asyncio.sleep(0)

    return ManagedLifecycle(
        boot_id="boot-1", deployment_id="orin-lab",
        specs={"chat": object()}, adapter_for=lambda model_id: adapter,
        observers={"chat": observer}, instance_lookup=registry.instance,
        policy=policy or TEST_POLICY, now=current, sleep=advance,
    )


@pytest.mark.asyncio
async def test_managed_load_reports_the_independently_observed_instance_and_fence() -> None:
    adapter = ManagedFakeAdapter()
    bridge = lifecycle(adapter, ScriptedObserver([v3_observation(pv.RUNNING, INSTANCE)]))

    result = await bridge.load(Operation("op-1", "chat", 3, 0), deadline())

    assert result.presence is Presence.RUNNING and result.healthy is True
    assert result.instance == INSTANCE
    assert result.instance_id == f"{INSTANCE.container_id}:{INSTANCE.started_at}"
    # K2: the bridge derives the verdict deadline — min(window, sample + max age)
    assert result.valid_until == 1.0
    # every lifecycle action carries a complete non-execution fence matching the book's operation
    assert adapter.loads == [Fence("boot-1", "chat", 3, "op-1", None, None)]


@pytest.mark.asyncio
async def test_managed_load_stays_unknown_when_the_observation_cannot_verify_the_instance() -> None:
    adapter = ManagedFakeAdapter()
    bridge = lifecycle(adapter, ScriptedObserver([v3_observation(pv.UNKNOWN)]))

    result = await bridge.load(Operation("op-1", "chat", 1, 0), deadline())

    assert result.presence is Presence.UNKNOWN
    assert result.instance is None


@pytest.mark.asyncio
async def test_managed_load_samples_exactly_twice_until_the_instance_is_witnessed() -> None:
    """One sample is not a verdict: UNKNOWN then RUNNING is exactly two observations."""
    adapter = ManagedFakeAdapter()
    observer = ScriptedObserver([v3_observation(pv.UNKNOWN),             # accepted, not serving yet
                                 v3_observation(pv.RUNNING, INSTANCE)])  # the facts arrive
    bridge = lifecycle(adapter, observer)

    result = await bridge.load(Operation("op-1", "chat", 1, 0), deadline())

    assert result.presence is Presence.RUNNING and result.healthy is True
    assert len(observer.targets) == 2  # exactly, never "at least"


@pytest.mark.asyncio
async def test_managed_stop_polls_the_four_facts_and_the_book_owns_the_identity() -> None:
    adapter = ManagedFakeAdapter()
    observer = ScriptedObserver([v3_observation(pv.UNKNOWN, None),       # stop still in flight
                                 v3_observation(pv.STOPPED, INSTANCE)])  # the four facts land
    book = make_book()
    accepted(book)
    bridge = lifecycle(adapter, observer, book=book)
    assert bridge.instance("chat") == INSTANCE

    result = await bridge.stop(Operation("op-2", "chat", 1, 0), deadline())

    assert adapter.stops == [INSTANCE]  # the StopAck goes out for the book's identity only
    assert result.presence is Presence.STOPPED
    assert bridge.instance("chat") == INSTANCE  # a stop never writes the identity here
    book.stopped(book.begin_eviction(["chat"])[0], 1.0)  # only a proven, committed stop clears it
    assert book.instance("chat") is None


@pytest.mark.asyncio
async def test_a_stop_ack_with_a_still_running_container_does_not_release() -> None:
    adapter = ManagedFakeAdapter()
    observer = ScriptedObserver([v3_observation(pv.RUNNING, INSTANCE)])
    book = make_book()
    accepted(book)
    bridge = lifecycle(adapter, observer, book=book)

    result = await bridge.stop(Operation("op-2", "chat", 1, 0), deadline())

    assert result.presence is Presence.RUNNING  # the book must not move before the facts land
    assert bridge.instance("chat") == INSTANCE


@pytest.mark.asyncio
async def test_a_stop_without_a_verified_instance_releases_the_orphan_by_model() -> None:
    """A load that never verified leaves a container no per-instance stop can address."""
    adapter = ManagedFakeAdapter(load_state=pv.UNKNOWN)
    observer = ScriptedObserver([v3_observation(pv.UNKNOWN),         # the load never verified
                                 v3_observation(pv.STOPPED, INSTANCE)])  # the release lands
    bridge = lifecycle(adapter, observer)
    await bridge.load(Operation("op-1", "chat", 1, 0), deadline())
    assert bridge.instance("chat") is None

    result = await bridge.stop(Operation("op-2", "chat", 1, 0), deadline())

    assert adapter.releases == ["chat"]  # released by name, not through an identity we never had
    assert adapter.stops == []           # and never with a fabricated instance
    assert result.presence is Presence.STOPPED


@pytest.mark.asyncio
async def test_a_docker_outage_makes_the_stop_unprovable_not_successful() -> None:
    adapter = ManagedFakeAdapter()
    bridge = lifecycle(adapter, BrokenObserver())
    await bridge.load(Operation("op-1", "chat", 1, 0), deadline())  # load verify also fails → still UNKNOWN
    assert bridge.instance("chat") is None

    result = await bridge.stop(Operation("op-2", "chat", 1, 0), deadline())

    assert result.presence is Presence.UNKNOWN  # never RUNNING, never a fake STOPPED
    assert result.detail_code == "observer_failed"


# --- RP04/K1/K2/K4: bounded witness, refusal reasons and the orphan release ----


@pytest.mark.asyncio
async def test_a_load_without_an_observer_is_refused_at_once() -> None:
    adapter = ManagedFakeAdapter()
    bridge = ManagedLifecycle(
        boot_id="boot-1", deployment_id="orin-lab", specs={"chat": object()},
        adapter_for=lambda model_id: adapter, observers={}, instance_lookup=make_book().instance,
        policy=TEST_POLICY, now=Clock(), sleep=lambda _: asyncio.sleep(0),
    )

    result = await bridge.load(Operation("op-1", "chat", 1, 0), deadline())

    assert result.presence is Presence.UNKNOWN
    assert result.detail_code == "observer_missing"


@pytest.mark.asyncio
async def test_an_exhausted_window_dispatches_no_observation_at_all() -> None:
    adapter = ManagedFakeAdapter()
    observer = ScriptedObserver([v3_observation(pv.RUNNING, INSTANCE)])
    bridge = lifecycle(adapter, observer)

    result = await bridge.load(Operation("op-1", "chat", 1, 0), 0.0)  # the window is already gone

    assert observer.targets == []
    assert result.detail_code == "verify_deadline_exhausted"


@pytest.mark.asyncio
async def test_a_running_sample_that_describes_the_past_is_refused() -> None:
    adapter = ManagedFakeAdapter()
    observer = ScriptedObserver([v3_observation(pv.RUNNING, INSTANCE, sampled_at=-5.0)])
    bridge = lifecycle(adapter, observer)

    result = await bridge.load(Operation("op-1", "chat", 1, 0), deadline())

    assert result.presence is Presence.UNKNOWN
    assert result.detail_code == "observation_stale"


@pytest.mark.asyncio
async def test_a_running_sample_without_a_complete_identity_is_refused() -> None:
    adapter = ManagedFakeAdapter()
    observer = ScriptedObserver([v3_observation(pv.RUNNING, None)])
    bridge = lifecycle(adapter, observer)

    result = await bridge.load(Operation("op-1", "chat", 1, 0), deadline())

    assert result.presence is Presence.UNKNOWN
    assert result.detail_code == "observation_identity_mismatch"


@pytest.mark.asyncio
async def test_a_running_sample_of_another_model_is_refused() -> None:
    adapter = ManagedFakeAdapter()
    stranger = InstanceIdentity("c-other", "2026-09-18T05:00:00Z", "orin-lab", "other",
                                "llama-cpp", "b" * 64, "repo/llama@sha256:" + "c" * 64)
    observer = ScriptedObserver([v3_observation(pv.RUNNING, stranger)])
    bridge = lifecycle(adapter, observer)

    result = await bridge.load(Operation("op-1", "chat", 1, 0), deadline())

    assert result.presence is Presence.UNKNOWN
    assert result.detail_code == "observation_identity_mismatch"


@pytest.mark.asyncio
async def test_a_cold_load_accepts_a_qualified_instance_when_no_identity_was_known() -> None:
    """First load: the book has no instance, so any complete identity of this model is fine."""
    adapter = ManagedFakeAdapter()
    observer = ScriptedObserver([v3_observation(pv.RUNNING, INSTANCE)])
    book = make_book()
    bridge = lifecycle(adapter, observer, book=book)
    assert book.instance("chat") is None

    result = await bridge.load(Operation("op-1", "chat", 1, 0), deadline())

    assert result.presence is Presence.RUNNING
    assert result.instance == INSTANCE
    assert book.instance("chat") is None  # the bridge reports; only the scheduler commits


@pytest.mark.asyncio
async def test_an_unresolved_launch_never_proves_a_stop() -> None:
    """K4: no launch record source means the launch dimension stays unknown, not stopped."""
    adapter = ManagedFakeAdapter()
    observer = ScriptedObserver([v3_observation(pv.STOPPED, INSTANCE, launch_resolved=False)])
    bridge = lifecycle(adapter, observer)

    result = await bridge.load(Operation("op-1", "chat", 1, 0), deadline())

    assert result.presence is Presence.UNKNOWN
    assert result.detail_code == "launch_unresolved"


@pytest.mark.asyncio
async def test_a_proven_stop_during_a_load_is_reported_not_silently_failed() -> None:
    adapter = ManagedFakeAdapter()
    observer = ScriptedObserver([v3_observation(pv.STOPPED, INSTANCE)])
    bridge = lifecycle(adapter, observer)

    result = await bridge.load(Operation("op-1", "chat", 1, 0), deadline())

    assert result.presence is Presence.STOPPED
    assert result.detail_code == "load_proven_stopped"


def _expected(identity_digest: str) -> dict:
    return {"chat": pv.ExpectedInstance(deployment_id="orin-lab", model_id="chat", runtime_id="llama-cpp",
                                        image_digest="repo/llama@sha256:" + "c" * 64,
                                        identity_digest=identity_digest)}


@pytest.mark.asyncio
async def test_the_bridge_rechecks_the_identity_against_the_composition_expectation() -> None:
    """K4: the labels layer is not the only gate; a mismatched fact still cannot land."""
    adapter = ManagedFakeAdapter()
    observer = ScriptedObserver([v3_observation(pv.RUNNING, INSTANCE)])
    bridge = ManagedLifecycle(
        boot_id="boot-1", deployment_id="orin-lab", specs={"chat": object()},
        adapter_for=lambda model_id: adapter, observers={"chat": observer},
        instance_lookup=make_book().instance, policy=TEST_POLICY, now=Clock(),
        sleep=lambda _: asyncio.sleep(0), expected=_expected("e" * 64),  # another configuration
    )

    result = await bridge.load(Operation("op-1", "chat", 1, 0), deadline())

    assert result.presence is Presence.UNKNOWN
    assert result.detail_code == "observation_identity_mismatch"


@pytest.mark.asyncio
async def test_the_expected_configuration_digest_accepts_the_matching_container() -> None:
    adapter = ManagedFakeAdapter()
    observer = ScriptedObserver([v3_observation(pv.RUNNING, INSTANCE)])
    bridge = ManagedLifecycle(
        boot_id="boot-1", deployment_id="orin-lab", specs={"chat": object()},
        adapter_for=lambda model_id: adapter, observers={"chat": observer},
        instance_lookup=make_book().instance, policy=TEST_POLICY, now=Clock(),
        sleep=lambda _: asyncio.sleep(0), expected=_expected("b" * 64),
    )

    result = await bridge.load(Operation("op-1", "chat", 1, 0), deadline())

    assert result.presence is Presence.RUNNING
    assert result.instance == INSTANCE


@pytest.mark.asyncio
async def test_the_orphan_release_receives_the_caller_deadline() -> None:
    adapter = ManagedFakeAdapter(load_state=pv.UNKNOWN)
    observer = ScriptedObserver([v3_observation(pv.STOPPED, INSTANCE)])
    bridge = lifecycle(adapter, observer)
    awaited = deadline()

    await bridge.stop(Operation("op-2", "chat", 1, 0), awaited)

    assert adapter.releases == ["chat"]
    assert adapter.release_deadlines == [awaited]  # declared capability, bounded by the caller
