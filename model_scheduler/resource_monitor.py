"""Unified-memory admission samples sourced exclusively from psutil."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import monotonic

import psutil


@dataclass(frozen=True)
class ResourceSnapshot:
    total_bytes: int
    available_bytes: int
    used_bytes: int
    source: str
    sampled_at: float

    @property
    def utilization(self) -> float:
        return 0.0 if self.total_bytes <= 0 else 1 - self.available_bytes / self.total_bytes

    def age_at(self, now: float) -> float | None:
        age = now - self.sampled_at
        return age if age >= 0 else None


class ResourceMonitor:
    """GPU diagnostics never participate in scheduler admission decisions."""

    def snapshot_now(self, now: float | None = None) -> ResourceSnapshot:
        memory = psutil.virtual_memory()
        total, available = int(memory.total), int(memory.available)
        return ResourceSnapshot(total, available, total - available, "psutil", monotonic() if now is None else now)

    async def snapshot(self) -> ResourceSnapshot:
        return await asyncio.to_thread(self.snapshot_now)
