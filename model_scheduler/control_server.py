"""The C08 control listener: one process, two entries, identity from peer credentials.

`plan/08-execution-plan.md` (C08, P17) fixes this module's shape:

* the loopback TCP entry keeps uvicorn; the control entry is an `asyncio`
  Unix listener whose HTTP framing comes from the locked h11 dependency, so
  neither side reaches into uvicorn internals and ONE scheduler/Book/boot_id
  is shared by both;
* the trusted identity is read from the ACCEPTED SOCKET (Linux
  ``SO_PEERCRED``, macOS ``LOCAL_PEERCRED``) before a single request byte is
  parsed, and injected into the ASGI scope as ``sms.peer``. ``X-Owner``,
  ``X-UID``, bodies, reverse-proxy headers and the generic ASGI ``client``
  field are never identity;
* only bounded HTTP/1.1 is accepted (GET/POST, no CONNECT/TRACE, no
  Upgrade/Proxy-Connection, a header byte cap and a body cap); protocol
  abuse closes the connection WITHOUT an HTTP answer (the C05 table has no
  code for framing errors);
* `stop()` first stops accepting, then cancels and awaits every in-flight
  connection task and closes its transport, so no task or fd survives —
  the shared lifespan cleanup runs exactly once in the owner of this server,
  never in the ASGI app.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import grp
import h11
import json
import logging
import os
import socket
import struct
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping
from uuid import uuid4

from . import control_protocol_v1 as cp
from .contracts_v2 import CONTROL_SOCKET_MODE, CONTROL_SOCKET_PATH
from .control_identity import IdentityError, PeerIdentity, owner_from_peer
from .control_protocol_v1 import MAX_INLINE_INPUT_BYTES

logger = logging.getLogger(__name__)

CONTROL_APP_NAME = "control-v1"
_ALLOWED_METHODS = frozenset({"GET", "POST"})
_FORBIDDEN_HEADERS = frozenset({"upgrade", "proxy-connection"})


class ControlServerError(RuntimeError):
    pass


def peer_uid_of(sock: socket.socket) -> int:
    """The kernel's answer to "which uid opened this accepted Unix connection"."""
    if sys.platform.startswith("linux"):
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("III"))
        _pid, uid, _gid = struct.unpack("III", raw)
        return int(uid)
    if sys.platform == "darwin":
        # struct xucred: u8 version, u8 length, pad2, u32 uid, u32 gid, u32 revision, groups[]
        sol_local = getattr(socket, "SOL_LOCAL", 0)
        raw = sock.getsockopt(sol_local, socket.LOCAL_PEERCRED, 84)
        return int(struct.unpack_from("<I", raw, 4)[0])
    raise ControlServerError("peer credentials are only supported on Linux and macOS")


def build_tcp_skeleton_app(*, boot_id: str, scheduler: Any = None, shutdown_grace_seconds: float = 30.0):
    """The loopback TCP entry while P19 restores the full legacy surface.

    It owns the ONE process lifespan (uvicorn runs it; the control listener
    never does) and proves the P17 sharing contract: /live and /health report
    the same boot the control socket reports, and no /internal route is ever
    registered here, so `/internal/*` is a 404 on TCP by construction.
    """
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    state = {"started": 0, "cleanups": 0, "shutting_down": False}

    async def lifespan(app):  # noqa: ANN001 - Starlette app instance
        state["started"] += 1
        try:
            yield
        finally:
            state["cleanups"] += 1
            state["shutting_down"] = True
            if scheduler is not None:
                with suppress(Exception):
                    await scheduler.shutdown(time.monotonic() + shutdown_grace_seconds)

    async def live(_request):
        return JSONResponse({"ok": True})

    async def health(_request):
        return JSONResponse({"ok": not state["shutting_down"], "boot_id": boot_id, "lifespan_started": state["started"]})

    app = Starlette(routes=[Route("/live", live), Route("/health", health)], lifespan=lifespan)
    app.state.sms = state
    return app


def build_control_app(*, boot_id: str) -> Callable[..., Awaitable[None]]:
    """The control ASGI skeleton that PROVES C08 identity delivery.

    P18 lands the full /internal session/execution/blob routes on this app;
    P17 pins that `owner` can only come from the injected peer credential and
    that anything else answers with the C05 error body shape.
    """

    async def app(scope: Mapping[str, Any], receive: Callable[[], Awaitable[Mapping[str, Any]]],
                  send: Callable[[Mapping[str, Any]], Awaitable[None]]) -> None:
        request_id = str(scope.get("sms.request_id") or "")
        if scope["type"] != "http":
            await _send_json(send, 404, cp.error_document("not_found", "only over the control socket", request_id))
            return
        try:
            owner = owner_from_peer(scope.get("sms.peer"))
        except IdentityError as exc:
            await _send_json(send, cp.ERROR_STATUS.get(exc.code, 403),
                             cp.error_document(exc.code if exc.code in cp.ERROR_STATUS else "peer_forbidden",
                                               str(exc), request_id))
            return
        if scope["method"] == "GET" and scope["path"] == "/internal/peer":
            await _send_json(send, 200, {"owner": owner, "boot_id": boot_id, "via": CONTROL_APP_NAME})
            return
        await _send_json(send, 404, cp.error_document("not_found", "the route is not served here", request_id))

    return app


async def _send_json(send, status: int, payload: Mapping[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode("ascii")),
                            (b"connection", b"close")]})
    await send({"type": "http.response.body", "body": body})


class ControlServer:
    """A bounded HTTP/1.1 ASGI listener over one Unix socket with peer-cred identity."""

    def __init__(
        self,
        app: Callable[..., Awaitable[None]],
        *,
        socket_path: Path | str,
        allowed_uids: Iterable[int],
        peer_group: str | None = None,
        mode: int = CONTROL_SOCKET_MODE,
        max_header_bytes: int = 16 * 1024,
        max_body_bytes: int = MAX_INLINE_INPUT_BYTES,
        request_timeout_seconds: float = 10.0,
    ) -> None:
        self._app = app
        self.socket_path = Path(socket_path)
        self._allowed = frozenset(int(uid) for uid in allowed_uids)
        if not self._allowed:
            raise ControlServerError("the control listener needs at least one allowed uid")
        if any(uid < 0 for uid in self._allowed):
            raise ControlServerError("allowed uids must be non-negative")
        self._peer_group = peer_group
        self._mode = mode
        self._max_header_bytes = max_header_bytes
        self._max_body_bytes = max_body_bytes
        self._request_timeout = request_timeout_seconds
        self._server: asyncio.AbstractServer | None = None
        self._connections: set[asyncio.Task[None]] = set()
        self._refusing = False

    async def start(self) -> None:
        parent = self.socket_path.parent
        if not parent.is_dir():
            raise ControlServerError("the control socket directory must exist before serving")
        if os.stat(parent).st_mode & 0o007:
            raise ControlServerError("the control socket directory must not be world-accessible")
        if self.socket_path.exists():
            if _socket_is_live(self.socket_path):
                raise ControlServerError("another control listener already owns the socket")
            self.socket_path.unlink()
        self._server = await asyncio.start_unix_server(self._on_connection, path=str(self.socket_path))
        try:
            os.chmod(self.socket_path, self._mode)
            if self._peer_group is not None:
                gid = grp.getgrnam(self._peer_group).gr_gid
                os.chown(self.socket_path, -1, gid)
        except OSError as exc:
            await self.stop()
            raise ControlServerError(f"cannot secure the control socket: {exc}") from exc

    async def stop(self) -> None:
        """C08 shutdown order: stop accepting, drain/cancel connections, then remove the socket."""
        self._refusing = True
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for task in list(self._connections):
            task.cancel()
        if self._connections:
            await asyncio.gather(*self._connections, return_exceptions=True)
        with suppress(FileNotFoundError):
            self.socket_path.unlink()

    @property
    def in_flight(self) -> int:
        return sum(1 for task in self._connections if not task.done())

    async def _on_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._connections.add(task)
        try:
            await self._serve_connection(reader, writer)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # protocol or transport abuse: close, never answer, never leak state
            logger.debug("control connection closed: %s", exc)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            if task is not None:
                self._connections.discard(task)

    async def _serve_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        sock = writer.transport.get_extra_info("socket")
        if sock is None:
            return
        uid = peer_uid_of(sock)
        if uid not in self._allowed:
            return  # closed before parsing a single byte; the allow list IS the admission
        # 1) bounded header phase: never feed h11 more than max_header_bytes before the terminator
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = await asyncio.wait_for(reader.read(4096), self._request_timeout)
            if not chunk:
                return
            buf += chunk
            if len(buf) > self._max_header_bytes:
                return  # oversized headers: no C05 code exists for framing, close silently
        header_blob, rest = buf.split(b"\r\n\r\n", 1)
        conn = h11.Connection(our_role=h11.SERVER)
        conn.receive_data(header_blob + b"\r\n\r\n")
        event = conn.next_event()
        if not isinstance(event, h11.Request):
            return  # malformed framing is protocol abuse: close, no answer
        if event.http_version != b"1.1" or event.method.decode("latin-1") not in _ALLOWED_METHODS:
            return
        headers = [(name.lower(), value) for name, value in event.headers]  # h11 hands raw byte pairs
        lowered = {name.decode("latin-1"): value.decode("latin-1") for name, value in headers}
        if any(name in lowered for name in _FORBIDDEN_HEADERS):
            return
        if any(token in lowered.get("connection", "").lower() for token in ("upgrade", "proxy")):
            return
        te = lowered.get("transfer-encoding", "").lower()
        if te not in ("", "identity"):
            return  # chunked request bodies are not accepted here
        try:
            declared = int(lowered.get("content-length", "0"))
        except ValueError:
            return
        if declared > self._max_body_bytes:
            refused = json.dumps(cp.error_document("payload_too_large", "the request body is too large", "")).encode("utf-8")
            _write_response(writer, 413, [(b"content-type", b"application/json"),
                                          (b"content-length", str(len(refused)).encode("ascii")),
                                          (b"connection", b"close")], refused)
            return
        body = rest[:declared]
        while len(body) < declared:
            chunk = await asyncio.wait_for(reader.read(min(65536, declared - len(body))), self._request_timeout)
            if not chunk:
                return
            body += chunk
        target, _, query = event.target.decode("utf-8").partition("?")
        scope: dict[str, Any] = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1", "method": event.method.decode("latin-1"),
            "scheme": "unix", "path": target, "raw_path": target.encode("utf-8"),
            "query_string": query.encode("utf-8"), "root_path": "",
            "headers": headers,
            "client": None, "server": (str(self.socket_path), 0),  # never identity (C08)
            "sms.peer": PeerIdentity(uid), "sms.request_id": uuid4().hex,
        }
        state = {"got_request": False, "status": 0, "headers": []}

        async def receive() -> Mapping[str, Any]:
            if state["got_request"]:
                return {"type": "http.disconnect"}
            state["got_request"] = True
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message: Mapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                state["status"] = int(message["status"])
                state["headers"] = [(bytes(k), bytes(v)) for k, v in message.get("headers", [])]
            elif message["type"] == "http.response.body":
                if message.get("more_body", False):
                    raise ControlServerError("streaming responses belong to the TCP gateway, not the control socket")
                payload = bytes(message.get("body", b""))
                base = [(k, v) for k, v in state["headers"] if k.lower() not in (b"content-length", b"connection")]
                _write_response(writer, state["status"],
                                base + [(b"content-length", str(len(payload)).encode("ascii")),
                                         (b"connection", b"close")],
                                payload)
            elif message["type"] == "http.response.pathsend":  # pragma: no cover - unsupported
                raise ControlServerError("pathsend is not supported on the control socket")

        await asyncio.wait_for(self._app(scope, receive, send), self._request_timeout * 6)


_RESPONSE_REASONS = {200: b"OK", 404: b"Not Found", 405: b"Method Not Allowed", 413: b"Payload Too Large"}


def _write_response(writer: asyncio.StreamWriter, status: int,
                    headers: list[tuple[bytes, bytes]], body: bytes) -> None:
    """Serialize one complete HTTP/1.1 answer; the control socket never streams responses.

    h11 parses requests (protocol errors are the framing guard); responses are a
    small explicit serialization so a header limit or an early close can never
    leave a half-written answer on the wire.
    """
    reason = _RESPONSE_REASONS.get(status, b"Response")
    out = b"HTTP/1.1 %d %s\r\n" % (status, reason)
    for name, value in headers:
        out += name + b": " + value + b"\r\n"
    out += b"\r\n" + body
    writer.write(out)


def _socket_is_live(path: Path) -> bool:
    try:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.5)
        probe.connect(str(path))
    except OSError:
        return False
    else:
        return True
    finally:
        try:
            probe.close()
        except Exception:
            pass
