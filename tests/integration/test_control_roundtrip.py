"""P18 integration: one real Unix-socket client drives the C05/C07 route surface.

The listener, the peer credential and the streaming body all exist for real
here; P17 proved identity delivery, this file proves the routes that ride it.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path

import httpx
import pytest

from model_scheduler import control_protocol_v1 as cp
from model_scheduler.blob_store import BlobStore
from model_scheduler.control_api import ControlAPI
from model_scheduler.control_server import ControlServer, build_control_app

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
