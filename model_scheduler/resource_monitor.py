from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional

import psutil


@dataclass(frozen=True)
class ResourceSnapshot:
    total_bytes: int
    available_bytes: int
    used_bytes: int
    source: str

    @property
    def utilization(self) -> float:
        return 0.0 if self.total_bytes <= 0 else 1.0 - self.available_bytes / self.total_bytes


class ResourceMonitor:
    """
    AGX/Jetson-friendly memory monitor.

    Priority:
      1. tegrastats (when requested/available)
      2. nvidia-smi (when available)
      3. psutil system memory

    On AGX Thor, unified memory makes system available memory the most portable
    baseline. The scheduler also keeps a configurable safety floor.
    """

    def __init__(self, provider: str = "auto", total_override: int = 0):
        self.provider = provider
        self.total_override = total_override

    async def snapshot(self) -> ResourceSnapshot:
        if self.provider in ("auto", "tegrastats"):
            snap = await asyncio.to_thread(self._tegrastats)
            if snap:
                return snap
            if self.provider == "tegrastats":
                raise RuntimeError("tegrastats was requested but no usable output was found")

        if self.provider in ("auto", "nvidia-smi"):
            snap = await asyncio.to_thread(self._nvidia_smi)
            if snap:
                return snap
            if self.provider == "nvidia-smi":
                raise RuntimeError("nvidia-smi was requested but no usable output was found")

        return self._psutil()

    def _psutil(self) -> ResourceSnapshot:
        vm = psutil.virtual_memory()
        total = self.total_override or vm.total
        available = min(vm.available, total)
        return ResourceSnapshot(total, available, total - available, "psutil")

    def _nvidia_smi(self) -> Optional[ResourceSnapshot]:
        exe = shutil.which("nvidia-smi")
        if not exe:
            return None
        try:
            out = subprocess.check_output(
                [exe, "--query-gpu=memory.total,memory.used", "--format=csv,noheader,nounits"],
                text=True, timeout=2,
            )
            totals = []
            used = []
            for line in out.strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 2:
                    totals.append(float(parts[0]) * 1024**2)
                    used.append(float(parts[1]) * 1024**2)
            if not totals:
                return None
            total = self.total_override or int(sum(totals))
            used_b = int(sum(used))
            return ResourceSnapshot(total, max(0, total - used_b), used_b, "nvidia-smi")
        except Exception:
            return None

    def _tegrastats(self) -> Optional[ResourceSnapshot]:
        exe = shutil.which("tegrastats")
        if not exe:
            return None
        try:
            # A one-shot tegrastats invocation is supported on Jetson platforms
            # through --interval/--count on recent releases.
            out = subprocess.check_output(
                [exe, "--interval", "100", "--count", "1"],
                text=True, timeout=3,
                stderr=subprocess.STDOUT,
            )
            # Typical forms include: RAM 12345/63836MB or RAM 12345/63836 MiB
            m = re.search(r"RAM\s+(\d+)\s*/\s*(\d+)\s*(?:MB|MiB)", out)
            if not m:
                return None
            used = int(m.group(1)) * 1024**2
            total = int(m.group(2)) * 1024**2
            if self.total_override:
                total = self.total_override
            return ResourceSnapshot(total, max(0, total - used), used, "tegrastats")
        except Exception:
            return None
