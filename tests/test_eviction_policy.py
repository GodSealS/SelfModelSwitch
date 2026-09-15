from __future__ import annotations

from model_scheduler.contracts import Capability, MemorySample, ModelSpec
from model_scheduler.eviction_policy import EvictionPolicy
from model_scheduler.model_registry import Book


def _book() -> Book:
    specs = {
        "hot": ModelSpec("hot", "http://127.0.0.1:10001", frozenset({Capability.CHAT}), 100, priority=100),
        "cold": ModelSpec("cold", "http://127.0.0.1:10002", frozenset({Capability.CHAT}), 100, priority=1),
        "pinned": ModelSpec("pinned", "http://127.0.0.1:10003", frozenset({Capability.CHAT}), 100, pinned=True, evictable=False),
    }
    book = Book(specs, model_budget=1_000, free_floor=0, margin=0)
    for model_id in specs:
        book.bootstrap_stopped(model_id)
        operation = book.begin_load(model_id, MemorySample(2_000, 2_000, 0), 0)
        book.loaded(operation, 0)
    book.acquire_ready("hot", "hot-request", 1)
    return book


def test_policy_chooses_low_heat_unpinned_idle_prefix() -> None:
    book = _book()
    selected = EvictionPolicy(book, max_evictions=2).choose(100, now=1)
    assert [candidate.model_id for candidate in selected] == ["cold"]


def test_policy_never_returns_a_partial_prefix_below_deficit() -> None:
    book = _book()
    assert EvictionPolicy(book, max_evictions=2).choose(250, now=1) == []
