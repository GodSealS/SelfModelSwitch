"""Atomic, I/O-free state transitions for model residency, leases and the two ledgers.

Every method is called while the scheduler's single asyncio.Condition is held.
The class intentionally performs no await or external observation: a state change
is committed before I/O starts, and async callers reconcile results with the
operation token afterwards.

The book keeps the C02 accounting for every model that is not proven stopped:

* the model budget B counts `effective_reserved_bytes` (R). A v2 registration's
  measured `reserved_bytes` is already R and is used as-is; a legacy v1 peak gets
  the historical `margin` applied exactly once at that compatibility boundary.
* the static physical budget counts `ceil(physical_resident_peak*1.15)` of the
  same set. It only exists once at least one registration carries a measured
  physical peak; an unmeasured model in such a book is refused instead of being
  guessed at, and a legacy book (no physical figures) keeps its single ledger.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any
from uuid import uuid4

from .contracts import Lease, MemorySample, ModelSpec, Operation, Outcome, State
from .contracts_v2 import ModelSpec as RegisteredModelSpec
from .contracts_v2 import effective_reserved_bytes, physical_reserved_bytes_from_peak
from .control_protocol_v1 import InstanceIdentity

# C02: after a proven stop the next admission needs a sample taken after the
# stop, and the scheduler waits at most this long for the memory to come back.
STOP_RESAMPLE_GRACE_SECONDS = 10.0


class Conflict(RuntimeError):
    pass


class StaleOperation(RuntimeError):
    pass


@dataclass
class Runtime:
    state: State = State.UNKNOWN
    generation: int = 0
    operation_id: str | None = None
    leases: dict[str, Lease] = field(default_factory=dict)
    admission_blocked: bool = False
    reservation: int = 0
    idle_since: float | None = None
    last_error: str | None = None
    heat_value: float = 0.0
    heat_updated_at: float = 0.0
    total_requests: int = 0
    total_tokens: int = 0
    usage_unknown_requests: int = 0
    stopped_at: float | None = None
    cancelling: dict[str, Lease] = field(default_factory=dict)
    # K2: the single accepted identity. Only `Book.loaded` writes it and only a
    # proven stop clears it; a failure, an UNKNOWN or a pending recovery keeps it.
    instance: InstanceIdentity | None = None


@dataclass(frozen=True)
class LedgerSpec:
    """The single internal registration shape the book accounts for (C02)."""

    model_id: str
    effective_reserved_bytes: int
    physical_reserved_bytes: int | None = None
    max_concurrency: int = 1
    pinned: bool = False
    evictable: bool = True
    preload: bool = False
    ttl_seconds: float = 0
    priority: int = 0

    @classmethod
    def from_registration(cls, spec: Any, *, legacy_v1_margin: float) -> LedgerSpec:
        if isinstance(spec, RegisteredModelSpec):
            peak = spec.physical_resident_peak_bytes
            return cls(
                model_id=spec.model_id,
                effective_reserved_bytes=effective_reserved_bytes(spec.reserved_bytes),
                physical_reserved_bytes=None if peak is None else physical_reserved_bytes_from_peak(peak),
                max_concurrency=spec.envelope.max_parallel,
            )
        if not isinstance(spec, ModelSpec):
            raise ValueError(f"unsupported registration for {getattr(spec, 'model_id', spec)!r}")
        return cls(
            model_id=spec.model_id,
            effective_reserved_bytes=effective_reserved_bytes(spec.reserved_bytes, legacy_v1_margin=legacy_v1_margin),
            max_concurrency=spec.max_concurrency,
            pinned=spec.pinned,
            evictable=spec.evictable,
            preload=spec.preload,
            ttl_seconds=spec.ttl_seconds,
            priority=spec.priority,
        )


class Book:
    """The one authoritative accounting book for all managed models."""

    def __init__(self, specs: dict[str, Any], *, model_budget: int, free_floor: int, margin: float = 0.15, max_sample_age: float = 2, half_life: float = 1800, request_weight: float = 1, token_weight: float = 0.0001):
        if model_budget <= 0 or free_floor < 0 or not 0 <= margin <= 1:
            raise ValueError("invalid memory policy")
        if max_sample_age <= 0 or half_life <= 0 or request_weight < 0 or token_weight < 0:
            raise ValueError("invalid time or heat policy")
        if not specs or any(mid != spec.model_id for mid, spec in specs.items()):
            raise ValueError("invalid model spec")
        self.model_budget, self.free_floor, self.margin = model_budget, free_floor, margin
        self.specs = specs.copy()
        self.ledger = {mid: LedgerSpec.from_registration(spec, legacy_v1_margin=margin) for mid, spec in specs.items()}
        if any(entry.max_concurrency <= 0 for entry in self.ledger.values()):
            raise ValueError("invalid model spec")
        self.runtime = {mid: Runtime(reservation=entry.effective_reserved_bytes) for mid, entry in self.ledger.items()}
        self.physical_enforced = any(entry.physical_reserved_bytes is not None for entry in self.ledger.values())
        self.max_sample_age, self.half_life = max_sample_age, half_life
        self.request_weight, self.token_weight = request_weight, token_weight
        self.epoch = 0
        self.recovering = False

    def ledger_spec(self, model_id: str) -> LedgerSpec:
        return self.ledger[model_id]

    def required_for(self, spec: Any) -> int:
        return self.required(spec.model_id)

    def required(self, model_id: str) -> int:
        return self.ledger[model_id].effective_reserved_bytes

    def physical_required(self, model_id: str) -> int | None:
        return self.ledger[model_id].physical_reserved_bytes

    @property
    def committed(self) -> int:
        return sum(item.reservation for item in self.runtime.values())

    @property
    def physical_committed(self) -> int:
        """The static physical figure of every model that is not proven stopped."""
        return sum(
            self.ledger[model_id].physical_reserved_bytes or 0
            for model_id, runtime in self.runtime.items()
            if runtime.reservation > 0
        )

    def physical_admissible(self, model_id: str) -> bool:
        """C02 static physical gate; a legacy book has no physical ledger to check."""
        if not self.physical_enforced:
            return True
        figure = self.ledger[model_id].physical_reserved_bytes
        if figure is None:
            return False  # an unmeasured model cannot be accounted, so it is not admitted
        others = sum(
            self.ledger[other_id].physical_reserved_bytes or 0
            for other_id, runtime in self.runtime.items()
            if other_id != model_id and runtime.reservation > 0
        )
        return others + figure <= self.model_budget

    def stop_settled(self, model_id: str, now: float) -> bool:
        """True once the post-stop reclamation window has elapsed (C02: at most 10s)."""
        stopped_at = self.runtime[model_id].stopped_at
        return stopped_at is not None and now - stopped_at >= STOP_RESAMPLE_GRACE_SECONDS

    def bootstrap_stopped(self, model_id: str) -> None:
        runtime = self.runtime[model_id]
        if runtime.state is not State.UNKNOWN or runtime.operation_id or runtime.leases:
            raise Conflict("not a bootstrap state")
        runtime.state, runtime.reservation = State.UNLOADED, 0
        runtime.instance = None

    def sample_valid(self, sample: MemorySample, now: float) -> bool:
        return 0 <= now - sample.sampled_at <= self.max_sample_age and sample.total_bytes > 0 and 0 <= sample.available_bytes <= sample.total_bytes

    def sample_after_stop(self, model_id: str, sample: MemorySample) -> bool:
        """C02: a sample that predates the last proven stop cannot admit a load.

        A sample taken at the stop instant or later is admissible; the rule exists
        to stop a pre-stop reading from being reused for the next load.
        """
        stopped_at = self.runtime[model_id].stopped_at
        return stopped_at is None or sample.sampled_at >= stopped_at

    def can_load(self, model_id: str, sample: MemorySample, now: float) -> bool:
        runtime, required = self.runtime[model_id], self.required(model_id)
        return (
            not self.recovering
            and runtime.state is State.UNLOADED
            and not runtime.leases
            and not runtime.admission_blocked
            and self.sample_valid(sample, now)
            and self.sample_after_stop(model_id, sample)
            and sample.available_bytes >= self.free_floor + required
            and self.committed + required <= self.model_budget
            and self.physical_admissible(model_id)
        )

    def begin_load(self, model_id: str, sample: MemorySample, now: float) -> Operation:
        if not self.can_load(model_id, sample, now):
            raise Conflict("load not admissible")
        runtime = self.runtime[model_id]
        runtime.generation += 1
        runtime.state, runtime.reservation = State.LOADING, self.required(model_id)
        runtime.operation_id, runtime.idle_since, runtime.last_error = uuid4().hex, None, None
        return Operation(runtime.operation_id, model_id, runtime.generation, self.epoch)

    def _operation(self, operation: Operation) -> Runtime:
        runtime = self.runtime[operation.model_id]
        if operation.epoch != self.epoch or (runtime.generation, runtime.operation_id) != (operation.generation, operation.operation_id):
            raise StaleOperation(operation.operation_id)
        return runtime

    def instance(self, model_id: str) -> InstanceIdentity | None:
        """The one identity this book accepted; None until a load is proven (K2).

        Read-only on purpose: adapters, bridges and the scheduler all look the
        identity up through here, so there is exactly one stored copy and no
        second cache can disagree with it.
        """
        return self.runtime[model_id].instance

    def loaded(self, operation: Operation, now: float, *, instance: InstanceIdentity | None = None) -> None:
        if instance is not None and not isinstance(instance, InstanceIdentity):
            raise ValueError("instance must be an InstanceIdentity or None")
        runtime = self._operation(operation)
        if runtime.state is not State.LOADING:
            raise Conflict("not loading")
        runtime.state, runtime.operation_id, runtime.admission_blocked, runtime.idle_since = State.READY, None, False, now
        runtime.instance = instance

    def failed(self, operation: Operation, code: str) -> None:
        runtime = self._operation(operation)
        runtime.state, runtime.operation_id, runtime.admission_blocked, runtime.last_error = State.ERROR, None, True, code

    def _heat(self, runtime: Runtime, now: float) -> float:
        return runtime.heat_value * math.pow(0.5, max(0, now - runtime.heat_updated_at) / self.half_life)

    def heat(self, model_id: str, now: float) -> float:
        return self._heat(self.runtime[model_id], now)

    def _add_heat(self, model_id: str, now: float, amount: float) -> None:
        runtime = self.runtime[model_id]
        runtime.heat_value, runtime.heat_updated_at = self._heat(runtime, now) + amount, now

    def acquire_ready(self, model_id: str, request_id: str, now: float) -> Lease:
        runtime = self.runtime[model_id]
        if self.recovering or runtime.state is not State.READY or runtime.admission_blocked or len(runtime.leases) >= self.ledger[model_id].max_concurrency:
            raise Conflict("model not admissible")
        if any(item.request_id == request_id for candidate in self.runtime.values() for item in candidate.leases.values()):
            raise Conflict("request already owns a lease")
        lease = Lease(uuid4().hex, request_id, model_id, runtime.generation)
        runtime.leases[lease.lease_id] = lease
        runtime.total_requests, runtime.idle_since = runtime.total_requests + 1, None
        self._add_heat(model_id, now, self.request_weight)
        return lease

    def can_admit_ready(self, model_id: str, sample: MemorySample, now: float) -> bool:
        runtime = self.runtime[model_id]
        return (not self.recovering and runtime.state is State.READY and not runtime.admission_blocked
                and len(runtime.leases) < self.ledger[model_id].max_concurrency
                and self.sample_valid(sample, now) and sample.available_bytes >= self.free_floor)

    def release(self, lease: Lease, outcome: Outcome, now: float, tokens: int | None = None) -> bool:
        runtime = self.runtime[lease.model_id]
        if runtime.leases.get(lease.lease_id) != lease or runtime.generation != lease.generation:
            return False
        if tokens is not None and (type(tokens) is not int or tokens < 0):
            raise ValueError("tokens must be a nonnegative integer")
        del runtime.leases[lease.lease_id]
        runtime.cancelling.pop(lease.lease_id, None)
        if outcome is Outcome.SUCCESS:
            if tokens is None:
                runtime.usage_unknown_requests += 1
            else:
                runtime.total_tokens += tokens
                self._add_heat(lease.model_id, now, tokens * self.token_weight)
        elif outcome is Outcome.ABORTED:
            runtime.state, runtime.admission_blocked, runtime.last_error = State.ERROR, True, "request_aborted"
        if not runtime.leases:
            runtime.idle_since = now
        return True

    def begin_cancel(self, lease: Lease) -> bool:
        """Accept one cancel; the lease and its budget stay until the terminal proof.

        A cancel acknowledgement is not evidence that computation stopped, so the
        model keeps its slot, its reservation and its admission block until the
        execution reports a trusted terminal state (C03/C04).
        """
        runtime = self.runtime[lease.model_id]
        if runtime.leases.get(lease.lease_id) != lease or runtime.generation != lease.generation:
            return False
        runtime.cancelling[lease.lease_id] = lease
        return True

    def is_cancelling(self, lease: Lease) -> bool:
        return self.runtime[lease.model_id].cancelling.get(lease.lease_id) == lease

    def cancelling_leases(self, model_id: str) -> tuple[Lease, ...]:
        return tuple(self.runtime[model_id].cancelling.values())

    def begin_eviction(self, model_ids: list[str], *, automatic: bool = True) -> list[Operation]:
        if self.recovering or not model_ids or len(set(model_ids)) != len(model_ids):
            raise Conflict("invalid eviction batch")
        for model_id in model_ids:
            runtime, spec = self.runtime[model_id], self.ledger[model_id]
            if runtime.state is not State.READY or runtime.leases or runtime.operation_id or spec.pinned or (automatic and not spec.evictable):
                raise Conflict("protected or changed candidate")
        result: list[Operation] = []
        for model_id in model_ids:
            runtime = self.runtime[model_id]
            runtime.state, runtime.admission_blocked, runtime.operation_id = State.EVICTING, True, uuid4().hex
            result.append(Operation(runtime.operation_id, model_id, runtime.generation, self.epoch))
        return result

    def freeze_for_switch(self, model_ids: list[str]) -> None:
        """Prevent new leases while a cold target waits for these models to drain.

        A freeze deliberately leaves existing leases intact.  It is an atomic
        reservation of *admission*, rather than a stop request, so a switch
        cannot strand an in-flight inference.
        """
        if self.recovering or not model_ids or len(set(model_ids)) != len(model_ids):
            raise Conflict("invalid switch freeze")
        for model_id in model_ids:
            runtime, spec = self.runtime[model_id], self.ledger[model_id]
            if (runtime.state is not State.READY or runtime.admission_blocked
                    or runtime.operation_id or spec.pinned or not spec.evictable):
                raise Conflict("switch candidate changed")
        for model_id in model_ids:
            self.runtime[model_id].admission_blocked = True

    def unfreeze_switch(self, model_ids: list[str]) -> None:
        """Release a previously established switch intent without changing leases."""
        for model_id in model_ids:
            runtime = self.runtime[model_id]
            if runtime.state is State.READY and runtime.operation_id is None:
                runtime.admission_blocked = False

    def begin_cleanup(self, model_ids: list[str]) -> list[Operation]:
        """Stop idle READY or ERROR models during recovery or process shutdown."""
        if self.recovering or not model_ids or len(set(model_ids)) != len(model_ids):
            raise Conflict("invalid cleanup batch")
        for model_id in model_ids:
            runtime = self.runtime[model_id]
            if runtime.state not in {State.READY, State.ERROR} or runtime.leases or runtime.operation_id:
                raise Conflict("cleanup candidate changed")
        operations: list[Operation] = []
        for model_id in model_ids:
            runtime = self.runtime[model_id]
            runtime.state, runtime.admission_blocked, runtime.operation_id = State.EVICTING, True, uuid4().hex
            operations.append(Operation(runtime.operation_id, model_id, runtime.generation, self.epoch))
        return operations

    def rollback_unsent(self, operation: Operation) -> None:
        runtime = self._operation(operation)
        if runtime.state is not State.EVICTING or runtime.leases:
            raise Conflict("cannot roll back")
        runtime.state, runtime.admission_blocked, runtime.operation_id = State.READY, False, None

    def stopped(self, operation: Operation, now: float | None = None) -> None:
        """Release both ledgers only here: `now` records when the stop was proven."""
        runtime = self._operation(operation)
        if runtime.state is not State.EVICTING or runtime.leases:
            raise Conflict("not safely stopping")
        runtime.state, runtime.operation_id, runtime.reservation = State.UNLOADED, None, 0
        runtime.admission_blocked, runtime.idle_since, runtime.last_error = False, None, None
        runtime.stopped_at = now
        runtime.instance = None  # a proven stop leaves nothing accepted

    def ttl_due(self, model_id: str, now: float) -> bool:
        runtime, spec = self.runtime[model_id], self.ledger[model_id]
        return (runtime.state is State.READY and not runtime.leases and not runtime.admission_blocked
                and not spec.pinned and spec.evictable and spec.ttl_seconds > 0
                and runtime.idle_since is not None and now - runtime.idle_since >= spec.ttl_seconds)

    def begin_recovery(self) -> int:
        if self.recovering:
            return self.epoch
        self.epoch += 1
        self.recovering = True
        for runtime in self.runtime.values():
            runtime.operation_id, runtime.admission_blocked = None, True
            if runtime.state is not State.UNLOADED:
                runtime.state, runtime.last_error = State.ERROR, "control_recovering"
        return self.epoch

    def finish_recovery(self, epoch: int, confirmed_stopped: frozenset[str]) -> None:
        if not self.recovering or epoch != self.epoch:
            raise StaleOperation("recovery epoch")
        if confirmed_stopped != frozenset(self.specs) or any(runtime.leases for runtime in self.runtime.values()):
            raise Conflict("recovery lacks stop evidence")
        for runtime in self.runtime.values():
            runtime.state, runtime.reservation, runtime.operation_id = State.UNLOADED, 0, None
            runtime.admission_blocked, runtime.idle_since, runtime.last_error = False, None, None
            runtime.instance = None  # every model was proven stopped
        self.recovering = False
