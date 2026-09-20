#!/usr/bin/env python3
"""The compat gateway: the only thing another machine is allowed to talk to.

The scheduler binds its compatibility port to loopback and its control plane is
a Unix socket with peer credentials (C08), so a deployment that must be reached
from another machine puts this process in front: it listens on one internal
address, authenticates every request with a bearer token, forwards only `/v1/*`
to the scheduler, and streams the response back chunk by chunk (SSE works).

It refuses to start without a token file, never forwards the caller's
credentials upstream, and never exposes a control path: `/api/*` and
`/internal/*` are answered 404 here and stay on the control socket.

    gateway.py --host 192.168.55.1 --port 8091 \
               --upstream http://127.0.0.1:8090 --token-file /etc/self-model-switch/gateway.token
"""
from __future__ import annotations

import argparse
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
from typing import Any
from urllib.parse import urlsplit

CHUNK_BYTES = 64 * 1024
FORWARDED_PREFIX = "/v1/"
HOP_BY_HOP = frozenset({"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
                        "te", "trailers", "transfer-encoding", "upgrade", "host"})


def load_token(path: Path) -> str:
    """The shared secret; an unreadable or empty file means the gateway must not start."""
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemExit(f"the gateway token file is not readable: {exc}") from exc
    if not text:
        raise SystemExit("the gateway token file is empty: an unauthenticated gateway is not a deployment")
    return text


class GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "sms-gateway/1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the stdlib signature
        sys.stderr.write("gateway: " + format % args + "\n")

    # -- helpers ---------------------------------------------------------

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization") or ""
        return header == f"Bearer {self.server.token}"  # type: ignore[attr-defined]

    def _refuse(self, status: int, message: str) -> None:
        body = (message + "\n").encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- the one route ---------------------------------------------------

    def _forward(self) -> None:
        target = urlsplit(self.path)
        segments = target.path.split("/")
        if any(segment in ("..", ".") for segment in segments) or "\\" in target.path:
            # A path that walks upwards is a routing trick, not a route.
            self._refuse(404, "not found")
            return
        if not target.path.startswith(FORWARDED_PREFIX):
            # The control plane and the admin surface never leave the machine.
            self._refuse(404, "not found")
            return
        if not self._authorized():
            self._refuse(401, "unauthorized")
            return
        upstream = self.server.upstream  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {key: value for key, value in self.headers.items()
                   if key.lower() not in HOP_BY_HOP and key.lower() != "authorization"}
        headers["X-Forwarded-For"] = self.client_address[0]
        connection = HTTPConnection(upstream.hostname, upstream.port or 80,
                                    timeout=self.server.upstream_timeout)  # type: ignore[attr-defined]
        try:
            connection.request(self.command, self.path, body=body, headers=headers)
            response = connection.getresponse()
            self.send_response(response.status)
            chunked = response.getheader("Transfer-Encoding") == "chunked"
            for key, value in response.getheaders():
                if key.lower() in HOP_BY_HOP:
                    continue
                self.send_header(key, value)
            if chunked:
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Connection", "close")
            self.end_headers()
            while True:
                chunk = response.read(CHUNK_BYTES)
                if not chunk:
                    break
                if chunked:
                    self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
                else:
                    self.wfile.write(chunk)
                self.wfile.flush()  # SSE arrives while it is produced
            if chunked:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        except OSError as exc:
            self._refuse(502, f"the scheduler is not reachable: {exc}")
        finally:
            connection.close()

    do_GET = _forward
    do_POST = _forward
    do_PUT = _forward
    do_DELETE = _forward
    do_PATCH = _forward


class Gateway(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def build_server(*, host: str, port: int, upstream: str, token: str, upstream_timeout: float = 900.0) -> Gateway:
    parsed = urlsplit(upstream)
    if parsed.scheme != "http" or not parsed.hostname:
        raise SystemExit("the upstream must be an http URL, e.g. http://127.0.0.1:8090")
    server = Gateway((host, port), GatewayHandler)
    server.upstream = parsed  # type: ignore[attr-defined]
    server.token = token  # type: ignore[attr-defined]
    server.upstream_timeout = upstream_timeout  # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="the authenticated compat gateway in front of the scheduler")
    parser.add_argument("--host", required=True, help="the internal address other machines reach")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--upstream", required=True, help="the scheduler's loopback base URL")
    parser.add_argument("--token-file", required=True, help="a root-owned file holding the shared token")
    parser.add_argument("--upstream-timeout", type=float, default=900.0)
    args = parser.parse_args(argv)
    server = build_server(host=args.host, port=args.port, upstream=args.upstream,
                          token=load_token(Path(args.token_file)), upstream_timeout=args.upstream_timeout)
    sys.stderr.write(f"gateway: listening on {args.host}:{args.port} -> {args.upstream} (streaming)\n")
    server.serve_forever()
    return 0


def serve_in_thread(**kwargs: Any) -> tuple[Gateway, threading.Thread]:
    """The test seam: the same server, on an ephemeral port, in a thread."""
    server = build_server(**kwargs)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


if __name__ == "__main__":
    raise SystemExit(main())
