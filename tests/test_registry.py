from __future__ import annotations

import pytest

from model_scheduler.contracts import Capability, MemorySample, ModelSpec, Outcome, State
from model_scheduler.model_registry import Book, Conflict, StaleOperation


def make_book(concurrency: int = 1) -> Book:
    specs = {
        name: ModelSpec(name, f"http://127.0.0.1:{10001 + index}", frozenset({Capability.CHAT}), 100, max_concurrency=concurrency)
        for index, name in enumerate(("a", "b", "c"))
    }
    book = Book(specs, model_budget=300, free_floor=20, margin=0, half_life=10)
    for model_id in specs:
        book.bootstrap_stopped(model_id)
    return book


def load(book: Book, model_id: str, now: float = 0) -> None:
    operation = book.begin_load(model_id, MemorySample(1000, 900, now), now)
    book.loaded(operation, now)


def test_atomic_batch_eviction_blocks_every_selected_model() -> None:
    book = make_book()
    load(book, "a")
    load(book, "b")

    operations = book.begin_eviction(["a", "b"])

    with pytest.raises(Conflict):
        book.acquire_ready("b", "new-request", 1)
    book.stopped(operations[0])
    assert book.runtime["b"].state is State.EVICTING
    assert book.committed == 100


def test_failed_stop_keeps_budget_and_unsent_batch_member_recovers() -> None:
    book = make_book()
    load(book, "a")
    load(book, "b")
    first, second = book.begin_eviction(["a", "b"])

    book.failed(first, "stop_timeout")
    book.rollback_unsent(second)

    assert book.runtime["a"].state is State.ERROR
    assert book.runtime["b"].state is State.READY
    assert book.committed == 200


def test_duplicate_release_cannot_release_another_request() -> None:
    book = make_book(concurrency=2)
    load(book, "a")
    first = book.acquire_ready("a", "first", 0)
    second = book.acquire_ready("a", "second", 0)

    assert book.release(first, Outcome.SUCCESS, 1, tokens=4) is True
    assert book.release(first, Outcome.SUCCESS, 1, tokens=4) is False
    assert set(book.runtime["a"].leases) == {second.lease_id}


def test_aborted_request_isolates_model_without_harming_other_lease() -> None:
    book = make_book(concurrency=2)
    load(book, "a")
    first = book.acquire_ready("a", "first", 0)
    second = book.acquire_ready("a", "second", 0)

    book.release(first, Outcome.ABORTED, 1)

    assert book.runtime["a"].state is State.ERROR
    assert second.lease_id in book.runtime["a"].leases
    with pytest.raises(Conflict):
        book.acquire_ready("a", "third", 1)


def test_stale_load_operation_cannot_overwrite_new_generation() -> None:
    book = make_book()
    old = book.begin_load("a", MemorySample(1000, 900, 0), 0)
    book.failed(old, "load_timeout")

    with pytest.raises(StaleOperation):
        book.loaded(old, 901)
    assert book.committed == 100


def test_ttl_begins_only_after_last_lease_releases() -> None:
    specs = {"a": ModelSpec("a", "http://127.0.0.1:10001", frozenset({Capability.CHAT}), 100, max_concurrency=2, ttl_seconds=10)}
    book = Book(specs, model_budget=300, free_floor=20, margin=0); book.bootstrap_stopped("a")
    load(book, "a", 0)
    first, second = book.acquire_ready("a", "first", 1), book.acquire_ready("a", "second", 2)
    book.release(first, Outcome.SUCCESS, 5)
    assert not book.ttl_due("a", 100)
    book.release(second, Outcome.SUCCESS, 100)
    assert not book.ttl_due("a", 109.9)
    assert book.ttl_due("a", 110)


def test_ready_admission_requires_a_fresh_sample_and_free_floor() -> None:
    book = make_book()
    load(book, "a", 0)
    assert book.can_admit_ready("a", MemorySample(1_000, 20, 0), 1)
    assert not book.can_admit_ready("a", MemorySample(1_000, 19, 0), 1)
    assert not book.can_admit_ready("a", MemorySample(1_000, 900, -10), 1)
