"""C04 exclusive sessions: one model, one client, one serialized lifecycle.

The manager is a pure state machine. Every method is synchronous, performs no
I/O and takes the caller's clock reading, so the scheduler can decide interactive
and session grants under its single condition and run the actual lifecycle I/O in
one worker afterwards.

Rules encoded here (plan/02-scheduler.md §3):

* a session is `PREPARING` while it waits for exclusive residency, `ACTIVE` while
  it holds the model, `DRAINING` during cleanup and `CLOSED` only once the stop is
  confirmed; a cleanup that cannot be confirmed becomes `BLOCKED` and keeps the
  slot and the budget until it is reconciled;
* at most one session holds the model: two racing clients can never both be
  `ACTIVE`;
* the wait budget, the preparation budget and the hard deadline are configuration,
  a heartbeat only refreshes the soft TTL, and a drain yield keeps the waiter in
  the queue with its original sequence and deadline (the queue owns the ordering);
* pinned or preload registrations conflict with exclusive residency;
  the scheduler refuses the session instead of removing the configuration.
"""
from __future__ import annotations

from dataclasses import dataclass

from .request_queue import RequestQueue, WaitKind

PREPARING = "preparing"
ACTIVE = "active"
DRAINING = "draining"
CLOSED = "closed"
BLOCKED = "blocked"
SESSION_PHASES = frozenset({PREPARING, ACTIVE, DRAINING, CLOSED, BLOCKED})

# A session that holds or is releasing the model keeps its slot and budget.
_HOLDING_PHASES = frozenset({ACTIVE, DRAINING, BLOCKED})


class SessionError(RuntimeError):
    """Base class for session refusals; the API layer maps these to statuses."""


class SessionConflict(SessionError):
    """The request contradicts session state (pinned/preload, duplicate, racing)."""


class SessionNotFound(SessionError):
    """No session with that id exists."""


@dataclass
class SessionRecord:
    session_id: str
    model_id: str
    client_id: str
    priority: int
    created_at: float
    wait_deadline: float
    prepare_deadline: float
    hard_deadline: float
    ttl_seconds: float
    phase: str = PREPARING
    heartbeat_at: float | None = None
    activated_at: float | None = None
    draining_at: float | None = None
    draining_reason: str | None = None
    closed_at: float | None = None
    blocked_at: float | None = None
    blocked_reason: str | None = None
    retry_at: float = 0.0
    sequence: int = 0

    @property
    def expires_at(self) -> float:
        """Soft TTL from the last activity, never beyond the hard deadline."""
        activity = self.heartbeat_at if self.heartbeat_at is not None else (
            self.activated_at if self.activated_at is not None else self.created_at
        )
        return min(self.hard_deadline, activity + self.ttl_seconds)

    @property
    def preparation_expired_at(self) -> float:
        return min(self.wait_deadline, self.prepare_deadline)


class SessionManager:
    """The single owner of session state; time and the queue are passed in."""

    def __init__(
        self,
        *,
        max_sessions: int = 128,
        wait_seconds: float = 1800.0,
        hard_deadline_seconds: float = 3600.0,
        heartbeat_seconds: float = 10.0,
        ttl_seconds: float = 30.0,
        prepare_seconds: float = 900.0,
        drain_seconds: float = 30.0,
        retry_seconds: float = 30.0,
        cleanup_seconds: float = 60.0,
        cancel_seconds: float = 10.0,
        stop_grace_seconds: float = 30.0,
        reconcile_seconds: float = 5.0,
    ) -> None:
        if min(max_sessions, wait_seconds, hard_deadline_seconds, heartbeat_seconds, ttl_seconds,
               prepare_seconds, drain_seconds, retry_seconds, cleanup_seconds, cancel_seconds,
               stop_grace_seconds, reconcile_seconds) <= 0:
            raise ValueError("session policy values must be positive")
        self.max_sessions = max_sessions
        self.wait_seconds = wait_seconds
        self.hard_deadline_seconds = hard_deadline_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.ttl_seconds = ttl_seconds
        self.prepare_seconds = prepare_seconds
        self.drain_seconds = drain_seconds
        self.retry_seconds = retry_seconds
        self.cleanup_seconds = cleanup_seconds
        self.cancel_seconds = cancel_seconds
        self.stop_grace_seconds = stop_grace_seconds
        self.reconcile_seconds = reconcile_seconds
        self.records: dict[str, SessionRecord] = {}
        self.active_id: str | None = None
        self.in_flight: dict[str, int] = {}

    # -- lifecycle -----------------------------------------------------------

    def create(self, session_id: str, model_id: str, client_id: str, *, now: float, queue: RequestQueue, priority: int = 0, hard_deadline_seconds: float | None = None) -> SessionRecord:
        """Register one PREPARING waiter; the queue owns its position."""
        if session_id in self.records:
            raise SessionConflict("duplicate_session")
        if len(self.records) >= self.max_sessions:
            raise SessionConflict("session_capacity")
        hard = self.hard_deadline_seconds if hard_deadline_seconds is None else min(hard_deadline_seconds, self.hard_deadline_seconds)
        if hard <= 0:
            raise SessionConflict("invalid_hard_deadline")
        record = SessionRecord(
            session_id=session_id,
            model_id=model_id,
            client_id=client_id,
            priority=priority,
            created_at=now,
            wait_deadline=min(now + self.wait_seconds, now + hard),
            prepare_deadline=min(now + self.prepare_seconds, now + hard),
            hard_deadline=now + hard,
            ttl_seconds=self.ttl_seconds,
        )
        waiter = queue.enqueue(session_id, model_id, priority, record.wait_deadline, now, kind=WaitKind.SESSION)
        record.sequence = waiter.sequence
        self.records[session_id] = record
        self.in_flight[session_id] = 0
        return record

    def get(self, session_id: str) -> SessionRecord:
        record = self.records.get(session_id)
        if record is None:
            raise SessionNotFound(session_id)
        return record

    def is_live(self, session_id: str, now: float) -> bool:
        """C04: an ACTIVE session is live only inside its soft TTL and hard deadline."""
        record = self.records.get(session_id)
        return record is not None and record.phase == ACTIVE and now < record.expires_at

    def heartbeat(self, session_id: str, now: float) -> SessionRecord:
        record = self.get(session_id)
        if record.phase not in {PREPARING, ACTIVE}:
            raise SessionConflict("session_not_open")
        if record.phase == ACTIVE and now >= record.expires_at:
            # 到期瞬间起 renew 被拒绝；heartbeat 不能复活已失效的会话。
            raise SessionConflict("session_expired")
        record.heartbeat_at = now
        return record

    def mark_active(self, session_id: str, now: float) -> SessionRecord:
        record = self.get(session_id)
        if record.phase == ACTIVE:
            raise SessionConflict("session_already_active")
        if record.phase != PREPARING:
            raise SessionConflict("session_not_preparing")
        if self.active_id is not None and self.active_id != session_id:
            raise SessionConflict("session_already_active")
        record.phase, record.activated_at, record.heartbeat_at, record.retry_at = ACTIVE, now, now, 0.0
        self.active_id = session_id
        return record

    def begin_drain(self, session_id: str, now: float, reason: str) -> SessionRecord:
        record = self.get(session_id)
        if record.phase == CLOSED:
            return record
        record.phase = DRAINING
        record.draining_at, record.draining_reason = now, reason
        if self.active_id == session_id:
            self.active_id = None
        return record

    def mark_closed(self, session_id: str, now: float) -> SessionRecord:
        """The only phase that releases the slot; idempotent by design."""
        record = self.get(session_id)
        if record.phase is CLOSED:
            return record
        record.phase, record.closed_at = CLOSED, now
        record.retry_at = 0.0
        if self.active_id == session_id:
            self.active_id = None
        self.in_flight[session_id] = 0
        return record

    def mark_blocked(self, session_id: str, now: float, reason: str) -> SessionRecord:
        record = self.get(session_id)
        if record.phase == CLOSED:
            raise SessionConflict("session_closed")
        record.phase, record.blocked_at, record.blocked_reason = BLOCKED, now, reason
        record.retry_at = now + self.reconcile_seconds
        if self.active_id == session_id:
            self.active_id = None
        return record

    def yield_prepare(self, session_id: str, now: float) -> SessionRecord:
        """Undo a freeze without losing the waiter: it retries after the backoff."""
        record = self.get(session_id)
        if record.phase != PREPARING:
            raise SessionConflict("session_not_preparing")
        record.retry_at = now + self.retry_seconds
        return record

    # -- decisions -----------------------------------------------------------

    def holding_id(self) -> str | None:
        """The session that currently holds (or is releasing) the model, if any."""
        for session_id, record in self.records.items():
            if record.phase in _HOLDING_PHASES:
                return session_id
        return None

    def candidate(self, session_id: str, now: float) -> SessionRecord | None:
        """The record if it may start preparing now, else None."""
        record = self.records.get(session_id)
        if record is None or record.phase != PREPARING:
            return None
        if now < record.retry_at or now >= record.preparation_expired_at or now >= record.hard_deadline:
            return None
        if self.holding_id() is not None:
            return None
        return record

    def expired(self, now: float) -> tuple[SessionRecord, ...]:
        """Sessions whose budget ran out; the scheduler drives their cleanup."""
        return tuple(
            record for record in self.records.values()
            if (record.phase == PREPARING and now >= record.preparation_expired_at)
            or (record.phase == ACTIVE and now >= record.expires_at)
        )

    def blocked_due(self, now: float) -> tuple[SessionRecord, ...]:
        return tuple(record for record in self.records.values() if record.phase == BLOCKED and now >= record.retry_at)

    def pending(self) -> bool:
        return any(record.phase != CLOSED for record in self.records.values())

    def view(self, session_id: str, *, now: float) -> dict[str, object]:
        record = self.get(session_id)
        return {
            "session_id": record.session_id,
            "model_id": record.model_id,
            "client_id": record.client_id,
            "phase": record.phase,
            "priority": record.priority,
            "sequence": record.sequence,
            "in_flight": self.in_flight.get(session_id, 0),
            "wait_remaining_ms": max(0, int((record.wait_deadline - now) * 1000)),
            "hard_remaining_ms": max(0, int((record.hard_deadline - now) * 1000)),
            "expires_in_ms": max(0, int((record.expires_at - now) * 1000)),
            "draining_reason": record.draining_reason,
            "blocked_reason": record.blocked_reason,
        }
