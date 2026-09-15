from __future__ import annotations

import asyncio

import pytest

from model_scheduler.contracts import Capability, MemorySample, ModelSpec, Observation, Outcome, Presence, RecoveryResult
from model_scheduler.model_registry import Book
from model_scheduler.scheduler import ModelScheduler, QueueFull


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


class ReclaimingResources:
    def __init__(self): self.available = 110
    async def snapshot(self): return MemorySample(1_000, self.available, asyncio.get_running_loop().time())


class ReclaimingBackend:
    def __init__(self, resources): self.resources = resources; self.stops: list[str] = []
    async def load(self, operation, deadline): return Observation(Presence.RUNNING, operation.model_id, True, 0)
    async def stop(self, operation, deadline):
        self.stops.append(operation.model_id); self.resources.available = 1_000
        return Observation(Presence.STOPPED, None, False, 0)


def book() -> Book:
    spec = ModelSpec("chat", "http://127.0.0.1:10003", frozenset({Capability.CHAT}), 100)
    result = Book({"chat": spec}, model_budget=1_000, free_floor=20, margin=0)
    result.bootstrap_stopped("chat")
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
async def test_unload_requires_backend_stop_evidence() -> None:
    backend = Backend(); backend.finish.set()
    scheduler = ModelScheduler(book(), Resources(), backend)
    lease = await scheduler.acquire("chat", "request", asyncio.get_running_loop().time() + 1)
    await scheduler.release(lease, Outcome.SUCCESS)
    await scheduler.unload("chat", asyncio.get_running_loop().time() + 1)
    assert scheduler.book.runtime["chat"].state.value == "unloaded"


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
        scheduler._waiters.add("waiting")
        scheduler._waiter_models["waiting"] = "chat"
    assert await scheduler.sweep_ttl(asyncio.get_running_loop().time() + 1) == ()
    assert registry.runtime["chat"].state.value == "ready"


@pytest.mark.asyncio
async def test_scheduler_recovery_uses_injected_control_port() -> None:
    scheduler = ModelScheduler(book(), Resources(), Backend(), recovery=Recovery())
    await scheduler.recover(asyncio.get_running_loop().time() + 1)
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
