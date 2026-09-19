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
from .collector import derive_attribution

EXECUTION_TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})
DEFAULT_TIMEOUT_SECONDS = 60.0
POLL_INTERVAL_SECONDS = 0.2


class DriverError(RuntimeError):
    """A refusal to pretend: the driver reports what the API said, never a guess."""


@dataclass(frozen=True)
class ApiResponse:
    status: int
    document: Mapping[str, Any]
    raw: bytes = b""  # a blob read is bytes; the parsed document is its JSON twin

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def observed_of(request: Mapping[str, Any], output: Mapping[str, Any] | None) -> dict:
    """What this round really reached, read from the request and the response.

    A declared boundary is only proven when the round's own usage says so: the
    image count and edge are read back from what was sent, and the token counts
    come from the model's usage. Anything the round cannot show stays absent,
    which fails the boundary comparison instead of satisfying it.
    """
    observed: dict[str, Any] = {"parallel": request.get("n_parallel") if isinstance(request, Mapping) else None}
    images, edge = _image_boundary(request)
    if images:
        observed["images"] = images
    if edge is not None:
        observed["image_edge_pixels"] = edge
    usage = output.get("usage") if isinstance(output, Mapping) else None
    if not isinstance(usage, Mapping):
        return observed
    for key, field in (("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens")):
        value = usage.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            continue
        observed[key] = value
    return observed


def _image_boundary(request: Mapping[str, Any]) -> tuple[int, int | None]:
    """How many images were sent and how large the largest one really is."""
    images = 0
    edge: int | None = None
    if not isinstance(request, Mapping):
        return 0, None
    for message in request.get("messages") or []:
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, Mapping) or part.get("type") != "image_url":
                continue
            url = part.get("image_url", {})
            url = url.get("url") if isinstance(url, Mapping) else None
            if not isinstance(url, str):
                continue
            images += 1
            size = _png_edge(url)
            if size is not None:
                edge = max(edge or 0, size)
    return images, edge


def _png_edge(url: str) -> int | None:
    """The edge of an inline PNG, read from its header; a claim is not a measurement."""
    import base64
    import struct

    marker = "base64,"
    if not url.startswith("data:image/") or marker not in url:
        return None
    try:
        raw = base64.b64decode(url.split(marker, 1)[1], validate=False)
    except (ValueError, TypeError):
        return None
    if raw[:8] != b"\x89PNG\r\n\x1a\n" or len(raw) < 24:
        return None
    width, height = struct.unpack(">II", raw[16:24])
    return max(width, height)


def provider_of(instance: Mapping[str, Any] | None) -> str | None:
    """Who ran this action: the runtime identity the deployment reported.

    Nothing is inferred from the model being "probably loaded" — either the API
    handed back an instance identity, or the action has no provider and stays
    unattributable.
    """
    if not isinstance(instance, Mapping):
        return None
    runtime_id, image_digest = instance.get("runtime_id"), instance.get("image_digest")
    if not isinstance(runtime_id, str) or not runtime_id:
        return None
    if not isinstance(image_digest, str) or not image_digest:
        return None
    return f"{runtime_id}/{image_digest}"


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
        return ApiResponse(status, document, body_bytes)


GENERATION_PARAMETERS = ("temperature", "top_p", "seed", "ignore_eos")


def parameters_of(request: Mapping[str, Any]) -> dict:
    """The generation controls a fixture asks for, in the protocol's own vocabulary.

    A fixture that must consume its declared output budget asks for `ignore_eos`;
    the request body carries it, the protocol carries it as a parameter, and
    anything the protocol does not define is simply not forwarded.
    """
    if not isinstance(request, Mapping):
        return {}
    return {key: request[key] for key in GENERATION_PARAMETERS if key in request}


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
    sampler_factory: Callable[[], Any] | None = None  # see acceptance.device_activity
    deadline_seconds: float = 1800.0
    ready_timeout_seconds: float = 1800.0
    heartbeat_interval_seconds: float = 10.0  # the C04 policy's own heartbeat cadence
    _sessions: dict[str, Session] = field(default_factory=dict, init=False)
    _counter: int = field(default=0, init=False)
    _executions: dict[str, str] = field(default_factory=dict, init=False)  # execution_id -> model_id
    _stop_proven: dict[str, bool] = field(default_factory=dict, init=False)

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
            # The matrix needs independent cold starts, one after another: the open
            # session is closed (its model is unloaded) so the next start really
            # begins from nothing. Two *simultaneous* sessions of one model is what
            # must never happen.
            self.stop(model_id)
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
             "input": {"inline": dict(request)}, "parameters": parameters_of(request),
             "idempotency_key": key}),
            f"execution create for {model_id!r}")
        execution_id = document.get("execution_id")
        if not isinstance(execution_id, str) or not execution_id:
            raise DriverError(f"execution create for {model_id!r} returned no execution_id")
        self._executions[execution_id] = model_id
        return {"execution_id": execution_id, "view": document}

    def execute(self, model_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        """Create an execution and poll it to a terminal state; nothing is retried.

        The window is what makes the case attributable: device samples are taken
        *around* this execution only, and the answer itself is read from the blob
        the service published — a view that merely says `succeeded` is never an
        output.
        """
        sampler = None if self.sampler_factory is None else self.sampler_factory()
        try:
            view, execution_id = self._execute_view(model_id, request, sampler=sampler)
        finally:
            rows = () if sampler is None else tuple(sampler.stop())
        instance = view.get("instance")
        output = self._published_output(view.get("result"))
        return {"execution_id": execution_id, "state": view.get("state"), "dispatch_state": view.get("dispatch_state"),
                "compute_quiescent": view.get("compute_quiescent"), "result": view.get("result"),
                "output": output, "observed": observed_of(request, output),
                "provider": provider_of(instance), "instance": instance,
                "device_activity": derive_attribution(rows) if rows else None, "samples": rows,
                "fence": view.get("fence"), "error": view.get("error"), "view": view}

    def _execute_view(self, model_id: str, request: Mapping[str, Any], *, sampler: Any) -> tuple[Mapping[str, Any], str]:
        session = self._session_for(model_id)
        self._beat_if_due(model_id)
        key = self._next_id("execution")
        created = self._require(self.transport.request(
            "POST", "/internal/executions",
            {"session_token": session.token, "operation": operation_of(request),
             "input": {"inline": dict(request)}, "parameters": parameters_of(request),
             "idempotency_key": key}),
            f"execution create for {model_id!r}")
        execution_id = created.get("execution_id")
        if not isinstance(execution_id, str) or not execution_id:
            raise DriverError(f"execution create for {model_id!r} returned no execution_id")
        self._executions[execution_id] = model_id
        if sampler is not None:
            sampler.start(instance=created.get("instance"))
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
        return view, execution_id

    def _published_output(self, result: Any) -> Mapping[str, Any] | None:
        """Read what the execution actually produced: the blob it references.

        A result that points nowhere leaves no output, and a blob that will not
        parse is an error rather than a renamed empty dict: without real content
        there is nothing for the capability checks to examine.
        """
        if not isinstance(result, Mapping):
            return None
        blob_id = result.get("blob_id")
        if not isinstance(blob_id, str) or not blob_id:
            return None
        response = self.transport.request("GET", f"/internal/blobs/{blob_id}")
        if not response.ok:
            detail = response.document.get("error", response.document)
            raise DriverError(f"the output blob {blob_id} cannot be read: {response.status} {detail}")
        if not response.raw:
            return None
        try:
            document = json.loads(response.raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DriverError(f"the output blob {blob_id} is not readable JSON: {exc}") from exc
        return document if isinstance(document, dict) else None

    def cancel(self, model_id: str, execution_id: str) -> Mapping[str, Any]:
        session = self._session_for(model_id)
        document = self._require(self.transport.request(
            "POST", f"/internal/executions/{execution_id}/cancel", {"session_token": session.token}),
            f"execution cancel {execution_id}")
        return {"execution_id": execution_id, "view": document}

    def stop(self, model_id: str) -> Mapping[str, Any]:
        """Close the session; the deployment stops what it started.

        Nothing open means nothing to stop: the matrix runs a stop case and then
        reloads, so a missing session is a no-op, not a failure.
        """
        session = self._sessions.get(model_id)
        if session is None:
            # Nothing is open because the previous stop was proven: keep that proof,
            # a reload must not be refused just because the stop case already ran.
            proven = self._stop_proven.get(model_id) is True
            return {"session_id": None, "state": "closed" if proven else None, "stop_proven": proven,
                    "note": "no session was open"}
        self._beat_if_due(model_id)
        closed = self._require(self.transport.request("POST", f"/internal/sessions/{session.session_id}/close",
                                                       {"session_token": session.token}),
                               f"session close for {model_id!r}")
        del self._sessions[model_id]
        # The API's own CLOSED view is the stop proof the case matrix requires.
        proven = str(closed.get("state")) == "closed"
        self._stop_proven[model_id] = proven
        return {"session_id": session.session_id, "state": closed.get("state"), "stop_proven": proven,
                "view": closed}

    def cleanup(self, model_id: str) -> Mapping[str, Any]:
        """Close any session left open; inline inputs mean no blobs to delete."""
        session = self._sessions.pop(model_id, None)
        if session is None:
            return {"session_id": None, "state": "closed", "note": "no session was open"}
        closed = self._require(self.transport.request("POST", f"/internal/sessions/{session.session_id}/close",
                                                       {"session_token": session.token}),
                               f"cleanup close for {model_id!r}")
        return {"session_id": session.session_id, "state": closed.get("state"), "view": closed}
