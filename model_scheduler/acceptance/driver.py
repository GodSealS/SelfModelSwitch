"""The real case driver: the P18 control API, and nothing else.

`acceptance/backend_cases.py` defines `CaseDriver` as *the official API surface
the executor is allowed to use*. This module is the implementation that talks to
a running deployment over the control socket (`/internal/sessions`,
`/internal/executions`, `/internal/blobs`). It never starts, stops or reconfigures
a deployment by itself: without a live control socket it refuses, so a `run` can
never quietly become a model launch.

Everything below the transport is pure mapping, and the transport is a protocol,
so the driver is testable without a socket.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import socket
import time
from typing import Any, Callable, Mapping, Protocol
from uuid import uuid4

from ..control_protocol_v1 import PROTOCOL_VERSION, PROTOCOL_VERSION_HEADER

EXECUTION_TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})
DEFAULT_TIMEOUT_SECONDS = 60.0
POLL_INTERVAL_SECONDS = 0.2


class DriverError(RuntimeError):
    """A refusal to pretend: the driver reports what the API said, never a guess."""


@dataclass(frozen=True)
class ApiResponse:
    status: int
    document: Mapping[str, Any]

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class ControlTransport(Protocol):
    """The only outside dependency: one request, one response."""

    def request(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> ApiResponse: ...


class UnixControlTransport:
    """HTTP/1.1 over the control socket; peer identity comes from the socket itself."""

    def __init__(self, socket_path: str | Path, *, timeout: float = DEFAULT_TIMEOUT_SECONDS,
                 protocol_version: int = PROTOCOL_VERSION) -> None:
        self.socket_path = Path(socket_path)
        self.timeout = timeout
        self.protocol_version = protocol_version

    def request(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> ApiResponse:
        payload = b"" if body is None else json.dumps(body).encode("utf-8")
        headers = [f"{method} {path} HTTP/1.1", "Host: localhost",
                   f"{PROTOCOL_VERSION_HEADER}: {self.protocol_version}", "Connection: close"]
        if payload:
            headers += ["Content-Type: application/json", f"Content-Length: {len(payload)}"]
        request = ("\r\n".join(headers) + "\r\n\r\n").encode("ascii") + payload
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(self.timeout)
                connection.connect(str(self.socket_path))
                connection.sendall(request)
                raw = bytearray()
                while True:
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    raw += chunk
        except OSError as exc:
            raise DriverError(f"cannot reach the control socket {self.socket_path}: {exc}") from exc
        head, _, body_bytes = bytes(raw).partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        try:
            status = int(lines[0].split(" ")[1])
        except (IndexError, ValueError) as exc:
            raise DriverError(f"the control socket answered nothing parseable: {head[:120]!r}") from exc
        document: Mapping[str, Any] = {}
        if body_bytes:
            try:
                parsed = json.loads(body_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise DriverError(f"status {status} with an unreadable body: {exc}") from exc
            if isinstance(parsed, dict):
                document = parsed
        return ApiResponse(status, document)


def operation_of(request: Mapping[str, Any]) -> str:
    """The operation the API accepts, read from the request the fixture defines.

    The executor hands the driver the fixture payload only, so the operation is
    derived here — and an undecidable payload is refused, never guessed.
    """
    if not isinstance(request, Mapping):
        raise DriverError("the request must be an object")
    messages = request.get("messages")
    if isinstance(messages, list):
        for message in messages:
            content = message.get("content") if isinstance(message, Mapping) else None
            if isinstance(content, list) and any(isinstance(part, Mapping) and part.get("type") == "image_url"
                                                 for part in content):
                return "vision"
        return "chat"
    if "input" in request and "query" not in request:
        return "embeddings"
    if "query" in request and "documents" in request:
        return "rerank"
    raise DriverError("cannot derive the operation: the API accepts chat, embeddings, rerank or vision")


@dataclass
class Session:
    """What the executor needs to know about the live session it opened."""

    session_id: str
    token: str
    model_id: str
    view: Mapping[str, Any]
    last_heartbeat: float = 0.0


@dataclass
class ControlApiCaseDriver:
    """`CaseDriver` over the control API. Sessions are the unit of load/stop."""

    transport: ControlTransport
    owner: str = "acceptance"
    sleep: Callable[[float], None] = field(default=lambda seconds: __import__("time").sleep(seconds))
    deadline_seconds: float = 1800.0
    ready_timeout_seconds: float = 1800.0
    heartbeat_interval_seconds: float = 10.0  # the C04 policy's own heartbeat cadence
    _sessions: dict[str, Session] = field(default_factory=dict, init=False)
    _counter: int = field(default=0, init=False)
    _executions: dict[str, str] = field(default_factory=dict, init=False)  # execution_id -> model_id

    # -- helpers -----------------------------------------------------------

    def _next_id(self, prefix: str) -> str:
        """A unique idempotency key per logical call.

        Reusing a key is a *replay*: the API answers with the object's current
        state and never re-issues the owner token, which would strand the driver.
        """
        self._counter += 1
        return f"{prefix}-{uuid4().hex[:16]}-{self._counter}"

    def _require(self, response: ApiResponse, what: str) -> Mapping[str, Any]:
        if not response.ok:
            detail = response.document.get("error", response.document)
            raise DriverError(f"{what} failed with {response.status}: {detail}")
        return response.document

    def _session_for(self, model_id: str) -> Session:
        session = self._sessions.get(model_id)
        if session is None:
            raise DriverError(f"no open session for {model_id!r}: the executor must load it first")
        return session

    # -- the official API surface -----------------------------------------

    def boot_id(self) -> str:
        """The deployment's boot identity, from the peer endpoint (C08 path)."""
        document = self._require(self.transport.request("GET", "/internal/peer"), "peer identity read")
        value = document.get("boot_id")
        if not isinstance(value, str) or not value:
            raise DriverError("the control socket did not report a boot_id")
        return value

    def load(self, model_id: str, *, cold: bool) -> Mapping[str, Any]:
        """Open a session: the deployment loads the model, the driver only asks.

        The API owns the identifiers: `POST /internal/sessions` returns
        `session_id`, `boot_id` and `owner_token`; the driver never invents them.
        """
        if model_id in self._sessions:
            raise DriverError(f"{model_id!r} already has an open session: a load must not stack")
        key = self._next_id("session")
        document = self._require(self.transport.request(
            "POST", "/internal/sessions",
            {"model_id": model_id, "idempotency_key": key, "correlation_id": self.owner}),
            f"session create for {model_id!r}")
        session_id, token = document.get("session_id"), document.get("owner_token")
        if not isinstance(session_id, str) or not session_id:
            raise DriverError(f"session create for {model_id!r} returned no session_id")
        if not isinstance(token, str) or not token:
            raise DriverError(f"session create for {model_id!r} returned no owner_token")
        session = Session(session_id, token, model_id, document, last_heartbeat=time.monotonic())
        self._sessions[model_id] = session
        # A `load` means the model is loaded *and usable*: wait until the session is
        # really ACTIVE. Submitting earlier is refused by the API ("only an ACTIVE
        # session may submit"), and a load that returns early would be a false pass.
        view = self._await_active(session)
        return {"session_id": session_id, "cold": cold, "state": view.get("state"),
                "phase": view.get("phase"), "boot_id": view.get("boot_id"), "view": view}

    def _await_active(self, session: Session) -> Mapping[str, Any]:
        deadline_seconds = self.ready_timeout_seconds
        waited = 0.0
        view: Mapping[str, Any] = session.view
        while True:
            state, phase = str(view.get("state")), view.get("phase")
            if state == "active" and phase in (None, "active"):
                return view
            if state in ("closed", "blocked"):
                raise DriverError(f"session {session.session_id} ended as {state!r} before it became active")
            if waited >= deadline_seconds:
                raise DriverError(f"session {session.session_id} did not become active in {deadline_seconds}s "
                                  f"(state {state!r}, phase {phase!r})")
            self.sleep(POLL_INTERVAL_SECONDS)
            waited += POLL_INTERVAL_SECONDS
            view = self._require(self.transport.request("GET", f"/internal/sessions/{session.session_id}"),
                                 f"session read {session.session_id}")
            # A session idle-expires by policy (C04): keep it alive while it prepares,
            # otherwise a cold start outlives the TTL and the execution is cancelled.
            if time.monotonic() - session.last_heartbeat >= self.heartbeat_interval_seconds:
                view = self.heartbeat(session.model_id)["view"]

    def heartbeat(self, model_id: str) -> Mapping[str, Any]:
        """Keep the session alive: the C04 policy expires an idle session in 30 s."""
        session = self._session_for(model_id)
        document = self._require(self.transport.request(
            "POST", f"/internal/sessions/{session.session_id}/heartbeat", {"session_token": session.token}),
            f"session heartbeat for {model_id!r}")
        session.view = document
        session.last_heartbeat = time.monotonic()
        return {"session_id": session.session_id, "state": document.get("state"), "view": document}

    def _beat_if_due(self, model_id: str) -> None:
        session = self._session_for(model_id)
        if time.monotonic() - session.last_heartbeat >= self.heartbeat_interval_seconds:
            self.heartbeat(model_id)

    def start(self, model_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        """Create an execution without waiting for it (the cancel case needs one)."""
        session = self._session_for(model_id)
        self._beat_if_due(model_id)
        key = self._next_id("execution")
        document = self._require(self.transport.request(
            "POST", "/internal/executions",
            {"session_token": session.token, "operation": operation_of(request),
             "input": {"inline": dict(request)}, "parameters": {}, "idempotency_key": key}),
            f"execution create for {model_id!r}")
        execution_id = document.get("execution_id")
        if not isinstance(execution_id, str) or not execution_id:
            raise DriverError(f"execution create for {model_id!r} returned no execution_id")
        self._executions[execution_id] = model_id
        return {"execution_id": execution_id, "view": document}

    def execute(self, model_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        """Create an execution and poll it to a terminal state; nothing is retried."""
        session = self._session_for(model_id)
        self._beat_if_due(model_id)
        key = self._next_id("execution")
        created = self._require(self.transport.request(
            "POST", "/internal/executions",
            {"session_token": session.token, "operation": operation_of(request),
             "input": {"inline": dict(request)}, "parameters": {}, "idempotency_key": key}),
            f"execution create for {model_id!r}")
        execution_id = created.get("execution_id")
        if not isinstance(execution_id, str) or not execution_id:
            raise DriverError(f"execution create for {model_id!r} returned no execution_id")
        self._executions[execution_id] = model_id
        view = created
        waited = 0.0
        while str(view.get("state")) not in EXECUTION_TERMINAL_STATES:
            if waited >= self.deadline_seconds:
                raise DriverError(f"execution {execution_id} did not reach a terminal state in "
                                  f"{self.deadline_seconds}s (state {view.get('state')!r})")
            self.sleep(POLL_INTERVAL_SECONDS)
            waited += POLL_INTERVAL_SECONDS
            # The session must stay alive for the whole execution, not just until it is
            # submitted: an idle expiry cancels the execution mid-flight.
            self._beat_if_due(model_id)
            view = self._require(self.transport.request("GET", f"/internal/executions/{execution_id}"),
                                 f"execution read {execution_id}")
        return {"execution_id": execution_id, "state": view.get("state"), "dispatch_state": view.get("dispatch_state"),
                "compute_quiescent": view.get("compute_quiescent"), "result": view.get("result"),
                "instance": view.get("instance"), "fence": view.get("fence"), "error": view.get("error"),
                "view": view}

    def cancel(self, model_id: str, execution_id: str) -> Mapping[str, Any]:
        document = self._require(self.transport.request("POST", f"/internal/executions/{execution_id}/cancel", {}),
                                 f"execution cancel {execution_id}")
        return {"execution_id": execution_id, "view": document}

    def stop(self, model_id: str) -> Mapping[str, Any]:
        """Close the session; the deployment stops what it started."""
        session = self._session_for(model_id)
        closed = self._require(self.transport.request("POST", f"/internal/sessions/{session.session_id}/close",
                                                       {"session_token": session.token}),
                               f"session close for {model_id!r}")
        del self._sessions[model_id]
        return {"session_id": session.session_id, "state": closed.get("state"), "view": closed}

    def cleanup(self, model_id: str) -> Mapping[str, Any]:
        """Close any session left open; inline inputs mean no blobs to delete."""
        session = self._sessions.pop(model_id, None)
        if session is None:
            return {"session_id": None, "state": "closed", "note": "no session was open"}
        closed = self._require(self.transport.request("POST", f"/internal/sessions/{session.session_id}/close",
                                                       {"session_token": session.token}),
                               f"cleanup close for {model_id!r}")
        return {"session_id": session.session_id, "state": closed.get("state"), "view": closed}
