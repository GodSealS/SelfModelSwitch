"""Single-owner async scheduler built around the atomic :class:`Book`.

Interactive requests and C04 exclusive sessions are decided under the same
condition; every lifecycle I/O (load, drain, stop) happens outside it, and all
session lifecycle I/O runs in one worker so two clients can never both become
ACTIVE. A session that cannot get the model to itself within the drain window
yields and retries later without losing its queue position, and a session that
holds a lease never has it killed.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import inspect
from time import monotonic
from typing import Callable, Mapping

from .contracts import ControlRecoveryPort, Lease, MemorySample, Observation, Outcome, Presence
from .eviction_policy import EvictionPolicy
from .model_registry import Book, Conflict, StaleOperation
from .request_queue import RequestQueue, WaitKind, WaitState
from .session_manager import ACTIVE, BLOCKED, CLOSED, DRAINING, PREPARING, SessionConflict, SessionManager, SessionNotFound


class QueueFull(RuntimeError):
    pass


class ModelUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class SwitchIntent:
    request_id: str
    target_id: str
    frozen_models: tuple[str, ...]
    expires_at: float


class ModelScheduler:
    """Coordinates shared loads and leases without ever awaiting under its lock."""

    def __init__(self, book: Book, resources, backend, *, queue_capacity: int = 128, priority_aging_seconds: float = 30, poll_interval_seconds: float = 1, max_evictions: int = 8, switch_drain_timeout_seconds: float = 30, switch_retry_seconds: float = 30, switch_window_seconds: float = 10, max_switches_in_window: int = 3, cooldown_seconds: float = 15, recovery: ControlRecoveryPort | None = None, admission_guard=None, sessions: SessionManager | None = None, clock: Callable[[], float] = monotonic, event_sink: Callable[[str, Mapping[str, object]], None] | None = None):
        if min(poll_interval_seconds, switch_drain_timeout_seconds, switch_retry_seconds, switch_window_seconds, cooldown_seconds) <= 0 or max_switches_in_window < 1:
            raise ValueError("scheduler intervals must be positive")
        self.book = book
        self.resources = resources
        self.backend = backend
        self.recovery = recovery
        self.admission_guard = admission_guard
        self.queue_capacity = queue_capacity
        self.poll_interval_seconds = poll_interval_seconds
        self.eviction_policy = EvictionPolicy(book, max_evictions=max_evictions)
        self._condition = asyncio.Condition()
        self._queue = RequestQueue(capacity=queue_capacity, aging_seconds=priority_aging_seconds)
        self._loads: dict[str, asyncio.Task[None]] = {}
        self._eviction: asyncio.Task[tuple[str, ...]] | None = None
        self._shutting_down = False
        self._storage_unavailable = False
        self._switch_drain_timeout_seconds = switch_drain_timeout_seconds
        self._switch_retry_seconds = switch_retry_seconds
        self._switch_intent: SwitchIntent | None = None
        self._next_switch_attempt: dict[str, float] = {}
        self._switch_window_seconds = switch_window_seconds
        self._max_switches_in_window = max_switches_in_window
        self._cooldown_seconds = cooldown_seconds
        self._switch_successes: deque[float] = deque()
        self._cold_load_not_before = 0.0
        self._recovery_task: asyncio.Task[None] | None = None
        self.sessions = sessions if sessions is not None else SessionManager()
        self._clock = clock
        self.event_sink = event_sink
        self._session_worker: asyncio.Task[None] | None = None
        self._session_leases: dict[str, set[str]] = {}
        self._session_freeze: str | None = None
        # P14: the execution service installs a drain hook; the scheduler calls it
        # outside its condition so queue cancellation never blocks the single lock.
        self.execution_hook = None

    async def _notify_execution_drain(self, session_id: str) -> None:
        """Tell the execution service (if wired) that a session started draining."""
        hook = self.execution_hook
        if hook is None:
            return
        try:
            await hook(session_id)
        except Exception as exc:
            self._emit("execution_hook_failed", {"session_id": session_id, "error": type(exc).__name__})

    async def acquire(self, model_id: str, request_id: str, deadline: float, *, session_id: str | None = None) -> Lease:
        if model_id not in self.book.specs:
            raise KeyError(model_id)
        if deadline <= self._clock():
            raise TimeoutError("queue deadline elapsed")
        async with self._condition:
            if self._shutting_down:
                raise ModelUnavailable("service_shutting_down")
            if self._storage_unavailable:
                raise ModelUnavailable("storage_unavailable")
            if session_id is not None:
                record = self.sessions.get(session_id)
                if record.model_id != model_id or record.phase != ACTIVE:
                    raise SessionConflict("session_not_active")
                if not self.sessions.is_live(session_id, self._clock()):
                    raise SessionConflict("session_expired")
                runtime = self.book.runtime[model_id]
                if runtime.state.value == "ready" and len(runtime.leases) >= self.book.ledger[model_id].max_concurrency:
                    raise Conflict("session_slots_exhausted")
            elif self._session_freeze is not None:
                # A session is being granted or holds the model exclusively.
                raise Conflict("session_exclusive")
            try:
                kind = WaitKind.SESSION if session_id is not None else WaitKind.INTERACTIVE
                self._queue.enqueue(request_id, model_id, self.book.specs[model_id].priority, deadline, self._clock(), kind=kind)
            except ValueError as exc:
                raise Conflict("duplicate waiter") from exc
            except OverflowError as exc:
                raise QueueFull("queue_full")
        try:
            while True:
                snapshot = await self.resources.snapshot()
                sample = MemorySample(snapshot.total_bytes, snapshot.available_bytes, snapshot.sampled_at)
                admitted = await self._admission_allowed()
                needs_sample = False
                async with self._condition:
                    now = self._clock()
                    self._expire_switch_intent(now)
                    if self._shutting_down:
                        raise ModelUnavailable("service_shutting_down")
                    if self._storage_unavailable:
                        raise ModelUnavailable("storage_unavailable")
                    if now >= deadline or not self._queue.contains(request_id):
                        self._queue.remove(request_id, WaitState.EXPIRED)
                        raise TimeoutError("queue deadline elapsed")
                    if self._queue.is_head(request_id, now, eligible=self._cold_request_eligible):
                        try:
                            if not admitted or not self.book.can_admit_ready(model_id, sample, now):
                                raise Conflict("ready admission blocked")
                            lease = self.book.acquire_ready(model_id, request_id, now)
                        except Conflict:
                            runtime = self.book.runtime[model_id]
                            if runtime.state.value == "error":
                                raise ModelUnavailable(runtime.last_error or "model_error")
                            needs_sample = (runtime.state.value == "unloaded" and model_id not in self._loads
                                            and self._eviction is None
                                            and (self._switch_intent is None or self._switch_intent.request_id == request_id)
                                            and now >= self._next_switch_attempt.get(model_id, 0))
                        else:
                            self._queue.remove(request_id, WaitState.CLAIMED)
                            if session_id is not None:
                                self._note_session_lease(session_id, lease)
                            return lease
                if needs_sample:
                    # Sampling is external I/O; take it outside the condition and
                    # re-check state before committing the operation below.
                    started_operation = False
                    async with self._condition:
                        runtime = self.book.runtime[model_id]
                        if admitted and runtime.state.value == "unloaded" and model_id not in self._loads:
                            now = self._clock()
                            if self.book.can_load(model_id, sample, now):
                                operation = self.book.begin_load(model_id, sample, now)
                                self._loads[model_id] = asyncio.create_task(self._finish_load(operation, deadline))
                                started_operation = True
                            elif self._eviction is None:
                                started_operation = self._establish_or_advance_switch(request_id, model_id, sample, deadline, now)
                        self._condition.notify_all()
                    if started_operation:
                        continue
                async with self._condition:
                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        raise TimeoutError("queue deadline elapsed")
                    try:
                        await asyncio.wait_for(self._condition.wait(), min(remaining, self.poll_interval_seconds))
                    except asyncio.TimeoutError as exc:
                        if self._clock() >= deadline:
                            raise TimeoutError("queue deadline elapsed") from exc
        finally:
            async with self._condition:
                self._queue.remove(request_id)
                self._cancel_switch_intent(request_id)
                self._condition.notify_all()

    def _cold_request_eligible(self, item) -> bool:
        if item.kind is WaitKind.INTERACTIVE and self._session_freeze is not None:
            return False  # an exclusive grant in progress holds the model
        runtime = self.book.runtime[item.model_id]
        if runtime.state.value == "loading" or runtime.state.value == "evicting":
            return False
        if runtime.state.value == "unloaded":
            now = self._clock()
            return now >= self._cold_load_not_before and now >= self._next_switch_attempt.get(item.model_id, 0)
        return True

    def _establish_or_advance_switch(self, request_id: str, model_id: str, sample: MemorySample, deadline: float, now: float) -> bool:
        """Freeze one complete eviction set, then stop it only after it drains."""
        intent = self._switch_intent
        if intent is None:
            candidates = self.eviction_policy.choose(
                self._load_deficit(model_id, sample), now=now, include_busy=True
            )
            if not candidates:
                return False
            models = tuple(candidate.model_id for candidate in candidates)
            try:
                self.book.freeze_for_switch(list(models))
            except Conflict:
                return False
            self._switch_intent = SwitchIntent(
                request_id, model_id, models,
                min(deadline, now + self._switch_drain_timeout_seconds),
            )
            intent = self._switch_intent
        if intent.request_id != request_id:
            return False
        if any(self.book.runtime[model_id].leases for model_id in intent.frozen_models):
            return False
        try:
            operations = self.book.begin_eviction(list(intent.frozen_models))
        except Conflict:
            self._cancel_switch_intent(intent.request_id)
            return False
        self._switch_intent = None
        self._eviction = asyncio.create_task(self._run_eviction(operations, deadline))
        return True

    def _cancel_switch_intent(self, request_id: str) -> None:
        intent = self._switch_intent
        if intent is not None and intent.request_id == request_id:
            self.book.unfreeze_switch(list(intent.frozen_models))
            self._switch_intent = None

    def _expire_switch_intent(self, now: float) -> None:
        intent = self._switch_intent
        if intent is not None and (now >= intent.expires_at or not self._queue.contains(intent.request_id)):
            self.book.unfreeze_switch(list(intent.frozen_models))
            self._next_switch_attempt[intent.target_id] = now + self._switch_retry_seconds
            self._switch_intent = None

    async def _finish_load(self, operation, deadline: float) -> None:
        recover = False
        try:
            observation: Observation = await self.backend.load(operation, deadline)
            async with self._condition:
                if observation.presence is Presence.RUNNING and observation.healthy:
                    now = self._clock()
                    try:
                        self.book.loaded(operation, now)
                    except StaleOperation:
                        # A late success for a superseded operation must not move the books.
                        self._emit_writeback_rejection("load", operation, "stale_operation")
                    else:
                        self._record_cold_load(now)
                else:
                    self.book.failed(operation, observation.detail_code or "load_unverified")
                    recover = self.recovery is not None and not self.book.recovering
                self._condition.notify_all()
        except (Exception, asyncio.CancelledError) as exc:
            async with self._condition:
                try:
                    self.book.failed(operation, "load_failed")
                    recover = self.recovery is not None and not self.book.recovering
                except StaleOperation:
                    pass
                self._condition.notify_all()
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            async with self._condition:
                self._loads.pop(operation.model_id, None)
                self._condition.notify_all()
            if recover:
                self._start_recovery(deadline)

    def _record_cold_load(self, now: float) -> None:
        self._switch_successes.append(now)
        while self._switch_successes and now - self._switch_successes[0] > self._switch_window_seconds:
            self._switch_successes.popleft()
        if len(self._switch_successes) >= self._max_switches_in_window:
            self._cold_load_not_before = max(
                self._cold_load_not_before,
                now + self._cooldown_seconds,
                self._switch_successes[0] + self._switch_window_seconds,
            )

    def _start_recovery(self, deadline: float) -> None:
        if self._recovery_task is None or self._recovery_task.done():
            self._recovery_task = asyncio.create_task(self._recover_after_unverified_load(deadline))

    async def _recover_after_unverified_load(self, deadline: float) -> None:
        """Use the narrow recovery port after a control action has uncertain state."""
        try:
            async with self._condition:
                while any(runtime.leases for runtime in self.book.runtime.values()):
                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        for runtime in self.book.runtime.values():
                            for lease in tuple(runtime.leases.values()):
                                self.book.release(lease, Outcome.ABORTED, self._clock())
                        break
                    try:
                        await asyncio.wait_for(self._condition.wait(), remaining)
                    except asyncio.TimeoutError:
                        continue
            await self.recover(deadline)
        except (Conflict, ModelUnavailable):
            # The failed model remains ERROR and admissions stay conservative.
            pass
        finally:
            async with self._condition:
                self._recovery_task = None
                self._condition.notify_all()

    async def _admission_allowed(self) -> bool:
        if self.admission_guard is None:
            return True
        try:
            value = self.admission_guard()
            if inspect.isawaitable(value):
                value = await value
            return value is True
        except Exception:
            return False

    async def monitor_storage_once(self, deadline: float) -> bool:
        """Poll the injected storage guard and execute one fail-closed transition."""
        if await self._admission_allowed():
            return not self._storage_unavailable
        await self.storage_lost(deadline)
        return False

    def _load_deficit(self, model_id: str, sample: MemorySample) -> int:
        required = self.book.required(model_id)
        return max(0, self.book.free_floor + required - sample.available_bytes,
                   self.book.committed + required - self.book.model_budget)

    async def _run_eviction(self, operations, deadline: float) -> tuple[str, ...]:
        try:
            return await self._finish_eviction(operations, deadline)
        finally:
            async with self._condition:
                self._eviction = None
                self._condition.notify_all()

    async def _finish_eviction(self, operations, deadline: float) -> tuple[str, ...]:
        stopped: list[str] = []
        for index, operation in enumerate(operations):
            try:
                observation = await self.backend.stop(operation, deadline)
            except asyncio.CancelledError:
                async with self._condition:
                    self.book.failed(operation, "stop_unverified")
                    self._rollback_pending(operations[index + 1:])
                    self._condition.notify_all()
                raise
            except Exception:
                observation = None
            async with self._condition:
                if observation is not None and observation.presence is Presence.STOPPED:
                    try:
                        self.book.stopped(operation, self._clock())
                    except StaleOperation:
                        self._emit_writeback_rejection("stop", operation, "stale_operation")
                    else:
                        stopped.append(operation.model_id)
                else:
                    self.book.failed(operation, observation.detail_code if observation else "stop_unverified")
                    self._rollback_pending(operations[index + 1:])
                    self._condition.notify_all()
                    return tuple(stopped)
                self._condition.notify_all()
        return tuple(stopped)

    async def release(self, lease: Lease, outcome: Outcome, tokens: int | None = None) -> None:
        async with self._condition:
            self.book.release(lease, outcome, self._clock(), tokens)
            self._forget_session_lease(lease.lease_id)
            self._condition.notify_all()

    async def preload(self, deadline: float) -> tuple[str, ...]:
        """Bring configured resident models to READY without granting user leases."""
        ready: list[str] = []
        for model_id, spec in self.book.specs.items():
            if spec.preload:
                await self._preload_one(model_id, deadline)
                ready.append(model_id)
        return tuple(ready)

    async def _preload_one(self, model_id: str, deadline: float) -> None:
        while True:
            needs_sample = False
            async with self._condition:
                if self._storage_unavailable:
                    raise ModelUnavailable("storage_unavailable")
                runtime = self.book.runtime[model_id]
                if runtime.state.value == "ready":
                    return
                if runtime.state.value == "error":
                    raise ModelUnavailable(runtime.last_error or "preload_failed")
                needs_sample = (runtime.state.value == "unloaded" and not self._loads and self._eviction is None)
            if needs_sample:
                snapshot = await self.resources.snapshot()
                sample = MemorySample(snapshot.total_bytes, snapshot.available_bytes, snapshot.sampled_at)
                admitted = await self._admission_allowed()
                started_operation = False
                async with self._condition:
                    runtime = self.book.runtime[model_id]
                    now = self._clock()
                    if admitted and runtime.state.value == "unloaded" and not self._loads and self._eviction is None:
                        if self.book.can_load(model_id, sample, now):
                            operation = self.book.begin_load(model_id, sample, now)
                            self._loads[model_id] = asyncio.create_task(self._finish_load(operation, deadline))
                            started_operation = True
                        else:
                            candidates = self.eviction_policy.choose(self._load_deficit(model_id, sample), now=now)
                            if candidates:
                                operations = self.book.begin_eviction([candidate.model_id for candidate in candidates])
                                self._eviction = asyncio.create_task(self._run_eviction(operations, deadline))
                                started_operation = True
                    self._condition.notify_all()
                if started_operation:
                    continue
            async with self._condition:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise TimeoutError("preload deadline elapsed")
                try:
                    await asyncio.wait_for(self._condition.wait(), min(remaining, self.poll_interval_seconds))
                except asyncio.TimeoutError as exc:
                    if self._clock() >= deadline:
                        raise TimeoutError("preload deadline elapsed") from exc

    async def recover(self, deadline: float) -> None:
        """Reconcile all model accounting through the injected root-owned helper."""
        if self.recovery is None:
            raise ModelUnavailable("control_recovery_unavailable")
        async with self._condition:
            if self.book.recovering:
                raise Conflict("recovery_in_progress")
            if any(runtime.leases for runtime in self.book.runtime.values()):
                raise Conflict("recovery_has_leases")
            epoch = self.book.begin_recovery()
            self._condition.notify_all()
        result = await self.recovery.recover(deadline)
        async with self._condition:
            if not result.ok:
                self._condition.notify_all()
                raise ModelUnavailable(result.error_code or "control_recovery_failed")
            try:
                self.book.finish_recovery(epoch, frozenset(result.stopped_models))
            except Conflict as exc:
                self._condition.notify_all()
                raise ModelUnavailable("control_recovery_incomplete") from exc
            self._condition.notify_all()

    async def shutdown(self, deadline: float) -> tuple[str, ...]:
        """Close admission, drain leases to deadline, then stop managed models."""
        async with self._condition:
            self._shutting_down = True
            self._queue.clear()
            draining = [session_id for session_id, record in self.sessions.records.items() if record.phase != CLOSED]
            for session_id in draining:
                self.sessions.begin_drain(session_id, self._clock(), "service_shutdown")
                self._release_session_freeze(session_id)
            self._condition.notify_all()
        for session_id in draining:  # P14: cancel the sessions' executions before draining leases
            await self._notify_execution_drain(session_id)
        async with self._condition:
            while any(runtime.leases for runtime in self.book.runtime.values()) and self._clock() < deadline:
                try:
                    await asyncio.wait_for(self._condition.wait(), deadline - self._clock())
                except asyncio.TimeoutError:
                    break
            for runtime in self.book.runtime.values():
                for lease in tuple(runtime.leases.values()):
                    self.book.release(lease, Outcome.ABORTED, self._clock())
            if self._eviction is not None:
                task = self._eviction
            else:
                targets = [model_id for model_id, runtime in self.book.runtime.items() if runtime.state.value in {"ready", "error"} and not runtime.leases]
                if not targets:
                    return ()
                operations = self.book.begin_cleanup(targets)
                task = asyncio.create_task(self._run_eviction(operations, deadline))
                self._eviction = task
            self._condition.notify_all()
        return await task

    async def storage_lost(self, deadline: float) -> tuple[str, ...]:
        """Fail closed on a verified SSD fault, then stop every managed model.

        This is intentionally distinct from normal shutdown: the scheduler
        remains live for diagnostics but rejects all inference until an
        explicit recovery/restart validates storage again.
        """
        async with self._condition:
            if self._storage_unavailable:
                return ()
            self._storage_unavailable = True
            self._queue.clear()
            for runtime in self.book.runtime.values():
                for lease in tuple(runtime.leases.values()):
                    self.book.release(lease, Outcome.ABORTED, self._clock())
            if self._eviction is not None:
                task = self._eviction
            else:
                targets = [
                    model_id for model_id, runtime in self.book.runtime.items()
                    if runtime.state.value in {"ready", "error"} and not runtime.leases
                ]
                if not targets:
                    self._condition.notify_all()
                    return ()
                operations = self.book.begin_cleanup(targets)
                task = asyncio.create_task(self._run_eviction(operations, deadline))
                self._eviction = task
            self._condition.notify_all()
        return await task

    async def storage_recovered(self, deadline: float) -> tuple[str, ...]:
        """Explicitly reopen storage only after fresh validation and recovery."""
        if not await self._admission_allowed():
            raise ModelUnavailable("storage_unavailable")
        async with self._condition:
            if not self._storage_unavailable:
                return ()
        await self.recover(deadline)
        async with self._condition:
            self._storage_unavailable = False
            self._condition.notify_all()
        return await self.preload(deadline)

    async def unload(self, model_id: str, deadline: float) -> None:
        async with self._condition:
            if model_id not in self.book.specs: raise KeyError(model_id)
            spec, runtime = self.book.specs[model_id], self.book.runtime[model_id]
            if spec.pinned: raise Conflict("model_pinned")
            if runtime.state.value == "unloaded": return
            if self._eviction is not None or runtime.leases or runtime.operation_id or runtime.state.value != "ready": raise Conflict("model_busy")
            operation = self.book.begin_eviction([model_id], automatic=False)[0]
            task = asyncio.create_task(self._run_eviction([operation], deadline))
            self._eviction = task
            self._condition.notify_all()
        stopped = await task
        if model_id not in stopped:
            raise ModelUnavailable("stop_unverified")
            self._condition.notify_all()

    async def sweep_ttl(self, deadline: float) -> tuple[str, ...]:
        """Stop due idle models, without allowing TTL to outrun queued demand."""
        now = self._clock()
        async with self._condition:
            if self._eviction is not None:
                return ()
            waiting_models = self._queue.waiting_models()
            model_ids = [
                model_id for model_id in self.book.specs
                if model_id not in waiting_models and self.book.ttl_due(model_id, now)
            ]
            if not model_ids:
                return ()
            operations = self.book.begin_eviction(model_ids)
            task = asyncio.create_task(self._run_eviction(operations, deadline))
            self._eviction = task
            self._condition.notify_all()
        return await task

    def _emit(self, kind: str, payload: Mapping[str, object]) -> None:
        if self.event_sink is None:
            return
        try:
            self.event_sink(kind, payload)
        except Exception:
            # Evidence collection must never break a lifecycle transition.
            pass

    def _emit_writeback_rejection(self, stage: str, operation, reason: str) -> None:
        self._emit("writeback_rejected", {
            "stage": stage,
            "reason": reason,
            "operation_id": operation.operation_id,
            "model_id": operation.model_id,
            "generation": operation.generation,
            "epoch": operation.epoch,
        })

    async def cancel(self, lease: Lease) -> bool:
        """Accept one cancel; the lease, the slot and the budget stay until it ends."""
        async with self._condition:
            accepted = self.book.begin_cancel(lease)
            self._condition.notify_all()
            return accepted

    # -- C04 exclusive sessions ---------------------------------------------

    def _note_session_lease(self, session_id: str, lease: Lease) -> None:
        leases = self._session_leases.setdefault(session_id, set())
        leases.add(lease.lease_id)
        self.sessions.in_flight[session_id] = len(leases)

    def _forget_session_lease(self, lease_id: str) -> None:
        for session_id, leases in self._session_leases.items():
            if lease_id in leases:
                leases.discard(lease_id)
                self.sessions.in_flight[session_id] = len(leases)
                return

    def _release_session_freeze(self, session_id: str) -> None:
        if self._session_freeze == session_id:
            self._session_freeze = None

    def _exclusive_conflict(self, model_id: str) -> str | None:
        """pinned/preload registrations conflict with exclusive residency (C04)."""
        if any(entry.pinned or entry.preload for entry in self.book.ledger.values()):
            return "pinned_or_preload_conflict"
        return None

    def _ensure_session_worker(self) -> None:
        if self._session_worker is None or self._session_worker.done():
            self._session_worker = asyncio.create_task(self._run_session_lifecycle())

    async def register_session(self, model_id: str, client_id: str, session_id: str, *, priority: int = 0,
                               hard_deadline_seconds: float | None = None) -> dict[str, object]:
        """Register one PREPARING session and let the lifecycle worker load it (no wait).

        This is the P18 control-route semantics ("202 session handle, queue the
        load"): the caller gets the fresh view and polls; `open_session` keeps
        the waiting behaviour for the in-process callers that need ACTIVE.
        """
        async with self._condition:
            if model_id not in self.book.specs:
                raise KeyError(model_id)
            if self._shutting_down:
                raise ModelUnavailable("service_shutting_down")
            if self._storage_unavailable:
                raise ModelUnavailable("storage_unavailable")
            conflict = self._exclusive_conflict(model_id)
            if conflict is not None:
                raise SessionConflict(conflict)
            self.sessions.create(
                session_id, model_id, client_id,
                now=self._clock(), queue=self._queue, priority=priority,
                hard_deadline_seconds=hard_deadline_seconds,
            )
            self._ensure_session_worker()
            self._condition.notify_all()
            return self.sessions.view(session_id, now=self._clock())

    async def open_session(self, model_id: str, client_id: str, session_id: str, *, priority: int = 0, hard_deadline_seconds: float | None = None, deadline: float | None = None) -> dict[str, object]:
        """Register a session and wait for it to become ACTIVE (or fail closed)."""
        await self.register_session(model_id, client_id, session_id, priority=priority,
                                    hard_deadline_seconds=hard_deadline_seconds)
        async with self._condition:
            record = self.sessions.get(session_id)
        wait_until = record.wait_deadline if deadline is None else min(record.wait_deadline, deadline)
        while True:
            async with self._condition:
                current = self.sessions.get(session_id)
                if current.phase == ACTIVE:
                    return self.sessions.view(session_id, now=self._clock())
                if current.phase == CLOSED:
                    raise SessionConflict("session_closed")
                if current.phase == BLOCKED:
                    raise ModelUnavailable("session_blocked")
                remaining = min(wait_until, current.preparation_expired_at) - self._clock()
                if remaining <= 0:
                    raise TimeoutError("session_wait_deadline")
                try:
                    await asyncio.wait_for(self._condition.wait(), min(remaining, self.poll_interval_seconds))
                except asyncio.TimeoutError:
                    continue

    async def heartbeat_session(self, session_id: str) -> dict[str, object]:
        async with self._condition:
            self.sessions.heartbeat(session_id, self._clock())
            self._condition.notify_all()
            return self.sessions.view(session_id, now=self._clock())

    async def close_session(self, session_id: str, *, reason: str = "client_close", deadline: float | None = None) -> dict[str, object]:
        """Drain, stop and close; idempotent, and BLOCKED keeps the slot and budget."""
        async with self._condition:
            record = self.sessions.get(session_id)
            if record.phase == CLOSED:
                return self.sessions.view(session_id, now=self._clock())
            self.sessions.begin_drain(session_id, self._clock(), reason)
            self._queue.remove(session_id, WaitState.CANCELLED)
            self._ensure_session_worker()
            self._condition.notify_all()
        await self._notify_execution_drain(session_id)
        while True:
            async with self._condition:
                current = self.sessions.get(session_id)
                if current.phase in {CLOSED, BLOCKED}:
                    return self.sessions.view(session_id, now=self._clock())
                if deadline is not None and self._clock() >= deadline:
                    return self.sessions.view(session_id, now=self._clock())
                try:
                    await asyncio.wait_for(self._condition.wait(), self.poll_interval_seconds)
                except asyncio.TimeoutError:
                    continue

    async def session_view(self, session_id: str) -> dict[str, object]:
        async with self._condition:
            return self.sessions.view(session_id, now=self._clock())

    async def _run_session_lifecycle(self) -> None:
        """The single session worker: every lifecycle step runs here, never under the lock."""
        try:
            while True:
                plan = await self._next_session_plan()
                if plan is None:
                    async with self._condition:
                        if not self.sessions.pending():
                            return
                        try:
                            await asyncio.wait_for(self._condition.wait(), self.poll_interval_seconds)
                        except asyncio.TimeoutError:
                            pass
                    continue
                action, session_id = plan
                if action == "expire":
                    await self._expire_session(session_id)
                elif action == "prepare":
                    await self._prepare_session(session_id)
                else:  # close and reconcile share the same cleanup path
                    await self._drain_session(session_id)
        finally:
            async with self._condition:
                self._session_worker = None
                self._condition.notify_all()

    async def _next_session_plan(self) -> tuple[str, str] | None:
        async with self._condition:
            now = self._clock()
            for record in self.sessions.expired(now):
                if record.phase == PREPARING:
                    self._queue.remove(record.session_id, WaitState.EXPIRED)
                return ("expire", record.session_id)
            for record in self.sessions.blocked_due(now):
                return ("reconcile", record.session_id)
            head = self._queue.head(
                now,
                kind=WaitKind.SESSION,
                eligible=lambda item: self.sessions.candidate(item.request_id, now) is not None,
            )
            if head is not None:
                return ("prepare", head.request_id)
            for record in self.sessions.records.values():
                if record.phase == DRAINING:
                    return ("close", record.session_id)
            return None

    async def _expire_session(self, session_id: str) -> None:
        async with self._condition:
            record = self.sessions.get(session_id)
            if record.phase == PREPARING:
                self._queue.remove(session_id, WaitState.EXPIRED)
                self.sessions.mark_closed(session_id, self._clock())
                self._release_session_freeze(session_id)
                self._condition.notify_all()
                return
            self.sessions.begin_drain(session_id, self._clock(), "expired")
            self._condition.notify_all()
        await self._notify_execution_drain(session_id)

    async def _prepare_session(self, session_id: str) -> None:
        async with self._condition:
            record = self.sessions.get(session_id)
            if record.phase != PREPARING:
                return
            model_id = record.model_id
            prepare_deadline = record.preparation_expired_at
            self._session_freeze = session_id
            drain_deadline = self._clock() + self.sessions.drain_seconds
            self._condition.notify_all()
        if not await self._wait_for_leases_to_drain(drain_deadline):
            # Undo the freeze, keep the waiter, retry after the backoff (C04).
            async with self._condition:
                self.sessions.yield_prepare(session_id, self._clock())
                self._release_session_freeze(session_id)
                self._condition.notify_all()
            return
        try:
            await self._stop_other_models(model_id, min(prepare_deadline, self._clock() + self.sessions.cleanup_seconds))
            await self._preload_one(model_id, min(prepare_deadline, self._clock() + self.sessions.prepare_seconds))
        except (Conflict, ModelUnavailable, TimeoutError):
            async with self._condition:
                self.sessions.mark_blocked(session_id, self._clock(), "prepare_unconfirmed")
                self._condition.notify_all()
            return
        async with self._condition:
            record = self.sessions.get(session_id)
            activate = record.phase == PREPARING
            if activate:
                self.sessions.mark_active(session_id, self._clock())
                self._queue.remove(session_id, WaitState.GRANTED)
            ended_model_id = record.model_id
            self._condition.notify_all()
        if not activate:
            # The session ended while its model was still loading: clean the
            # instance up instead of reviving a session nobody owns any more.
            await self._stop_models([ended_model_id], self._clock() + self.sessions.stop_grace_seconds)
            async with self._condition:
                self.sessions.mark_closed(session_id, self._clock())
                self._release_session_freeze(session_id)
                self._condition.notify_all()

    async def _drain_session(self, session_id: str) -> None:
        async with self._condition:
            record = self.sessions.get(session_id)
            model_id = record.model_id
            cancel_deadline = self._clock() + self.sessions.cancel_seconds
            cleanup_deadline = self._clock() + self.sessions.stop_grace_seconds
        await self._wait_for_session_leases(session_id, cancel_deadline)
        confirmed = await self._stop_models([model_id], cleanup_deadline)
        async with self._condition:
            if confirmed:
                self.sessions.mark_closed(session_id, self._clock())
                self._release_session_freeze(session_id)
            else:
                self.sessions.mark_blocked(session_id, self._clock(), "stop_unconfirmed")
            self._condition.notify_all()

    async def _wait_for_leases_to_drain(self, deadline: float) -> bool:
        while True:
            async with self._condition:
                if not any(runtime.leases for runtime in self.book.runtime.values()):
                    return True
                if self._clock() >= deadline:
                    return False
                try:
                    await asyncio.wait_for(self._condition.wait(), self.poll_interval_seconds)
                except asyncio.TimeoutError:
                    continue

    async def _wait_for_session_leases(self, session_id: str, deadline: float) -> bool:
        while True:
            async with self._condition:
                record = self.sessions.get(session_id)
                if not self._session_leases.get(session_id) and not self.book.runtime[record.model_id].leases:
                    return True
                if self._clock() >= deadline:
                    return False
                try:
                    await asyncio.wait_for(self._condition.wait(), self.poll_interval_seconds)
                except asyncio.TimeoutError:
                    continue

    async def _stop_other_models(self, keep_model_id: str, deadline: float) -> None:
        async with self._condition:
            targets = [
                model_id for model_id, runtime in self.book.runtime.items()
                if model_id != keep_model_id and runtime.state.value in {"ready", "error"} and not runtime.leases
            ]
        if not targets:
            return
        if not await self._stop_models(targets, deadline):
            raise ModelUnavailable("stop_unconfirmed")

    async def _stop_models(self, model_ids: list[str], deadline: float) -> bool:
        """One serialized stop batch; True only when every model is proven stopped."""
        async with self._condition:
            if self._eviction is not None:
                task = self._eviction
                borrowed = True
            else:
                targets = [
                    model_id for model_id in model_ids
                    if self.book.runtime[model_id].state.value in {"ready", "error"} and not self.book.runtime[model_id].leases
                ]
                if not targets:
                    return True
                operations = self.book.begin_cleanup(targets)
                task = asyncio.create_task(self._run_eviction(operations, deadline))
                self._eviction = task
                borrowed = False
                self._condition.notify_all()
        await task
        if borrowed:
            return True  # the in-flight batch owns the stop; the next attempt re-checks
        async with self._condition:
            return all(self.book.runtime[model_id].state.value == "unloaded" for model_id in model_ids)

    def _rollback_pending(self, operations) -> None:
        for operation in operations:
            try:
                self.book.rollback_unsent(operation)
            except StaleOperation:
                pass

    async def status(self) -> dict[str, object]:
        snapshot = await self.resources.snapshot()
        async with self._condition:
            now = self._clock()
            total, available = snapshot.total_bytes, snapshot.available_bytes
            sampled_at = snapshot.sampled_at
            sample_age = snapshot.age_at(now) if callable(getattr(snapshot, "age_at", None)) else max(0.0, now - sampled_at)
            intent = self._switch_intent
            return {
                "resources": {
                    "total_bytes": total,
                    "available_bytes": available,
                    "used_bytes": getattr(snapshot, "used_bytes", total - available),
                    "utilization": getattr(snapshot, "utilization", 1 - available / total if total else 1),
                    "source": getattr(snapshot, "source", "unknown"),
                    "sample_age_seconds": sample_age,
                },
                "admission": {
                    "shutting_down": self._shutting_down,
                    "storage_unavailable": self._storage_unavailable,
                    "recovering": self.book.recovering,
                    "cold_load_not_before": self._cold_load_not_before,
                    "switch": None if intent is None else {
                        "target_id": intent.target_id,
                        "frozen_models": list(intent.frozen_models),
                        "expires_in_seconds": max(0.0, intent.expires_at - now),
                    },
                },
                "queue_size": self._queue.size,
                "sessions": {
                    "active_id": self.sessions.active_id,
                    "frozen": self._session_freeze is not None,
                    "pending": [
                        self.sessions.view(session_id, now=now)
                        for session_id, record in self.sessions.records.items() if record.phase != CLOSED
                    ],
                },
                "models": {
                    model_id: {
                        "state": "active" if runtime.state.value == "ready" and runtime.leases else runtime.state.value,
                        "generation": runtime.generation,
                        "in_flight": len(runtime.leases),
                        "cancelling": len(runtime.cancelling),
                        "waiting_requests": self._queue.waiting_count(model_id),
                        "capabilities": sorted(capability.value for capability in self.book.specs[model_id].capabilities),
                        "reserved_bytes": self.book.specs[model_id].reserved_bytes,
                        "effective_reserved_bytes": self.book.required(model_id),
                        "priority": self.book.specs[model_id].priority,
                        "evictable": self.book.specs[model_id].evictable,
                        "pinned": self.book.specs[model_id].pinned,
                        "preload": self.book.specs[model_id].preload,
                        "max_concurrency": self.book.specs[model_id].max_concurrency,
                        "ttl_seconds": self.book.specs[model_id].ttl_seconds,
                        "idle_seconds": None if runtime.leases or runtime.idle_since is None else max(0.0, now - runtime.idle_since),
                        "heat": self.book.heat(model_id, now),
                        "total_requests": runtime.total_requests,
                        "total_tokens": runtime.total_tokens,
                        "usage_unknown_requests": runtime.usage_unknown_requests,
                        "admission_blocked": runtime.admission_blocked,
                        "last_error": runtime.last_error,
                    }
                    for model_id, runtime in self.book.runtime.items()
                },
            }
