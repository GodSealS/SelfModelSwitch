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

_BLOB_ID_PATTERN = r"[a-z0-9](?:[a-z0-9._-]{0,61}[a-z0-9])?"
_BLOB_ROUTE = re.compile(rf"^/internal/blobs/({_BLOB_ID_PATTERN})$")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_MAX_ERROR_TEXT = 512

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
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.boot_id = boot_id
        self.blobs = blobs
        self.scheduler = scheduler
        self.service = service
        self.tokens = tokens
        self.clock = clock
        self._routes: dict[tuple[str, str], Callable[..., Awaitable[None]]] = {
            ("POST", "/internal/blobs"): self._blob_upload,
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

    def _route(self, method: str, path: str) -> Callable[..., Awaitable[None]] | None:
        handler = self._routes.get((method, path))
        if handler is not None:
            return handler
        if _BLOB_ROUTE.fullmatch(path) is not None:
            return {"GET": self._blob_read, "DELETE": self._blob_delete}.get(method)
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
