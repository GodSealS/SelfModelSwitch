"""Verified lifecycle adapters between llama-swap and the scheduler book.

Two generations live here on purpose:

* :class:`LlamaSwapBackend` is the shipped v1 path (container-name/port facts).
* :class:`ManagedLifecycle` is the P16 v2/v3 bridge: it lets the scheduler's
  cold-load and eviction paths drive a real :class:`~model_scheduler.ports_v3.BackendPort`
  adapter (llama.cpp) while every book move still waits for the independent
  C03 observation. A control response — load health OR a StopAck — is never a
  verdict: only ``stopped_is_proven`` (the four facts, via the injected
  ``ObserverPort``) clears the instance record and lets the caller settle
  dispatched executions with trusted terminal evidence.

The bridge also owns the ``model_id -> InstanceIdentity`` mapping so the
adapter's execute/cancel/stop always carry the same identity the books saw at
READY time (P16 AC1: "instance/Fence consistent at every step").
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import monotonic
from typing import Any, Awaitable, Callable, Mapping, Protocol

from .contracts import Observation, Operation, Presence
from .control_protocol_v1 import Fence, InstanceIdentity
from .ports_v3 import ObservationTarget, STOPPED, RUNNING


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


class ManagedLifecycle:
    """The v1-shaped scheduler backend over a v3 adapter plus a C03 observer (P16).

    ``load`` asks the adapter (which drives llama-swap and probes health/slots)
    and then *re-verifies* the instance independently before the books move to
    READY; the verified ``InstanceIdentity`` is remembered for execute/stop.
    ``stop`` sends the unload command for that exact identity and then polls
    the observer until the four stop facts prove STOPPED — a StopAck with a
    still-running container releases nothing.
    """

    def __init__(
        self,
        *,
        boot_id: str,
        deployment_id: str,
        specs: Mapping[str, Any],
        adapter_for: Callable[[str], Any],
        observers: Mapping[str, Any],
        poll_seconds: float = 0.05,
    ) -> None:
        if not boot_id or not deployment_id or poll_seconds <= 0:
            raise ValueError("a managed lifecycle needs a boot, a deployment and a poll interval")
        self._boot_id = boot_id
        self._deployment_id = deployment_id
        self._specs = dict(specs)
        self._adapter_for = adapter_for
        self._observers = dict(observers)
        self._poll_seconds = poll_seconds
        self._instances: dict[str, InstanceIdentity] = {}

    def instance(self, model_id: str) -> InstanceIdentity | None:
        """The identity the books saw at READY; None once a stop is proven."""
        return self._instances.get(model_id)

    def fence(self, model_id: str, generation: int, operation_id: str) -> Fence:
        return Fence(self._boot_id, model_id, generation, operation_id, None, None)

    async def observe(self, model_id: str, deadline: float) -> Any:
        """One independent fact sample (admin/health endpoints may surface it)."""
        return await self._observe(model_id, self._instances.get(model_id), deadline)

    async def load(self, operation: Operation, deadline: float) -> Observation:
        model_id = operation.model_id
        adapter = self._adapter_for(model_id)
        try:
            verified = await adapter.load(self._specs[model_id], self.fence(model_id, operation.generation, operation.operation_id), deadline)
        except Exception:
            return Observation(Presence.UNKNOWN, None, False, monotonic(), "load_failed")
        if verified.state != RUNNING:
            return Observation(Presence.UNKNOWN, None, False, monotonic(), "load_unverified")
        # A healthy adapter answer is still a control response: re-verify via the observer.
        observation = await self._verify_running(model_id, verified.instance, deadline)
        if observation is None or observation.state != RUNNING or observation.instance is None:
            return Observation(Presence.UNKNOWN, None, False, monotonic(), "instance_unverified")
        self._instances[model_id] = observation.instance
        return Observation(Presence.RUNNING, f"{observation.instance.container_id}:{observation.instance.started_at}",
                           True, observation.sampled_at_monotonic)

    async def stop(self, operation: Operation, deadline: float) -> Observation:
        model_id = operation.model_id
        identity = self._instances.get(model_id)
        adapter = self._adapter_for(model_id)
        if identity is not None:
            try:
                await adapter.stop(
                    identity, self.fence(model_id, operation.generation, operation.operation_id), deadline)
            except Exception:
                pass  # the facts decide; an ack or a failed control call proves nothing either way
        else:
            # A load that failed verification can leave the control plane holding a container
            # this boot never accepted. Releasing it by model name is the only way that
            # orphan can be let go, and the facts still have to prove the stop below.
            release = getattr(adapter, "release", None)
            if release is not None:
                try:
                    await release(model_id)
                except Exception:
                    pass
        observation = await self._poll_stopped(model_id, identity, deadline)
        if observation is not None and observation.state == STOPPED:
            self._instances.pop(model_id, None)
            return Observation(Presence.STOPPED, None, False, observation.sampled_at_monotonic)
        if observation is not None and observation.state == RUNNING:
            return Observation(Presence.RUNNING, None, True, observation.sampled_at_monotonic, "stop_unverified")
        return Observation(Presence.UNKNOWN, None, False, monotonic(), "stop_unverified")

    async def _verify_running(self, model_id: str, identity: InstanceIdentity, deadline: float):
        """Poll the independent observation until it agrees the instance is running (C03).

        One sample is not a verdict. A control plane answers as soon as it accepted the
        load, while the container still has to bind its port and pass its own health
        handshake, so sampling once made a load that was still starting look
        unverifiable — and an unverifiable load can only be refused, which costs a whole
        stop and reload to recover. The stop path already polls for its four facts until
        the caller's deadline; the load path now does the same, and a proven STOPPED
        still ends the wait immediately because it is a verdict rather than a sample.
        """
        loop = asyncio.get_event_loop()
        last = None
        while True:
            last = await self._observe(model_id, identity, deadline)
            if last is not None and last.state == RUNNING and last.instance is not None:
                return last
            if last is not None and last.state == STOPPED:
                return last
            if loop.time() >= deadline:
                return last
            await asyncio.sleep(self._poll_seconds)

    async def _observe(self, model_id: str, identity: InstanceIdentity | None, deadline: float):
        observer = self._observers.get(model_id)
        if observer is None:
            return None
        target = ObservationTarget(deployment_id=self._deployment_id,
                                   container_id=None if identity is None else identity.container_id)
        try:
            return await observer.observe(target, deadline)
        except Exception:
            return None

    async def _poll_stopped(self, model_id: str, identity: InstanceIdentity | None, deadline: float):
        """Poll until the four facts prove STOPPED or a running container contradicts the ack."""
        loop = asyncio.get_event_loop()
        last = None
        while True:
            last = await self._observe(model_id, identity, deadline)
            if last is not None and last.state in {STOPPED, RUNNING}:
                return last
            if loop.time() >= deadline:
                return last
            await asyncio.sleep(self._poll_seconds)
