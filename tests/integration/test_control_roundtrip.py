"""P18 integration: one real Unix-socket client drives the C05/C07 route surface.

The listener, the peer credential and the streaming body all exist for real
here; P17 proved identity delivery, this file proves the routes that ride it.
"""
from __future__ import annotations

import asyncio
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
from model_scheduler.control_server import ControlServer, build_control_app
from model_scheduler.idempotency import IdempotencyStore
from model_scheduler.model_registry import Book
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


class _Resources:
    def __init__(self, clock: _Clock) -> None:
        self.clock = clock

    async def snapshot(self) -> MemorySample:
        return MemorySample(10_000, 9_000, self.clock.now)


class _Control:
    """v1 lifecycle control: load/stop answer RUNNING/STOPPED observations."""

    async def load(self, operation, deadline):
        return Observation(Presence.RUNNING, f"instance-{operation.model_id}", True, 0)

    async def stop(self, operation, deadline):
        return Observation(Presence.STOPPED, None, False, 0)


def _chat_book() -> Book:
    spec = ModelSpec("chat", "http://127.0.0.1:18080", frozenset({Capability.CHAT}), 100, max_concurrency=1)
    book = Book({"chat": spec}, model_budget=1_000, free_floor=20, margin=0)
    book.bootstrap_stopped("chat")
    return book


async def _serve_api(sdir: Path, tmp_path: Path):
    """The real session stack behind a real control socket."""
    clock = _Clock()
    sessions = SessionManager(wait_seconds=100.0, hard_deadline_seconds=3600.0, heartbeat_seconds=10.0,
                              ttl_seconds=30.0, prepare_seconds=100.0, drain_seconds=30.0, retry_seconds=30.0,
                              cleanup_seconds=60.0, cancel_seconds=10.0, stop_grace_seconds=30.0, reconcile_seconds=5.0)
    scheduler = ModelScheduler(_chat_book(), _Resources(clock), _Control(), sessions=sessions, clock=clock,
                               poll_interval_seconds=0.01)
    api = ControlAPI(boot_id="boot-18", blobs=BlobStore(tmp_path / "blobs", clock=clock), scheduler=scheduler,
                     tokens=TokenAuthority(boot_key=b"t" * 32, boot_id="boot-18", clock=clock),
                     idempotency=IdempotencyStore(boot_key=b"i" * 32, clock=clock), clock=clock)
    server = ControlServer(build_control_app(boot_id="boot-18", api=api),
                           socket_path=sdir / "control.sock", allowed_uids=(os.getuid(),))
    return scheduler, server


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
    _, server = await _serve_api(sdir, tmp_path)
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
