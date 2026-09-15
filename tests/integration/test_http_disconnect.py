from __future__ import annotations

import asyncio

import httpx
import pytest
import uvicorn

from app import create_app
from model_scheduler.contracts import Lease, Outcome


class Scheduler:
    def __init__(self) -> None:
        self.released = asyncio.Event()
        self.outcomes: list[Outcome] = []

    async def acquire(self, model_id: str, request_id: str, deadline: float) -> Lease:
        return Lease("lease", request_id, model_id, 1)

    async def release(self, lease: Lease, outcome: Outcome, tokens=None) -> None:
        self.outcomes.append(outcome)
        self.released.set()


class Gateway:
    async def open(self, lease, capability, payload, deadline):
        class Opened:
            status_code = 200
            headers = {"content-type": "text/event-stream"}

            def __init__(self) -> None:
                self.closed = asyncio.Event()

            def iter_bytes(self):
                async def iterator():
                    try:
                        yield b"data: partial\n\n"
                        await asyncio.Event().wait()
                    finally:
                        self.closed.set()
                return iterator()

            async def aclose(self) -> None:
                self.closed.set()

        return Opened()


async def _start(app):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error"))
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started and server.servers:
            socket = next(iter(server.servers)).sockets[0]
            return server, task, socket.getsockname()[1]
        await asyncio.sleep(0.01)
    server.should_exit = True
    await task
    raise AssertionError("uvicorn did not start")


@pytest.mark.asyncio
async def test_real_client_disconnect_aborts_sse_lease() -> None:
    scheduler = Scheduler()
    server, task, port = await _start(create_app(scheduler=scheduler, gateway=Gateway()))
    try:
        async with httpx.AsyncClient() as client:
            async with client.stream(
                "POST",
                f"http://127.0.0.1:{port}/v1/chat/completions",
                json={"model": "qwen-small", "messages": [], "stream": True},
            ) as response:
                assert response.status_code == 200
                assert await anext(response.aiter_bytes()) == b"data: partial\n\n"
        await asyncio.wait_for(scheduler.released.wait(), 2)
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
    assert scheduler.outcomes == [Outcome.ABORTED]
