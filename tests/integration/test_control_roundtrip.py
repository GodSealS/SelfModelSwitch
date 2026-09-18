"""P18 integration: one real Unix-socket client drives the C05/C07 route surface.

The listener, the peer credential and the streaming body all exist for real
here; P17 proved identity delivery, this file proves the routes that ride it.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

import httpx
import pytest

from model_scheduler import control_protocol_v1 as cp
from model_scheduler.blob_store import BlobStore
from model_scheduler.contracts import Capability, MemorySample, ModelSpec, Observation, Presence
from model_scheduler.control_api import ControlAPI
from model_scheduler.control_identity import TokenAuthority
from model_scheduler.control_protocol_v1 import InstanceIdentity
from model_scheduler.control_server import ControlServer, build_control_app
from model_scheduler.execution_service import ExecutionService, blob_owner_for
from model_scheduler.idempotency import IdempotencyStore
from model_scheduler.model_registry import Book
from model_scheduler.ports_v3 import CancelAck, ExecutionHandle, ExecutionRequest, TerminationEvidence
from model_scheduler.scheduler import ModelScheduler
from model_scheduler.session_manager import SessionManager

VERSION = cp.PROTOCOL_VERSION_HEADER


@pytest.fixture
def sdir():
    """A short-lived directory under /tmp: macOS caps AF_UNIX paths at 104 bytes."""
    created = Path(tempfile.mkdtemp(prefix="sms18-"))
    yield created
    shutil.rmtree(created, ignore_errors=True)


async def _request(uds_path, method: str, path: str, *, headers=None, content=None) -> httpx.Response:
    transport = httpx.AsyncHTTPTransport(uds=uds_path)
    async with httpx.AsyncClient(transport=transport, base_url="http://control.invalid") as client:
        return await client.request(method, path, headers=headers or {}, content=content)


def _stack(sdir: Path, tmp_path: Path):
    blobs = BlobStore(tmp_path / "blobs", clock=lambda: 1_000.0)
    api = ControlAPI(boot_id="boot-18", blobs=blobs, clock=lambda: 1_000.0)
    server = ControlServer(build_control_app(boot_id="boot-18", api=api),
                           socket_path=sdir / "control.sock", allowed_uids=(os.getuid(),))
    return blobs, server


def _versioned(**extra: str) -> dict[str, str]:
    return {VERSION: str(cp.PROTOCOL_VERSION), **extra}


def _json_headers() -> dict[str, str]:
    return _versioned(**{"Content-Type": "application/json"})


class _Clock:
    """One clock for the whole stack so tokens, sessions and idempotency agree."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _Resources:
    def __init__(self, clock: _Clock) -> None:
        self.clock = clock

    async def snapshot(self) -> MemorySample:
        return MemorySample(10_000, 9_000, self.clock.now)


class _Control:
    """v1 lifecycle control: load/stop answer RUNNING/STOPPED observations."""

    def __init__(self) -> None:
        self.stops: list[str] = []

    async def load(self, operation, deadline):
        return Observation(Presence.RUNNING, f"instance-{operation.model_id}", True, 0)

    async def stop(self, operation, deadline):
        self.stops.append(operation.model_id)
        return Observation(Presence.STOPPED, None, False, 0)


def _chat_book() -> Book:
    spec = ModelSpec("chat", "http://127.0.0.1:18080", frozenset({Capability.CHAT}), 100, max_concurrency=1)
    book = Book({"chat": spec}, model_budget=1_000, free_floor=20, margin=0)
    book.bootstrap_stopped("chat")
    return book


_INSTANCE = InstanceIdentity(
    container_id="c-exec", started_at="2026-09-18T05:00:00Z", deployment_id="orin-local", model_id="chat",
    runtime_id="llama-cpp", candidate_digest="b" * 64, image_digest="repo/llama@sha256:" + "c" * 64,
)


class _FakeExec:
    """A BackendPort fake: a gate holds one execution open, so the queue behind it stays queued."""

    def __init__(self, *, gate: asyncio.Event | None = None) -> None:
        self.gate = gate
        self.requests: list[ExecutionRequest] = []

    async def execute(self, request: ExecutionRequest, fence, deadline) -> ExecutionHandle:
        self.requests.append(request)
        if self.gate is not None:
            await self.gate.wait()
        return ExecutionHandle(execution_id=request.execution_id, instance=_INSTANCE)

    async def cancel(self, handle: ExecutionHandle, deadline: float) -> CancelAck:
        return CancelAck(execution_id=handle.execution_id, accepted=True)

    async def load(self, spec, fence, deadline):  # pragma: no cover - P16 territory
        raise AssertionError("the execution service must not load models")

    async def stop(self, identity, fence, deadline):  # pragma: no cover - P16 territory
        raise AssertionError("the execution service must not stop models")


@dataclass
class _ApiStack:
    clock: _Clock
    scheduler: ModelScheduler
    service: ExecutionService
    blobs: BlobStore
    control: _Control
    backend: _FakeExec
    server: ControlServer


async def _serve_api(sdir: Path, tmp_path: Path, *, backend: _FakeExec | None = None) -> _ApiStack:
    """The real session/execution stack behind a real control socket."""
    clock = _Clock()
    sessions = SessionManager(wait_seconds=100.0, hard_deadline_seconds=3600.0, heartbeat_seconds=10.0,
                              ttl_seconds=30.0, prepare_seconds=100.0, drain_seconds=30.0, retry_seconds=30.0,
                              cleanup_seconds=60.0, cancel_seconds=10.0, stop_grace_seconds=30.0, reconcile_seconds=5.0)
    control = _Control()
    scheduler = ModelScheduler(_chat_book(), _Resources(clock), control, sessions=sessions, clock=clock,
                               poll_interval_seconds=0.01)
    blobs = BlobStore(tmp_path / "blobs", clock=clock)
    tokens = TokenAuthority(boot_key=b"t" * 32, boot_id="boot-18", clock=clock)
    idempotency = IdempotencyStore(boot_key=b"i" * 32, clock=clock)
    exec_backend = backend if backend is not None else _FakeExec()
    service = ExecutionService(scheduler, blobs=blobs, backend_for=lambda model_id: exec_backend, boot_id="boot-18",
                               clock=clock, poll_seconds=0.01, tokens=tokens, idempotency=idempotency)
    api = ControlAPI(boot_id="boot-18", blobs=blobs, scheduler=scheduler, service=service, tokens=tokens,
                     idempotency=idempotency, clock=clock)
    server = ControlServer(build_control_app(boot_id="boot-18", api=api),
                           socket_path=sdir / "control.sock", allowed_uids=(os.getuid(),))
    return _ApiStack(clock=clock, scheduler=scheduler, service=service, blobs=blobs, control=control,
                     backend=exec_backend, server=server)


async def _eventually(condition, *, attempts: int = 300) -> bool:
    for _ in range(attempts):
        if await condition():
            return True
        await asyncio.sleep(0.01)
    return await condition()


@pytest.mark.asyncio
async def test_a_real_client_round_trips_a_five_mib_blob(sdir, tmp_path) -> None:
    blobs, server = _stack(sdir, tmp_path)
    await server.start()
    try:
        payload = b"z" * (5 * 1024 * 1024)  # one byte past five mebibytes: the old 4 MiB buffered cap is gone
        digest = hashlib.sha256(payload).hexdigest()
        created = await _request(str(server.socket_path), "POST", "/internal/blobs",
                                 headers=_versioned(**{"Content-Type": "application/json",
                                                       "X-Content-SHA256": digest}),
                                 content=payload)
        assert created.status_code == 201, created.text
        ref = cp.parse_blob_ref(created.json())
        assert ref.size_bytes == len(payload) and blobs.metadata.get(ref.blob_id) is not None

        fetched = await _request(str(server.socket_path), "GET", f"/internal/blobs/{ref.blob_id}",
                                 headers=_versioned())
        assert fetched.status_code == 200 and fetched.content == payload

        deleted = await _request(str(server.socket_path), "DELETE", f"/internal/blobs/{ref.blob_id}",
                                 headers=_versioned())
        assert deleted.status_code == 204
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_a_chunked_upload_without_content_length_publishes(sdir, tmp_path) -> None:
    blobs, server = _stack(sdir, tmp_path)
    payload = b'{"streamed": true}'
    await server.start()

    async def blocks():
        yield b'{"streamed":'
        yield b" true}"

    try:
        created = await _request(str(server.socket_path), "POST", "/internal/blobs",
                                 headers=_versioned(**{"Content-Type": "application/json",
                                                       "X-Content-SHA256": hashlib.sha256(payload).hexdigest()}),
                                 content=blocks())
        assert created.status_code == 201, created.text
        assert cp.parse_blob_ref(created.json()).size_bytes == len(payload)
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_the_version_gate_and_the_error_document_ride_the_socket(sdir, tmp_path) -> None:
    _, server = _stack(sdir, tmp_path)
    await server.start()
    try:
        unversioned = await _request(str(server.socket_path), "POST", "/internal/blobs",
                                     headers={"Content-Type": "application/json"}, content=b"{}")
        assert unversioned.status_code == 400
        assert unversioned.json()["error"]["code"] == "unsupported_protocol"

        unknown = await _request(str(server.socket_path), "GET", "/internal/nothing", headers=_versioned())
        assert unknown.status_code == 404
        detail = unknown.json()["error"]
        assert detail["code"] == "not_found" and detail["retryable"] is False
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_a_real_client_drives_a_session_from_create_to_close(sdir, tmp_path) -> None:
    server = (await _serve_api(sdir, tmp_path)).server
    await server.start()

    def body(document: dict) -> bytes:
        return json.dumps(document).encode("utf-8")

    try:
        created = await _request(str(server.socket_path), "POST", "/internal/sessions", headers=_json_headers(),
                                 content=body({"model_id": "chat", "idempotency_key": "s-1"}))
        assert created.status_code == 202, created.text
        view = cp.parse_session_view(created.json())
        assert view.owner_token is not None

        async def active() -> bool:
            read = await _request(str(server.socket_path), "GET", f"/internal/sessions/{view.session_id}",
                                  headers=_versioned())
            return read.status_code == 200 and read.json()["state"] == "active"

        assert await _eventually(active), "the session never reached ACTIVE"

        heartbeat = await _request(str(server.socket_path), "POST",
                                   f"/internal/sessions/{view.session_id}/heartbeat", headers=_json_headers(),
                                   content=body({"session_token": view.owner_token}))
        assert heartbeat.status_code == 200

        closed = None
        for _ in range(300):
            response = await _request(str(server.socket_path), "POST", f"/internal/sessions/{view.session_id}/close",
                                      headers=_json_headers(), content=body({"session_token": view.owner_token}))
            if response.status_code == 200:
                closed = response
                break
            assert response.status_code == 202, response.text
            await asyncio.sleep(0.01)
        assert closed is not None, "the session never finished closing"
        final = cp.parse_session_view(closed.json())
        assert final.state == "closed" and final.owner_token is None

        replay = await _request(str(server.socket_path), "POST", "/internal/sessions", headers=_json_headers(),
                                content=body({"model_id": "chat", "idempotency_key": "s-1"}))
        assert replay.status_code == 202 and replay.json()["session_id"] == view.session_id
    finally:
        await server.stop()


async def _activate_session(socket_path: Path, *, key: str = "s-exec") -> dict:
    created = await _request(str(socket_path), "POST", "/internal/sessions", headers=_json_headers(),
                             content=json.dumps({"model_id": "chat", "idempotency_key": key}).encode("utf-8"))
    assert created.status_code == 202, created.text
    view = cp.parse_session_view(created.json())

    async def active() -> bool:
        read = await _request(str(socket_path), "GET", f"/internal/sessions/{view.session_id}", headers=_versioned())
        return read.status_code == 200 and read.json()["state"] == "active"

    assert await _eventually(active), "the session never reached ACTIVE"
    return view


def _execution_body(token: str, *, key: str, content: str = "hi") -> bytes:
    return json.dumps({
        "session_token": token, "operation": "chat",
        "input": {"inline": {"messages": [{"role": "user", "content": content}]}},
        "parameters": {"max_tokens": 8}, "idempotency_key": key,
    }).encode("utf-8")


@pytest.mark.asyncio
async def test_a_queued_execution_cancels_with_not_started_evidence(sdir, tmp_path) -> None:
    gate = asyncio.Event()
    stack = await _serve_api(sdir, tmp_path, backend=_FakeExec(gate=gate))
    await stack.server.start()
    socket_path = stack.server.socket_path
    token_body = lambda token: json.dumps({"session_token": token}).encode("utf-8")  # noqa: E731
    try:
        view = await _activate_session(socket_path)

        first = await _request(str(socket_path), "POST", "/internal/executions", headers=_json_headers(),
                               content=_execution_body(view.owner_token, key="e-1", content="hold the slot"))
        assert first.status_code == 202, first.text
        running_id = cp.parse_execution_view(first.json()).execution_id

        async def dispatched() -> bool:
            read = await _request(str(socket_path), "GET", f"/internal/executions/{running_id}", headers=_versioned())
            return read.status_code == 200 and read.json()["state"] == "running"

        assert await _eventually(dispatched), "the first execution never dispatched"

        second = await _request(str(socket_path), "POST", "/internal/executions", headers=_json_headers(),
                                content=_execution_body(view.owner_token, key="e-2", content="wait in the queue"))
        assert second.status_code == 202
        queued_id = cp.parse_execution_view(second.json()).execution_id

        cancelled = await _request(str(socket_path), "POST", f"/internal/executions/{queued_id}/cancel",
                                   headers=_json_headers(), content=token_body(view.owner_token))

        assert cancelled.status_code == 200, cancelled.text
        final = cp.parse_execution_view(cancelled.json())
        assert final.state == "cancelled" and final.dispatch_state == "not_started"
        assert final.compute_quiescent is True and final.instance is None  # a queued cancel invents no container

        again = await _request(str(socket_path), "POST", f"/internal/executions/{queued_id}/cancel",
                               headers=_json_headers(), content=token_body(view.owner_token))
        assert again.status_code == 200  # a terminal execution answers its current state
    finally:
        gate.set()
        await stack.server.stop()


@pytest.mark.asyncio
async def test_the_same_key_never_dispatches_a_second_inference(sdir, tmp_path) -> None:
    stack = await _serve_api(sdir, tmp_path)
    await stack.server.start()
    socket_path = stack.server.socket_path
    try:
        view = await _activate_session(socket_path)
        body = _execution_body(view.owner_token, key="e-dup")

        first = await _request(str(socket_path), "POST", "/internal/executions", headers=_json_headers(), content=body)
        replay = await _request(str(socket_path), "POST", "/internal/executions", headers=_json_headers(), content=body)

        assert first.status_code == 202 and replay.status_code == 202
        assert replay.json()["execution_id"] == first.json()["execution_id"]
        assert len(stack.backend.requests) == 1  # one inference, never two
    finally:
        await stack.server.stop()


@pytest.mark.asyncio
async def test_a_real_client_uploads_executes_reads_the_result_and_closes(sdir, tmp_path) -> None:
    stack = await _serve_api(sdir, tmp_path)
    await stack.server.start()
    socket_path = stack.server.socket_path

    def body(document: dict) -> bytes:
        return json.dumps(document).encode("utf-8")

    try:
        view = await _activate_session(socket_path, key="s-e2e")

        payload = json.dumps({"messages": [{"role": "user", "content": "hello"}]}).encode("utf-8")
        created = await _request(str(socket_path), "POST", "/internal/blobs",
                                 headers=_versioned(**{"Content-Type": "application/json",
                                                       "X-Content-SHA256": hashlib.sha256(payload).hexdigest()}),
                                 content=payload)
        assert created.status_code == 201, created.text
        ref = cp.parse_blob_ref(created.json())

        submitted = await _request(str(socket_path), "POST", "/internal/executions", headers=_json_headers(),
                                   content=body({"session_token": view.owner_token, "operation": "chat",
                                                 "input": {"blob": created.json()}, "parameters": {"max_tokens": 8},
                                                 "idempotency_key": "e-e2e"}))
        assert submitted.status_code == 202, submitted.text
        execution_id = cp.parse_execution_view(submitted.json()).execution_id

        async def dispatched() -> bool:
            return len(stack.backend.requests) == 1

        assert await _eventually(dispatched), "the execution never reached the backend"

        output = b'{"choices": [{"message": {"role": "assistant", "content": "hi"}}]}'
        record = stack.service.record(execution_id)
        await stack.service.report_output(execution_id, output)
        settled = await stack.service.report_terminal(execution_id, TerminationEvidence(
            fence=record.fence, dispatch_state="dispatched", compute_quiescent=True,
            device_synchronized=True, reason="completed", instance=_INSTANCE))
        assert settled["state"] == "succeeded"

        read = await _request(str(socket_path), "GET", f"/internal/executions/{execution_id}", headers=_versioned())
        assert read.status_code == 200
        final = cp.parse_execution_view(read.json())
        assert final.state == "succeeded" and final.compute_quiescent is True and final.result is not None

        result = await _request(str(socket_path), "GET", f"/internal/blobs/{final.result.blob_id}", headers=_versioned())
        assert result.status_code == 200 and result.content == output
        assert result.headers["content-type"] == "application/json"

        released = await _request(str(socket_path), "DELETE", f"/internal/blobs/{ref.blob_id}", headers=_versioned())
        assert released.status_code == 204  # the read lease ended with the settled execution

        closed = None
        for _ in range(300):
            response = await _request(str(socket_path), "POST", f"/internal/sessions/{view.session_id}/close",
                                      headers=_json_headers(), content=body({"session_token": view.owner_token}))
            if response.status_code == 200:
                closed = response
                break
            assert response.status_code == 202, response.text
            await asyncio.sleep(0.01)
        assert closed is not None, "the session never finished closing"
        assert cp.parse_session_view(closed.json()).state == "closed"
        assert stack.control.stops == ["chat"]  # the drain proved STOPPED before the session closed
    finally:
        await stack.server.stop()


@pytest.mark.asyncio
async def test_an_expired_blob_reference_answers_410(sdir, tmp_path) -> None:
    stack = await _serve_api(sdir, tmp_path)
    await stack.server.start()
    socket_path = stack.server.socket_path
    try:
        payload = b'{"messages": []}'
        created = await _request(str(socket_path), "POST", "/internal/blobs",
                                 headers=_versioned(**{"Content-Type": "application/json",
                                                       "X-Content-SHA256": hashlib.sha256(payload).hexdigest()}),
                                 content=payload)
        ref = cp.parse_blob_ref(created.json())

        stack.clock.advance(86_400 + 1)  # past the 24h result/blob retention
        expired = await _request(str(socket_path), "GET", f"/internal/blobs/{ref.blob_id}", headers=_versioned())

        assert expired.status_code == 410
        assert expired.json()["error"]["code"] == "reference_expired"
    finally:
        await stack.server.stop()


@pytest.mark.asyncio
async def test_a_disconnect_mid_upload_publishes_nothing(sdir, tmp_path) -> None:
    stack = await _serve_api(sdir, tmp_path)
    await stack.server.start()
    socket_path = stack.server.socket_path
    owner = blob_owner_for(f"uid:{os.getuid()}")
    payload = b'{"messages": []}'
    try:
        _, writer = await asyncio.open_unix_connection(str(socket_path))
        writer.write(b"POST /internal/blobs HTTP/1.1\r\nHost: c\r\nX-SMS-Protocol-Version: 1\r\n"
                     b"Content-Type: application/json\r\n"
                     + f"X-Content-SHA256: {hashlib.sha256(payload).hexdigest()}\r\n".encode("ascii")
                     + b"Content-Length: 4096\r\n\r\n" + payload[:4])
        await writer.drain()
        writer.close()  # the client vanishes mid-body; nothing may publish and nothing may leak

        await asyncio.sleep(0.2)
        assert stack.blobs.metadata.usage(owner).total_bytes == 0
    finally:
        await stack.server.stop()


@pytest.mark.asyncio
async def test_concurrent_submits_with_one_key_are_reproducible(sdir, tmp_path) -> None:
    stack = await _serve_api(sdir, tmp_path)
    await stack.server.start()
    socket_path = stack.server.socket_path
    try:
        view = await _activate_session(socket_path, key="s-race")
        body = _execution_body(view.owner_token, key="e-race")

        responses = await asyncio.gather(*[
            _request(str(socket_path), "POST", "/internal/executions", headers=_json_headers(), content=body)
            for _ in range(3)])

        accepted = [r for r in responses if r.status_code == 202]
        refused = [r for r in responses if r.status_code != 202]
        assert accepted, "at least the first concurrent submission must be accepted"
        assert len({r.json()["execution_id"] for r in accepted}) == 1  # exactly one object, ever
        assert all(r.json()["error"]["code"] == "busy" for r in refused)  # the losers are a busy replay, never a second run
        assert accepted[0].json()["state"] in {"queued", "running"}

        async def dispatched() -> bool:
            return len(stack.backend.requests) == 1

        assert await _eventually(dispatched)
        assert len(stack.backend.requests) == 1  # one inference, whatever the interleaving was
    finally:
        await stack.server.stop()
