"""P17 C08: one scheduler process, two listeners, identity from peer credentials only.

The plan fixes the shape: the TCP entry keeps uvicorn, the control entry is an
`asyncio` Unix listener whose HTTP framing comes from the locked h11 dependency,
and the trusted UID is read from the ACCEPTED SOCKET before any request byte is
parsed. Everything this file proves is therefore driven over a real `AF_UNIX`
connection: an allowed uid reaches the ASGI app with a `PeerIdentity`; a
non-allowed uid is closed before the first byte of HTTP is answered; forged
`X-Owner`/`X-UID` headers change nothing; protocol abuse (oversized headers,
half packets, CONNECT/TRACE) closes the connection without leaking tasks or
file descriptors; and the socket lands 0660 under the configured path.

The Linux two-real-UID gate case at the bottom is honest about its platform:
it skips (not passes) where the environment cannot prove two distinct uids.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import socket
import subprocess
import tempfile
from pathlib import Path

import httpx
import psutil
import pytest

from model_scheduler.control_identity import PeerIdentity
from model_scheduler.control_server import (
    CONTROL_APP_NAME,
    ControlServer,
    build_control_app,
    peer_uid_of,
)


@pytest.fixture
def sdir():
    """A short-lived directory under /tmp: macOS caps AF_UNIX paths at 104 bytes."""
    created = Path(tempfile.mkdtemp(prefix="sms17-"))
    yield created
    shutil.rmtree(created, ignore_errors=True)


async def _request(uds_path: str, *, method="GET", path="/internal/peer", headers=None) -> httpx.Response:
    transport = httpx.AsyncHTTPTransport(uds=uds_path)
    async with httpx.AsyncClient(transport=transport, base_url="http://control.invalid") as client:
        return await client.request(method, path, headers=headers or {})


def _stat_mode(path) -> int:
    return os.stat(path).st_mode & 0o777


@pytest.mark.asyncio
async def test_an_allowed_peer_reaches_the_app_with_a_trusted_identity(sdir) -> None:
    app = build_control_app(boot_id="boot-17")
    server = ControlServer(app, socket_path=sdir / "control.sock", allowed_uids=(os.getuid(),))
    await server.start()
    try:
        response = await _request(str(server.socket_path))
        assert response.status_code == 200
        body = response.json()
        assert body["owner"] == f"uid:{os.getuid()}"
        assert body["boot_id"] == "boot-17"
        assert body["via"] == CONTROL_APP_NAME  # proof of C08 delivery, routes land in P18
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_the_peer_identity_is_read_from_the_socket_not_a_header(sdir) -> None:
    app = build_control_app(boot_id="boot-17")
    server = ControlServer(app, socket_path=sdir / "control.sock", allowed_uids=(os.getuid(),))
    await server.start()
    try:
        forged = await _request(str(server.socket_path), headers={"X-Owner": "uid:0", "X-Uid": "0"})
        assert forged.json()["owner"] == f"uid:{os.getuid()}"  # headers are never identity
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_a_uid_outside_the_allow_list_is_closed_before_any_response(sdir) -> None:
    server = ControlServer(build_control_app(boot_id="boot-x"), socket_path=sdir / "control.sock",
                           allowed_uids=(os.getuid() + 1 if os.getuid() < 65534 else os.getuid() - 1,))
    await server.start()
    try:
        reader, writer = await asyncio.open_unix_connection(str(server.socket_path))
        writer.write(b"GET /internal/peer HTTP/1.1\r\nHost: control\r\n\r\n")
        await writer.drain()
        answer = await asyncio.wait_for(reader.read(4096), 5)
        assert answer == b""  # refused without parsing, without an HTTP answer, without body leakage
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_socket_is_group_readable_zero_six_six_zero_under_a_private_parent(sdir) -> None:
    root = sdir / "run" / "self-model-switch"
    root.mkdir(parents=True)
    os.chmod(root, 0o750)
    server = ControlServer(build_control_app(boot_id="b"), socket_path=root / "control.sock",
                           allowed_uids=(os.getuid(),))
    await server.start()
    try:
        assert _stat_mode(server.socket_path) == 0o660  # C08 / plan/03-api.md 0660
    finally:
        await server.stop()
    assert not server.socket_path.exists()  # stop() removes the listener socket, not someone else's


@pytest.mark.asyncio
async def test_oversized_headers_and_half_packets_close_without_leaking_tasks_or_fds(sdir) -> None:
    server = ControlServer(build_control_app(boot_id="b"), socket_path=sdir / "control.sock",
                           allowed_uids=(os.getuid(),), max_header_bytes=2048,
                           request_timeout_seconds=0.2)
    await server.start()
    before_tasks = len({id(t) for t in asyncio.all_tasks()})
    before_fds = psutil.Process().num_fds()
    try:
        # oversized headers: a request line + header block beyond the configured bound
        reader, writer = await asyncio.open_unix_connection(str(server.socket_path))
        writer.write(b"GET /internal/peer HTTP/1.1\r\nHost: c\r\n" + b"X-Junk: " + b"a" * 4096 + b"\r\n\r\n")
        await writer.drain()
        assert (await asyncio.wait_for(reader.read(4096), 5)) == b""
        writer.close()
        await writer.wait_closed()

        # half packet: bytes begin, a request never completes, the timeout reaps the connection
        reader, writer = await asyncio.open_unix_connection(str(server.socket_path))
        writer.write(b"GET /internal/peer HTTP/1.1\r\nHost: c\r\n")
        await writer.drain()
        assert (await asyncio.wait_for(reader.read(4096), 5)) == b""
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()
    assert len({id(t) for t in asyncio.all_tasks()}) <= before_tasks  # no connection task survived
    assert psutil.Process().num_fds() <= before_fds                   # no fd survived


@pytest.mark.asyncio
async def test_only_http_11_get_and_post_and_no_upgrade_or_proxy_methods(sdir) -> None:
    server = ControlServer(build_control_app(boot_id="b"), socket_path=sdir / "control.sock",
                           allowed_uids=(os.getuid(),))
    await server.start()

    async def raw_rejects(payload: bytes) -> None:
        reader, writer = await asyncio.open_unix_connection(str(server.socket_path))
        writer.write(payload)
        await writer.drain()
        assert (await asyncio.wait_for(reader.read(4096), 5)) == b""
        writer.close()
        await writer.wait_closed()

    try:
        await raw_rejects(b"CONNECT control.invalid:443 HTTP/1.1\r\nHost: c\r\n\r\n")  # proxy methods: refused
        await raw_rejects(b"TRACE /internal/peer HTTP/1.1\r\nHost: c\r\n\r\n")
        await raw_rejects(b"GET /internal/peer HTTP/1.0\r\n\r\n")  # only 1.1 framing
        await raw_rejects(b"GET /internal/peer HTTP/1.1\r\nHost: c\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n")
        # a normal GET still works after all that abuse
        assert (await _request(str(server.socket_path))).status_code == 200
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_unknown_paths_get_the_c05_error_body_over_the_socket(sdir) -> None:
    server = ControlServer(build_control_app(boot_id="b"), socket_path=sdir / "control.sock",
                           allowed_uids=(os.getuid(),))
    await server.start()
    try:
        response = await _request(str(server.socket_path), path="/internal/nothing")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"
    finally:
        await server.stop()


def test_peer_uid_on_a_socketpair_is_this_process() -> None:
    first, second = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        assert peer_uid_of(first) == os.getuid()
    finally:
        first.close()
        second.close()


# -- the S-gate case: two REAL distinct UIDs on Linux; skipping never counts as passing -----

@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="C08 real two-UID gate: Linux SO_PEERCRED only")
def test_two_real_uids_on_linux_one_allowed_one_not(sdir) -> None:
    """Allowed uid connects; a different real uid is closed. Requires permission to drop uid."""
    if os.geteuid() != 0:
        pytest.skip("two real UIDs need the freedom to run a client as another uid (root or a provisioned test group)")
    server_uid, alien_uid = os.getuid(), 65534  # nobody
    program = (
        "import socket,sys;"
        f"s=socket.socket(socket.AF_UNIX);s.connect({str(sdir / 'control.sock')!r});"
        "s.sendall(b'GET /internal/peer HTTP/1.1\\r\\nHost: c\\r\\n\\r\\n');"
        "sys.stdout.buffer.write(s.recv(4096))"
    )

    async def drive() -> None:
        loop = asyncio.new_event_loop()

        async def main():
            srv = ControlServer(build_control_app(boot_id="gate"), socket_path=sdir / "control.sock",
                                allowed_uids=(server_uid,))
            await srv.start()
            try:
                allowed = subprocess.run([sys.executable, "-c", program], capture_output=True, timeout=10)
                denied = subprocess.run([sys.executable, "-c", program], capture_output=True, timeout=10,
                                        user=alien_uid, group=alien_uid)
                assert b"200 OK" in allowed.stdout and f"uid:{server_uid}".encode() in allowed.stdout
                assert denied.stdout == b""  # closed without an HTTP answer
            finally:
                await srv.stop()
        loop.run_until_complete(main())
        loop.close()

    drive()
