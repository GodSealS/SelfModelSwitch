from __future__ import annotations

import asyncio

import httpx
import pytest

from app import create_app
from model_scheduler.contracts import Lease, Outcome


class Scheduler:
    def __init__(self) -> None:
        self.released = asyncio.Event()
        self.outcomes: list[Outcome] = []

    async def acquire(self, model_id, request_id, deadline):
        return Lease("lease", request_id, model_id, 1)

    async def release(self, lease, outcome, tokens=None):
        self.outcomes.append(outcome)
        self.released.set()


class BlockingGateway:
    def __init__(self) -> None:
        self.opened = asyncio.Event()

    async def open(self, lease, capability, payload, deadline):
        self.opened.set()
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_cancelling_gateway_open_releases_chat_lease() -> None:
    scheduler = Scheduler()
    gateway = BlockingGateway()
    transport = httpx.ASGITransport(app=create_app(scheduler=scheduler, gateway=gateway))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        request = asyncio.create_task(client.post("/v1/chat/completions", json={"model": "qwen-small", "messages": []}))
        await gateway.opened.wait()
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
    await asyncio.wait_for(scheduler.released.wait(), 1)
    assert scheduler.outcomes == [Outcome.ABORTED]
