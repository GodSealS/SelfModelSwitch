from __future__ import annotations

import asyncio
import contextlib

import pytest

from model_scheduler.contracts import Capability, MemorySample, ModelSpec, Observation, Outcome, Presence, RecoveryResult, State
from model_scheduler.model_registry import Book, Conflict
from model_scheduler.request_queue import WaitKind, WaitState
from model_scheduler.scheduler import ModelScheduler, ModelUnavailable, QueueFull
from model_scheduler.session_manager import SessionConflict, SessionManager, SessionNotFound


class Resources:
    async def snapshot(self):
        return MemorySample(10_000, 9_000, asyncio.get_running_loop().time())


class Backend:
    def __init__(self):
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        self.loads = 0

    async def load(self, operation, deadline):
        self.loads += 1
        self.started.set()
        await self.finish.wait()
        return Observation(Presence.RUNNING, "instance", True, 0)

    async def stop(self, operation, deadline):
        return Observation(Presence.STOPPED, None, False, 0)


class Recovery:
    async def recover(self, deadline):
        return RecoveryResult(True, "complete", None, ("chat",))


class SignallingRecovery(Recovery):
    def __init__(self):
        self.called = asyncio.Event()

    async def recover(self, deadline):
        self.called.set()
        return await super().recover(deadline)


class ReclaimingResources:
    def __init__(self): self.available = 110
    async def snapshot(self): return MemorySample(1_000, self.available, asyncio.get_running_loop().time())


class ReclaimingBackend:
    def __init__(self, resources): self.resources = resources; self.stops: list[str] = []
    async def load(self, operation, deadline): return Observation(Presence.RUNNING, operation.model_id, True, 0)
    async def stop(self, operation, deadline):
        self.stops.append(operation.model_id); self.resources.available = 1_000
        return Observation(Presence.STOPPED, None, False, 0)


class RecoveringReadyResources:
    def __init__(self): self.calls = 0
    async def snapshot(self):
        self.calls += 1
        available = 10 if self.calls == 1 else 9_000
        return MemorySample(10_000, available, asyncio.get_running_loop().time())


class RecoveringAdmissionGuard:
    def __init__(self): self.calls = 0
    async def __call__(self):
        self.calls += 1
        return self.calls > 1


class UnderProvisionedResources:
    def __init__(self):
        self.calls = 0

    async def snapshot(self):
        self.calls += 1
        return MemorySample(1_000, 0, asyncio.get_running_loop().time())


def book() -> Book:
    spec = ModelSpec("chat", "http://127.0.0.1:10003", frozenset({Capability.CHAT}), 100)
    result = Book({"chat": spec}, model_budget=1_000, free_floor=20, margin=0)
    result.bootstrap_stopped("chat")
    return result


def two_model_book() -> Book:
    specs = {model_id: ModelSpec(model_id, f"http://127.0.0.1:{10003 + index}", frozenset({Capability.CHAT}), 100) for index, model_id in enumerate(("first", "second"))}
    result = Book(specs, model_budget=1_000, free_floor=20, margin=0)
    for model_id in specs: result.bootstrap_stopped(model_id)
    return result


@pytest.mark.asyncio
async def test_shared_load_grants_one_lease_without_holding_condition_for_io() -> None:
    backend = Backend()
    scheduler = ModelScheduler(book(), Resources(), backend, queue_capacity=2)
    first = asyncio.create_task(scheduler.acquire("chat", "first", asyncio.get_running_loop().time() + 1))
    await backend.started.wait()
    second = asyncio.create_task(scheduler.acquire("chat", "second", asyncio.get_running_loop().time() + 1))
    await asyncio.sleep(0)
    assert backend.loads == 1
    backend.finish.set()
    lease = await first
    with pytest.raises(TimeoutError):
        await second
    await scheduler.release(lease, Outcome.SUCCESS)


@pytest.mark.asyncio
async def test_one_hundred_waiters_for_one_model_share_a_single_cold_load() -> None:
    spec = ModelSpec("chat", "http://127.0.0.1:10003", frozenset({Capability.CHAT}), 100, max_concurrency=100)
    registry = Book({"chat": spec}, model_budget=1_000, free_floor=20, margin=0)
    registry.bootstrap_stopped("chat")
    backend = Backend()
    scheduler = ModelScheduler(registry, Resources(), backend, queue_capacity=128)
    deadline = asyncio.get_running_loop().time() + 2
    waiters = [asyncio.create_task(scheduler.acquire("chat", f"request-{index}", deadline)) for index in range(100)]

    await backend.started.wait()
    backend.finish.set()
    leases = await asyncio.gather(*waiters)
    assert backend.loads == 1
    assert len({lease.lease_id for lease in leases}) == 100
    for lease in leases:
        await scheduler.release(lease, Outcome.SUCCESS)


@pytest.mark.asyncio
async def test_waiter_capacity_is_bounded() -> None:
    backend = Backend()
    scheduler = ModelScheduler(book(), Resources(), backend, queue_capacity=1)
    first = asyncio.create_task(scheduler.acquire("chat", "first", asyncio.get_running_loop().time() + 1))
    await backend.started.wait()
    with pytest.raises(QueueFull):
        await scheduler.acquire("chat", "second", asyncio.get_running_loop().time() + 1)
    backend.finish.set()
    lease = await first
    await scheduler.release(lease, Outcome.SUCCESS)


@pytest.mark.asyncio
async def test_cancelled_waiter_is_removed_before_a_shared_load_finishes() -> None:
    backend = Backend()
    scheduler = ModelScheduler(book(), Resources(), backend)
    waiting = asyncio.create_task(scheduler.acquire("chat", "cancelled", asyncio.get_running_loop().time() + 1))
    await backend.started.wait()
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert scheduler._queue.size == 0
    backend.finish.set()


@pytest.mark.asyncio
async def test_only_one_cold_load_runs_across_different_models() -> None:
    backend = Backend()
    scheduler = ModelScheduler(two_model_book(), Resources(), backend)
    first = asyncio.create_task(scheduler.acquire("first", "first-request", asyncio.get_running_loop().time() + 1))
    await backend.started.wait()
    second = asyncio.create_task(scheduler.acquire("second", "second-request", asyncio.get_running_loop().time() + 1))
    await asyncio.sleep(0)
    assert backend.loads == 1
    backend.finish.set()
    first_lease, second_lease = await first, await second
    assert backend.loads == 2
    await scheduler.release(first_lease, Outcome.SUCCESS)
    await scheduler.release(second_lease, Outcome.SUCCESS)


@pytest.mark.asyncio
async def test_switch_cooldown_delays_only_the_next_cold_load() -> None:
    backend = Backend(); backend.finish.set()
    scheduler = ModelScheduler(
        two_model_book(), Resources(), backend, poll_interval_seconds=0.001,
        switch_window_seconds=0.01, max_switches_in_window=1, cooldown_seconds=0.02,
    )
    first = await scheduler.acquire("first", "first-request", asyncio.get_running_loop().time() + 1)
    await scheduler.release(first, Outcome.SUCCESS)

    second_task = asyncio.create_task(scheduler.acquire("second", "second-request", asyncio.get_running_loop().time() + 1))
    await asyncio.sleep(0.005)
    assert backend.loads == 1
    ready = await scheduler.acquire("first", "ready-request", asyncio.get_running_loop().time() + 1)
    assert ready.model_id == "first"
    await scheduler.release(ready, Outcome.SUCCESS)
    second = await second_task
    assert backend.loads == 2
    await scheduler.release(second, Outcome.SUCCESS)


@pytest.mark.asyncio
async def test_unload_requires_backend_stop_evidence() -> None:
    backend = Backend(); backend.finish.set()
    scheduler = ModelScheduler(book(), Resources(), backend)
    lease = await scheduler.acquire("chat", "request", asyncio.get_running_loop().time() + 1)
    await scheduler.release(lease, Outcome.SUCCESS)
    await scheduler.unload("chat", asyncio.get_running_loop().time() + 1)
    assert scheduler.book.runtime["chat"].state.value == "unloaded"


@pytest.mark.asyncio
async def test_manual_unload_fails_when_stop_is_not_confirmed() -> None:
    class UnverifiedBackend(Backend):
        async def stop(self, operation, deadline): return Observation(Presence.UNKNOWN, None, False, 0, "still_running")
    backend = UnverifiedBackend(); backend.finish.set()
    scheduler = ModelScheduler(book(), Resources(), backend)
    lease = await scheduler.acquire("chat", "request", asyncio.get_running_loop().time() + 1)
    await scheduler.release(lease, Outcome.SUCCESS)
    with pytest.raises(ModelUnavailable, match="stop_unverified"):
        await scheduler.unload("chat", asyncio.get_running_loop().time() + 1)
    assert scheduler.book.runtime["chat"].state.value == "error"


@pytest.mark.asyncio
async def test_ttl_sweep_stops_only_idle_models_without_waiters() -> None:
    backend = Backend(); backend.finish.set()
    spec = ModelSpec("chat", "http://127.0.0.1:10003", frozenset({Capability.CHAT}), 100, ttl_seconds=0.001)
    registry = Book({"chat": spec}, model_budget=1_000, free_floor=20, margin=0)
    registry.bootstrap_stopped("chat")
    scheduler = ModelScheduler(registry, Resources(), backend)
    lease = await scheduler.acquire("chat", "request", asyncio.get_running_loop().time() + 1)
    await scheduler.release(lease, Outcome.SUCCESS)
    await asyncio.sleep(0.002)
    assert await scheduler.sweep_ttl(asyncio.get_running_loop().time() + 1) == ("chat",)
    assert registry.runtime["chat"].state.value == "unloaded"


@pytest.mark.asyncio
async def test_ttl_sweep_does_not_stop_a_model_with_a_waiter() -> None:
    backend = Backend(); backend.finish.set()
    spec = ModelSpec("chat", "http://127.0.0.1:10003", frozenset({Capability.CHAT}), 100, ttl_seconds=0.001)
    registry = Book({"chat": spec}, model_budget=1_000, free_floor=20, margin=0)
    registry.bootstrap_stopped("chat")
    scheduler = ModelScheduler(registry, Resources(), backend)
    lease = await scheduler.acquire("chat", "initial", asyncio.get_running_loop().time() + 1)
    await scheduler.release(lease, Outcome.SUCCESS)
    await asyncio.sleep(0.002)
    async with scheduler._condition:
        scheduler._queue.enqueue("waiting", "chat", 0, asyncio.get_running_loop().time() + 1, asyncio.get_running_loop().time())
    assert await scheduler.sweep_ttl(asyncio.get_running_loop().time() + 1) == ()
    assert registry.runtime["chat"].state.value == "ready"


@pytest.mark.asyncio
async def test_scheduler_recovery_uses_injected_control_port() -> None:
    scheduler = ModelScheduler(book(), Resources(), Backend(), recovery=Recovery())
    await scheduler.recover(asyncio.get_running_loop().time() + 1)
    assert scheduler.book.runtime["chat"].state.value == "unloaded"


@pytest.mark.asyncio
async def test_scheduler_keeps_accounting_conservative_when_recovery_stop_evidence_is_incomplete() -> None:
    class IncompleteRecovery:
        async def recover(self, deadline):
            return RecoveryResult(True, "complete", None, ())

    scheduler = ModelScheduler(book(), Resources(), Backend(), recovery=IncompleteRecovery())

    with pytest.raises(ModelUnavailable, match="control_recovery_incomplete"):
        await scheduler.recover(asyncio.get_running_loop().time() + 1)

    assert scheduler.book.recovering is True
    assert scheduler.book.runtime["chat"].state.value == "unloaded"


@pytest.mark.asyncio
async def test_second_recovery_request_is_rejected_while_helper_is_running() -> None:
    class BlockingRecovery:
        def __init__(self):
            self.started = asyncio.Event()
            self.finish = asyncio.Event()

        async def recover(self, deadline):
            self.started.set()
            await self.finish.wait()
            return RecoveryResult(True, "complete", None, ("chat",))

    recovery = BlockingRecovery()
    scheduler = ModelScheduler(book(), Resources(), Backend(), recovery=recovery)
    first = asyncio.create_task(scheduler.recover(asyncio.get_running_loop().time() + 1))
    await recovery.started.wait()
    with pytest.raises(Conflict, match="recovery_in_progress"):
        await scheduler.recover(asyncio.get_running_loop().time() + 1)
    recovery.finish.set()
    await first


@pytest.mark.asyncio
async def test_unverified_load_triggers_injected_global_recovery() -> None:
    class UnverifiedLoadBackend(Backend):
        async def load(self, operation, deadline):
            return Observation(Presence.UNKNOWN, None, False, 0, "load_timeout")

    recovery = SignallingRecovery()
    scheduler = ModelScheduler(book(), Resources(), UnverifiedLoadBackend(), recovery=recovery)
    with pytest.raises(ModelUnavailable, match="load_timeout"):
        await scheduler.acquire("chat", "request", asyncio.get_running_loop().time() + 1)
    await asyncio.wait_for(recovery.called.wait(), 1)
    for _ in range(100):
        if scheduler.book.runtime["chat"].state.value == "unloaded":
            break
        await asyncio.sleep(0)
    assert scheduler.book.runtime["chat"].state.value == "unloaded"


@pytest.mark.asyncio
async def test_cold_load_evicts_a_complete_idle_prefix_before_loading() -> None:
    specs = {
        "resident": ModelSpec("resident", "http://127.0.0.1:10001", frozenset({Capability.CHAT}), 100, priority=1),
        "target": ModelSpec("target", "http://127.0.0.1:10002", frozenset({Capability.CHAT}), 100, priority=1),
    }
    registry = Book(specs, model_budget=150, free_floor=20, margin=0)
    for model_id in specs: registry.bootstrap_stopped(model_id)
    operation = registry.begin_load("resident", MemorySample(1_000, 1_000, 0), 0)
    registry.loaded(operation, 0)
    resources = ReclaimingResources(); backend = ReclaimingBackend(resources)
    scheduler = ModelScheduler(registry, resources, backend)
    lease = await scheduler.acquire("target", "target-request", asyncio.get_running_loop().time() + 1)
    assert backend.stops == ["resident"]
    assert registry.runtime["resident"].state.value == "unloaded"
    assert lease.model_id == "target"


@pytest.mark.asyncio
async def test_cold_target_freezes_busy_evictable_model_until_its_lease_drains() -> None:
    specs = {
        "resident": ModelSpec("resident", "http://127.0.0.1:10001", frozenset({Capability.CHAT}), 100, max_concurrency=2),
        "target": ModelSpec("target", "http://127.0.0.1:10002", frozenset({Capability.CHAT}), 100),
    }
    registry = Book(specs, model_budget=150, free_floor=20, margin=0)
    for model_id in specs:
        registry.bootstrap_stopped(model_id)
    operation = registry.begin_load("resident", MemorySample(1_000, 1_000, 0), 0)
    registry.loaded(operation, 0)
    active = registry.acquire_ready("resident", "active", 0)
    resources = ReclaimingResources()
    scheduler = ModelScheduler(registry, resources, ReclaimingBackend(resources), poll_interval_seconds=0.001)

    waiting = asyncio.create_task(scheduler.acquire("target", "cold-target", asyncio.get_running_loop().time() + 1))
    for _ in range(100):
        if registry.runtime["resident"].admission_blocked:
            break
        await asyncio.sleep(0)
    assert registry.runtime["resident"].admission_blocked is True
    assert not waiting.done()

    await scheduler.release(active, Outcome.SUCCESS)
    target = await waiting
    assert registry.runtime["resident"].state.value == "unloaded"
    await scheduler.release(target, Outcome.SUCCESS)


@pytest.mark.asyncio
async def test_switch_freeze_expires_and_restores_resident_admission() -> None:
    specs = {
        "resident": ModelSpec("resident", "http://127.0.0.1:10001", frozenset({Capability.CHAT}), 100, max_concurrency=2),
        "target": ModelSpec("target", "http://127.0.0.1:10002", frozenset({Capability.CHAT}), 100),
    }
    registry = Book(specs, model_budget=150, free_floor=20, margin=0)
    for model_id in specs:
        registry.bootstrap_stopped(model_id)
    operation = registry.begin_load("resident", MemorySample(1_000, 1_000, 0), 0)
    registry.loaded(operation, 0)
    registry.acquire_ready("resident", "active", 0)
    resources = ReclaimingResources()
    scheduler = ModelScheduler(
        registry, resources, ReclaimingBackend(resources), poll_interval_seconds=0.001,
        switch_drain_timeout_seconds=0.01, switch_retry_seconds=1,
    )

    waiting = asyncio.create_task(scheduler.acquire("target", "cold-target", asyncio.get_running_loop().time() + 1))
    await asyncio.sleep(0.03)  # Longer than the configured switch drain timeout.
    assert registry.runtime["resident"].admission_blocked is False
    assert registry.acquire_ready("resident", "new-resident", asyncio.get_running_loop().time()).model_id == "resident"
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting


@pytest.mark.asyncio
async def test_preload_residency_does_not_create_a_user_lease_or_heat() -> None:
    spec = ModelSpec("chat", "http://127.0.0.1:10003", frozenset({Capability.CHAT}), 100, preload=True)
    registry = Book({"chat": spec}, model_budget=1_000, free_floor=20, margin=0)
    registry.bootstrap_stopped("chat")
    backend = Backend(); backend.finish.set()
    scheduler = ModelScheduler(registry, Resources(), backend)
    assert await scheduler.preload(asyncio.get_running_loop().time() + 1) == ("chat",)
    runtime = registry.runtime["chat"]
    assert runtime.state.value == "ready"
    assert runtime.total_requests == 0 and not runtime.leases and registry.heat("chat", 1) == 0


@pytest.mark.asyncio
async def test_preload_waits_for_a_new_resource_sample_instead_of_spinning() -> None:
    spec = ModelSpec("chat", "http://127.0.0.1:10003", frozenset({Capability.CHAT}), 100, preload=True)
    registry = Book({"chat": spec}, model_budget=1_000, free_floor=20, margin=0)
    registry.bootstrap_stopped("chat")
    resources = UnderProvisionedResources()
    scheduler = ModelScheduler(registry, resources, Backend(), poll_interval_seconds=0.001)

    with pytest.raises(TimeoutError, match="preload deadline elapsed"):
        await scheduler.preload(asyncio.get_running_loop().time() + 0.01)
    assert resources.calls < 20


@pytest.mark.asyncio
async def test_shutdown_rejects_admission_aborts_remaining_leases_and_stops_models() -> None:
    backend = Backend(); backend.finish.set()
    scheduler = ModelScheduler(book(), Resources(), backend)
    lease = await scheduler.acquire("chat", "active", asyncio.get_running_loop().time() + 1)
    stopped = await scheduler.shutdown(asyncio.get_running_loop().time())
    assert stopped == ("chat",)
    assert scheduler.book.runtime["chat"].state.value == "unloaded"
    with pytest.raises(ModelUnavailable, match="shutting_down"):
        await scheduler.acquire("chat", "new", asyncio.get_running_loop().time() + 1)
    assert scheduler.book.release(lease, Outcome.SUCCESS, asyncio.get_running_loop().time()) is False


@pytest.mark.asyncio
async def test_storage_loss_rejects_requests_aborts_leases_and_stops_models() -> None:
    backend = Backend(); backend.finish.set()
    scheduler = ModelScheduler(book(), Resources(), backend)
    lease = await scheduler.acquire("chat", "active", asyncio.get_running_loop().time() + 1)

    assert await scheduler.storage_lost(asyncio.get_running_loop().time() + 1) == ("chat",)
    assert scheduler.book.runtime["chat"].state.value == "unloaded"
    assert scheduler.book.release(lease, Outcome.SUCCESS, asyncio.get_running_loop().time()) is False
    with pytest.raises(ModelUnavailable, match="storage_unavailable"):
        await scheduler.acquire("chat", "new", asyncio.get_running_loop().time() + 1)


@pytest.mark.asyncio
async def test_storage_loss_keeps_error_and_budget_when_stop_is_unverified() -> None:
    class UnverifiedStopBackend(Backend):
        async def stop(self, operation, deadline):
            return Observation(Presence.UNKNOWN, None, False, 0, "still_running")

    backend = UnverifiedStopBackend(); backend.finish.set()
    scheduler = ModelScheduler(book(), Resources(), backend)
    lease = await scheduler.acquire("chat", "active", asyncio.get_running_loop().time() + 1)

    assert await scheduler.storage_lost(asyncio.get_running_loop().time() + 1) == ()
    assert scheduler.book.runtime["chat"].state.value == "error"
    assert scheduler.book.committed == scheduler.book.required("chat")
    assert scheduler.book.release(lease, Outcome.SUCCESS, asyncio.get_running_loop().time()) is False


@pytest.mark.asyncio
async def test_storage_monitor_transition_runs_the_storage_loss_sequence() -> None:
    registry = book()
    operation = registry.begin_load("chat", MemorySample(10_000, 9_000, 0), 0)
    registry.loaded(operation, 0)
    backend = Backend(); backend.finish.set()
    scheduler = ModelScheduler(registry, Resources(), backend, admission_guard=lambda: False)

    assert await scheduler.monitor_storage_once(asyncio.get_running_loop().time() + 1) is False
    assert registry.runtime["chat"].state.value == "unloaded"


@pytest.mark.asyncio
async def test_preload_fails_immediately_after_storage_loss() -> None:
    spec = ModelSpec("chat", "http://127.0.0.1:10003", frozenset({Capability.CHAT}), 100, preload=True)
    registry = Book({"chat": spec}, model_budget=1_000, free_floor=20, margin=0)
    registry.bootstrap_stopped("chat")
    scheduler = ModelScheduler(registry, Resources(), Backend())
    await scheduler.storage_lost(asyncio.get_running_loop().time() + 1)

    with pytest.raises(ModelUnavailable, match="storage_unavailable"):
        await scheduler.preload(asyncio.get_running_loop().time() + 1)


@pytest.mark.asyncio
async def test_explicit_storage_recovery_revalidates_then_reopens_admission() -> None:
    class Guard:
        allowed = True

        async def __call__(self):
            return self.allowed

    guard = Guard()
    backend = Backend(); backend.finish.set()
    scheduler = ModelScheduler(book(), Resources(), backend, admission_guard=guard, recovery=Recovery())
    await scheduler.storage_lost(asyncio.get_running_loop().time() + 1)

    await scheduler.storage_recovered(asyncio.get_running_loop().time() + 1)
    lease = await scheduler.acquire("chat", "after-recovery", asyncio.get_running_loop().time() + 1)
    assert lease.model_id == "chat"


@pytest.mark.asyncio
async def test_storage_recovery_refuses_to_reopen_when_revalidation_fails() -> None:
    class Guard:
        async def __call__(self):
            return False

    scheduler = ModelScheduler(book(), Resources(), Backend(), admission_guard=Guard(), recovery=Recovery())
    await scheduler.storage_lost(asyncio.get_running_loop().time() + 1)

    with pytest.raises(ModelUnavailable, match="storage_unavailable"):
        await scheduler.storage_recovered(asyncio.get_running_loop().time() + 1)
    assert (await scheduler.status())["admission"]["storage_unavailable"] is True


@pytest.mark.asyncio
async def test_ready_admission_waits_for_fresh_free_memory_sample() -> None:
    registry = book()
    operation = registry.begin_load("chat", MemorySample(10_000, 9_000, 0), 0)
    registry.loaded(operation, 0)
    scheduler = ModelScheduler(registry, RecoveringReadyResources(), Backend(), poll_interval_seconds=0.001)
    lease = await scheduler.acquire("chat", "request", asyncio.get_running_loop().time() + 1)
    assert lease.model_id == "chat"


@pytest.mark.asyncio
async def test_ready_admission_waits_for_storage_guard() -> None:
    registry = book()
    operation = registry.begin_load("chat", MemorySample(10_000, 9_000, 0), 0)
    registry.loaded(operation, 0)
    guard = RecoveringAdmissionGuard()
    scheduler = ModelScheduler(registry, Resources(), Backend(), poll_interval_seconds=0.001, admission_guard=guard)
    lease = await scheduler.acquire("chat", "request", asyncio.get_running_loop().time() + 1)
    assert lease.model_id == "chat"
    assert guard.calls >= 2


@pytest.mark.asyncio
async def test_status_exposes_admission_faults_with_memory_sample_compatible_resources() -> None:
    scheduler = ModelScheduler(book(), Resources(), Backend())
    initial = await scheduler.status()
    assert initial["resources"]["used_bytes"] == 1_000
    assert initial["admission"]["storage_unavailable"] is False

    await scheduler.storage_lost(asyncio.get_running_loop().time() + 1)
    faulted = await scheduler.status()
    assert faulted["admission"]["storage_unavailable"] is True


@pytest.mark.asyncio
async def test_status_projects_model_contract_fields_and_waiting_count() -> None:
    registry = book()
    operation = registry.begin_load("chat", MemorySample(10_000, 9_000, 0), 0)
    registry.loaded(operation, 0)
    lease = registry.acquire_ready("chat", "active", 0)
    scheduler = ModelScheduler(registry, Resources(), Backend())
    async with scheduler._condition:
        scheduler._queue.enqueue("waiting", "chat", 0, asyncio.get_running_loop().time() + 1, asyncio.get_running_loop().time())

    model = (await scheduler.status())["models"]["chat"]
    assert model["state"] == "active"
    assert model["waiting_requests"] == 1
    assert model["capabilities"] == ["chat"]
    assert model["reserved_bytes"] == 100
    assert model["effective_reserved_bytes"] == 100
    assert model["max_concurrency"] == 1
    assert model["idle_seconds"] is None
    assert model["total_requests"] == 1
    await scheduler.release(lease, Outcome.SUCCESS)


# ---------------------------------------------------------------------------
# P09: C04 exclusive sessions on the same scheduler authority.
# ---------------------------------------------------------------------------


class ManualClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class SessionBackend:
    def __init__(self) -> None:
        self.loads: list[str] = []
        self.stops: list[str] = []
        self.stop_failures: set[str] = set()

    async def load(self, operation, deadline):
        self.loads.append(operation.model_id)
        return Observation(Presence.RUNNING, f"instance-{operation.model_id}", True, 0)

    async def stop(self, operation, deadline):
        self.stops.append(operation.model_id)
        if operation.model_id in self.stop_failures:
            return Observation(Presence.UNKNOWN, None, False, 0, "stop_unverified")
        return Observation(Presence.STOPPED, None, False, 0)


def session_policy(**overrides) -> SessionManager:
    policy = {
        "wait_seconds": 100.0,
        "hard_deadline_seconds": 200.0,
        "heartbeat_seconds": 10.0,
        "ttl_seconds": 30.0,
        "prepare_seconds": 100.0,
        "drain_seconds": 30.0,
        "retry_seconds": 30.0,
        "cleanup_seconds": 60.0,
        "cancel_seconds": 10.0,
        "stop_grace_seconds": 30.0,
        "reconcile_seconds": 5.0,
    }
    policy.update(overrides)
    return SessionManager(**policy)


class ClockedResources:
    """The same sample shape as `Resources`, on the scheduler's injected clock."""

    def __init__(self, clock: ManualClock) -> None:
        self.clock = clock

    async def snapshot(self):
        return MemorySample(10_000, 9_000, self.clock())


def session_scheduler(registry: Book, backend: SessionBackend, clock: ManualClock, sessions: SessionManager | None = None) -> ModelScheduler:
    return ModelScheduler(
        registry, ClockedResources(clock), backend,
        sessions=sessions if sessions is not None else session_policy(),
        clock=clock,
        poll_interval_seconds=0.01,
    )


async def eventually(predicate, *, attempts: int = 300) -> bool:
    for _ in range(attempts):
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


@pytest.mark.asyncio
async def test_a_session_takes_the_model_exclusively_and_stops_the_others() -> None:
    clock, backend = ManualClock(), SessionBackend()
    registry = two_model_book()
    scheduler = session_scheduler(registry, backend, clock)
    other = await scheduler.acquire("second", "interactive-1", asyncio.get_running_loop().time() + 10)
    await scheduler.release(other, Outcome.SUCCESS)

    view = await asyncio.wait_for(scheduler.open_session("first", "client-a", "session-1", hard_deadline_seconds=200), 5)

    assert view["phase"] == "active"
    assert backend.stops == ["second"]  # every other model was stopped and confirmed
    assert "first" in backend.loads
    assert registry.runtime["second"].state is State.UNLOADED
    with pytest.raises(Conflict):
        await scheduler.acquire("first", "interactive-2", asyncio.get_running_loop().time() + 10)

    closed = await asyncio.wait_for(scheduler.close_session("session-1"), 5)

    assert closed["phase"] == "closed"
    assert registry.runtime["first"].state is State.UNLOADED  # a normal end cleans up exactly the same way
    lease = await scheduler.acquire("first", "interactive-3", asyncio.get_running_loop().time() + 10)
    await scheduler.release(lease, Outcome.SUCCESS)


@pytest.mark.asyncio
async def test_a_held_lease_is_never_killed_and_the_session_yields_instead() -> None:
    clock, backend = ManualClock(), SessionBackend()
    registry = book()
    sessions = session_policy(drain_seconds=30.0, retry_seconds=30.0)
    scheduler = session_scheduler(registry, backend, clock, sessions)
    lease = await scheduler.acquire("chat", "interactive-1", asyncio.get_running_loop().time() + 10)
    opening = asyncio.create_task(scheduler.open_session("chat", "client-a", "session-1", hard_deadline_seconds=200))

    assert await eventually(lambda: scheduler._session_freeze == "session-1")
    assert registry.runtime["chat"].leases.get(lease.lease_id) == lease  # the lease is never revoked
    assert sessions.get("session-1").phase == "preparing"

    clock.advance(30.0)  # the drain window elapses
    assert await eventually(lambda: scheduler._session_freeze is None)
    yielded = sessions.get("session-1")
    assert yielded.phase == "preparing"
    assert yielded.retry_at == 60.0
    waiter = scheduler._queue.get("session-1")
    assert waiter is not None and waiter.state is WaitState.WAITING and waiter.kind is WaitKind.SESSION

    await scheduler.release(lease, Outcome.SUCCESS)
    clock.advance(30.0)  # the backoff elapses and the retained waiter is retried

    view = await asyncio.wait_for(opening, 5)
    assert view["phase"] == "active"
    assert scheduler._queue.get("session-1") is None


@pytest.mark.asyncio
async def test_two_clients_racing_cannot_both_become_active() -> None:
    clock, backend = ManualClock(), SessionBackend()
    registry = book()
    sessions = session_policy()
    scheduler = session_scheduler(registry, backend, clock, sessions)
    first = asyncio.create_task(scheduler.open_session("chat", "client-a", "session-1", hard_deadline_seconds=200))
    second = asyncio.create_task(scheduler.open_session("chat", "client-b", "session-2", hard_deadline_seconds=200))

    assert await eventually(lambda: sessions.active_id is not None)
    active = [session_id for session_id, record in sessions.records.items() if record.phase == "active"]
    winner = active[0]
    loser = "session-2" if winner == "session-1" else "session-1"
    assert len(active) == 1 and sessions.active_id == winner
    assert sessions.records[loser].phase == "preparing"

    await asyncio.wait_for(first if winner == "session-1" else second, 5)
    await asyncio.wait_for(scheduler.close_session(winner), 5)
    pending = second if winner == "session-1" else first
    assert (await asyncio.wait_for(pending, 5))["phase"] == "active"
    await asyncio.wait_for(scheduler.close_session(loser), 5)


@pytest.mark.asyncio
async def test_a_session_uses_the_registered_parallel_slots() -> None:
    clock, backend = ManualClock(), SessionBackend()
    spec = ModelSpec("chat", "http://127.0.0.1:10003", frozenset({Capability.CHAT}), 100, max_concurrency=2)
    registry = Book({"chat": spec}, model_budget=1_000, free_floor=20, margin=0)
    registry.bootstrap_stopped("chat")
    sessions = session_policy()
    scheduler = session_scheduler(registry, backend, clock, sessions)
    await asyncio.wait_for(scheduler.open_session("chat", "client-a", "session-1", hard_deadline_seconds=200), 5)

    first = await scheduler.acquire("chat", "req-1", asyncio.get_running_loop().time() + 10, session_id="session-1")
    second = await scheduler.acquire("chat", "req-2", asyncio.get_running_loop().time() + 10, session_id="session-1")
    with pytest.raises(Conflict):
        await scheduler.acquire("chat", "req-3", asyncio.get_running_loop().time() + 10, session_id="session-1")
    assert sessions.in_flight["session-1"] == 2

    await scheduler.release(first, Outcome.SUCCESS)
    third = await scheduler.acquire("chat", "req-3", asyncio.get_running_loop().time() + 10, session_id="session-1")
    await scheduler.release(second, Outcome.SUCCESS)
    await scheduler.release(third, Outcome.SUCCESS)
    with pytest.raises(SessionNotFound):
        await scheduler.acquire("chat", "req-4", asyncio.get_running_loop().time() + 10, session_id="session-9")
    await asyncio.wait_for(scheduler.close_session("session-1"), 5)


@pytest.mark.asyncio
async def test_pinned_or_preload_configuration_refuses_a_session() -> None:
    clock, backend = ManualClock(), SessionBackend()
    spec = ModelSpec("chat", "http://127.0.0.1:10003", frozenset({Capability.CHAT}), 100, pinned=True, preload=True)
    registry = Book({"chat": spec}, model_budget=1_000, free_floor=20, margin=0)
    registry.bootstrap_stopped("chat")
    scheduler = session_scheduler(registry, backend, clock)

    with pytest.raises(SessionConflict):
        await scheduler.open_session("chat", "client-a", "session-1", hard_deadline_seconds=200)
    assert scheduler.sessions.records == {}


@pytest.mark.asyncio
async def test_health_information_stays_available_while_a_session_waits() -> None:
    clock, backend = ManualClock(), SessionBackend()
    registry = book()
    scheduler = session_scheduler(registry, backend, clock)
    lease = await scheduler.acquire("chat", "interactive-1", asyncio.get_running_loop().time() + 10)
    opening = asyncio.create_task(scheduler.open_session("chat", "client-a", "session-1", hard_deadline_seconds=200))

    assert await eventually(lambda: scheduler._session_freeze == "session-1")
    status = await asyncio.wait_for(scheduler.status(), 2)

    assert status["sessions"]["pending"][0]["phase"] == "preparing"
    assert status["sessions"]["frozen"] is True
    await scheduler.release(lease, Outcome.SUCCESS)
    await asyncio.wait_for(opening, 5)
    await asyncio.wait_for(scheduler.close_session("session-1"), 5)


@pytest.mark.asyncio
async def test_an_unconfirmed_cleanup_blocks_and_keeps_the_budget_until_it_reconciles() -> None:
    clock, backend = ManualClock(), SessionBackend()
    registry = book()
    sessions = session_policy()
    scheduler = session_scheduler(registry, backend, clock, sessions)
    await asyncio.wait_for(scheduler.open_session("chat", "client-a", "session-1", hard_deadline_seconds=200), 5)
    backend.stop_failures.add("chat")

    closed = await asyncio.wait_for(scheduler.close_session("session-1"), 5)

    assert closed["phase"] == "blocked"
    assert closed["blocked_reason"] == "stop_unconfirmed"
    assert registry.runtime["chat"].state is State.ERROR
    assert registry.committed > 0  # the slot and the budget stay on the books

    backend.stop_failures.clear()
    clock.advance(5.0)
    assert await eventually(lambda: sessions.get("session-1").phase == "closed")
    assert registry.committed == 0


# ---------------------------------------------------------------------------
# P10: cancel/TTL/restart never release early, and late write-backs are fenced.
# ---------------------------------------------------------------------------


class GatedBackend(SessionBackend):
    """A backend whose load can be held open across a restart/recovery."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.gate = asyncio.Event()

    async def load(self, operation, deadline):
        self.loads.append(operation.model_id)
        self.started.set()
        await self.gate.wait()
        return Observation(Presence.RUNNING, f"instance-{operation.model_id}", True, 0)


class GatedRecovery:
    def __init__(self) -> None:
        self.called = asyncio.Event()
        self.gate = asyncio.Event()

    async def recover(self, deadline):
        self.called.set()
        await self.gate.wait()
        return RecoveryResult(True, "complete", None, ("chat",))


@pytest.mark.asyncio
async def test_an_accepted_cancel_keeps_the_lease_and_the_budget_until_the_proof() -> None:
    clock, backend = ManualClock(), SessionBackend()
    registry = book()
    scheduler = session_scheduler(registry, backend, clock)
    lease = await scheduler.acquire("chat", "req-1", asyncio.get_running_loop().time() + 10)

    assert await scheduler.cancel(lease) is True

    assert registry.runtime["chat"].leases.get(lease.lease_id) == lease  # the slot is still held
    assert registry.runtime["chat"].cancelling[lease.lease_id] == lease
    assert registry.committed > 0  # and so is the budget
    status = await scheduler.status()
    assert status["models"]["chat"]["cancelling"] == 1
    with pytest.raises(Conflict):
        registry.begin_eviction(["chat"])  # a live (cancelling) lease blocks a stop

    await scheduler.release(lease, Outcome.ABORTED)  # the trusted terminal proof

    assert registry.runtime["chat"].cancelling == {}
    assert registry.committed > 0  # an aborted request keeps the reservation until STOPPED
    assert registry.runtime["chat"].state is State.ERROR
    await asyncio.wait_for(scheduler.shutdown(asyncio.get_running_loop().time() + 5), 5)
    assert registry.committed == 0


@pytest.mark.asyncio
async def test_a_late_load_write_back_is_rejected_and_keeps_the_raw_fence() -> None:
    backend, recovery = GatedBackend(), GatedRecovery()
    events: list[tuple[str, dict]] = []
    registry = book()
    scheduler = ModelScheduler(
        registry, Resources(), backend,
        recovery=recovery,
        event_sink=lambda kind, payload: events.append((kind, dict(payload))),
        poll_interval_seconds=0.01,
    )
    acquiring = asyncio.create_task(scheduler.acquire("chat", "req-1", asyncio.get_running_loop().time() + 30))
    await backend.started.wait()
    recovering = asyncio.create_task(scheduler.recover(asyncio.get_running_loop().time() + 30))
    await recovery.called.wait()  # begin_recovery already bumped the epoch

    backend.gate.set()  # the load "succeeds" for an operation that is now stale

    assert await eventually(lambda: any(kind == "writeback_rejected" for kind, _ in events))
    acquiring.cancel()
    recovery.gate.set()
    await recovering
    with contextlib.suppress(asyncio.CancelledError, ModelUnavailable):
        await acquiring  # recovery refuses the waiter; either way nothing was applied

    kind, payload = next(item for item in events if item[0] == "writeback_rejected")
    assert kind == "writeback_rejected"
    assert payload["stage"] == "load"
    assert payload["reason"] == "stale_operation"
    assert payload["operation_id"] and payload["generation"] >= 1
    assert payload["epoch"] == 0  # the fence the operation was created under
    assert registry.epoch >= 1  # recovery moved the epoch, so that write-back is stale
    assert registry.runtime["chat"].state is not State.READY  # the late success changed nothing


class ClockedReclaimingResources:
    def __init__(self, clock: ManualClock) -> None:
        self.clock = clock
        self.available = 110

    async def snapshot(self):
        return MemorySample(10_000, self.available, self.clock())


class ClockedReclaimingBackend:
    def __init__(self, resources: ClockedReclaimingResources) -> None:
        self.resources = resources
        self.loads: list[str] = []
        self.stops: list[str] = []

    async def load(self, operation, deadline):
        self.loads.append(operation.model_id)
        return Observation(Presence.RUNNING, operation.model_id, True, 0)

    async def stop(self, operation, deadline):
        self.stops.append(operation.model_id)
        self.resources.available = 9_000
        return Observation(Presence.STOPPED, None, False, 0)


@pytest.mark.asyncio
async def test_a_switch_drain_timeout_unfreezes_and_keeps_the_waiter_for_a_later_retry() -> None:
    clock = ManualClock()
    specs = {
        "first": ModelSpec("first", "http://127.0.0.1:10001", frozenset({Capability.CHAT}), 100, max_concurrency=2),
        "second": ModelSpec("second", "http://127.0.0.1:10002", frozenset({Capability.CHAT}), 100),
    }
    registry = Book(specs, model_budget=150, free_floor=20, margin=0)
    for model_id in specs:
        registry.bootstrap_stopped(model_id)
    operation = registry.begin_load("first", MemorySample(1_000, 1_000, 0), 0)
    registry.loaded(operation, 0)
    holder = registry.acquire_ready("first", "holder", 0)
    resources = ClockedReclaimingResources(clock)
    backend = ClockedReclaimingBackend(resources)
    scheduler = ModelScheduler(
        registry, resources, backend,
        clock=clock, poll_interval_seconds=0.01,
        switch_drain_timeout_seconds=30.0, switch_retry_seconds=30.0,
    )
    waiter = asyncio.create_task(scheduler.acquire("second", "waiter", asyncio.get_running_loop().time() + 30))

    assert await eventually(lambda: scheduler._switch_intent is not None)
    assert scheduler._switch_intent.frozen_models == ("first",)
    assert registry.runtime["first"].admission_blocked is True
    clock.advance(30.0)  # the drain window elapses without the holder's lease draining

    assert await eventually(lambda: scheduler._switch_intent is None)
    assert scheduler._queue.get("waiter") is not None  # the waiter keeps its place
    assert scheduler._next_switch_attempt["second"] >= 60.0
    assert registry.runtime["first"].leases.get(holder.lease_id) == holder  # never killed
    assert registry.runtime["first"].admission_blocked is False  # the freeze was undone

    await scheduler.release(holder, Outcome.SUCCESS)
    clock.advance(30.0)
    retried = await asyncio.wait_for(waiter, 5)

    assert retried.model_id == "second"
    assert backend.stops == ["first"]
    await scheduler.release(retried, Outcome.SUCCESS)


@pytest.mark.asyncio
async def test_ttl_and_an_unverified_stop_never_release_the_budget() -> None:
    clock, backend = ManualClock(), SessionBackend()
    spec = ModelSpec("chat", "http://127.0.0.1:10003", frozenset({Capability.CHAT}), 100, ttl_seconds=5.0)
    registry = Book({"chat": spec}, model_budget=1_000, free_floor=20, margin=0)
    registry.bootstrap_stopped("chat")
    scheduler = session_scheduler(registry, backend, clock)
    lease = await scheduler.acquire("chat", "req-1", asyncio.get_running_loop().time() + 10)
    await scheduler.release(lease, Outcome.SUCCESS)
    backend.stop_failures.add("chat")

    assert registry.ttl_due("chat", 5.0)
    clock.advance(5.0)
    stopped = await asyncio.wait_for(scheduler.sweep_ttl(asyncio.get_running_loop().time() + 5), 5)

    assert stopped == ()
    assert registry.runtime["chat"].state is State.ERROR  # TTL only begins a stop
    assert registry.committed > 0  # and the budget is kept while the stop is unproven


@pytest.mark.asyncio
async def test_close_heartbeat_and_a_late_load_race_without_reviving_the_session() -> None:
    clock, backend = ManualClock(), GatedBackend()
    registry = book()
    sessions = session_policy()
    scheduler = session_scheduler(registry, backend, clock, sessions)
    opening = asyncio.create_task(scheduler.open_session("chat", "client-a", "session-1", hard_deadline_seconds=200))
    await backend.started.wait()  # the load is in flight while the session is still PREPARING

    heartbeating = asyncio.create_task(scheduler.heartbeat_session("session-1"))
    closing = asyncio.create_task(scheduler.close_session("session-1"))
    backend.gate.set()  # the load lands after the close was requested

    closed = await asyncio.wait_for(closing, 5)

    assert closed["phase"] == "closed"
    assert sessions.active_id is None
    assert await eventually(lambda: registry.runtime["chat"].state is State.UNLOADED)  # no orphan residency
    with contextlib.suppress(SessionConflict, ModelUnavailable, asyncio.CancelledError):
        await heartbeating
    with contextlib.suppress(SessionConflict, ModelUnavailable, asyncio.CancelledError):
        await opening
    assert registry.committed == 0

    lease = await asyncio.wait_for(scheduler.acquire("chat", "after-close", asyncio.get_running_loop().time() + 5), 5)
    await scheduler.release(lease, Outcome.SUCCESS)


@pytest.mark.asyncio
async def test_an_expired_session_refuses_a_submit_and_never_becomes_active() -> None:
    clock, backend = ManualClock(), SessionBackend()
    registry = book()
    sessions = session_policy(ttl_seconds=30.0)
    scheduler = session_scheduler(registry, backend, clock, sessions)
    await asyncio.wait_for(scheduler.open_session("chat", "client-a", "session-1", hard_deadline_seconds=200), 5)

    clock.advance(30.0)  # no heartbeat: the soft TTL lapses

    assert sessions.is_live("session-1", 30.0) is False
    with pytest.raises(SessionConflict):
        await scheduler.acquire("chat", "req-1", asyncio.get_running_loop().time() + 5, session_id="session-1")
    with pytest.raises(SessionConflict):
        await scheduler.heartbeat_session("session-1")
    assert await eventually(lambda: sessions.get("session-1").phase in {"draining", "closed"})
    assert await eventually(lambda: registry.committed == 0)


@pytest.mark.asyncio
async def test_storage_recovery_revalidates_and_never_replays_inference() -> None:
    clock, backend = ManualClock(), SessionBackend()
    registry = book()
    guard_calls = {"count": 0}

    async def guard() -> bool:
        guard_calls["count"] += 1
        return True

    scheduler = ModelScheduler(
        registry, ClockedResources(clock), backend,
        recovery=Recovery(), admission_guard=guard, clock=clock, poll_interval_seconds=0.01,
    )
    lease = await scheduler.acquire("chat", "req-1", asyncio.get_running_loop().time() + 10)
    await scheduler.release(lease, Outcome.SUCCESS)
    loads_before = len(backend.loads)

    await asyncio.wait_for(scheduler.storage_lost(asyncio.get_running_loop().time() + 5), 5)

    assert registry.runtime["chat"].state is State.UNLOADED
    guard_calls["count"] = 0
    await asyncio.wait_for(scheduler.storage_recovered(asyncio.get_running_loop().time() + 5), 5)

    assert guard_calls["count"] > 0  # the mount/asset validation is re-run before reopening
    assert len(backend.loads) == loads_before  # nothing is replayed automatically
    assert registry.runtime["chat"].leases == {}
    lease = await scheduler.acquire("chat", "req-2", asyncio.get_running_loop().time() + 5)
    await scheduler.release(lease, Outcome.SUCCESS)


@pytest.mark.asyncio
async def test_a_v2_registration_acquires_through_the_ledger() -> None:
    """The target bring-up bug: a v2 registration carries no scheduling fields.

    `contracts_v2.ModelSpec` has no `priority` (the ledger supplies the default),
    so the acquire path must read the ledger — reading the raw spec crashed.
    """
    from model_scheduler.contracts_v2 import (
        AssetRef,
        DeploymentSpec,
        Envelope,
        ModelSpec as RegisteredModel,
        RuntimeSpec,
    )
    from model_scheduler.runtime import ledger_specs_from

    runtime = RuntimeSpec(runtime_id="rt", profile_id="llama-cpp-gguf-v1",
                          image_digest="img@sha256:" + "a" * 64, adapter_sha256="b" * 64,
                          lock_sha256="c" * 64, startup_args=("--no-webui",))
    model = RegisteredModel(model_id="chat", runtime_id="rt", capabilities=("chat",),
                            assets=(AssetRef(role="model", path="m.gguf", sha256="d" * 64, size_bytes=1),),
                            port=10003, envelope=Envelope(ctx_size=4096, max_input_tokens=2048,
                                                          max_output_tokens=1024, max_parallel=1,
                                                          max_image_tokens=0, max_image_edge_pixels=0, max_images=0),
                            timeout_seconds=60, reserved_bytes=100, measured=False,
                            measurement_ref=None, physical_resident_peak_bytes=None)
    specs = ledger_specs_from(registration=DeploymentSpec(runtimes=(runtime,), models=(model,)))
    registry = Book(specs, model_budget=1_000, free_floor=20, margin=0)
    registry.bootstrap_stopped("chat")
    backend = Backend()
    scheduler = ModelScheduler(registry, Resources(), backend, queue_capacity=2)

    pending = asyncio.create_task(scheduler.acquire("chat", "v2-request", asyncio.get_running_loop().time() + 1))
    await backend.started.wait()
    backend.finish.set()
    lease = await pending

    assert lease is not None
    await scheduler.release(lease, Outcome.SUCCESS)
    # `unload` reads `pinned` too: a v2 spec has none, so the ledger must supply it.
    await scheduler.unload("chat", asyncio.get_running_loop().time() + 2)
    for _ in range(20):
        if scheduler.book.runtime["chat"].state.value == "unloaded":
            break
        await asyncio.sleep(0.02)
    assert scheduler.book.runtime["chat"].state.value == "unloaded"
