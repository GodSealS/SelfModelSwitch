"""Unified-memory admission samples sourced exclusively from psutil.

The scheduler injects samples; nothing here reads scheduler state. Two shapes
are produced:

* `ResourceSnapshot` is the legacy v1 view (total/available) used by the old
  runtime composition, and
* `ports_v3.MemorySample` is the C02 fact set (total, free, available) used by
  the two ledgers. `free` is what makes `system_nonfree_upper_bound_v1`
  computable, and `available` is the figure compared against the free floor F.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import monotonic
from typing import Iterable

import psutil

from .contracts import MemorySample as AdmissionSample
from .ports_v3 import MemorySample


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


def memory_sample_from(*, total: int, free: int, available: int, sampled_at: float) -> MemorySample:
    """One validated C02 sample; impossible figures are refused, never clamped."""
    for name, value in (("total", total), ("free", free), ("available", available)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"memory {name} must be a non-negative integer")
    if total <= 0 or free > total or available > total:
        raise ValueError("memory sample figures are impossible")
    return MemorySample(
        mem_total_bytes=total,
        mem_free_bytes=free,
        mem_available_bytes=available,
        sampled_at_monotonic=sampled_at,
    )


def system_nonfree_upper_bound_v1(samples: Iterable[MemorySample]) -> int:
    """`system_nonfree_upper_bound_v1`: max(MemTotal - MemFree) over one window.

    The figure deliberately includes the OS, page cache and other processes; it
    is a system-level upper bound, not the model's net residency, and summing it
    across models can double-count the background.
    """
    figures = [sample.mem_total_bytes - sample.mem_free_bytes for sample in samples]
    if not figures:
        raise ValueError("the physical upper bound needs at least one sample")
    return max(figures)


def admission_sample(sample: MemorySample) -> AdmissionSample:
    """The v1 Book boundary: available bytes and the monotonic sample instant."""
    return AdmissionSample(
        total_bytes=sample.mem_total_bytes,
        available_bytes=sample.mem_available_bytes,
        sampled_at=sample.sampled_at_monotonic,
    )


class ResourceMonitor:
    """GPU diagnostics never participate in scheduler admission decisions."""

    def snapshot_now(self, now: float | None = None) -> ResourceSnapshot:
        memory = psutil.virtual_memory()
        total, available = int(memory.total), int(memory.available)
        return ResourceSnapshot(total, available, total - available, "psutil", monotonic() if now is None else now)

    async def snapshot(self) -> ResourceSnapshot:
        return await asyncio.to_thread(self.snapshot_now)

    def memory_sample(self, now: float | None = None) -> MemorySample:
        memory = psutil.virtual_memory()
        return memory_sample_from(
            total=int(memory.total),
            free=int(memory.free),
            available=int(memory.available),
            sampled_at=monotonic() if now is None else now,
        )

    async def memory_sample_async(self, now: float | None = None) -> MemorySample:
        return await asyncio.to_thread(self.memory_sample, now)
