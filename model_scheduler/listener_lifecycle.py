"""K7: the readiness primitives the v2 owner runs its two entries on.

`plan/08-execution-plan.md` (C08, K7) fixes the shape:

* `ServingGate` is ONE in-process flag. No client header, body or path can open
  it; the owner opens it only after both entries are bound, the single lifespan
  is running and the prepared sockets are accepting. Both business entries wear
  it as an ASGI wrapper that is checked **before** dispatch: while it is closed
  a business request is answered with that entry's own 503 and the body is never
  read — no Blob, no queue entry, no load;
* `/live` stays available as a diagnostic and `/health` must keep answering 503
  instead of claiming a readiness the deployment does not have;
* `TcpServerAdapter` drives ONE prepared TCP socket through uvicorn. `ready` is
  settled by uvicorn's own startup result — success or failure — so readiness is
  never inferred from a sleep, and `stop(deadline)` is bounded and idempotent.
  Nothing here reaches into uvicorn's internal server list: the socket is passed
  in through the supported `sockets=` parameter, exactly as `uvicorn.Server`
  itself drives `startup()`/`main_loop()`/`shutdown()`.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import socket
import time
from typing import Any, Awaitable, Callable, Mapping
from uuid import uuid4

import uvicorn

HEALTH_CHECKS = ("llama_swap", "storage", "resources", "preload", "control")
HEALTH_PATH = "/health"
LIVE_PATH = "/live"
ASGIApp = Callable[[Mapping[str, Any], Callable[[], Awaitable[Mapping[str, Any]]],
                    Callable[[Mapping[str, Any]], Awaitable[None]]], Awaitable[None]]


class ListenerError(RuntimeError):
    """A listener lifecycle misuse: double serve, or a socket handed to two servers."""


class ServingGate:
    """The one readiness flag shared by both entries (K7).

    It is a plain in-process object on purpose: readiness is decided by the
    owner of the process, never by anything a client sends.
    """

    __slots__ = ("_ready",)

    def __init__(self) -> None:
        self._ready = False

    @property
    def ready(self) -> bool:
        return self._ready

    def open(self) -> None:
        self._ready = True

    def close(self) -> None:
        self._ready = False


def gateway_refusal(path: str, request_id: str) -> tuple[int, dict]:
    """The gateway entry's own 503 pair while the gate is closed (K7).

    `/health` answers the surface's health document (all checks false) and any
    other path answers the surface's `service_unavailable` error body, so a
    client sees exactly the shapes it already knows.
    """
    if path == HEALTH_PATH:
        return 503, {"ok": False, "checks": {name: False for name in HEALTH_CHECKS},
                     "reason": "dependencies_unready"}
    return 503, {"error": {"message": "Service is not ready", "type": "upstream_error",
                           "code": "service_unavailable", "param": None},
                 "request_id": request_id}


def control_refusal(path: str, request_id: str) -> tuple[int, dict]:
    """The control entry's C05 error document while the gate is closed (K7)."""
    from .control_protocol_v1 import error_document

    return 503, error_document("temporarily_unavailable", "the deployment is not ready", request_id)


def gated_app(app: ASGIApp, gate: ServingGate, *, exemption_paths: tuple[str, ...] = (LIVE_PATH,),
              refusal: Callable[[str, str], tuple[int, dict]] = gateway_refusal) -> ASGIApp:
    """Wrap one entry so the gate is checked before any dispatch (K7).

    A closed gate answers without touching `receive`: the request body is never
    read, so no Blob is created, nothing is queued and nothing can be loaded.
    """

    async def wrapped(scope, receive, send):
        if scope["type"] == "http" and not gate.ready and scope.get("path") not in exemption_paths:
            request_id = uuid4().hex
            status, document = refusal(str(scope.get("path") or ""), request_id)
            await _send_json(send, status, document, request_id)
            return
        await app(scope, receive, send)

    return wrapped


async def _send_json(send, status: int, document: Mapping[str, Any], request_id: str) -> None:
    body = json.dumps(document).encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode("ascii")),
                            (b"x-request-id", request_id.encode("ascii")),
                            (b"connection", b"close")]})
    await send({"type": "http.response.body", "body": body})


class TcpServerAdapter:
    """A thin uvicorn owner over ONE prepared socket (K7).

    `ready` settles with the real startup result: `None` once uvicorn's
    `startup()` returned (the listening sockets exist), or the raised error when
    it did not. It is never a guess and never a sleep.
    """

    def __init__(self, app: ASGIApp, *, log_level: str = "info") -> None:
        self._app = app
        self._log_level = log_level
        self._server: uvicorn.Server | None = None
        self._serving: asyncio.Task[None] | None = None
        self._ready: asyncio.Future[None] | None = None

    @property
    def ready(self) -> asyncio.Future[None]:
        """Settled by the adapter: `None` on success, the error on failure."""
        if self._ready is None:
            self._ready = asyncio.get_running_loop().create_future()
        return self._ready

    async def serve(self, prepared_socket: socket.socket) -> None:
        """Serve one already-bound socket until `stop()` (or a signal) ends it."""
        if self._server is not None:
            raise ListenerError("this adapter already serves one prepared socket")
        # asgi3 is declared, never guessed: both entries are ASGI3 by construction
        # (FastAPI and the gate wrapper), and the guess mis-reads bound methods.
        config = uvicorn.Config(self._app, log_level=self._log_level, lifespan="off", interface="asgi3")
        server = uvicorn.Server(config)
        self._server = server
        self._serving = asyncio.current_task()
        if not config.loaded:
            config.load()
        # The app's lifespan is entered by the owner (K7), so uvicorn runs with
        # `lifespan="off"`; `Server` still needs its (off) handler installed.
        server.lifespan = config.lifespan_class(config)
        try:
            with server.capture_signals():
                await server.startup(sockets=[prepared_socket])
                self._settle(None)
                await server.main_loop()
                if server.started:
                    await server.shutdown(sockets=[prepared_socket])
        except BaseException as error:
            self._settle(error)
            raise

    async def stop(self, deadline: float) -> None:
        """Ask the server to exit and wait for it; idempotent and deadline-bounded."""
        server = self._server
        if server is not None:
            server.should_exit = True
        serving, self._serving = self._serving, None
        if serving is not None and not serving.done():
            remaining = max(0.0, deadline - time.monotonic())
            try:
                await asyncio.wait_for(asyncio.shield(serving), remaining)
            except asyncio.TimeoutError:
                serving.cancel()
                with suppress(BaseException):
                    await serving
            except asyncio.CancelledError:
                serving.cancel()
                with suppress(BaseException):
                    await serving
                raise
        # A startup failure nobody awaited must never be reported to the GC later.
        if self._ready is not None and self._ready.done() and not self._ready.cancelled():
            self._ready.exception()

    def _settle(self, error: BaseException | None) -> None:
        future = self.ready
        if future.done():
            return
        if error is None:
            future.set_result(None)
        elif isinstance(error, asyncio.CancelledError):
            future.cancel()
        else:
            future.set_exception(error)
