"""The C05/C07 control route surface behind the C08 socket (M04/P18).

`plan/08-execution-plan.md` (C05/C07, P18) fixes this module's shape:

* the routes live behind the accepted Unix socket only; `/internal/peer` stays
  with the listener app (it proves C08 identity delivery), everything else is
  routed here, and the trusted owner always comes from the injected peer
  credential — never from a header or a body;
* every new control request must present `X-SMS-Protocol-Version: 1`; a missing
  or unsupported version answers 400 `unsupported_protocol` before any route;
* every refusal is a C05 error document built from the CLOSED error table, and a
  handler that has already begun a response can never emit a second one;
* the Blob routes (C07) stream: POST validates the media type and the required
  `X-Content-SHA256`, then feeds the body chunk by chunk into `BlobStore.upload`
  under the 1 GiB bound; GET re-checks the owner and answers 410 for an active
  tombstone; DELETE is 204 unless a read lease still holds the file
  (409 `blob_in_use`).

Sessions and executions land on this same surface in the later P18 slices.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
import time
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping
from uuid import uuid4

from . import control_protocol_v1 as cp
from .blob_metadata import DELETED
from .blob_store import MAX_BLOB_BYTES, BlobStore, BlobStoreError
from .contracts_v2 import ContractError
from .control_identity import IdentityError, owner_from_peer
from .control_server import send_json
from .execution_service import ExecutionError, blob_owner_for
from .idempotency import COMPLETED, IdempotencyError, IdempotencyStore
from .scheduler import ModelUnavailable
from .session_manager import BLOCKED, CLOSED, DRAINING, PREPARING, SessionConflict, SessionNotFound

_BLOB_ID_PATTERN = r"[a-z0-9](?:[a-z0-9._-]{0,61}[a-z0-9])?"
_BLOB_ROUTE = re.compile(rf"^/internal/blobs/({_BLOB_ID_PATTERN})$")
_SESSION_ROUTE = re.compile(rf"^/internal/sessions/({_BLOB_ID_PATTERN})(/heartbeat|/close)?$")
_EXECUTION_ROUTE = re.compile(rf"^/internal/executions/({_BLOB_ID_PATTERN})(/cancel)?$")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_MAX_ERROR_TEXT = 512

# Session refusals onto the closed C05 table.
_SESSION_CONFLICTS = {"session_capacity": "queue_full", "duplicate_session": "busy",
                      "invalid_hard_deadline": "contract_violation"}
_SESSION_UNAVAILABLE = {"storage_unavailable": "storage_unavailable", "session_blocked": "temporarily_unavailable",
                        "service_shutting_down": "temporarily_unavailable"}

# Blob-store refusals onto the closed C05 error table.
_BLOB_FAILURES = {
    "not_found": "not_found",
    "expired": "reference_expired",
    "too_large": "payload_too_large",
    "quota_exceeded": "quota_exceeded",
    "unsupported_media_type": "unsupported_media_type",
    "checksum_mismatch": "contract_violation",
    "forbidden": "not_found",  # another owner must not learn the object exists
    "unreadable": "storage_unavailable",
    "storage": "storage_unavailable",
    "cancelled": "busy",
}


class _Refused(Exception):
    """A handler's explicit refusal carrying a C05 code; the status comes from the table."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass
class _SessionBinding:
    """The API's own session bookkeeping: who owns it and how its token is rebuilt.

    C05 stores only a token fingerprint for validation, and GET recomputes the
    identical token from the bound fields (`issued_at` + `expires_at`), so the
    plain token never has to be persisted.
    """

    session_id: str
    owner: str
    model_id: str
    issued_at: float
    expires_at: float
    fingerprint: str


class ControlAPI:
    """The `/internal` route surface except `/internal/peer`, as one ASGI callable."""

    def __init__(
        self,
        *,
        boot_id: str,
        blobs: BlobStore | None = None,
        scheduler: Any = None,
        service: Any = None,
        tokens: Any = None,
        idempotency: IdempotencyStore | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.boot_id = boot_id
        self.blobs = blobs
        self.scheduler = scheduler
        self.service = service
        self.tokens = tokens
        self.idempotency = idempotency
        self.clock = clock
        self._sessions: dict[str, _SessionBinding] = {}
        self._session_tokens: dict[str, str] = {}
        self._routes: dict[tuple[str, str], Callable[..., Awaitable[None]]] = {
            ("POST", "/internal/blobs"): self._blob_upload,
            ("POST", "/internal/sessions"): self._session_create,
            ("POST", "/internal/executions"): self._execution_create,
        }

    async def __call__(
        self,
        scope: Mapping[str, Any],
        receive: Callable[[], Awaitable[Mapping[str, Any]]],
        send: Callable[[Mapping[str, Any]], Awaitable[None]],
    ) -> None:
        request_id = str(scope.get("sms.request_id") or "")
        started = {"value": False}

        async def guarded_send(message: Mapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                started["value"] = True
            await send(message)

        try:
            owner = owner_from_peer(scope.get("sms.peer"))
            path = str(scope.get("path") or "")
            if path.startswith("/internal/"):
                presented = _header(scope, cp.PROTOCOL_VERSION_HEADER)
                if presented != str(cp.PROTOCOL_VERSION):
                    raise _Refused("unsupported_protocol",
                                   "the control protocol version header is missing or unsupported")
            handler = self._route(str(scope.get("method") or ""), path)
            if handler is None:
                raise _Refused("not_found", "the route is not served here")
            await handler(owner, scope, receive, guarded_send, request_id)
        except _Refused as exc:
            await self._refuse(guarded_send, exc.code, str(exc), request_id, started)
        except ContractError as exc:
            await self._refuse(guarded_send, "contract_violation", str(exc), request_id, started)
        except BlobStoreError as exc:
            await self._refuse(guarded_send, _BLOB_FAILURES.get(exc.code, "storage_unavailable"),
                               str(exc), request_id, started)
        except IdentityError as exc:
            code = exc.code if exc.code in cp.ERROR_STATUS else "peer_forbidden"
            await self._refuse(guarded_send, code, str(exc), request_id, started)
        except ExecutionError as exc:
            await self._refuse(guarded_send, exc.code, str(exc), request_id, started)
        except SessionConflict as exc:
            await self._refuse(guarded_send, _SESSION_CONFLICTS.get(str(exc), "busy"), str(exc), request_id, started)
        except SessionNotFound as exc:
            await self._refuse(guarded_send, "not_found", str(exc), request_id, started)
        except ModelUnavailable as exc:
            await self._refuse(guarded_send, _SESSION_UNAVAILABLE.get(str(exc), "temporarily_unavailable"),
                               str(exc), request_id, started)
        except KeyError as exc:
            await self._refuse(guarded_send, "not_found",
                               f"unknown model: {exc.args[0] if exc.args else exc}", request_id, started)

    def _route(self, method: str, path: str) -> Callable[..., Awaitable[None]] | None:
        handler = self._routes.get((method, path))
        if handler is not None:
            return handler
        if _BLOB_ROUTE.fullmatch(path) is not None:
            return {"GET": self._blob_read, "DELETE": self._blob_delete}.get(method)
        session = _SESSION_ROUTE.fullmatch(path)
        if session is not None:
            suffix = session.group(2)
            if suffix is None:
                return self._session_read if method == "GET" else None
            if method != "POST":
                return None
            return self._session_heartbeat if suffix == "/heartbeat" else self._session_close
        execution = _EXECUTION_ROUTE.fullmatch(path)
        if execution is not None:
            if execution.group(2) is None:
                return self._execution_read if method == "GET" else None
            return self._execution_cancel if method == "POST" else None
        return None

    async def _refuse(self, send, code: str, message: str, request_id: str, started: Mapping[str, bool]) -> None:
        if started["value"]:
            return  # the response already began; a second start would corrupt the stream
        if code not in cp.ERROR_STATUS:
            code = "temporarily_unavailable"
        await send_json(send, cp.ERROR_STATUS[code], cp.error_document(code, message[:_MAX_ERROR_TEXT], request_id))

    # -- C07: blobs ------------------------------------------------------------------

    async def _blob_upload(self, owner, scope, receive, send, request_id) -> None:
        blobs = self._require_blobs()
        media_type = (_header(scope, "content-type") or "").strip().lower()
        if media_type not in blobs.media_types:
            raise _Refused("unsupported_media_type",
                           "only the registered blob media types are accepted")
        digest = _header(scope, "x-content-sha256")
        if digest is None or not _SHA256_RE.fullmatch(digest):
            raise ContractError("x-content-sha256 must be a lowercase 64-hex sha-256")
        declared = _declared_size(scope)
        if declared is not None:
            if declared < 1:
                raise ContractError("a blob is between 1 byte and 1 GiB")
            if declared > MAX_BLOB_BYTES:
                raise _Refused("payload_too_large", "a blob is at most 1 GiB")
        record = await blobs.upload(
            blob_id=uuid4().hex,
            owner=blob_owner_for(owner),
            media_type=media_type,
            chunks=_body_chunks(receive, limit=MAX_BLOB_BYTES),
            expected_sha256=digest,
            declared_size=declared,
        )
        reference = {"blob_id": record.blob_id, "owner": record.owner, "sha256": record.sha256,
                     "size_bytes": record.size_bytes, "media_type": record.media_type}
        cp.parse_blob_ref(reference)  # the response is the contract, not a hope
        await send_json(send, 201, reference)

    async def _blob_read(self, owner, scope, receive, send, request_id) -> None:
        blobs = self._require_blobs()
        blob_id = _blob_id_of(scope)
        blob_owner = blob_owner_for(owner)
        record = blobs.metadata.get(blob_id)
        if record is None or record.owner != blob_owner:
            raise _Refused("not_found", "no such blob belongs to this owner")
        if record.state == DELETED:
            raise _Refused("reference_expired", "the blob is deleted and its tombstone is still active")
        body = await blobs.read_all(blob_id, blob_owner, f"api-{request_id}")
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", record.media_type.encode("ascii")),
                                (b"content-length", str(len(body)).encode("ascii")),
                                (b"connection", b"close")]})
        await send({"type": "http.response.body", "body": body})

    async def _blob_delete(self, owner, scope, receive, send, request_id) -> None:
        blobs = self._require_blobs()
        outcome = await blobs.delete(_blob_id_of(scope), blob_owner_for(owner), now=self.clock())
        if outcome == "held":
            raise _Refused("blob_in_use", "a read lease still holds this blob")
        await send({"type": "http.response.start", "status": 204, "headers": [(b"connection", b"close")]})
        await send({"type": "http.response.body", "body": b""})

    def _require_blobs(self) -> BlobStore:
        if self.blobs is None:
            raise _Refused("temporarily_unavailable", "the blob store is not wired into this process")
        return self.blobs

    # -- C05: sessions ---------------------------------------------------------------

    async def _session_create(self, owner, scope, receive, send, request_id) -> None:
        scheduler, tokens, idempotency = self._require_sessions()
        parsed = cp.parse_session_create_request(await _json_body(receive, limit=cp.MAX_INLINE_INPUT_BYTES))
        record = None
        if idempotency is not None:
            payload = {"model_id": parsed.model_id, "correlation_id": parsed.correlation_id}
            record = idempotency.begin(route="session.create", owner=owner, key=parsed.idempotency_key, payload=payload)
            if record.state == COMPLETED:
                replayed = self._sessions.get(record.resource_id or "")
                if replayed is None:
                    raise _Refused("not_found", "the replayed session is gone with its boot")
                await send_json(send, 202, await self._session_document(replayed))  # the object's current state
                return
        try:
            session_id = uuid4().hex
            view = await scheduler.register_session(parsed.model_id, owner, session_id)
            binding = self._bind(owner, parsed.model_id, session_id)
            document = self._session_document_sync(binding, view)
        except BaseException:
            if record is not None:
                idempotency.fail(record)  # a refusal must not strand the key
            raise
        if record is not None:
            idempotency.complete(record, status=202, resource_id=session_id, body=document)
        await send_json(send, 202, document)

    async def _session_read(self, owner, scope, receive, send, request_id) -> None:
        self._require_sessions()
        await send_json(send, 200, await self._session_document(self._owned_session(scope, owner)))

    async def _session_heartbeat(self, owner, scope, receive, send, request_id) -> None:
        scheduler, _, _ = self._require_sessions()
        binding = self._owned_session(scope, owner)
        token = _session_token_of(await _json_body(receive, limit=cp.MAX_INLINE_INPUT_BYTES))
        self._verify_session_token(binding, owner, token)
        view = await scheduler.heartbeat_session(binding.session_id)
        await send_json(send, 200, self._session_document_sync(binding, view))

    async def _session_close(self, owner, scope, receive, send, request_id) -> None:
        scheduler, _, _ = self._require_sessions()
        binding = self._owned_session(scope, owner)
        token = _session_token_of(await _json_body(receive, limit=cp.MAX_INLINE_INPUT_BYTES))
        self._verify_session_token(binding, owner, token)
        view = await scheduler.close_session(binding.session_id, reason="client_close")
        document = self._session_document_sync(binding, view)
        await send_json(send, 200 if document["state"] == CLOSED else 202, document)

    def _require_sessions(self):
        if self.scheduler is None or self.tokens is None:
            raise _Refused("temporarily_unavailable", "the session services are not wired into this process")
        return self.scheduler, self.tokens, self.idempotency

    def _owned_session(self, scope: Mapping[str, Any], owner: str) -> _SessionBinding:
        match = _SESSION_ROUTE.fullmatch(str(scope.get("path") or ""))
        if match is None:
            raise _Refused("not_found", "the route is not served here")
        binding = self._sessions.get(match.group(1))
        if binding is None or binding.owner != owner:
            raise _Refused("not_found", "no such session belongs to this owner")
        return binding

    def _bind(self, owner: str, model_id: str, session_id: str) -> _SessionBinding:
        issued_at = round(float(self.clock()), 3)
        expires_at = round(issued_at + float(self.scheduler.sessions.hard_deadline_seconds), 3)
        token = self._issue_token(owner, model_id, session_id, issued_at, expires_at)
        binding = _SessionBinding(session_id=session_id, owner=owner, model_id=model_id, issued_at=issued_at,
                                  expires_at=expires_at, fingerprint=self.tokens.fingerprint(token))
        self._sessions[session_id] = binding
        self._session_tokens[binding.fingerprint] = session_id
        return binding

    def _issue_token(self, owner: str, model_id: str, session_id: str, issued_at: float, expires_at: float) -> str:
        return self.tokens.issue(owner=owner, model_id=model_id, session_id=session_id,
                                 ttl_seconds=expires_at - issued_at, now=issued_at)

    def _verify_session_token(self, binding: _SessionBinding, owner: str, token: str) -> None:
        try:
            self.tokens.verify(token, owner=owner, model_id=binding.model_id,
                               session_id=binding.session_id, now=self.clock())
        except IdentityError as exc:
            raise ExecutionError(exc.code, str(exc)) from exc

    async def _session_document(self, binding: _SessionBinding) -> dict:
        view = await self.scheduler.session_view(binding.session_id)
        return self._session_document_sync(binding, view)

    def _session_document_sync(self, binding: _SessionBinding, view: Mapping[str, Any]) -> dict:
        state = str(view["phase"])
        phase = None
        if state == PREPARING:
            phase = "loading" if self.scheduler.sessions.holding_id() == binding.session_id else "queued"
        elif state == DRAINING:
            phase = "draining_existing"
        error = None
        if state == BLOCKED:
            reason = str(view.get("blocked_reason") or "temporarily_unavailable")
            code = reason if reason in cp.ERROR_STATUS else "temporarily_unavailable"
            error = {"code": code, "message": reason, "retryable": code in cp.RETRYABLE_ERROR_CODES}
        token = None if state == CLOSED else self._issue_token(
            binding.owner, binding.model_id, binding.session_id, binding.issued_at, binding.expires_at)
        document = {
            "session_id": binding.session_id, "state": state, "phase": phase, "boot_id": self.boot_id,
            "model_id": binding.model_id,
            "expires_in_ms": max(0, int(view["expires_in_ms"])),
            "hard_remaining_ms": max(0, int(view["hard_remaining_ms"])),
            "owner_token": token, "error": error,
        }
        cp.parse_session_view(document)  # the response is the contract, not a hope
        return document

    # -- C05: executions -------------------------------------------------------------

    async def _execution_create(self, owner, scope, receive, send, request_id) -> None:
        service, tokens = self._require_service()
        document = await _json_body(receive, limit=cp.MAX_INLINE_INPUT_BYTES)
        parsed = cp.parse_execution_create_request(document)
        session_id = self._session_tokens.get(tokens.fingerprint(parsed.session_token))
        if session_id is None:
            raise ExecutionError("stale_token", "the session token does not belong to this boot")
        view = await service.submit(session_id=session_id, owner=owner, document=document)
        await send_json(send, 202, view)  # a replayed key returns the object's current state

    async def _execution_read(self, owner, scope, receive, send, request_id) -> None:
        service, _ = self._require_service()
        await send_json(send, 200, await service.view(_execution_id_of(scope), owner=owner))

    async def _execution_cancel(self, owner, scope, receive, send, request_id) -> None:
        service, _ = self._require_service()
        token = _session_token_of(await _json_body(receive, limit=cp.MAX_INLINE_INPUT_BYTES))
        view = await service.cancel(_execution_id_of(scope), owner=owner, session_token=token)
        await send_json(send, 200 if str(view.get("state")) in cp.EXECUTION_TERMINAL_STATES else 202, view)

    def _require_service(self):
        if self.service is None or self.tokens is None:
            raise _Refused("temporarily_unavailable", "the execution service is not wired into this process")
        return self.service, self.tokens


def _header(scope: Mapping[str, Any], name: str) -> str | None:
    wanted = name.lower().encode("latin-1")
    for key, value in scope.get("headers") or []:
        if bytes(key).lower() == wanted:
            return bytes(value).decode("latin-1")
    return None


def _declared_size(scope: Mapping[str, Any]) -> int | None:
    raw = _header(scope, "content-length")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ContractError("content-length must be an integer") from exc


def _blob_id_of(scope: Mapping[str, Any]) -> str:
    match = _BLOB_ROUTE.fullmatch(str(scope.get("path") or ""))
    if match is None:
        raise _Refused("not_found", "the route is not served here")
    return match.group(1)


def _execution_id_of(scope: Mapping[str, Any]) -> str:
    match = _EXECUTION_ROUTE.fullmatch(str(scope.get("path") or ""))
    if match is None:
        raise _Refused("not_found", "the route is not served here")
    return match.group(1)


async def _body_chunks(receive: Callable[[], Awaitable[Mapping[str, Any]]], *, limit: int) -> AsyncIterator[bytes]:
    """Stream the ASGI request body as bounded chunks; never buffer the whole upload."""
    total = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            raise _Refused("temporarily_unavailable", "the client disconnected while its body was read")
        block = message.get("body", b"")
        if block:
            total += len(block)
            if total > limit:
                raise _Refused("payload_too_large", f"the request body must be at most {limit} bytes")
            yield bytes(block)
        if not message.get("more_body", False):
            return


async def _json_body(receive: Callable[[], Awaitable[Mapping[str, Any]]], *, limit: int) -> Any:
    """Read one bounded JSON body under the strict-JSON rules of C05."""
    payload = bytearray()
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            raise _Refused("temporarily_unavailable", "the client disconnected while its body was read")
        block = message.get("body", b"")
        if block:
            payload += block
            if len(payload) > limit:
                raise _Refused("payload_too_large", f"the request body must be at most {limit} bytes")
        if not message.get("more_body", False):
            break
    try:
        return json.loads(payload.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
    except ContractError:
        raise  # duplicate keys are a strict-JSON contract violation, not a syntax error
    except (ValueError, UnicodeDecodeError) as exc:
        raise _Refused("malformed_json", "the request body must be one JSON object") from exc


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ContractError(f"duplicate key: {key!r}")
        document[key] = value
    return document


def _session_token_of(document: Any) -> str:
    if not isinstance(document, dict) or set(document) != {"session_token"}:
        raise ContractError('the request body must be exactly {"session_token": "..."}')
    token = document["session_token"]
    if not isinstance(token, str) or not token or len(token) > 4096:
        raise ContractError("session_token must be a non-empty string of at most 4096 characters")
    return token
