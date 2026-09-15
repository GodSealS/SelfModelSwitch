"""Verified lifecycle adapter between llama-swap and the scheduler book."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import monotonic
from typing import Protocol

from .contracts import Observation, Operation, Presence


@dataclass(frozen=True)
class ManagedModel:
    container_name: str
    port: int


class LlamaSwapControl(Protocol):
    async def load(self, model_id: str) -> None: ...
    async def unload(self, model_id: str) -> None: ...


class ProcessEvidence(Protocol):
    def observe(self, model_id: str, container_name: str, port: int) -> Observation: ...


class LlamaSwapBackend:
    """No control response is trusted until independently observed afterward."""

    def __init__(self, control: LlamaSwapControl, observer: ProcessEvidence, models: dict[str, ManagedModel]):
        self.control = control
        self.observer = observer
        self.models = models.copy()

    async def observe(self, model_id: str) -> Observation:
        model = self.models.get(model_id)
        if model is None:
            return Observation(Presence.UNKNOWN, None, False, monotonic(), "unknown_model")
        try:
            return await asyncio.to_thread(self.observer.observe, model_id, model.container_name, model.port)
        except Exception:
            return Observation(Presence.UNKNOWN, None, False, monotonic(), "observation_failed")

    async def load(self, operation: Operation, deadline: float) -> Observation:
        return await self._control_then_observe(operation.model_id, deadline, "load")

    async def stop(self, operation: Operation, deadline: float) -> Observation:
        return await self._control_then_observe(operation.model_id, deadline, "unload")

    async def _control_then_observe(self, model_id: str, deadline: float, action: str) -> Observation:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return Observation(Presence.UNKNOWN, None, False, monotonic(), f"control_{action}_timeout")
        try:
            async with asyncio.timeout(remaining):
                if action == "load":
                    await self.control.load(model_id)
                else:
                    await self.control.unload(model_id)
        except (asyncio.TimeoutError, Exception):
            return Observation(Presence.UNKNOWN, None, False, monotonic(), f"control_{action}_failed")
        return await self.observe(model_id)
