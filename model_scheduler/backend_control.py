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

The bridge does *not* own an identity: it looks the accepted one up in the book
(K2), so the adapter's execute/cancel/stop always carry the same identity the
books saw at READY time (P16 AC1: "instance/Fence consistent at every step").
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import monotonic
from typing import Any, Awaitable, Callable, Mapping, Protocol

from .contracts import Observation, Operation, Presence
from .control_protocol_v1 import Fence, InstanceIdentity
from .ports_v3 import STOPPED, RUNNING, ExpectedInstance, LifecyclePolicy, ObservationTarget


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

    ``load`` asks the adapter — a control response, never a verdict — and then
    re-verifies the instance through the independent observer inside one bounded
    window. ``stop`` sends the unload for the book's identity and polls until the
    four facts prove STOPPED.

    The bridge owns no identity of its own: `instance()` is a read-only lookup into
    the book, which is the single owner (K2). A load or a stop therefore never
    *writes* an identity; it only reports a fact the scheduler may then commit.
    """

    def __init__(
        self,
        *,
        boot_id: str,
        deployment_id: str,
        specs: Mapping[str, Any],
        adapter_for: Callable[[str], Any],
        observers: Mapping[str, Any],
        instance_lookup: Callable[[str], InstanceIdentity | None],
        policy: LifecyclePolicy | None = None,
        now: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        expected: Mapping[str, ExpectedInstance] | None = None,
    ) -> None:
        if not boot_id or not deployment_id:
            raise ValueError("a managed lifecycle needs a boot and a deployment")
        if instance_lookup is None:
            raise ValueError("a managed lifecycle needs the book's identity lookup")
        self._boot_id = boot_id
        self._deployment_id = deployment_id
        self._specs = dict(specs)
        self._adapter_for = adapter_for
        self._observers = dict(observers)
        self._instance_lookup = instance_lookup
        self._policy = policy or LifecyclePolicy()
        self._now = now or monotonic
        self._sleep = sleep or asyncio.sleep
        self._expected = dict(expected) if expected is not None else None

    def instance(self, model_id: str) -> InstanceIdentity | None:
        """Read-only: the book is the single owner of the accepted identity."""
        return self._instance_lookup(model_id)

    def fence(self, model_id: str, generation: int, operation_id: str) -> Fence:
        return Fence(self._boot_id, model_id, generation, operation_id, None, None)

    async def observe(self, model_id: str, deadline: float) -> Any:
        """One independent fact sample (admin/health endpoints may surface it)."""
        return await self._observe(model_id, self._instance_lookup(model_id), deadline)

    async def load(self, operation: Operation, deadline: float) -> Observation:
        model_id = operation.model_id
        adapter = self._adapter_for(model_id)
        try:
            verified = await adapter.load(self._specs[model_id], self.fence(model_id, operation.generation, operation.operation_id), deadline)
        except Exception:
            return self._unknown("load_failed")
        if verified.state != RUNNING:
            return self._unknown("load_unverified")
        if self._observers.get(model_id) is None:
            return self._unknown("observer_missing")  # no observer: refuse at once, never guess
        # A healthy adapter answer is still only a control response, so the instance has
        # to be witnessed. The witness gets its own window *inside* the caller's.
        verify_deadline = min(deadline, self._now() + self._policy.verify_window_seconds)
        return await self._verify_running(model_id, verified.instance, verify_deadline)

    async def _verify_running(self, model_id: str, identity: InstanceIdentity | None, verify_deadline: float) -> Observation:
        """Witness the instance inside one bounded window (K1).

        One sample is not a verdict: a control plane answers as soon as it accepted the
        load, while the container still has to bind its port and pass its own handshake.
        A proven STOPPED ends the wait immediately because it is a verdict, not a sample;
        anything else is polled — but never past the window, and never on a sample that
        arrived too late to still describe the world.
        """
        while True:
            if self._now() >= verify_deadline:
                return self._unknown("verify_deadline_exhausted")
            observation = await self._observe(model_id, identity, verify_deadline)
            if observation is None:
                return self._unknown("observer_failed")
            if observation.state == STOPPED:
                if not observation.launch_resolved:
                    return self._unknown("launch_unresolved")  # K4: never a stop, never resolved
                return self._translated(observation, self._valid_until(observation, verify_deadline),
                                        "load_proven_stopped")
            if observation.state == RUNNING:
                valid_until = self._valid_until(observation, verify_deadline)
                reason = self._refusal_reason(observation, identity, model_id, valid_until)
                return self._unknown(reason) if reason else self._translated(observation, valid_until)
            remaining = verify_deadline - self._now()
            if remaining <= 0:
                return self._unknown("verify_deadline_exhausted")
            await self._sleep(min(self._policy.poll_seconds, remaining))

    def _valid_until(self, observation: Any, deadline: float) -> float:
        """K2: the verdict's own acceptance deadline, derived here and never by the observer."""
        return min(deadline, observation.sampled_at_monotonic + self._policy.observation_max_age_seconds)

    def _refusal_reason(self, observation: Any, identity: InstanceIdentity | None, model_id: str,
                        valid_until: float) -> str | None:
        """Why a RUNNING sample cannot be accepted; None means it can."""
        now = self._now()
        instance = observation.instance
        if instance is None:
            return "observation_identity_mismatch"  # RUNNING without a complete identity
        if now >= valid_until:
            return "observation_stale"  # the verdict has run out
        if not 0 <= now - observation.sampled_at_monotonic <= self._policy.observation_max_age_seconds:
            return "observation_stale"  # a future stamp or a sample describing the past
        if instance.deployment_id != self._deployment_id or instance.model_id != model_id:
            return "observation_identity_mismatch"
        if identity is not None and (instance.container_id != identity.container_id
                                     or instance.started_at != identity.started_at):
            return "observation_identity_mismatch"
        expected = None if self._expected is None else self._expected.get(model_id)
        if expected is not None and (
            instance.runtime_id != expected.runtime_id
            or instance.image_digest != expected.image_digest
            or instance.candidate_digest != expected.identity_digest
        ):
            # K4: the bridge re-checks the fields against the composition's own
            # expectation, so a fact that slipped past the labels still cannot land.
            return "observation_identity_mismatch"
        return None

    async def stop(self, operation: Operation, deadline: float) -> Observation:
        model_id = operation.model_id
        identity = self._instance_lookup(model_id)
        adapter = self._adapter_for(model_id)
        if identity is not None:
            try:
                await adapter.stop(
                    identity, self.fence(model_id, operation.generation, operation.operation_id), deadline)
            except Exception:
                pass  # the facts decide; an ack or a failed control call proves nothing either way
        else:
            # A load that failed verification can leave the control plane holding a container
            # this boot never accepted. `release` is a declared capability of a managed
            # adapter, not something discovered with getattr, and it is bounded by the same
            # deadline; the facts below still have to prove the stop.
            try:
                await adapter.release(model_id, deadline)
            except Exception:
                pass
        if self._observers.get(model_id) is None:
            return self._unknown("observer_missing")
        return await self._poll_stopped(model_id, identity, deadline)

    async def _poll_stopped(self, model_id: str, identity: InstanceIdentity | None, deadline: float) -> Observation:
        """Poll until the four facts prove STOPPED or a running container contradicts the ack."""
        while True:
            if self._now() >= deadline:
                return self._unknown("stop_unverified")
            observation = await self._observe(model_id, identity, deadline)
            if observation is None:
                return self._unknown("observer_failed")
            if observation.state == STOPPED:
                if not observation.launch_resolved:
                    return self._unknown("launch_unresolved")
                return self._translated(observation, self._valid_until(observation, deadline))
            if observation.state == RUNNING:
                return self._translated(observation, self._valid_until(observation, deadline), "stop_unverified")
            remaining = deadline - self._now()
            if remaining <= 0:
                return self._unknown("stop_unverified")
            await self._sleep(min(self._policy.poll_seconds, remaining))

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

    def _translated(self, observation: Any, valid_until: float, code: str | None = None) -> Observation:
        instance = observation.instance
        running = observation.state == RUNNING
        return Observation(
            presence=Presence.RUNNING if running else Presence.STOPPED if observation.state == STOPPED else Presence.UNKNOWN,
            instance_id=None if instance is None or not running else f"{instance.container_id}:{instance.started_at}",
            healthy=running,
            observed_at=observation.sampled_at_monotonic,
            detail_code=code,
            instance=instance,
            valid_until=valid_until,
        )

    def _unknown(self, code: str) -> Observation:
        return Observation(Presence.UNKNOWN, None, False, self._now(), code)
