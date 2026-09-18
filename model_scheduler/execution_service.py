"""Session-scoped execution queue and trusted result submission (M04/P14).

This is the generic execution slice: it joins session admission (C04), the
Blob read lease and output publication (C07), the idempotency registry and
tokens (P13) and the scheduler's single lease authority (C02/C04) behind one
service. It owns no HTTP route (P17/P18) and loads no model (P16): dispatch
goes through `scheduler.acquire` so one lock still decides interactive and
session admission, and the inference itself runs in an injected `BackendPort`.

Rules fixed by `plan/08-execution-plan.md` (P14) and honoured here:

* each session has one execution queue (capacity 128); a waiting execution's
  deadline is `min(enqueue + 1800s, session hard deadline)`; a cancellation
  that never dispatched terminates with `not_started` evidence and never
  carries a container identity;
* only a live ACTIVE session may submit; every dispatch re-validates the
  session, the capability, the protocol limits, the fence/generation and the
  resources, then takes exactly one scheduler lease and (for a Blob input)
  exactly one read lease;
* `succeeded` requires the published result AND an accepted terminal
  evidence — either may arrive first, neither alone is enough; a late output
  after a cancel or a timeout is never published, and a failed execution
  keeps its output reservation in `pending_cleanup` until it is abandoned.

Writeback is arbitrated by the single C03 rule (`writeback_decision`): a
stale boot/generation/operation, a foreign execution or an older attempt is
rejected with no side effect, and a duplicate terminal is a no-op. All state
mutations happen under one service lock in the event loop; every external
call (scheduler, blob store, backend) happens outside it, and the scheduler
invokes the drain hook outside its own condition, so no lock is ever held
across an await the other side waits for.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
from time import monotonic
from typing import Any, Callable, Mapping
from uuid import uuid4

from . import control_protocol_v1 as cp
from .blob_store import BlobStoreError, MAX_BLOB_BYTES, OutputReservation
from .contracts import Lease, Outcome
from .contracts_v2 import canonical_json_bytes
from .control_identity import IdentityError, TokenAuthority
from .control_protocol_v1 import ErrorDetail, ExecutionInput, Fence, InstanceIdentity
from .idempotency import COMPLETED, PENDING, IdempotencyError, IdempotencyRecord, IdempotencyStore
from .ports_v3 import ExecutionRequest, TerminationEvidence
from .session_manager import ACTIVE, SessionConflict, SessionNotFound

EXECUTION_QUEUE_CAPACITY = 128
EXECUTION_WAIT_SECONDS = 1800.0

QUEUED, RUNNING, SUCCEEDED, FAILED, CANCELLING, CANCELLED = (
    "queued", "running", "succeeded", "failed", "cancelling", "cancelled")
_TERMINAL = cp.EXECUTION_TERMINAL_STATES
_CANCEL_CODES = frozenset({"queue_timeout", "session_expired", "session_not_active"})

# Blob-store failures mapped onto the closed C05 error table.
_BLOB_CODES = {"not_found": "not_found", "expired": "reference_expired", "too_large": "payload_too_large",
               "quota_exceeded": "quota_exceeded", "storage": "storage_unavailable",
               "forbidden": "contract_violation", "unsupported_media_type": "unsupported_media_type"}


def blob_owner_for(owner: str) -> str:
    """`uid:1000` (peer identity) -> `uid-1000` (a directory-safe blob owner)."""
    return owner.replace(":", "-")


class ExecutionError(RuntimeError):
    """A service refusal with a stable C05 code; the API layer maps statuses (P18)."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


class NotDispatched(Exception):
    """An adapter-asserted refusal: the request provably never reached the backend (C03)."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


def _error(code: str, message: str) -> ErrorDetail:
    return ErrorDetail(code=code, message=message, retryable=code in cp.RETRYABLE_ERROR_CODES)


async def _one_shot(body: bytes):
    yield body


@dataclass
class ExecutionRecord:
    """One execution's whole service-side state; mutations happen under the service lock."""

    execution_id: str
    session_id: str
    model_id: str
    owner: str
    operation: str
    input: ExecutionInput
    parameters: dict
    deadline: float
    fence: Fence
    output_blob_id: str
    state: str = QUEUED
    error: ErrorDetail | None = None
    terminal_code: str | None = None
    terminal_message: str = ""
    result: cp.BlobRef | None = None
    evidence: TerminationEvidence | None = None
    output_pending: bytes | None = None
    publishing: bool = False
    dispatch_state: str = "not_started"
    dispatch_started: bool = False
    dispatch_claimed: bool = False
    instance: InstanceIdentity | None = None
    lease: Lease | None = None
    lease_released: bool = False
    reservation: OutputReservation | None = None
    input_ctx: Any = None
    input_closed: bool = False
    cancel_pending: bool = False
    settled: bool = False
    awaiting_quiescence: bool = False
    expected_instance: InstanceIdentity | None = None
    idempotency: IdempotencyRecord | None = None
    execute_task: Any = None

    @property
    def terminal(self) -> bool:
        return self.state in _TERMINAL

    def view(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "execution_id": self.execution_id,
            "state": self.state,
            "dispatch_state": self.dispatch_state,
            "compute_quiescent": True if self.terminal else None,
            "result": None if self.result is None else {
                "blob_id": self.result.blob_id, "owner": self.result.owner, "sha256": self.result.sha256,
                "size_bytes": self.result.size_bytes, "media_type": self.result.media_type},
            "error": None if self.error is None else {
                "code": self.error.code, "message": self.error.message, "retryable": self.error.retryable},
            "instance": None if self.instance is None else {
                "container_id": self.instance.container_id, "started_at": self.instance.started_at,
                "deployment_id": self.instance.deployment_id, "model_id": self.instance.model_id,
                "runtime_id": self.instance.runtime_id, "candidate_digest": self.instance.candidate_digest,
                "image_digest": self.instance.image_digest},
            "fence": cp.fence_document(self.fence),
        }
        cp.parse_execution_view(document)  # an invalid view must never escape the service
        return document


class ExecutionService:
    """The execution queue and result-submission service for one scheduler."""

    def __init__(
        self,
        scheduler,
        *,
        blobs,
        backend_for: Callable[[str], Any],
        boot_id: str,
        clock: Callable[[], float] | None = None,
        queue_capacity: int = EXECUTION_QUEUE_CAPACITY,
        wait_seconds: float = EXECUTION_WAIT_SECONDS,
        poll_seconds: float = 1.0,
        output_limit_bytes: int = MAX_BLOB_BYTES,
        output_media_type: str = "application/json",
        tokens: TokenAuthority | None = None,
        idempotency: IdempotencyStore | None = None,
        event_sink: Callable[[str, Mapping[str, object]], None] | None = None,
        managed_termination: bool = False,
        stop_grace_seconds: float = 30.0,
    ) -> None:
        if queue_capacity < 1 or wait_seconds <= 0 or poll_seconds <= 0 or output_limit_bytes <= 0 or stop_grace_seconds <= 0:
            raise ValueError("execution policy values must be positive")
        self._scheduler = scheduler
        self._sessions = scheduler.sessions
        self._book = scheduler.book
        self._blobs = blobs
        self._backend_for = backend_for
        self._boot_id = boot_id
        self._clock = clock if clock is not None else monotonic
        self._queue_capacity = queue_capacity
        self._wait_seconds = wait_seconds
        self._poll_seconds = poll_seconds
        self._output_limit = min(output_limit_bytes, MAX_BLOB_BYTES)
        self._output_media_type = output_media_type
        self._tokens = tokens
        self._idempotency = idempotency
        self._event_sink = event_sink
        self._lock = asyncio.Lock()
        self._records: dict[str, ExecutionRecord] = {}
        self._dispatchers: dict[str, asyncio.Task[None]] = {}
        self._pending_cleanup: set[str] = set()
        # P16 managed termination: dispatched executions settle through a proven instance
        # stop unless the adapter itself claims a trusted per-request sync protocol.
        self._managed = bool(managed_termination)
        self._stop_grace_seconds = stop_grace_seconds
        self._quiescing: set[str] = set()
        self._quiescers: dict[str, asyncio.Task[None]] = {}
        self._publish_inflight: set[str] = set()
        if getattr(scheduler, "execution_hook", None) is not None:
            raise RuntimeError("the scheduler already owns an execution hook")
        scheduler.execution_hook = self._on_session_draining

    # -- public API ----------------------------------------------------------

    def record(self, execution_id: str) -> ExecutionRecord:
        entry = self._records.get(execution_id)
        if entry is None:
            raise ExecutionError("not_found", "unknown execution")
        return entry

    def records(self) -> Mapping[str, ExecutionRecord]:
        return dict(self._records)

    @property
    def pending_cleanup(self) -> tuple[str, ...]:
        """Output reservations that failed/cancelled executions still must abandon."""
        return tuple(sorted(self._pending_cleanup))

    async def submit(self, *, session_id: str, owner: str, document: Mapping) -> dict[str, Any]:
        """Queue one execution on an ACTIVE session; a replayed idempotency key returns the original."""
        parsed = cp.parse_execution_create_request(document)
        idem: IdempotencyRecord | None = None
        try:
            async with self._lock:
                try:
                    session = self._sessions.get(session_id)
                except SessionNotFound as exc:
                    raise ExecutionError("not_found", "unknown session") from exc
                if self._tokens is not None:
                    try:
                        self._tokens.verify(parsed.session_token, owner=owner, model_id=session.model_id, session_id=session_id)
                    except IdentityError as exc:
                        raise ExecutionError(exc.code, str(exc)) from exc
                if parsed.input.blob is not None and parsed.input.blob.owner != blob_owner_for(owner):
                    raise ExecutionError("not_found", "the blob reference does not belong to this owner")
                if self._idempotency is not None:
                    payload = {"session_id": session_id, "operation": parsed.operation,
                               "input": dict(document["input"]), "parameters": dict(document["parameters"])}
                    fingerprint = None if self._tokens is None else self._tokens.fingerprint(parsed.session_token)
                    try:
                        idem = self._idempotency.begin(route="execution.create", owner=owner, key=parsed.idempotency_key,
                                                       payload=payload, token_fingerprint=fingerprint)
                    except IdempotencyError as exc:
                        raise ExecutionError(exc.code, str(exc)) from exc
                    if idem.state == COMPLETED:
                        original = self._records.get(idem.resource_id or "")
                        if original is not None:
                            return original.view()  # replay returns the object's current state
                        raise ExecutionError("not_found", "the replayed object is gone with its session")
                if session.phase != ACTIVE:
                    raise ExecutionError("session_not_active", "only an ACTIVE session may submit")
                if not self._sessions.is_live(session_id, self._clock()):
                    raise ExecutionError("session_expired", "the session is no longer live")
                capabilities = {capability.value for capability in self._book.specs[session.model_id].capabilities}
                if parsed.operation not in capabilities:
                    raise ExecutionError("capability_mismatch", "the model does not register this operation")
                pending = sum(1 for entry in self._records.values()
                              if entry.session_id == session_id and entry.state == QUEUED)
                if pending >= self._queue_capacity:
                    raise ExecutionError("queue_full", "the session execution queue is full")
                generation = self._book.runtime[session.model_id].generation
                if generation < 1:
                    raise ExecutionError("session_not_active", "the session model has no live generation")
                execution_id = uuid4().hex
                record = ExecutionRecord(
                    execution_id=execution_id, session_id=session_id, model_id=session.model_id, owner=owner,
                    operation=parsed.operation, input=parsed.input, parameters=dict(parsed.parameters),
                    deadline=min(self._clock() + self._wait_seconds, session.hard_deadline),
                    fence=Fence(self._boot_id, session.model_id, generation, uuid4().hex, execution_id, 1),
                    output_blob_id=f"o-{execution_id}", idempotency=idem,
                )
                self._records[execution_id] = record
                view = record.view()
                if idem is not None:
                    self._idempotency.complete(idem, status=202, resource_id=execution_id, body=view,
                                               active_until=record.deadline)
                self._emit("execution_queued", {"execution_id": execution_id, "session_id": session_id,
                                                "fence": cp.fence_document(record.fence)})
                self._ensure_dispatcher(session_id)
                return view
        except ExecutionError:
            if idem is not None and idem.state == PENDING:
                self._idempotency.fail(idem)  # a refusal must not strand the key
            raise

    async def view(self, execution_id: str, *, owner: str) -> dict[str, Any]:
        entry = self.record(execution_id)
        if entry.owner != owner:
            raise ExecutionError("not_found", "the execution does not belong to this owner")
        return entry.view()

    async def cancel(self, execution_id: str, *, owner: str, session_token: str | None = None) -> dict[str, Any]:
        """Cancel one execution: queued ends at once with `not_started`; dispatched waits for its terminal."""
        async with self._lock:
            entry = self.record(execution_id)
            if entry.owner != owner:
                raise ExecutionError("not_found", "the execution does not belong to this owner")
            if self._tokens is not None and session_token is not None:
                try:
                    self._tokens.verify(session_token, owner=owner, model_id=entry.model_id, session_id=entry.session_id)
                except IdentityError as exc:
                    raise ExecutionError(exc.code, str(exc)) from exc
            if entry.settled:
                return entry.view()  # 200-shaped: already terminal
            if not entry.dispatch_started:
                self._terminate_local(entry, "queue_timeout", "cancelled before dispatch")
                teardown = True
            else:
                entry.cancel_pending = True
                self._mark_reason(entry, "execution_timeout", "cancelled")
                if not entry.terminal:
                    entry.state = CANCELLING
                await self._cancel_side_effects(entry)
                teardown = self._evaluate(entry)
        await self._advance(entry, teardown)
        return entry.view()

    async def report_output(self, execution_id: str, body: bytes) -> dict[str, Any]:
        """Deliver one execution's result bytes; they publish only together with the terminal."""
        if not isinstance(body, (bytes, bytearray)):
            raise ExecutionError("contract_violation", "output must be bytes")
        async with self._lock:
            entry = self.record(execution_id)
            if entry.settled:
                self._emit("output_dropped", {"execution_id": execution_id, "reason": "already settled"})
                return entry.view()
            if entry.cancel_pending:
                entry.output_pending = None  # a late output is never published
                await self._cancel_reservation(entry)
                self._emit("output_dropped", {"execution_id": execution_id, "reason": "cancelled"})
                return entry.view()
            if entry.publishing:
                raise ExecutionError("busy", "a publish is already in flight for this execution")
            entry.output_pending = bytes(body)
            teardown = self._evaluate(entry)
        await self._advance(entry, teardown)
        return entry.view()

    async def report_terminal(self, execution_id: str, evidence: TerminationEvidence) -> dict[str, Any] | None:
        """Apply one backend terminal report under the C03 writeback rule; `None` means rejected."""
        async with self._lock:
            entry = self.record(execution_id)
            decision = cp.writeback_decision(entry.fence, evidence.fence)
            if not decision.accepted:
                self._emit("writeback_rejected", {"stage": "execution", "reason": decision.reason,
                                                  "execution_id": execution_id, "fence": cp.fence_document(evidence.fence)})
                return None
            if evidence.dispatch_state == "not_started" and entry.dispatch_state == "dispatched":
                self._emit("writeback_rejected", {"stage": "execution", "reason": "already_dispatched",
                                                  "execution_id": execution_id, "fence": cp.fence_document(evidence.fence)})
                return None
            if entry.evidence is not None:
                return entry.view()  # duplicate terminal: an idempotent no-op, nothing is re-published
            entry.evidence = evidence
            if evidence.dispatch_state == "dispatched":
                entry.dispatch_state = "dispatched"
                entry.instance = evidence.instance
            teardown = self._evaluate(entry)
        await self._advance(entry, teardown)
        return entry.view()

    # -- settling decisions ----------------------------------------------------

    def _evaluate(self, record: ExecutionRecord) -> bool:
        """Under the lock: decide whether a terminal settles now; True means fully settled (teardown)."""
        if record.settled or record.evidence is None:
            return False
        if record.cancel_pending or record.terminal_code is not None:
            # a non-terminal view never carries an error (P02): the reason materializes at the terminal
            record.error = _error(record.terminal_code or "backend_failed",
                                  record.terminal_message or "the execution ended without a result")
            self._mark_settled(record, CANCELLED if record.cancel_pending else FAILED)
            return True
        if record.output_pending is not None:
            if record.reservation is None:
                record.error = _error("backend_failed", "a result cannot publish without dispatch")
                self._mark_settled(record, FAILED)
                return True
            if not record.publishing:  # exactly one publish in flight, even if the handle lands mid-publish
                record.publishing = True  # the caller publishes outside the lock via _advance
            return False
        return False  # terminal without a result: never a fake success; keep waiting

    def _mark_reason(self, record: ExecutionRecord, code: str, message: str) -> None:
        """The first reason to end an execution wins; it materializes only in the terminal view."""
        if record.terminal_code is None:
            record.terminal_code = code
            record.terminal_message = message

    def _mark_settled(self, record: ExecutionRecord, state: str) -> None:
        record.state = state
        record.settled = True
        self._finish_idempotency(record)
        self._emit("execution_settled", {"execution_id": record.execution_id, "state": state,
                                         "fence": cp.fence_document(record.fence)})

    def _terminate_local(self, record: ExecutionRecord, code: str, message: str) -> None:
        """A never-dispatched execution terminates with `not_started` proof and no fake identity."""
        record.evidence = TerminationEvidence(fence=record.fence, dispatch_state="not_started",
                                              compute_quiescent=True, device_synchronized=False, reason=code)
        record.error = _error(code, message)
        self._mark_settled(record, CANCELLED if code in _CANCEL_CODES else FAILED)

    async def _advance(self, record: ExecutionRecord, settled: bool) -> None:
        if record.publishing and record.execution_id not in self._publish_inflight:
            self._publish_inflight.add(record.execution_id)
            try:
                await self._publish_and_settle(record)
            finally:
                self._publish_inflight.discard(record.execution_id)
            settled = record.settled
        if settled:
            await self._teardown(record)

    async def _publish_and_settle(self, record: ExecutionRecord) -> None:
        body, reservation = record.output_pending, record.reservation
        published = None
        failure: str | None = None
        if body is None or reservation is None:
            failure = "backend_failed"
        else:
            try:
                published = await self._blobs.write_output(
                    reservation, chunks=_one_shot(bytes(body)), expected_sha256=hashlib.sha256(bytes(body)).hexdigest())
            except BlobStoreError as exc:
                failure = "cancelled" if exc.code == "cancelled" else _BLOB_CODES.get(exc.code, "backend_failed")
            except Exception:
                failure = "storage_unavailable"
        async with self._lock:
            record.publishing = False
            record.output_pending = None
            if record.settled:
                return
            if failure == "cancelled":
                record.cancel_pending = True
                record.error = record.error or _error("execution_timeout", "a late result is never published")
                self._pending_cleanup.add(record.output_blob_id)
                self._mark_settled(record, CANCELLED)
            elif failure is not None:
                record.error = _error(failure, "the result could not be published")
                self._pending_cleanup.add(record.output_blob_id)
                self._mark_settled(record, FAILED)
            elif published is not None:
                record.result = cp.BlobRef(blob_id=published.blob_id, owner=published.owner, sha256=published.sha256,
                                           size_bytes=published.size_bytes, media_type=published.media_type)
                self._mark_settled(record, SUCCEEDED)
            else:  # pragma: no cover - defensive
                record.error = _error("backend_failed", "publish returned nothing")
                self._mark_settled(record, FAILED)
        await self._teardown(record)

    def _finish_idempotency(self, record: ExecutionRecord) -> None:
        if record.idempotency is not None and self._idempotency is not None:
            try:
                self._idempotency.complete(record.idempotency, status=200, resource_id=record.execution_id,
                                           body=record.view())
            except Exception:
                pass

    # -- exactly-one resource teardown ----------------------------------------

    async def _teardown(self, record: ExecutionRecord) -> None:
        if record.lease is not None and not record.lease_released:
            record.lease_released = True
            await self._scheduler.release(record.lease,
                                          Outcome.SUCCESS if record.state == SUCCEEDED else Outcome.REJECTED)
        if record.input_ctx is not None and not record.input_closed:
            record.input_closed = True
            try:
                await record.input_ctx.__aexit__(None, None, None)
            except Exception:
                pass
        if record.reservation is not None and record.state != SUCCEEDED:
            # a non-success settle must return the ceiling; a failing abandon keeps it tracked
            try:
                await self._blobs.cancel_output(record.reservation, fence=record.fence)
                await self._blobs.abandon_output(record.reservation)
                self._pending_cleanup.discard(record.output_blob_id)
            except Exception:
                self._pending_cleanup.add(record.output_blob_id)

    # -- dispatch ----------------------------------------------------------------

    def _ensure_dispatcher(self, session_id: str) -> None:
        task = self._dispatchers.get(session_id)
        if task is None or task.done():
            self._dispatchers[session_id] = asyncio.create_task(self._dispatch_loop(session_id))

    async def _dispatch_loop(self, session_id: str) -> None:
        try:
            while True:
                expired: list[ExecutionRecord] = []
                heads: list[ExecutionRecord] = []
                async with self._lock:
                    now = self._clock()
                    for entry in self._records.values():
                        if entry.session_id != session_id or entry.settled or entry.dispatch_started or entry.dispatch_claimed:
                            continue
                        if entry.state != QUEUED:
                            continue
                        if entry.model_id in self._quiescing:
                            continue  # AC2: a fallback stop is owed; new dispatches freeze (heartbeats and expiry do not)
                        (expired if now >= entry.deadline else heads).append(entry)
                    for entry in expired:
                        self._terminate_local(entry, "queue_timeout", "queue deadline elapsed")
                    for entry in heads:
                        entry.dispatch_claimed = True
                    unsettled = any(entry.session_id == session_id and not entry.settled for entry in self._records.values())
                for entry in heads:
                    asyncio.create_task(self._run(entry))
                if not expired and not heads and not unsettled:
                    return
                await asyncio.sleep(self._poll_seconds)
        finally:
            self._dispatchers[session_id] = None

    async def _run(self, record: ExecutionRecord) -> None:
        execution_id = record.execution_id
        try:
            refusal = await self._pre_dispatch_guard(record)
            if refusal is not None:
                _state, code, message = refusal
                async with self._lock:
                    if record.settled:
                        return
                    self._terminate_local(record, code, message)
                await self._teardown(record)
                return
            if record.settled:
                return
            blob_path = await self._open_input_lease(record)
            if blob_path is self._BLOB_FAILED:
                return
            lease = await self._admit(record)
            if lease is self._ADMIT_FAILED:
                return
            if lease is self._ADMIT_RETRY:
                async with self._lock:
                    record.dispatch_claimed = False  # keep the queue position; the next scan retries
                return
            settled_while_admitted = False
            async with self._lock:
                if record.settled:  # cancelled while it waited for the slot
                    settled_while_admitted = True
                else:
                    record.lease = lease
                    if lease.generation != record.fence.generation:
                        record.fence = Fence(self._boot_id, record.model_id, lease.generation,
                                             record.fence.operation_id, execution_id, record.fence.attempt)
            if settled_while_admitted:
                await self._teardown(record)  # releases the fresh lease exactly once, without dispatching
                return
            fence = record.fence
            reservation = await self._blobs.reserve_output(
                blob_id=record.output_blob_id, owner=blob_owner_for(record.owner),
                media_type=self._output_media_type, limit_bytes=self._output_limit, fence=fence)
            discarded = False
            async with self._lock:
                if record.settled or record.fence != fence:
                    discarded = True
                else:
                    record.reservation = reservation
                    record.dispatch_started = True
                    if not record.terminal:
                        record.state = RUNNING
                    self._emit("execution_dispatched", {"execution_id": execution_id,
                                                        "fence": cp.fence_document(record.fence)})
            if discarded:
                try:
                    await self._blobs.cancel_output(reservation, fence=fence)
                    await self._blobs.abandon_output(reservation)
                except Exception:
                    pass
                await self._teardown(record)
                return
            await self._execute(record, blob_path)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never strand a record or a lease
            await self._terminate_uncertain(record, f"service failure: {exc}")

    async def _pre_dispatch_guard(self, record: ExecutionRecord) -> tuple[str, str, str] | None:
        """AC2: re-validate session, capability, protocol limits and the live generation before dispatch."""
        async with self._lock:
            if record.settled:
                return None
            try:
                session = self._sessions.get(record.session_id)
            except SessionNotFound:
                return (CANCELLED, "session_not_active", "the session is gone")
            if session.phase != ACTIVE or not self._sessions.is_live(record.session_id, self._clock()):
                return (CANCELLED, "session_expired", "the session is no longer live")
            capabilities = {capability.value for capability in self._book.specs[record.model_id].capabilities}
            if record.operation not in capabilities:
                return (FAILED, "capability_mismatch", "the model no longer registers this operation")
            if record.input.inline is not None and len(canonical_json_bytes(record.input.inline)) > cp.MAX_INLINE_INPUT_BYTES:
                return (FAILED, "payload_too_large", "the inline input exceeds the protocol limit")
            if self._book.runtime[record.model_id].generation != record.fence.generation:
                record.fence = Fence(self._boot_id, record.model_id, self._book.runtime[record.model_id].generation,
                                     record.fence.operation_id, record.execution_id, record.fence.attempt)
            return None

    _BLOB_FAILED = object()

    async def _open_input_lease(self, record: ExecutionRecord):
        """AC2: a Blob input is protected by exactly one verified read lease for the whole execution."""
        if record.input.blob is None:
            return None
        try:
            ctx = self._blobs.lease(record.input.blob.blob_id, blob_owner_for(record.owner), record.execution_id)
            reader = await ctx.__aenter__()
        except BlobStoreError as exc:
            code = _BLOB_CODES.get(exc.code, "not_found")
            async with self._lock:
                if not record.settled:
                    self._terminate_local(record, code, "the blob input is not readable")
            await self._teardown(record)
            return self._BLOB_FAILED
        async with self._lock:
            record.input_ctx = ctx
        return str(reader.record.path)

    _ADMIT_FAILED, _ADMIT_RETRY = object(), object()

    async def _admit(self, record: ExecutionRecord):
        """AC2: one lease from the single authority; envelope/resource/slot decisions stay in the scheduler."""
        try:
            return await self._scheduler.acquire(record.model_id, record.execution_id, record.deadline,
                                                 session_id=record.session_id)
        except TimeoutError:
            async with self._lock:
                if not record.settled:
                    self._terminate_local(record, "queue_timeout", "queue deadline elapsed")
            await self._teardown(record)
            return self._ADMIT_FAILED
        except SessionConflict as exc:
            key = str(exc.args[0]) if exc.args and str(exc.args[0]) in cp.ERROR_STATUS else "session_not_active"
            async with self._lock:
                if not record.settled:
                    self._terminate_local(record, key, "the session is no longer active")
            await self._teardown(record)
            return self._ADMIT_FAILED
        except Exception as exc:
            detail = str(exc.args[0]) if getattr(exc, "args", None) else ""
            if detail == "session_slots_exhausted":
                return self._ADMIT_RETRY
            async with self._lock:
                if not record.settled:
                    code = detail if detail in cp.ERROR_STATUS else (
                        "storage_unavailable" if "storage" in detail else "temporarily_unavailable")
                    self._terminate_local(record, code, "admission refused the execution")
            await self._teardown(record)
            return self._ADMIT_FAILED

    async def _execute(self, record: ExecutionRecord, blob_path: str | None) -> None:
        backend = self._backend_for(record.model_id)
        request = ExecutionRequest(execution_id=record.execution_id, operation=record.operation,
                                   parameters=dict(record.parameters), inline_input=record.input.inline,
                                   blob_path=blob_path, output_directory=None)
        task = asyncio.create_task(backend.execute(request, record.fence, record.deadline))
        record.execute_task = task
        done, _waiting = await asyncio.wait({task}, timeout=max(0.0, record.deadline - self._clock()))
        if done:
            await self._after_execute(record, task)
        else:
            await self._on_timeout(record)
            task.add_done_callback(lambda late: asyncio.ensure_future(self._after_execute(record, late)))

    async def _after_execute(self, record: ExecutionRecord, task: asyncio.Task) -> None:
        try:
            handle = task.result()
        except NotDispatched as exc:
            async with self._lock:
                if record.settled:
                    return
                self._terminate_local(record, exc.code, str(exc))
            await self._teardown(record)
            return
        except asyncio.CancelledError:  # pragma: no cover - service never cancels execute
            await self._terminate_uncertain(record, "the execution task was cancelled")
            return
        except Exception:
            await self._terminate_uncertain(record, "the backend raised without a trusted terminal")
            return
        backend = self._backend_for(record.model_id)
        body: bytes | None = None
        take = getattr(backend, "take_result", None)
        if take is not None:  # the managed adapter validated the whole answer; the service publishes it
            try:
                body = take(record.execution_id)
            except Exception:
                body = None
        need_quiesce = False
        async with self._lock:
            if record.settled:
                return  # a terminal was already published from trusted evidence; facts stay facts
            record.instance = handle.instance
            record.dispatch_state = "dispatched"
            if body is not None:
                if record.cancel_pending:
                    record.output_pending = None
                    await self._cancel_reservation(record)
                    self._emit("output_dropped", {"execution_id": record.execution_id, "reason": "cancelled"})
                elif record.publishing:
                    self._emit("output_dropped", {"execution_id": record.execution_id, "reason": "publish in flight"})
                else:
                    record.output_pending = bytes(body)
            if self._managed:
                claims = getattr(backend, "claims_device_quiescence", None)
                if claims is not None and claims():
                    if record.evidence is None:
                        record.evidence = TerminationEvidence(
                            fence=record.fence, dispatch_state="dispatched", compute_quiescent=True,
                            device_synchronized=True, reason="request_protocol_terminated", instance=handle.instance)
                else:
                    # HTTP done != device idle (P15): the request waits for a proven instance stop.
                    record.awaiting_quiescence = True
                    need_quiesce = True
            teardown = self._evaluate(record)
            cancel_pending = record.cancel_pending and not record.settled
        await self._advance(record, teardown)
        if need_quiesce:
            self._ensure_quiescer(record.model_id)
        if cancel_pending:
            try:
                await backend.cancel(handle, self._clock() + self._sessions.cancel_seconds)
            except Exception:
                pass  # the cancel ack is not proof; the terminal evidence stays the arbiter

    async def _on_timeout(self, record: ExecutionRecord) -> None:
        async with self._lock:
            if record.settled:
                return
            record.cancel_pending = True
            self._mark_reason(record, "execution_timeout", "execution deadline elapsed")
            if not record.terminal:
                record.state = CANCELLING
            await self._cancel_side_effects(record)
            settled_now = self._evaluate(record)
        if settled_now:  # the terminal evidence had already arrived: settle and release now
            await self._teardown(record)

    async def _terminate_uncertain(self, record: ExecutionRecord, message: str) -> None:
        """Dispatch happened but the outcome is unproven: no terminal is invented; lease and budget stay."""
        need_quiesce = False
        async with self._lock:
            if record.settled:
                return
            self._mark_reason(record, "backend_failed", message)  # not a client cancel: an unproven backend failure
            if not record.terminal:
                record.state = CANCELLING
            if self._managed and record.dispatch_started:
                # a broken stream is protocol-finished on our side: an instance stop may proceed
                # once the other requests finish; until its proof lands nothing settles.
                record.awaiting_quiescence = True
                if record.instance is None:
                    record.expected_instance = getattr(self._backend_for(record.model_id), "current_identity", None)
                need_quiesce = True
            await self._cancel_side_effects(record)
            settled_now = self._evaluate(record)
        if settled_now:
            await self._teardown(record)
        if need_quiesce:
            self._ensure_quiescer(record.model_id)

    # -- managed termination: one shared stop settles the quiescing batch ------

    def _ensure_quiescer(self, model_id: str) -> None:
        task = self._quiescers.get(model_id)
        if task is None or task.done():
            self._quiescers[model_id] = asyncio.create_task(self._quiesce_model(model_id))

    async def _quiesce_model(self, model_id: str) -> None:
        """P16 AC2/AC3: after every in-flight request finishes per protocol, one proven STOPPED
        terminates the awaiting batch; an unprovable stop keeps everything fail-closed."""
        loop = asyncio.get_event_loop()
        grace_until = loop.time() + self._stop_grace_seconds
        while True:
            async with self._lock:
                # "in flight" counts everything that may still touch the instance: a claimed admission,
                # a held lease, or a dispatched computation whose response has not ended per protocol.
                inflight = [entry for entry in self._records.values()
                            if entry.model_id == model_id and not entry.settled and not entry.awaiting_quiescence
                            and (entry.dispatch_started or entry.dispatch_claimed or entry.lease is not None)]
                batch = [entry for entry in self._records.values()
                         if entry.model_id == model_id and not entry.settled and entry.dispatch_started
                         and entry.awaiting_quiescence]
                if not batch and not inflight:
                    self._quiescing.discard(model_id)  # every terminal is proven: dispatch resumes
                    return
                self._quiescing.add(model_id)
                todo = [] if inflight else list(batch)
            for entry in todo:
                # an awaiting request's response already ended per protocol: its lease is not a
                # waiting object any more (AC2), while the budget stays until the stop is proven.
                await self._release_quiescence_lease(entry)
            if todo:
                unproven = False
                try:
                    # the shared stop goes through the single book path: ManagedLifecycle.stop
                    # asks llama-swap, the observer's four facts decide, `Book.stopped` clears the budget.
                    await self._scheduler.unload(model_id, self._clock() + self._stop_grace_seconds)
                except Exception as exc:
                    unproven = True
                    self._emit("termination_unproven", {"model_id": model_id, "reason": type(exc).__name__})
                if not unproven:
                    for entry in todo:
                        await self._settle_with_stopped_proof(entry)
            if loop.time() >= grace_until:
                return  # never invent a terminal: the records stay, the session will go BLOCKED (P10)
            await asyncio.sleep(self._poll_seconds)

    async def _release_quiescence_lease(self, record: ExecutionRecord) -> None:
        async with self._lock:
            if record.lease is None or record.lease_released:
                return
            record.lease_released = True
            lease = record.lease
            outcome = Outcome.SUCCESS if (record.error is None and not record.cancel_pending) else Outcome.REJECTED
        await self._scheduler.release(lease, outcome)

    async def _settle_with_stopped_proof(self, record: ExecutionRecord) -> None:
        async with self._lock:
            if record.settled or record.evidence is not None:
                return
            instance = record.instance or record.expected_instance
            if instance is None:
                return  # a dispatched terminal needs the full identity: stay unproven, never fake
            record.evidence = TerminationEvidence(fence=record.fence, dispatch_state="dispatched",
                                                  compute_quiescent=True, device_synchronized=True,
                                                  reason="independent_STOPPED", instance=instance)
            if record.dispatch_state != "dispatched":
                record.dispatch_state = "dispatched"
                record.instance = instance
            teardown = self._evaluate(record)
        await self._advance(record, teardown)

    # -- session drain (the scheduler's hook) -----------------------------------

    async def _on_session_draining(self, session_id: str) -> None:
        """Close/expiry atomically forbids submit; here every still-live execution of that session is cancelled."""
        settled: list[ExecutionRecord] = []
        async with self._lock:
            for entry in list(self._records.values()):
                if entry.session_id != session_id or entry.settled:
                    continue
                if not entry.dispatch_started:
                    self._terminate_local(entry, "queue_timeout", "the session closed before dispatch")
                    settled.append(entry)
                else:
                    entry.cancel_pending = True
                    self._mark_reason(entry, "session_expired", "the session closed while the execution ran")
                    if not entry.terminal:
                        entry.state = CANCELLING
                    await self._cancel_side_effects(entry)
                    if self._evaluate(entry):
                        settled.append(entry)
        for entry in settled:
            await self._teardown(entry)

    async def _cancel_side_effects(self, record: ExecutionRecord) -> None:
        """Mark the lease cancelling and the output unsubmittable; the lease and budget stay (P10)."""
        if record.lease is not None:
            try:
                await self._scheduler.cancel(record.lease)
            except Exception:
                pass
        await self._cancel_reservation(record)

    async def _cancel_reservation(self, record: ExecutionRecord) -> None:
        if record.reservation is None or record.result is not None:
            return
        try:
            if await self._blobs.cancel_output(record.reservation, fence=record.fence):
                self._pending_cleanup.add(record.output_blob_id)
        except Exception:
            pass

    # -- misc ---------------------------------------------------------------------

    def _emit(self, kind: str, payload: Mapping[str, object]) -> None:
        if self._event_sink is None:
            return
        try:
            self._event_sink(kind, payload)
        except Exception:
            pass
