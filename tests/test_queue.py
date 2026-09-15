from __future__ import annotations

import pytest

from model_scheduler.request_queue import RequestQueue, WaitState


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
