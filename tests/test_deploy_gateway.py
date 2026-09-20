"""P27: the gateway is the only door, and it asks for the token.

The upstream is a real HTTP server on an ephemeral port and the gateway is the
real process code, so what is asserted here is what another machine would get.
"""
from __future__ import annotations

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading

import pytest

from deploy.gateway import build_server, load_token, serve_in_thread

TOKEN = "s3cret-token"


class _UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen: list[tuple[str, str, str | None]] = []

    def log_message(self, *args: object) -> None:  # silence
        return None

    def do_POST(self) -> None:  # noqa: N802 - the stdlib signature
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        _UpstreamHandler.seen.append((self.command, self.path, self.headers.get("Authorization")))
        if self.path == "/v1/chat/completions":
            payload = b'{"choices":[{"message":{"content":"pong"}}]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


@pytest.fixture()
def upstream() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _UpstreamHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _UpstreamHandler.seen = []
    yield server
    server.shutdown()


def _call(port: int, path: str, *, token: str | None = TOKEN) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    connection.request("POST", path, body=b'{"model":"qwen-small"}', headers=headers)
    response = connection.getresponse()
    body = response.read()
    connection.close()
    return response.status, body


def test_the_gateway_forwards_v1_with_the_token_and_never_passes_it_on(upstream) -> None:
    gateway, _thread = serve_in_thread(host="127.0.0.1", port=0, upstream=f"http://127.0.0.1:{upstream.server_port}",
                                       token=TOKEN)
    try:
        status, body = _call(gateway.server_port, "/v1/chat/completions")
    finally:
        gateway.shutdown()

    assert status == 200 and b"pong" in body
    assert _UpstreamHandler.seen[-1][1] == "/v1/chat/completions"
    assert _UpstreamHandler.seen[-1][2] is None  # the caller's credential stops here


def test_without_the_token_nothing_reaches_the_scheduler(upstream) -> None:
    gateway, _thread = serve_in_thread(host="127.0.0.1", port=0, upstream=f"http://127.0.0.1:{upstream.server_port}",
                                       token=TOKEN)
    try:
        status, _body = _call(gateway.server_port, "/v1/chat/completions", token=None)
    finally:
        gateway.shutdown()

    assert status == 401
    assert _UpstreamHandler.seen == []  # the request never left the gateway


@pytest.mark.parametrize("path", ["/api/status", "/internal/sessions", "/v1/../../api/models",
                                  "/api/models/qwen-small/unload"])
def test_the_control_plane_is_not_reachable_through_the_gateway(upstream, path: str) -> None:
    gateway, _thread = serve_in_thread(host="127.0.0.1", port=0, upstream=f"http://127.0.0.1:{upstream.server_port}",
                                       token=TOKEN)
    try:
        status, _body = _call(gateway.server_port, path)
    finally:
        gateway.shutdown()

    assert status == 404
    assert _UpstreamHandler.seen == []


def test_an_unreachable_scheduler_is_502_not_a_hang() -> None:
    gateway, _thread = serve_in_thread(host="127.0.0.1", port=0, upstream="http://127.0.0.1:9", token=TOKEN,
                                       upstream_timeout=2.0)
    try:
        status, _body = _call(gateway.server_port, "/v1/chat/completions")
    finally:
        gateway.shutdown()

    assert status == 502


def test_it_refuses_to_start_without_a_token(tmp_path: Path) -> None:
    missing = tmp_path / "gateway.token"

    with pytest.raises(SystemExit, match="not readable"):
        load_token(missing)

    missing.write_text("   \n", encoding="utf-8")
    with pytest.raises(SystemExit, match="empty"):
        load_token(missing)

    missing.write_text(TOKEN + "\n", encoding="utf-8")
    assert load_token(missing) == TOKEN


def test_a_non_http_upstream_is_refused() -> None:
    with pytest.raises(SystemExit, match="http URL"):
        build_server(host="127.0.0.1", port=0, upstream="unix:///run/control.sock", token=TOKEN)
