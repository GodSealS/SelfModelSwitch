from __future__ import annotations

import asyncio

import pytest

from model_scheduler.contracts import Capability, MemorySample, ModelSpec, Observation, Outcome, Presence, RecoveryResult
from model_scheduler.model_registry import Book
from model_scheduler.scheduler import ModelScheduler, ModelUnavailable, QueueFull


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
