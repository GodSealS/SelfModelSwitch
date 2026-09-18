from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app import create_app
from model_scheduler.contracts import Capability, Lease, MemorySample, ModelSpec, Observation, Outcome, Presence, State


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


@pytest.mark.asyncio
async def test_disconnect_while_sending_done_keeps_the_chat_lease_aborted() -> None:
    class DoneGateway:
        async def open(self, lease, capability, payload, deadline):
            class Opened:
                status_code = 200
                headers = {"content-type": "text/event-stream"}

                def iter_bytes(self):
                    async def iterator():
                        yield b"data: [DONE]\n\n"
                    return iterator()

                async def aclose(self):
                    return None

            return Opened()

    scheduler = Scheduler()
    app = create_app(scheduler=scheduler, gateway=DoneGateway())
    body = json.dumps({"model": "qwen-small", "messages": [], "stream": True}).encode()
    sent_request = False

    async def receive():
        nonlocal sent_request
        if not sent_request:
            sent_request = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Event().wait()

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            raise asyncio.CancelledError

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8090),
    }
    await app(scope, receive, send)
    await asyncio.wait_for(scheduler.released.wait(), 1)
    assert scheduler.outcomes == [Outcome.ABORTED]


@pytest.mark.asyncio
async def test_a_disconnect_keeps_the_model_budget_until_a_proven_stop() -> None:
    from model_scheduler.model_registry import Book
    from model_scheduler.scheduler import ModelScheduler

    spec = ModelSpec("qwen-small", "http://127.0.0.1:10003", frozenset({Capability.CHAT}), 100)
    registry = Book({"qwen-small": spec}, model_budget=1_000, free_floor=20, margin=0)
    registry.bootstrap_stopped("qwen-small")

    class Resources:
        async def snapshot(self):
            return MemorySample(10_000, 9_000, asyncio.get_running_loop().time())

    class Backend:
        async def load(self, operation, deadline):
            return Observation(Presence.RUNNING, "instance", True, 0)

        async def stop(self, operation, deadline):
            return Observation(Presence.STOPPED, None, False, 0)

    scheduler = ModelScheduler(registry, Resources(), Backend())
    lease = await scheduler.acquire("qwen-small", "req-1", asyncio.get_running_loop().time() + 5)
    await scheduler.release(lease, Outcome.ABORTED)  # what a client disconnect does

    assert registry.runtime["qwen-small"].state is State.ERROR
    assert registry.committed > 0  # the abort keeps the reservation

    await scheduler.shutdown(asyncio.get_running_loop().time() + 5)

    assert registry.committed == 0  # only a proven stop releases it
