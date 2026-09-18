from __future__ import annotations

import pytest

from model_scheduler.request_queue import RequestQueue, WaitKind, WaitState


def test_queue_ages_waiting_work_and_keeps_fifo_ties() -> None:
    queue = RequestQueue(capacity=2, aging_seconds=10)
    queue.enqueue("old", "a", 0, 100, 0)
    queue.enqueue("new", "b", 1, 100, 9)
    assert queue.head(9).request_id == "new"
    assert queue.head(20).request_id == "old"


def test_queue_expires_before_capacity_and_never_grants_deadline_edge() -> None:
    queue = RequestQueue(capacity=1)
    queue.enqueue("expired", "a", 0, 10, 0)
    item = queue.enqueue("fresh", "b", 0, 20, 10)
    assert item.request_id == "fresh"
    assert queue.remove("fresh", WaitState.CANCELLED).state is WaitState.CANCELLED
    queue.enqueue("one", "a", 0, 20, 10)
    with pytest.raises(OverflowError):
        queue.enqueue("two", "b", 0, 20, 10)


def test_queue_orders_both_kinds_by_the_c04_key() -> None:
    queue = RequestQueue(capacity=4, aging_seconds=30)
    queue.enqueue("interactive", "a", 10, 100, 0)
    queue.enqueue("session", "b", 10, 100, 1, kind=WaitKind.SESSION)

    assert queue.head(1).request_id == "interactive"  # equal priority: the earlier sequence wins
    assert queue.head(1, kind=WaitKind.SESSION).request_id == "session"
    assert queue.head(1, kind=WaitKind.INTERACTIVE).request_id == "interactive"
    assert queue.head(31, kind=WaitKind.SESSION).request_id == "session"  # aging is the same for both


def test_a_retry_keeps_its_queue_position_and_deadline() -> None:
    queue = RequestQueue(capacity=2, aging_seconds=30)
    queue.enqueue("waiter", "a", 0, 60, 0, kind=WaitKind.SESSION)
    queue.enqueue("other", "b", 5, 900, 5)
    item = queue.remove("waiter", WaitState.CANCELLED)
    assert item is not None

    queue.requeue(item)  # the 30s drain yield: back in line, same sequence and deadline

    head = queue.head(6, kind=WaitKind.SESSION)
    assert head is not None
    assert (head.sequence, head.enqueued_at, head.deadline, head.state) == (1, 0.0, 60.0, WaitState.WAITING)
    assert queue.get("waiter") is head
    with pytest.raises(ValueError):
        queue.requeue(head)  # a waiter that is already queued is never duplicated


def test_requeue_respects_capacity_and_expiry() -> None:
    queue = RequestQueue(capacity=1)
    queue.enqueue("held", "a", 0, 100, 0, kind=WaitKind.SESSION)
    item = queue.remove("held")
    queue.enqueue("filler", "b", 0, 100, 0)
    with pytest.raises(OverflowError):
        queue.requeue(item)

    queue_expired = RequestQueue(capacity=2)
    queue_expired.enqueue("held", "a", 0, 5, 0, kind=WaitKind.SESSION)
    stale = queue_expired.remove("held")
    queue_expired.requeue(stale)
    assert queue_expired.head(5) is None  # a wait deadline that has passed still expires in the queue
    assert queue_expired.size == 0
