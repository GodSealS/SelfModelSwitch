from __future__ import annotations

import asyncio

import pytest

from model_scheduler.contracts import Capability, MemorySample, ModelSpec, Observation, Outcome, Presence
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
