from __future__ import annotations

import asyncio
from typing import Dict

from .config import ModelConfig
from .models import ModelRuntime, ModelState


class ModelRegistry:
    def __init__(self, configs: Dict[str, ModelConfig]):
        self.configs = configs
        self.runtime = {mid: ModelRuntime(mid) for mid in configs}
        self._lock = asyncio.Lock()

    def require(self, model_id: str) -> ModelConfig:
        if model_id not in self.configs:
            raise KeyError(f"Unknown model: {model_id}")
        return self.configs[model_id]

    def get_runtime(self, model_id: str) -> ModelRuntime:
        return self.runtime[model_id]

    async def update_state(self, model_id: str, state: ModelState):
        async with self._lock:
            r = self.runtime[model_id]
            r.state = state
            r.last_state_change = __import__("time").monotonic()

    def running(self):
        return [
            r for r in self.runtime.values()
            if r.state in {ModelState.LOADING, ModelState.READY, ModelState.ACTIVE}
        ]

    def evictable(self):
        out = []
        for r in self.runtime.values():
            c = self.configs[r.model_id]
            if r.state not in {ModelState.READY, ModelState.ACTIVE}:
                continue
            if r.in_flight > 0 or c.scheduling.pinned or not c.scheduling.evictable:
                continue
            out.append(r)
        return out

    def snapshot(self):
        return {
            mid: {
                "state": r.state.value,
                "last_used_seconds_ago": None if not r.last_used else max(0, __import__("time").monotonic() - r.last_used),
                "in_flight": r.in_flight,
                "total_requests": r.total_requests,
                "total_tokens": r.total_tokens,
                "reserved_bytes": self.configs[mid].memory.reserved_bytes,
                "priority": self.configs[mid].scheduling.priority,
                "evictable": self.configs[mid].scheduling.evictable,
                "pinned": self.configs[mid].scheduling.pinned,
                "last_error": r.last_error,
            }
            for mid, r in self.runtime.items()
        }
