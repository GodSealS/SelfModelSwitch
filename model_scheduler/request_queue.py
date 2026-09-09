from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import monotonic
from typing import Any, Optional


@dataclass
class QueuedRequest:
    model_id: str
    payload: dict[str, Any]
    enqueued_at: float
    future: asyncio.Future


class RequestQueue:
    def __init__(self, timeout_seconds: float = 1800):
        self.timeout_seconds = timeout_seconds
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._seq = 0
        self._lock = asyncio.Lock()

    async def put(self, model_id: str, payload: dict[str, Any], priority: int = 0) -> asyncio.Future:
        async with self._lock:
            self._seq += 1
            fut = asyncio.get_running_loop().create_future()
            item = QueuedRequest(model_id, payload, monotonic(), fut)
            # Higher priority first; FIFO within the same priority.
            await self._queue.put((-priority, self._seq, item))
            return fut

    async def get(self):
        _, _, item = await self._queue.get()
        return item

    def task_done(self):
        self._queue.task_done()

    def qsize(self) -> int:
        return self._queue.qsize()

    async def drain(self):
        items = []
        while not self._queue.empty():
            try:
                _, _, item = self._queue.get_nowait()
                items.append(item)
                self._queue.task_done()
            except asyncio.QueueEmpty:
                break
        return items
