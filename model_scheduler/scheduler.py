from __future__ import annotations

import asyncio
from time import monotonic
from collections import deque
from typing import Any, Callable

from .config import SchedulerConfig
from .eviction_policy import EvictionPolicy
from .heat_tracker import HeatTracker
from .llama_swap_client import LlamaSwapClient, LlamaSwapError
from .model_registry import Book
from .models import ModelState
from .resource_monitor import ResourceMonitor


class ResourceError(RuntimeError):
    pass


class ModelScheduler:
    def __init__(
        self,
        registry: Book,
        resources: ResourceMonitor,
        heat: HeatTracker,
        eviction: EvictionPolicy,
        llama: LlamaSwapClient,
        cfg: SchedulerConfig,
    ):
        self.registry = registry
        self.resources = resources
        self.heat = heat
        self.eviction = eviction
        self.llama = llama
        self.cfg = cfg
        self._model_locks = {mid: asyncio.Lock() for mid in registry.configs}
        self._global_lock = asyncio.Lock()
        self._switch_times = deque()
        self._cooldown_until = 0.0

    async def sync_running(self):
        running = set(await self.llama.running())
        for mid in self.registry.configs:
            r = self.registry.get_runtime(mid)
            if mid in running:
                if r.state in {ModelState.UNKNOWN, ModelState.UNLOADED, ModelState.ERROR}:
                    r.mark_loaded()
                    r.state = ModelState.READY
            else:
                if r.in_flight == 0:
                    r.state = ModelState.UNLOADED

    async def ensure_model(self, model_id: str):
        self.registry.require(model_id)
        runtime = self.registry.get_runtime(model_id)

        if runtime.state in {ModelState.READY, ModelState.ACTIVE}:
            return

        async with self._global_lock:
            await self.sync_running()
            runtime = self.registry.get_runtime(model_id)
            if runtime.state in {ModelState.READY, ModelState.ACTIVE}:
                return

            await self._respect_switch_rate()
            await self._ensure_resources(model_id)

            runtime.state = ModelState.LOADING
            try:
                await self.llama.load(model_id)
                runtime.mark_loaded()
            except Exception as e:
                runtime.mark_error(str(e))
                raise

            self._switch_times.append(monotonic())

    async def _ensure_resources(self, target_id: str):
        target = self.registry.configs[target_id]
        snap = await self.resources.snapshot()

        required = int(target.memory.reserved_bytes * (1.0 + self.cfg.resource_safety_margin))
        # Preserve a hard floor after the target is loaded.
        usable = max(0, snap.available_bytes - self.cfg.min_free_memory_bytes)

        if usable >= required:
            return

        deficit = required - usable
        selected = self.eviction.choose(deficit)

        if not selected:
            raise ResourceError(
                f"insufficient memory for {target_id}: required={required}, "
                f"available_usable={usable}, deficit={deficit}, "
                f"running={self.registry.snapshot()}"
            )

        for candidate in selected:
            r = self.registry.get_runtime(candidate.model_id)
            r.state = ModelState.EVICTING
            try:
                await self.llama.unload(candidate.model_id)
            except Exception as e:
                r.mark_error(str(e))
                raise
            finally:
                r.state = ModelState.UNLOADED

        # Give the OS/CUDA a moment to return memory, then verify.
        await asyncio.sleep(0.5)
        snap2 = await self.resources.snapshot()
        usable2 = max(0, snap2.available_bytes - self.cfg.min_free_memory_bytes)
        if usable2 < required:
            raise ResourceError(
                f"eviction completed but memory is still insufficient: "
                f"required={required}, usable={usable2}, source={snap2.source}"
            )

    async def acquire(self, model_id: str):
        await self.ensure_model(model_id)
        r = self.registry.get_runtime(model_id)
        r.in_flight += 1
        r.state = ModelState.ACTIVE
        r.touch()
        self.heat.record_request(r)

    async def release(self, model_id: str, tokens: int = 0):
        r = self.registry.get_runtime(model_id)
        r.in_flight = max(0, r.in_flight - 1)
        r.total_tokens += tokens
        self.heat.record_request(r, tokens=tokens)
        if r.in_flight == 0:
            r.state = ModelState.READY

    async def _respect_switch_rate(self):
        now = monotonic()
        while self._switch_times and now - self._switch_times[0] > self.cfg.thrash.switch_window_seconds:
            self._switch_times.popleft()

        if len(self._switch_times) >= self.cfg.thrash.max_switches_in_window:
            sleep_for = max(
                self.cfg.thrash.cooldown_seconds,
                self.cfg.thrash.switch_window_seconds - (now - self._switch_times[0]),
            )
            await asyncio.sleep(sleep_for)

    async def status(self):
        snap = await self.resources.snapshot()
        return {
            "resources": {
                "total_bytes": snap.total_bytes,
                "available_bytes": snap.available_bytes,
                "used_bytes": snap.used_bytes,
                "utilization": snap.utilization,
                "source": snap.source,
            },
            "models": self.registry.snapshot(),
            "queue_size": 0,
        }
