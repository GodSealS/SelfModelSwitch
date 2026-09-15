"""Pure queue bookkeeping; callers hold the scheduler's single condition."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class WaitState(str, Enum):
    WAITING = "waiting"
    GRANTED = "granted"
    CLAIMED = "claimed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


@dataclass
class QueuedRequest:
    request_id: str
    model_id: str
    priority: int
    enqueued_at: float
    deadline: float
    sequence: int
    state: WaitState = WaitState.WAITING


class RequestQueue:
    """Bounded, dynamically-aged request ordering without any I/O or awaits."""

    def __init__(self, *, capacity: int = 128, aging_seconds: float = 30):
        if capacity < 1 or aging_seconds <= 0:
            raise ValueError("invalid queue policy")
        self.capacity = capacity
        self.aging_seconds = aging_seconds
        self._items: dict[str, QueuedRequest] = {}
        self._sequence = 0

    def enqueue(self, request_id: str, model_id: str, priority: int, deadline: float, now: float) -> QueuedRequest:
        self.expire(now)
        if request_id in self._items:
            raise ValueError("duplicate waiter")
        if len(self._items) >= self.capacity:
            raise OverflowError("queue full")
        self._sequence += 1
        item = QueuedRequest(request_id, model_id, priority, now, deadline, self._sequence)
        self._items[request_id] = item
        return item

    def expire(self, now: float) -> tuple[QueuedRequest, ...]:
        expired = tuple(item for item in self._items.values() if now >= item.deadline)
        for item in expired:
            item.state = WaitState.EXPIRED
            del self._items[item.request_id]
        return expired

    def remove(self, request_id: str, state: WaitState = WaitState.CANCELLED) -> QueuedRequest | None:
        item = self._items.pop(request_id, None)
        if item is not None:
            item.state = state
        return item

    def head(self, now: float) -> QueuedRequest | None:
        self.expire(now)
        if not self._items:
            return None
        return min(self._items.values(), key=lambda item: (-self._priority(item, now), item.sequence))

    def is_head(self, request_id: str, now: float) -> bool:
        item = self.head(now)
        return item is not None and item.request_id == request_id

    def contains(self, request_id: str) -> bool:
        return request_id in self._items

    def waiting_models(self) -> frozenset[str]:
        return frozenset(item.model_id for item in self._items.values())

    def clear(self, state: WaitState = WaitState.CANCELLED) -> tuple[QueuedRequest, ...]:
        items = tuple(self._items.values())
        self._items.clear()
        for item in items:
            item.state = state
        return items

    @property
    def size(self) -> int:
        return len(self._items)

    def _priority(self, item: QueuedRequest, now: float) -> int:
        return item.priority + int(max(0, now - item.enqueued_at) // self.aging_seconds)
