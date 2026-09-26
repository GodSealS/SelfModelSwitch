"""Deterministic candidate selection over the scheduler's authoritative book."""
from __future__ import annotations

from dataclasses import dataclass

from .contracts import State
from .model_registry import Book


@dataclass(frozen=True)
class EvictionCandidate:
    model_id: str
    reserved_bytes: int
    heat: float
    priority: int
    score: float


class EvictionPolicy:
    """Choose a complete, stable idle eviction prefix without changing state."""

    def __init__(self, book: Book, *, max_evictions: int = 8):
        if max_evictions < 1:
            raise ValueError("max_evictions must be positive")
        self.book = book
        self.max_evictions = max_evictions

    def candidates(self, now: float, *, include_busy: bool = False) -> list[EvictionCandidate]:
        result: list[EvictionCandidate] = []
        for model_id in self.book.specs:
            runtime = self.book.runtime[model_id]
            # P19: a v2 registration's raw spec carries no scheduler roles at all, so the
            # ledger copy is the one source of `pinned`/`evictable`/`priority` — for v1 and
            # v2 alike. Reading them off the raw spec raises on the first eviction a v2
            # deployment actually needs.
            entry = self.book.ledger[model_id]
            if (runtime.state is not State.READY or (runtime.leases and not include_busy) or runtime.admission_blocked
                    or entry.pinned or not entry.evictable):
                continue
            reserved = self.book.required(model_id)
            heat = self.book.heat(model_id, now)
            # Lower priority/heat and a larger release are preferred.  model_id
            # stabilizes ties so equal clocks make equal decisions.
            score = (heat + entry.priority) / reserved
            result.append(EvictionCandidate(model_id, reserved, heat, entry.priority, score))
        return sorted(result, key=lambda candidate: (candidate.score, candidate.model_id))

    def choose(self, bytes_needed: int, *, now: float, include_busy: bool = False) -> list[EvictionCandidate]:
        if bytes_needed <= 0:
            return []
        selected: list[EvictionCandidate] = []
        released = 0
        for candidate in self.candidates(now, include_busy=include_busy):
            selected.append(candidate)
            released += candidate.reserved_bytes
            if released >= bytes_needed:
                return selected
            if len(selected) >= self.max_evictions:
                break
        return []
