"""K7/RP14: the shared gate and the thin TCP adapter the v2 owner runs on.

Two rules are under test:

* the gate is an in-process flag checked **before** dispatch, so a closed gate
  answers business with the entry's own 503 without reading a body, opening a
  Blob or queueing anything — `/live` stays available and `/health` never
  claims readiness;
* the TCP adapter reports uvicorn's REAL startup result through `ready`
  (settled on success and on failure alike — never inferred from a sleep) and
  `stop(deadline)` is bounded and idempotent.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import socket
import time

import httpx
import pytest

from model_scheduler.listener_lifecycle import (
    ListenerError,
    ServingGate,
    TcpServerAdapter,
    control_refusal,
    gated_app,
)


class Recorder:
    """An ASGI app plus receive/send recorders, so "never called" is provable."""

    def __init__(self) -> None:
        self.scopes: list[dict] = []
        self.received: list[dict] = []
        self.sent: list[dict] = []

    async def app(self, scope, receive, send):
        self.scopes.append(scope)
        message = await receive()
        self.received.append(message)
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-length", b"2")]})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive(self):
        message = {"type": "http.request", "body": b'{"probe": true}', "more_body": False}
        self.received.append(message)
        return message

    async def send(self, message):
        self.sent.append(message)


def http_scope(path: str, method: str = "GET") -> dict:
    return {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
            "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
            "root_path": "", "headers": [], "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 80)}


async def call(app, scope: dict, recorder: Recorder) -> None:
    await app(scope, recorder.receive, recorder.send)


def status_of(recorder: Recorder) -> int:
    return next(message["status"] for message in recorder.sent if message["type"] == "http.response.start")


def body_of(recorder: Recorder) -> dict:
    return json.loads(raw_body_of(recorder))


def raw_body_of(recorder: Recorder) -> bytes:
    return next(message["body"] for message in recorder.sent if message["type"] == "http.response.body")


def headers_of(recorder: Recorder) -> dict[bytes, bytes]:
    start = next(message for message in recorder.sent if message["type"] == "http.response.start")
    return {bytes(name).lower(): bytes(value) for name, value in start.get("headers", [])}


def test_the_gate_is_an_in_process_flag_that_starts_closed() -> None:
    gate = ServingGate()

    assert gate.ready is False
    gate.open()
    assert gate.ready is True
    gate.close()
    assert gate.ready is False


async def test_a_closed_gate_refuses_business_before_one_body_byte_or_handler_call() -> None:
    gate, recorder = ServingGate(), Recorder()
    app = gated_app(recorder.app, gate)

    await call(app, http_scope("/v1/chat/completions", "POST"), recorder)

    assert status_of(recorder) == 503
    assert body_of(recorder)["error"]["code"] == "service_unavailable"  # the surface's own 503 code
    assert headers_of(recorder)[b"x-request-id"]  # the refusal carries a request id like every answer
    assert recorder.scopes == []      # the handler was never dispatched
    assert recorder.received == []    # and the body was never read: no Blob, no queue, no load


async def test_live_stays_available_and_health_never_claims_readiness_while_closed() -> None:
    gate, recorder = ServingGate(), Recorder()
    app = gated_app(recorder.app, gate)

    await call(app, http_scope("/live"), recorder)
    assert status_of(recorder) == 200 and recorder.scopes  # liveness is diagnostic, never business

    other = Recorder()
    await call(app, http_scope("/health"), other)
    document = body_of(other)
    assert status_of(other) == 503
    assert document["ok"] is False and document["reason"] == "dependencies_unready"
    assert set(document["checks"]) == {"llama_swap", "storage", "resources", "preload", "control"}
    assert not any(document["checks"].values())
    assert other.scopes == []  # health is answered by the gate itself: it cannot report stale readiness


async def test_an_open_gate_dispatches_the_same_request_untouched() -> None:
    gate, recorder = ServingGate(), Recorder()
    app = gated_app(recorder.app, gate)
    gate.open()

    await call(app, http_scope("/v1/chat/completions", "POST"), recorder)

    assert status_of(recorder) == 200 and raw_body_of(recorder) == b"ok"
    assert len(recorder.scopes) == 1 and recorder.received  # the app read the body itself, once


async def test_the_control_entry_wears_its_own_c05_503_document() -> None:
    gate, recorder = ServingGate(), Recorder()
    app = gated_app(recorder.app, gate, refusal=control_refusal)

    await call(app, http_scope("/internal/sessions", "POST"), recorder)

    document = body_of(recorder)
    assert status_of(recorder) == 503
    assert document["error"]["code"] == "temporarily_unavailable"
    assert document["error"]["retryable"] is True
    assert recorder.scopes == [] and recorder.received == []


def test_a_gate_refusal_is_never_influenced_by_a_client_header() -> None:
    """The flag is in-process: no header, path or body can open it."""
    gate, recorder = ServingGate(), Recorder()
    app = gated_app(recorder.app, gate)
    scope = http_scope("/health")
    scope["headers"] = [(b"x-sms-ready", b"true"), (b"x-forwarded-for", b"127.0.0.1")]

    asyncio.run(call(app, scope, recorder))

    assert status_of(recorder) == 503 and recorder.scopes == []


def test_the_tcp_adapter_serves_one_prepared_socket_and_reports_real_readiness() -> None:
    async def main() -> None:
        prepared = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        prepared.bind(("127.0.0.1", 0))
        port = prepared.getsockname()[1]
        recorder = Recorder()
        adapter = TcpServerAdapter(recorder.app, log_level="warning")
        serving = asyncio.create_task(adapter.serve(prepared))
        try:
            await asyncio.wait_for(adapter.ready, 10)  # settled by uvicorn's own startup, not a sleep
            assert adapter.ready.result() is None
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                response = await client.get("/live")
            assert response.status_code == 200
            assert [scope["path"] for scope in recorder.scopes] == ["/live"]
            with pytest.raises(ListenerError):  # one adapter owns exactly one prepared socket
                await adapter.serve(prepared)
        finally:
            await adapter.stop(time.monotonic() + 5)
        await asyncio.wait_for(serving, 5)  # stop() really ended the server
        await adapter.stop(time.monotonic() + 5)  # and a second stop is a no-op

    asyncio.run(main())


def test_a_failed_startup_settles_ready_with_the_error_and_leaves_nothing_serving() -> None:
    async def main() -> None:
        broken = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        broken.bind(("127.0.0.1", 0))
        broken.close()  # handed over already closed: uvicorn's startup cannot succeed
        recorder = Recorder()
        adapter = TcpServerAdapter(recorder.app, log_level="warning")

        serving = asyncio.create_task(adapter.serve(broken))

        with pytest.raises(OSError):
            await asyncio.wait_for(adapter.ready, 10)  # the failure is reported, never a hung guess
        with pytest.raises(OSError):
            await asyncio.wait_for(serving, 5)
        assert recorder.scopes == []
        await adapter.stop(time.monotonic() + 1)  # cleanup after a failed startup is still safe

    asyncio.run(main())


def test_stop_before_serve_and_after_a_failure_is_a_no_op() -> None:
    async def main() -> None:
        adapter = TcpServerAdapter(Recorder().app, log_level="warning")
        await adapter.stop(time.monotonic() + 1)  # never served: nothing to stop
        assert adapter.ready.done() is False  # and nothing was invented for it

    asyncio.run(main())


def test_the_adapter_settles_ready_before_any_request_can_be_answered() -> None:
    """The ready future is the observable event: a request that succeeds must find it settled."""

    async def main() -> None:
        prepared = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        prepared.bind(("127.0.0.1", 0))
        port = prepared.getsockname()[1]
        adapter = TcpServerAdapter(Recorder().app, log_level="warning")
        serving = asyncio.create_task(adapter.serve(prepared))
        try:
            await asyncio.wait_for(adapter.ready, 10)
            assert adapter.ready.done()
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                assert (await client.get("/live")).status_code == 200
        finally:
            await adapter.stop(time.monotonic() + 5)
            with suppress(asyncio.CancelledError):
                await serving

    asyncio.run(main())
