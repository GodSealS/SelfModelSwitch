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
import grp
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
    build_tcp_skeleton_app,
    peer_uid_of,
)
from model_scheduler.idempotency import IdempotencyStore


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


async def _closed_without_answer(reader: asyncio.StreamReader) -> None:
    """The kernel refused this peer: EOF (macOS FIN) or ECONNRESET (Linux RST), never an answer."""
    try:
        answer = await asyncio.wait_for(reader.read(4096), 5)
    except ConnectionResetError:
        return
    assert answer == b""


async def _close_quietly(writer: asyncio.StreamWriter) -> None:
    """Close on our side; a Linux RST racing the close surfaces on wait_closed and means the same "no answer"."""
    from contextlib import suppress

    writer.close()
    with suppress(Exception):
        await writer.wait_closed()


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
        await _closed_without_answer(reader)  # refused before parsing, without an HTTP answer or body leakage
        await _close_quietly(writer)
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
        await _closed_without_answer(reader)
        await _close_quietly(writer)

        # half packet: bytes begin, a request never completes, the timeout reaps the connection
        reader, writer = await asyncio.open_unix_connection(str(server.socket_path))
        writer.write(b"GET /internal/peer HTTP/1.1\r\nHost: c\r\n")
        await writer.drain()
        await _closed_without_answer(reader)
        await _close_quietly(writer)
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
        await _closed_without_answer(reader)
        await _close_quietly(writer)

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


# -- P17 AC1/AC2: one boot shared by both listeners, one lifespan, explicit branches --------

import importlib.util  # noqa: E402

import run as run_module  # noqa: E402
import yaml  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "sms_test_config", str(Path(__file__).resolve().parents[1] / "test_config.py"))
_config_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_config_module)
V2_YAML = _config_module.V2


def _v2_config(tmp_path, *, mutate=None):
    document = yaml.safe_load(V2_YAML)
    document["blobs"]["root"] = str(tmp_path / "blobs")
    document["control"]["socket_path"] = str(tmp_path / "run" / "self-model-switch" / "control.sock")
    (tmp_path / "run" / "self-model-switch").mkdir(parents=True)
    os.chmod(tmp_path / "run" / "self-model-switch", 0o750)
    if mutate is not None:
        mutate(document)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    from model_scheduler.config import load_config
    return load_config(path)


def test_startup_plan_keeps_v1_and_locks_v2_before_any_build(sdir) -> None:
    v2 = _v2_config(sdir)
    plan = run_module.startup_plan(v2)
    assert plan.schema_version == 2 and plan.needs_swap_control is True
    assert plan.lock_path == sdir / "run" / "self-model-switch" / "scheduler.lock"
    v1_plan = run_module.startup_plan(type("C", (), {"schema_version": 1, "models": {}})())
    assert (v1_plan.schema_version, v1_plan.needs_swap_control, v1_plan.lock_path) == (
        1, True, run_module._V1_LOCK_PATH)


def test_v2_context_shares_one_boot_and_never_the_v1_backend(sdir, monkeypatch) -> None:
    def no_v1_join(*args, **kwargs):  # pragma: no cover - proves the branch never lands here
        raise AssertionError("v2 must not use the v1 four-model build_backend join")

    monkeypatch.setattr(run_module, "build_backend", no_v1_join)
    config = _v2_config(sdir)
    fake_ports = {"control": object(), "resources": None, "recovery": object(),
                  "observers": {mid: object() for mid in config.models},
                  "clients": {mid: httpx.AsyncClient(base_url="http://127.0.0.1:1") for mid in config.models}}
    context = run_module.build_v2_context(config, env={run_module._V2_DEPLOYMENT_ENV: "orin-lab"}, ports=fake_ports)
    assert context.boot_id and context.tokens.boot_id == context.boot_id
    assert sorted(context.book.specs) == ["embedding", "qwen-small"]
    # one scheduler behind one service behind one lifecycle: the context is the single join
    assert context.service._scheduler is context.scheduler
    assert context.service._tokens is context.tokens  # one authority signs and verifies every token (P18)
    assert isinstance(context.extras["idempotency"], IdempotencyStore)  # shared by the routes and the service
    assert context.lifecycle.instance("embedding") is None  # nothing loaded yet, and no guessed identity


def test_v2_tcp_app_serves_the_legacy_surface_and_never_the_control_routes(sdir) -> None:
    from fastapi.testclient import TestClient as _TestClient

    config = _v2_config(sdir)
    fake_ports = {"control": object(), "resources": None, "recovery": object(),
                  "observers": {mid: object() for mid in config.models},
                  "clients": {mid: httpx.AsyncClient(base_url="http://127.0.0.1:1") for mid in config.models}}
    context = run_module.build_v2_context(config, env={run_module._V2_DEPLOYMENT_ENV: "orin-lab"}, ports=fake_ports)
    app = run_module.build_v2_tcp_app(context)

    with _TestClient(app) as client:
        assert client.get("/live").status_code == 200
        listing = client.get("/v1/models")
        assert [entry["id"] for entry in listing.json()["data"]] == ["embedding", "qwen-small"]
        status = client.get("/api/status")
        assert status.json()["boot_id"] == context.boot_id
        assert status.json()["executions"] == {"total": 0, "active": 0, "pending_cleanup": 0}
        assert client.get("/internal/peer").status_code == 404  # control routes are Unix-socket only
        health = client.get("/health")
        assert health.status_code == 503  # the fake control port has no probe: health stays conservative
        assert health.json()["checks"]["control"] is False


def test_v2_refuses_to_guess_site_inputs(sdir) -> None:
    config = _v2_config(sdir)
    with pytest.raises(Exception) as missing_identity:
        run_module.build_v2_context(config, env={}, ports={})
    assert "SELFMODEL_SWITCH_DEPLOYMENT_ID" in str(missing_identity.value)
    env = {run_module._V2_DEPLOYMENT_ENV: "orin-lab"}
    with pytest.raises(Exception) as missing_swap:
        run_module.build_v2_context(config, env=env, ports={})
    assert "SELFMODEL_SWITCH_SWAP_CONTROL_URL" in str(missing_swap.value)


def test_swap_contract_is_imported_only_for_profiles_that_need_it(sdir, monkeypatch) -> None:
    monkeypatch.delitem(sys.modules, "model_scheduler.llama_swap_contract", raising=False)

    def to_hf(document) -> None:
        runtime = document["registration"]["runtimes"][0]
        runtime["profile_id"] = "hf-sharded-v1"
        runtime["startup_args"] = ["--host", "--port"]

    config = _v2_config(sdir, mutate=to_hf)
    plan = run_module.startup_plan(config)
    assert plan.needs_swap_control is False  # hf-sharded never joins llama-swap
    assert "model_scheduler.llama_swap_contract" not in sys.modules


def test_main_orders_lock_before_context_and_serves_the_v2_plan(sdir, monkeypatch) -> None:
    from contextlib import nullcontext

    config = _v2_config(sdir)
    seen: dict = {}

    def fake_acquire(path):
        seen["lock"] = path
        return nullcontext()

    def fake_build(cfg):
        seen["context"] = object()
        return run_module.RunContextV2(boot_id="b", scheduler=None, service=None, lifecycle=None, book=None,
                                       blobs=None, tokens=None, recovery=None, observers={}, config=None)

    monkeypatch.setattr(run_module, "load_config", lambda path: config)
    monkeypatch.setattr(run_module, "acquire", fake_acquire)
    monkeypatch.setattr(run_module, "build_v2_context", fake_build)
    monkeypatch.setattr(run_module, "serve_v2", lambda context: seen.__setitem__("served", context))
    assert run_module.main(["--config", str(sdir / "config.yaml")]) == 0
    assert seen["lock"] == sdir / "run" / "self-model-switch" / "scheduler.lock"
    assert list(seen) == ["lock", "context", "served"]  # lock first, one context, then serve


@pytest.mark.asyncio
async def test_both_listeners_share_one_boot_and_the_lifespan_runs_exactly_once(sdir) -> None:
    import uvicorn

    app = build_tcp_skeleton_app(boot_id="boot-shared", scheduler=None)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    control = ControlServer(build_control_app(boot_id="boot-shared"), socket_path=sdir / "control.sock",
                            allowed_uids=(os.getuid(),))
    serving = asyncio.create_task(server.serve())
    try:
        for _ in range(400):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started
        await control.start()
        port = server.servers[0].sockets[0].getsockname()[1]
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            health = await client.get("/health")
            assert health.status_code == 200 and health.json()["boot_id"] == "boot-shared"
            assert health.json()["lifespan_started"] == 1  # the TCP entry owns the one lifespan
            refused_on_tcp = await client.get("/internal/peer")
            assert refused_on_tcp.status_code == 404  # control routes are never registered on TCP
        via_socket = await _request(str(control.socket_path))
        assert via_socket.json()["boot_id"] == "boot-shared"
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 10)
        await control.stop()
    assert app.state.sms["started"] == 1 and app.state.sms["cleanups"] == 1  # start/cleanup each ONCE


# -- the S-gate case: two REAL distinct UIDs on Linux; skipping never counts as passing -----

@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="C08 real two-UID gate: Linux SO_PEERCRED only")
def test_two_real_uids_on_linux_one_allowed_one_not(sdir) -> None:
    """Allowed uid is served; a different REAL uid reaches the socket and is refused by the allow list."""
    if os.geteuid() != 0:
        pytest.skip("two real UIDs need the freedom to run a client as another uid (root or a provisioned test group)")
    server_uid, alien_uid = os.getuid(), 65534  # nobody
    alien = grp.getgrgid(alien_uid)
    # the alien uid must get PAST the filesystem to face the allow list: the directory stays
    # group-traversable but never world-accessible (ControlServer refuses 0o007 parents), and the
    # socket lands 0660 under the alien group; only SO_PEERCRED can then refuse the connection
    os.chown(sdir, server_uid, alien.gr_gid)
    os.chmod(sdir, 0o750)
    client_python = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable
    program = (
        "import socket,sys;"
        f"s=socket.socket(socket.AF_UNIX);s.connect({str(sdir / 'control.sock')!r});"
        "sys.stdout.buffer.write(b'connected\\n');sys.stdout.buffer.flush();"  # proves the socket admitted this uid
        "s.sendall(b'GET /internal/peer HTTP/1.1\\r\\nHost: c\\r\\n\\r\\n');"
        "sys.stdout.buffer.write(s.recv(4096))"
    )

    async def main() -> None:
        srv = ControlServer(build_control_app(boot_id="gate"), socket_path=sdir / "control.sock",
                            allowed_uids=(server_uid,), peer_group=alien.gr_name)
        await srv.start()
        try:
            # to_thread: a synchronous subprocess.run inside the serving loop starves its own server
            allowed = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", program],
                                              capture_output=True, timeout=10)
            denied = await asyncio.to_thread(subprocess.run, [client_python, "-c", program],
                                             capture_output=True, timeout=10, user=alien_uid, group=alien_uid)
        finally:
            await srv.stop()
        assert allowed.stdout.startswith(b"connected\n"), allowed.stdout
        assert b"200 OK" in allowed.stdout and f"uid:{server_uid}".encode() in allowed.stdout
        assert denied.stdout == b"connected\n", denied.stdout  # it connected; the allow list answered nothing

    asyncio.run(main())  # a never-awaited coroutine must not be able to call this case green
