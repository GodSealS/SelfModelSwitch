"""Session-scoped execution queue and result submission (M04/P14, C03/C04/C07).

The service layer is the vertical slice: an ACTIVE session submits bounded,
idempotent executions into a per-session queue; dispatch re-validates the
session, the capability, the protocol limits and the resources before it
grants exactly one scheduler lease and (for a Blob input) exactly one read
lease; a success requires the published result AND an accepted terminal
evidence, while a queued cancellation terminates with `not_started` proof and
never invents a container. Late output after a cancel or a timeout is never
published, and stale or duplicated callbacks change nothing.

The fakes below drive the ports_v3 `BackendPort` shape with out-of-order and
duplicate reports, which is the verification the plan fixes for this task.
"""
from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import Callable

import pytest

from model_scheduler import control_protocol_v1 as cp
from model_scheduler.blob_store import BlobStore
from model_scheduler.contracts import Capability, MemorySample, ModelSpec, Observation, Outcome, Presence
from model_scheduler.control_identity import TokenAuthority
from model_scheduler.control_protocol_v1 import Fence, InstanceIdentity
from model_scheduler.execution_service import (
    EXECUTION_QUEUE_CAPACITY,
    EXECUTION_WAIT_SECONDS,
    ExecutionError,
    ExecutionService,
    NotDispatched,
)
from model_scheduler.idempotency import IdempotencyStore
from model_scheduler.model_registry import Book
from model_scheduler.ports_v3 import CancelAck, ExecutionHandle, ExecutionRequest, TerminationEvidence
from model_scheduler.scheduler import ModelScheduler
from model_scheduler.session_manager import SessionManager


INSTANCE = InstanceIdentity(
    container_id="c-exec",
    started_at="2026-09-18T05:00:00Z",
    deployment_id="orin-local",
    model_id="chat",
    runtime_id="llama-cpp",
    candidate_digest="b" * 64,
    image_digest="repo/llama@sha256:" + "c" * 64,
)


class ManualClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ClockedResources:
    def __init__(self, clock: ManualClock) -> None:
        self.clock = clock

    async def snapshot(self) -> MemorySample:
        return MemorySample(10_000, 9_000, self.clock.now)


class ControlBackend:
    """v1 lifecycle control for load/stop only; execution never comes here."""

    def __init__(self) -> None:
        self.loads: list[str] = []
        self.stops: list[str] = []

    async def load(self, operation, deadline):
        self.loads.append(operation.model_id)
        return Observation(Presence.RUNNING, f"instance-{operation.model_id}", True, 0)

    async def stop(self, operation, deadline):
        self.stops.append(operation.model_id)
        return Observation(Presence.STOPPED, None, False, 0)


class FakeExec:
    """A BackendPort fake: gates, failures and out-of-order reports are scripted."""

    def __init__(self, *, gate: asyncio.Event | None = None, raises: Exception | None = None) -> None:
        self.gate = gate
        self.raises = raises
        self.requests: list[ExecutionRequest] = []
        self.fences: list[Fence] = []
        self.cancels: list[str] = []

    async def execute(self, request: ExecutionRequest, fence: Fence, deadline: float) -> ExecutionHandle:
        self.requests.append(request)
        self.fences.append(fence)
        if self.gate is not None:
            await self.gate.wait()
        if self.raises is not None:
            raise self.raises
        return ExecutionHandle(execution_id=request.execution_id, instance=INSTANCE)

    async def cancel(self, handle: ExecutionHandle, deadline: float) -> CancelAck:
        self.cancels.append(handle.execution_id)
        return CancelAck(execution_id=handle.execution_id, accepted=True)

    async def load(self, spec, fence, deadline):  # pragma: no cover - P16 territory
        raise AssertionError("execution service must not load models")

    async def stop(self, identity, fence, deadline):  # pragma: no cover - P16 territory
        raise AssertionError("execution service must not stop models")


@dataclass
class Stack:
    clock: ManualClock
    scheduler: ModelScheduler
    book: Book
    sessions: SessionManager
    blobs: BlobStore
    backend: FakeExec
    service: ExecutionService

    def leases(self) -> set[str]:
        return {lease.request_id for runtime in self.book.runtime.values() for lease in runtime.leases.values()}


def chat_book(max_concurrency: int = 1) -> Book:
    spec = ModelSpec("chat", "http://127.0.0.1:18080", frozenset({Capability.CHAT}), 100, max_concurrency=max_concurrency)
    result = Book({"chat": spec}, model_budget=1_000, free_floor=20, margin=0)
    result.bootstrap_stopped("chat")
    return result


def exec_document(*, operation: str = "chat", inline=None, blob=None, parameters=None, key: str = "k-1", token: str = "opaque-token") -> dict:
    if blob is not None:
        input_: dict = {"blob": blob}
    else:
        input_ = {"inline": inline if inline is not None else {"messages": [{"role": "user", "content": "hi"}]}}
    return {
        "session_token": token,
        "operation": operation,
        "input": input_,
        "parameters": parameters if parameters is not None else {"max_tokens": 8},
        "idempotency_key": key,
    }


def blob_ref(blob_id: str, owner: str, payload: bytes) -> dict:
    return {
        "blob_id": blob_id,
        "owner": owner,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
        "media_type": "application/json",
    }


async def start_stack(tmp_path, *, max_concurrency: int = 1, queue_capacity: int | None = None,
                      wait_seconds: float = EXECUTION_WAIT_SECONDS, backend: FakeExec | None = None,
                      tokens: TokenAuthority | None = None, idempotency: IdempotencyStore | None = None,
                      output_limit_bytes: int | None = None, ttl_seconds: float = 30.0,
                      hard_deadline_seconds: float = 3600.0, open_session: bool = True) -> Stack:
    clock = ManualClock()
    sessions = SessionManager(wait_seconds=100.0, hard_deadline_seconds=hard_deadline_seconds, heartbeat_seconds=10.0,
                              ttl_seconds=ttl_seconds, prepare_seconds=100.0, drain_seconds=30.0, retry_seconds=30.0,
                              cleanup_seconds=60.0, cancel_seconds=10.0, stop_grace_seconds=30.0, reconcile_seconds=5.0)
    book = chat_book(max_concurrency)
    control = ControlBackend()
    scheduler = ModelScheduler(book, ClockedResources(clock), control, sessions=sessions, clock=clock, poll_interval_seconds=0.01)
    if open_session:
        await asyncio.wait_for(scheduler.open_session("chat", "client-a", "session-1"), 5)
    blobs = BlobStore(tmp_path / "blobs", clock=clock)
    exec_backend = backend if backend is not None else FakeExec()
    service_kwargs: dict = {}
    if output_limit_bytes is not None:
        service_kwargs["output_limit_bytes"] = output_limit_bytes
    service = ExecutionService(
        scheduler, blobs=blobs, backend_for=lambda model_id: exec_backend, boot_id="boot-0001", clock=clock,
        queue_capacity=queue_capacity if queue_capacity is not None else EXECUTION_QUEUE_CAPACITY,
        wait_seconds=wait_seconds, poll_seconds=0.01, tokens=tokens, idempotency=idempotency,
        **service_kwargs,
    )
    stack = Stack(clock=clock, scheduler=scheduler, book=book, sessions=sessions, blobs=blobs, backend=exec_backend, service=service)
    return stack


async def eventually(predicate: Callable[[], bool], *, attempts: int = 600) -> bool:
    for _ in range(attempts):
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


def record_of(stack: Stack, execution_id: str):
    return stack.service.record(execution_id)


async def submit(stack: Stack, *, operation="chat", inline=None, blob=None, parameters=None, key="k-1",
                 owner="uid:1000", session_id="session-1", token="opaque-token") -> dict:
    document = exec_document(operation=operation, inline=inline, blob=blob, parameters=parameters, key=key, token=token)
    return await stack.service.submit(session_id=session_id, owner=owner, document=document)


def terminal_evidence(stack: Stack, view: dict, *, attempt: int | None = None, generation: int | None = None,
                      execution_id: str | None = None, operation_id: str | None = None) -> TerminationEvidence:
    fence = cp.parse_fence(view["fence"])
    applied = Fence(
        boot_id=fence.boot_id,
        model_id=fence.model_id,
        generation=fence.generation if generation is None else generation,
        operation_id=fence.operation_id if operation_id is None else operation_id,
        execution_id=fence.execution_id if execution_id is None else execution_id,
        attempt=fence.attempt if attempt is None else attempt,
    )
    return TerminationEvidence(
        fence=applied,
        dispatch_state="dispatched",
        compute_quiescent=True,
        device_synchronized=True,
        reason="completed",
        instance=INSTANCE,
    )


BODY = b'{"ok":true}'


@pytest.mark.asyncio
async def test_queue_policy_is_per_session_capped_and_deadlined(tmp_path) -> None:
    assert EXECUTION_QUEUE_CAPACITY == 128
    assert EXECUTION_WAIT_SECONDS == 1800.0
    stack = await start_stack(tmp_path)

    view = await submit(stack)
    assert view["state"] == "queued"
    assert cp.parse_execution_view(view)  # the service never emits an invalid view
    assert view["dispatch_state"] == "not_started" and view["instance"] is None
    assert view["result"] is None and view["error"] is None and view["compute_quiescent"] is None

    record = record_of(stack, view["execution_id"])
    assert record.deadline == stack.clock.now + EXECUTION_WAIT_SECONDS

    # Settle it, then close the session: only ACTIVE may submit.
    assert await eventually(lambda: len(stack.backend.requests) == 1)
    await stack.service.report_output(view["execution_id"], BODY)
    settled = await stack.service.report_terminal(view["execution_id"], terminal_evidence(stack, view))
    assert settled["state"] == "succeeded"
    assert settled["result"]["sha256"] == hashlib.sha256(BODY).hexdigest()
    assert settled["result"]["size_bytes"] == len(BODY)
    assert await stack.blobs.read_all(f"o-{view['execution_id']}", "uid-1000", "probe") == BODY
    await asyncio.wait_for(stack.scheduler.close_session("session-1"), 5)
    with pytest.raises(ExecutionError) as refused:
        await submit(stack, key="k-2")
    assert refused.value.code in {"session_not_active", "session_expired", "not_found"}


@pytest.mark.asyncio
async def test_an_unknown_backend_failure_names_itself_in_the_terminal(tmp_path) -> None:
    """'the backend raised' is not diagnosable; the real reason must survive."""
    stack = await start_stack(tmp_path, backend=FakeExec(raises=RuntimeError("upstream said 500: too long")))
    view = await submit(stack)

    assert await eventually(lambda: record_of(stack, view["execution_id"]).terminal_code is not None)
    # The outcome stays unproven until the device-side proof lands: no terminal is invented.
    await stack.service.report_terminal(view["execution_id"], terminal_evidence(stack, view))
    terminal = await stack.service.view(view["execution_id"], owner="uid:1000")

    assert terminal["state"] == "failed"
    # The reason must be readable: a generic sentence would leave it undiagnosable.
    assert "RuntimeError" in terminal["error"]["message"]
    assert "upstream said 500: too long" in terminal["error"]["message"]


@pytest.mark.asyncio
async def test_deadline_is_capped_by_the_session_hard_deadline(tmp_path) -> None:
    stack = await start_stack(tmp_path, hard_deadline_seconds=10.0, backend=FakeExec(gate=asyncio.Event()))
    view = await submit(stack)
    record = record_of(stack, view["execution_id"])
    assert stack.sessions.get("session-1").hard_deadline == 10.0
    assert record.deadline == 10.0  # min(0 + 1800, session hard deadline)


@pytest.mark.asyncio
async def test_queue_capacity_is_enforced_per_session(tmp_path) -> None:
    gate = asyncio.Event()
    stack = await start_stack(tmp_path, queue_capacity=2, backend=FakeExec(gate=gate))
    first = await submit(stack, key="k-1")
    await eventually(lambda: len(stack.backend.requests) == 1)
    second = await submit(stack, key="k-2")
    third = await submit(stack, key="k-3")
    with pytest.raises(ExecutionError) as full:
        await submit(stack, key="k-4")
    assert full.value.code == "queue_full"
    # The two waiting executions each wait in the scheduler queue, but no lease yet: the gate holds the only slot.
    assert len({first["execution_id"], second["execution_id"], third["execution_id"]}) == 3
    for pending in (second, third):
        cancelled = await stack.service.cancel(pending["execution_id"], owner="uid:1000")
        assert cancelled["state"] == "cancelled" and cancelled["dispatch_state"] == "not_started"
    gate.set()
    settled = await stack.service.report_terminal(first["execution_id"], terminal_evidence(stack, first))
    await stack.service.report_output(first["execution_id"], BODY)
    assert (await stack.service.view(first["execution_id"], owner="uid:1000"))["state"] == "succeeded"
    assert settled["state"] == "running"  # terminal alone cannot succeed without the result


@pytest.mark.asyncio
async def test_succeeded_requires_published_result_and_trusted_terminal_in_any_order(tmp_path) -> None:
    # terminal first, output second
    stack = await start_stack(tmp_path)
    view = await submit(stack, key="k-1")
    execution_id = view["execution_id"]
    await eventually(lambda: len(stack.backend.requests) == 1)

    pending = await stack.service.report_terminal(execution_id, terminal_evidence(stack, view))
    assert pending["state"] == "running" and pending["result"] is None  # no publish on terminal alone
    done = await stack.service.report_output(execution_id, BODY)
    assert done["state"] == "succeeded" and done["compute_quiescent"] is True
    assert done["result"]["sha256"] == hashlib.sha256(BODY).hexdigest()
    assert done["result"]["size_bytes"] == len(BODY)
    assert await stack.blobs.read_all(f"o-{execution_id}", "uid-1000", "probe") == BODY
    await eventually(lambda: not stack.leases())

    # output first, terminal second
    stack2 = await start_stack(tmp_path / "two")
    view2 = await submit(stack2, key="k-1")
    await eventually(lambda: len(stack2.backend.requests) == 1)
    waiting = await stack2.service.report_output(view2["execution_id"], BODY)
    assert waiting["state"] == "running" and waiting["result"] is None
    done2 = await stack2.service.report_terminal(view2["execution_id"], terminal_evidence(stack2, view2))
    assert done2["state"] == "succeeded" and done2["result"] is not None
    assert cp.parse_execution_view(done2)


@pytest.mark.asyncio
async def test_a_terminal_without_a_result_never_fakes_success(tmp_path) -> None:
    stack = await start_stack(tmp_path)
    view = await submit(stack)
    await eventually(lambda: len(stack.backend.requests) == 1)
    pending = await stack.service.report_terminal(view["execution_id"], terminal_evidence(stack, view))
    assert pending["state"] == "running"
    assert pending["compute_quiescent"] is None and pending["result"] is None
    # nothing was published (the output ceiling may still be reserved until the execution ends)
    assert stack.blobs.metadata.usage("uid-1000").published_bytes == 0

    # cancelling it now settles with the accepted evidence and the ceiling returns
    cancelled = await stack.service.cancel(view["execution_id"], owner="uid:1000")
    assert cancelled["state"] == "cancelled" and cancelled["compute_quiescent"] is True
    assert stack.blobs.metadata.usage("uid-1000").total_bytes == 0
    await eventually(lambda: not stack.leases())


@pytest.mark.asyncio
async def test_duplicate_out_of_order_and_stale_callbacks_are_no_ops(tmp_path) -> None:
    stack = await start_stack(tmp_path)
    view = await submit(stack)
    await eventually(lambda: len(stack.backend.requests) == 1)
    await stack.service.report_output(view["execution_id"], BODY)
    settled = await stack.service.report_terminal(view["execution_id"], terminal_evidence(stack, view))
    assert settled["state"] == "succeeded"
    before = stack.blobs.metadata.usage("uid-1000").total_bytes

    # a duplicate terminal changes nothing
    again = await stack.service.report_terminal(view["execution_id"], terminal_evidence(stack, view))
    assert again["state"] == "succeeded"
    assert stack.blobs.metadata.usage("uid-1000").total_bytes == before

    # a late terminal from a superseded generation cannot touch the record
    stale = terminal_evidence(stack, view, generation=view["fence"]["generation"] + 1)
    rejected = await stack.service.report_terminal(view["execution_id"], stale)
    assert rejected is None  # writeback rejected: no side effect at all
    runtime = stack.book.runtime["chat"]
    assert runtime.total_requests == 1  # the lease was released exactly once


@pytest.mark.asyncio
async def test_foreign_execution_and_attempt_zero_are_rejected(tmp_path) -> None:
    stack = await start_stack(tmp_path)
    view = await submit(stack)
    await eventually(lambda: len(stack.backend.requests) == 1)
    foreign = terminal_evidence(stack, view, execution_id="e-somebody-else")
    assert await stack.service.report_terminal(view["execution_id"], foreign) is None
    older = terminal_evidence(stack, view, attempt=0)
    assert await stack.service.report_terminal(view["execution_id"], older) is None
    assert (await stack.service.view(view["execution_id"], owner="uid:1000"))["state"] == "running"


@pytest.mark.asyncio
async def test_a_queued_cancellation_needs_no_fake_identity_and_no_lease(tmp_path) -> None:
    gate = asyncio.Event()
    stack = await start_stack(tmp_path, backend=FakeExec(gate=gate))
    first = await submit(stack, key="k-1")
    await eventually(lambda: len(stack.backend.requests) == 1)
    second = await submit(stack, key="k-2")

    cancelled = await stack.service.cancel(second["execution_id"], owner="uid:1000")
    assert cancelled["state"] == "cancelled"
    assert cancelled["dispatch_state"] == "not_started" and cancelled["instance"] is None
    assert cancelled["compute_quiescent"] is True
    assert cancelled["error"]["code"] == "queue_timeout"
    assert cp.parse_execution_view(cancelled)

    gate.set()
    done = await stack.service.report_output(first["execution_id"], BODY)
    settled = await stack.service.report_terminal(first["execution_id"], terminal_evidence(stack, first))
    assert settled["state"] == "succeeded"
    await eventually(lambda: not stack.leases())
    assert [request.execution_id for request in stack.backend.requests] == [first["execution_id"]]


@pytest.mark.asyncio
async def test_a_cancelled_running_execution_drops_late_output_and_holds_the_lease(tmp_path) -> None:
    gate = asyncio.Event()
    stack = await start_stack(tmp_path, backend=FakeExec(gate=gate))
    view = await submit(stack)
    execution_id = view["execution_id"]
    await eventually(lambda: len(stack.backend.requests) == 1)
    assert execution_id in stack.leases()

    cancelling = await stack.service.cancel(execution_id, owner="uid:1000")
    assert cancelling["state"] == "cancelling" and cancelling["result"] is None
    assert execution_id in stack.leases()  # the lease stays until a trusted terminal (P10 rule)
    assert cancelling["error"] is None  # a non-terminal view never carries an error (P02)
    assert stack.blobs.metadata.usage("uid-1000").published_bytes == 0  # the output is unsubmittable

    gate.set()
    await eventually(lambda: stack.backend.cancels == [execution_id])
    dropped = await stack.service.report_output(execution_id, BODY)
    assert dropped["state"] == "cancelling"  # late output is never published
    assert stack.blobs.metadata.usage("uid-1000").published_bytes == 0

    settled = await stack.service.report_terminal(execution_id, terminal_evidence(stack, view))
    assert settled["state"] == "cancelled"
    assert settled["error"]["code"] == "execution_timeout"
    assert settled["dispatch_state"] == "dispatched" and settled["instance"] is not None
    assert cp.parse_execution_view(settled)
    await eventually(lambda: not stack.leases())
    await eventually(lambda: not stack.service.pending_cleanup)
    assert stack.blobs.metadata.usage("uid-1000").total_bytes == 0  # the ceiling returns at cleanup


@pytest.mark.asyncio
async def test_a_timed_out_execution_waits_for_its_terminal_then_cancels(tmp_path) -> None:
    gate = asyncio.Event()
    stack = await start_stack(tmp_path, backend=FakeExec(gate=gate), wait_seconds=0.05)
    view = await submit(stack)
    execution_id = view["execution_id"]
    await eventually(lambda: len(stack.backend.requests) == 1)

    await eventually(lambda: record_of(stack, execution_id).state == "cancelling")
    assert execution_id in stack.leases()  # no fabricated terminal: the lease is kept
    gate.set()
    await eventually(lambda: stack.backend.cancels == [execution_id])

    dropped = await stack.service.report_output(execution_id, BODY)
    assert dropped["state"] == "cancelling"
    settled = await stack.service.report_terminal(execution_id, terminal_evidence(stack, view))
    assert settled["state"] == "cancelled" and settled["error"]["code"] == "execution_timeout"
    await eventually(lambda: not stack.leases())


@pytest.mark.asyncio
async def test_adapter_pre_dispatch_rejection_fails_without_dispatch(tmp_path) -> None:
    stack = await start_stack(tmp_path, backend=FakeExec(raises=NotDispatched("envelope_exceeded")))
    view = await submit(stack)
    failed = await eventually(lambda: record_of(stack, view["execution_id"]).state == "failed")
    assert failed
    record = record_of(stack, view["execution_id"])
    assert record.error is not None and record.error.code == "envelope_exceeded"
    assert record.dispatch_state == "not_started" and record.instance is None
    assert cp.parse_execution_view(await stack.service.view(view["execution_id"], owner="uid:1000"))
    await eventually(lambda: not stack.leases())
    await eventually(lambda: stack.blobs.metadata.usage("uid-1000").total_bytes == 0 and not stack.service.pending_cleanup)


@pytest.mark.asyncio
async def test_an_uncertain_backend_error_holds_the_lease_until_a_trusted_terminal(tmp_path) -> None:
    stack = await start_stack(tmp_path, backend=FakeExec(raises=RuntimeError("boom")))
    view = await submit(stack)
    execution_id = view["execution_id"]
    assert await eventually(lambda: record_of(stack, execution_id).state == "cancelling")
    assert execution_id in stack.leases()  # dispatch was started: no not_started proof exists
    output = await stack.service.report_output(execution_id, BODY)
    assert output["state"] == "cancelling" and output["result"] is None  # never published
    settled = await stack.service.report_terminal(execution_id, terminal_evidence(stack, view))
    assert settled["state"] == "failed" and settled["error"]["code"] == "backend_failed"
    await eventually(lambda: not stack.leases())


@pytest.mark.asyncio
async def test_blob_input_holds_exactly_one_reference_lease_for_its_lifetime(tmp_path) -> None:
    payload = b'{"input":1}'
    stack = await start_stack(tmp_path)
    await stack.blobs.upload(
        blob_id="in-1", owner="uid-1000", media_type="application/json",
        chunks=_stream(payload), expected_sha256=hashlib.sha256(payload).hexdigest(), declared_size=len(payload),
    )
    view = await submit(stack, blob=blob_ref("in-1", "uid-1000", payload))
    execution_id = view["execution_id"]
    assert await eventually(lambda: len(stack.backend.requests) == 1)
    request = stack.backend.requests[0]
    assert request.blob_path is not None and request.inline_input is None
    assert await stack.blobs.delete("in-1", "uid-1000") == "held"

    await stack.service.report_output(execution_id, BODY)
    await stack.service.report_terminal(execution_id, terminal_evidence(stack, view))
    assert await eventually(lambda: not stack.blobs.metadata.leases("in-1"))
    assert await stack.blobs.delete("in-1", "uid-1000") == "deleted"


async def _stream(payload: bytes):
    yield payload


@pytest.mark.asyncio
async def test_blob_reference_owner_mismatch_is_not_found(tmp_path) -> None:
    stack = await start_stack(tmp_path)
    with pytest.raises(ExecutionError) as refused:
        await submit(stack, blob=blob_ref("in-x", "uid-9999", b"x"))
    assert refused.value.code == "not_found"


@pytest.mark.asyncio
async def test_capability_mismatch_is_refused_at_submit(tmp_path) -> None:
    stack = await start_stack(tmp_path)
    with pytest.raises(ExecutionError) as refused:
        await submit(stack, operation="embeddings", parameters={})
    assert refused.value.code == "capability_mismatch"
    assert not stack.service.records()


@pytest.mark.asyncio
async def test_session_drain_cancels_queued_and_running_executions(tmp_path) -> None:
    gate = asyncio.Event()
    stack = await start_stack(tmp_path, backend=FakeExec(gate=gate))
    running = await submit(stack, key="k-1")
    await eventually(lambda: len(stack.backend.requests) == 1)
    queued = await submit(stack, key="k-2")

    closing = asyncio.create_task(stack.scheduler.close_session("session-1"))
    assert await eventually(lambda: record_of(stack, queued["execution_id"]).state == "cancelled")
    cancelled_view = await stack.service.view(queued["execution_id"], owner="uid:1000")
    assert cancelled_view["dispatch_state"] == "not_started" and cancelled_view["instance"] is None
    assert await eventually(lambda: record_of(stack, running["execution_id"]).state == "cancelling")

    gate.set()
    await eventually(lambda: stack.backend.cancels == [running["execution_id"]])
    settled = await stack.service.report_terminal(running["execution_id"], terminal_evidence(stack, running))
    assert settled["state"] == "cancelled"
    closed = await asyncio.wait_for(closing, 10)
    assert closed["phase"] == "closed"
    assert not stack.leases()


@pytest.mark.asyncio
async def test_idempotent_replay_returns_the_same_object_and_isolation_holds(tmp_path) -> None:
    idempotency = IdempotencyStore(boot_key=b"i" * 32, clock=lambda: 1_000.0)
    stack = await start_stack(tmp_path, idempotency=idempotency)
    first = await submit(stack, key="shared")
    replay = await submit(stack, key="shared")
    assert replay["execution_id"] == first["execution_id"]
    assert await eventually(lambda: len(stack.backend.requests) == 1)  # no duplicated side effect
    with pytest.raises(ExecutionError) as conflict:
        await submit(stack, key="shared", parameters={"max_tokens": 9})
    assert conflict.value.code == "idempotency_conflict"

    with pytest.raises(ExecutionError) as foreign:
        await stack.service.view(first["execution_id"], owner="uid:2000")
    assert foreign.value.code == "not_found"


@pytest.mark.asyncio
async def test_session_token_is_verified_against_owner_and_session(tmp_path) -> None:
    tokens = TokenAuthority(boot_key=b"t" * 32, boot_id="boot-0001", clock=lambda: 1_000.0)
    good = tokens.issue(owner="uid:1000", model_id="chat", session_id="session-1", ttl_seconds=600.0, now=1_000.0)
    other = tokens.issue(owner="uid:1000", model_id="chat", session_id="session-9", ttl_seconds=600.0, now=1_000.0)
    stack = await start_stack(tmp_path, tokens=tokens)

    with pytest.raises(ExecutionError) as refused:
        await submit(stack, token=other)
    assert refused.value.code == "stale_token"
    view = await submit(stack, token=good)
    assert view["state"] == "queued"


@pytest.mark.asyncio
async def test_over_limit_output_is_not_published_and_cleanup_is_tracked(tmp_path) -> None:
    stack = await start_stack(tmp_path, output_limit_bytes=4)
    view = await submit(stack)
    await eventually(lambda: len(stack.backend.requests) == 1)
    buffered = await stack.service.report_output(view["execution_id"], b"way too long")
    assert buffered["state"] == "running" and buffered["result"] is None
    # the publish attempt only happens together with the terminal, and it fails there
    settled = await stack.service.report_terminal(view["execution_id"], terminal_evidence(stack, view))
    assert settled["state"] == "failed" and settled["error"]["code"] == "payload_too_large"
    assert stack.blobs.metadata.usage("uid-1000").published_bytes == 0
    await eventually(lambda: not stack.service.pending_cleanup and not stack.leases())
    assert stack.blobs.metadata.usage("uid-1000").total_bytes == 0


@pytest.mark.asyncio
async def test_each_execution_holds_exactly_one_lease(tmp_path) -> None:
    gate = asyncio.Event()
    stack = await start_stack(tmp_path, max_concurrency=2, backend=FakeExec(gate=gate))
    first = await submit(stack, key="k-1")
    second = await submit(stack, key="k-2")
    assert await eventually(lambda: len(stack.backend.requests) == 2)
    assert stack.leases() == {first["execution_id"], second["execution_id"]}
    gate.set()
    for view in (first, second):
        await stack.service.report_output(view["execution_id"], BODY)
        await stack.service.report_terminal(view["execution_id"], terminal_evidence(stack, view))
    await eventually(lambda: not stack.leases())
    assert stack.book.runtime["chat"].total_requests == 2

def test_capability_names_read_both_registrations() -> None:
    """The v2 bring-up bug: plain strings and enum members must both read cleanly."""
    from dataclasses import dataclass

    from model_scheduler.contracts import Capability
    from model_scheduler.execution_service import _capability_names

    @dataclass
    class _Spec:
        capabilities: object

    assert _capability_names(_Spec(frozenset({Capability.CHAT, Capability.EMBEDDINGS}))) == {"chat", "embeddings"}
    assert _capability_names(_Spec(("vision", "chat"))) == {"vision", "chat"}  # v2: no `.value` to read
