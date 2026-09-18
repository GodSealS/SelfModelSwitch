from __future__ import annotations

import pytest

from model_scheduler.request_queue import RequestQueue, WaitKind
from model_scheduler.session_manager import (
    ACTIVE,
    BLOCKED,
    CLOSED,
    DRAINING,
    PREPARING,
    SessionConflict,
    SessionManager,
    SessionNotFound,
)


def manager(**overrides) -> SessionManager:
    policy = {
        "wait_seconds": 1800.0,
        "hard_deadline_seconds": 3600.0,
        "heartbeat_seconds": 10.0,
        "ttl_seconds": 30.0,
        "prepare_seconds": 900.0,
        "drain_seconds": 30.0,
        "retry_seconds": 30.0,
        "cleanup_seconds": 60.0,
        "cancel_seconds": 10.0,
        "stop_grace_seconds": 30.0,
        "reconcile_seconds": 5.0,
    }
    policy.update(overrides)
    return SessionManager(**policy)


def open_session(sessions: SessionManager, session_id: str = "session-1", *, now: float = 0.0, queue: RequestQueue | None = None, **kwargs):
    queue = queue if queue is not None else RequestQueue(capacity=8)
    return sessions.create(session_id, kwargs.pop("model_id", "chat"), "client-a", now=now, queue=queue, **kwargs)


def test_a_session_waits_no_longer_than_the_queue_budget_or_hard_deadline() -> None:
    queue = RequestQueue(capacity=8)
    sessions = manager()
    record = open_session(sessions, now=0.0, queue=queue, priority=7)

    assert (record.phase, record.priority) == (PREPARING, 7)
    assert record.wait_deadline == 1800.0  # C04 queue wait ceiling
    assert record.prepare_deadline == 900.0  # C04 preparation ceiling
    assert record.hard_deadline == 3600.0
    waiter = queue.get("session-1")
    assert waiter is not None and waiter.kind is WaitKind.SESSION and waiter.priority == 7

    short = manager(hard_deadline_seconds=600.0)
    record = open_session(short, "session-2", now=0.0, model_id="chat")
    assert (record.wait_deadline, record.prepare_deadline, record.hard_deadline) == (600.0, 600.0, 600.0)


def test_a_session_is_created_once_and_unknown_ids_are_refused() -> None:
    sessions = manager()
    open_session(sessions)

    with pytest.raises(SessionConflict):
        open_session(sessions)
    with pytest.raises(SessionNotFound):
        sessions.get("missing")
    with pytest.raises(SessionNotFound):
        sessions.heartbeat("missing", 1.0)


def test_preparing_sessions_expire_at_their_wait_or_prepare_deadline() -> None:
    sessions = manager(prepare_seconds=100.0, wait_seconds=1800.0)
    record = open_session(sessions, now=0.0)

    assert sessions.expired(99.9) == ()
    assert sessions.expired(record.prepare_deadline) == (record,)

    waiting = manager(prepare_seconds=900.0, wait_seconds=50.0)
    record = open_session(waiting, now=0.0)
    assert waiting.expired(50.0) == (record,)


def test_heartbeat_refreshes_the_ttl_but_never_the_hard_deadline() -> None:
    sessions = manager()
    record = open_session(sessions, now=0.0)
    sessions.mark_active("session-1", now=5.0)
    sessions.heartbeat("session-1", 10.0)

    assert not sessions.expired(39.9)
    assert sessions.expired(40.0) == (record,)  # heartbeat_at + ttl

    late = manager()
    late_record = open_session(late, now=0.0)
    late.mark_active("session-1", now=0.0)
    late.heartbeat("session-1", 3590.0)
    assert late_record.expires_at == 3600.0  # the TTL can never extend the hard deadline
    assert late.expired(3600.0) == (late_record,)
    assert not late.expired(3599.9)

    closed = manager()
    open_session(closed)
    closed.mark_closed("session-1", now=1.0)
    with pytest.raises(SessionConflict):
        closed.heartbeat("session-1", 2.0)


def test_only_one_session_can_be_active_and_two_racers_never_both_win() -> None:
    sessions = manager()
    queue = RequestQueue(capacity=8)
    open_session(sessions, "session-1", now=0.0, queue=queue)
    open_session(sessions, "session-2", now=0.0, queue=queue)

    sessions.mark_active("session-1", now=1.0)

    assert sessions.active_id == "session-1"
    with pytest.raises(SessionConflict):
        sessions.mark_active("session-2", now=1.1)
    sessions.begin_drain("session-1", now=2.0, reason="client_close")
    sessions.mark_closed("session-1", now=3.0)
    assert sessions.active_id is None
    assert sessions.mark_active("session-2", now=4.0).phase == ACTIVE


def test_a_drain_yield_keeps_the_waiter_and_backs_off_for_thirty_seconds() -> None:
    sessions = manager()
    open_session(sessions, now=0.0)

    record = sessions.yield_prepare("session-1", now=10.0)

    assert record.phase == PREPARING
    assert record.retry_at == 40.0
    assert sessions.candidate("session-1", 39.9) is None
    assert sessions.candidate("session-1", 40.0) is record


def test_candidates_require_an_unheld_session_and_a_live_budget() -> None:
    sessions = manager()
    open_session(sessions, "session-1", now=0.0)
    open_session(sessions, "session-2", now=0.0)
    sessions.mark_active("session-1", now=1.0)

    assert sessions.candidate("session-2", 2.0) is None  # another session holds the model
    sessions.begin_drain("session-1", now=2.0, reason="expired")
    assert sessions.candidate("session-2", 2.0) is None
    sessions.mark_closed("session-1", now=3.0)
    assert sessions.candidate("session-2", 3.0) is not None

    expired = manager()
    open_session(expired, "session-3", now=0.0)
    assert expired.candidate("session-3", 1800.0) is None


def test_a_blocked_cleanup_keeps_the_slot_and_reconciles_every_five_seconds() -> None:
    sessions = manager()
    open_session(sessions)
    sessions.mark_active("session-1", now=0.0)

    record = sessions.begin_drain("session-1", now=1.0, reason="client_close")
    assert record.phase == DRAINING
    blocked = sessions.mark_blocked("session-1", now=2.0, reason="stop_unconfirmed")

    assert blocked.phase == BLOCKED
    assert blocked.blocked_reason == "stop_unconfirmed"
    assert sessions.blocked_due(6.9) == ()
    assert sessions.blocked_due(7.0) == (blocked,)  # now + 5s
    assert sessions.active_id is None  # the slot is not freed by a block


def test_closing_is_idempotent_and_phase_ordered() -> None:
    sessions = manager()
    open_session(sessions)
    assert sessions.mark_closed("session-1", now=1.0).phase == CLOSED
    assert sessions.mark_closed("session-1", now=2.0).phase == CLOSED  # idempotent

    sessions2 = manager()
    open_session(sessions2, "session-9", now=0.0)
    sessions2.mark_active("session-9", now=1.0)
    sessions2.begin_drain("session-9", now=2.0, reason="client_close")
    with pytest.raises(SessionConflict):
        sessions2.mark_active("session-9", now=3.0)  # a draining session never comes back
    assert sessions2.mark_closed("session-9", now=4.0).phase == CLOSED


def test_the_view_reports_phase_and_remaining_budgets() -> None:
    sessions = manager()
    open_session(sessions, now=0.0)
    sessions.mark_active("session-1", now=2.0)
    sessions.heartbeat("session-1", 10.0)
    sessions.in_flight["session-1"] = 2

    view = sessions.view("session-1", now=11.0)

    assert view["session_id"] == "session-1"
    assert view["phase"] == ACTIVE
    assert view["expires_in_ms"] == 29000  # heartbeat_at + ttl - now
    assert view["hard_remaining_ms"] == 3589000
    assert view["in_flight"] == 2
