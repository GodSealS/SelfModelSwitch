"""Atomic, I/O-free state transitions for model residency and leases.

Every method is called while the scheduler's single asyncio.Condition is held.
The class intentionally performs no await or external observation: a state change
is committed before I/O starts, and async callers reconcile results with the
operation token afterwards.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from uuid import uuid4

from .contracts import Lease, MemorySample, ModelSpec, Operation, Outcome, State


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


class Book:
    """The one authoritative accounting book for all managed models."""

    def __init__(self, specs: dict[str, ModelSpec], *, model_budget: int, free_floor: int, margin: float = 0.15, max_sample_age: float = 2, half_life: float = 1800, request_weight: float = 1, token_weight: float = 0.0001):
        if model_budget <= 0 or free_floor < 0 or not 0 <= margin <= 1:
            raise ValueError("invalid memory policy")
        if max_sample_age <= 0 or half_life <= 0 or request_weight < 0 or token_weight < 0:
            raise ValueError("invalid time or heat policy")
        if not specs or any(mid != spec.model_id or spec.reserved_bytes <= 0 or spec.max_concurrency <= 0 for mid, spec in specs.items()):
            raise ValueError("invalid model spec")
        self.model_budget, self.free_floor, self.margin = model_budget, free_floor, margin
        self.specs = specs.copy()
        self.runtime = {mid: Runtime(reservation=self.required_for(spec)) for mid, spec in specs.items()}
        self.max_sample_age, self.half_life = max_sample_age, half_life
        self.request_weight, self.token_weight = request_weight, token_weight
        self.epoch = 0
        self.recovering = False

    def required_for(self, spec: ModelSpec) -> int:
        return math.ceil(spec.reserved_bytes * (1 + self.margin))

    def required(self, model_id: str) -> int:
        return self.required_for(self.specs[model_id])

    @property
    def committed(self) -> int:
        return sum(item.reservation for item in self.runtime.values())

    def bootstrap_stopped(self, model_id: str) -> None:
        runtime = self.runtime[model_id]
        if runtime.state is not State.UNKNOWN or runtime.operation_id or runtime.leases:
            raise Conflict("not a bootstrap state")
        runtime.state, runtime.reservation = State.UNLOADED, 0

    def sample_valid(self, sample: MemorySample, now: float) -> bool:
        return 0 <= now - sample.sampled_at <= self.max_sample_age and sample.total_bytes > 0 and 0 <= sample.available_bytes <= sample.total_bytes

    def can_load(self, model_id: str, sample: MemorySample, now: float) -> bool:
        runtime, required = self.runtime[model_id], self.required(model_id)
        return (not self.recovering and runtime.state is State.UNLOADED and not runtime.leases and not runtime.admission_blocked and self.sample_valid(sample, now) and sample.available_bytes >= self.free_floor + required and self.committed + required <= self.model_budget)

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

    def loaded(self, operation: Operation, now: float) -> None:
        runtime = self._operation(operation)
        if runtime.state is not State.LOADING:
            raise Conflict("not loading")
        runtime.state, runtime.operation_id, runtime.admission_blocked, runtime.idle_since = State.READY, None, False, now

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
        runtime, spec = self.runtime[model_id], self.specs[model_id]
        if self.recovering or runtime.state is not State.READY or runtime.admission_blocked or len(runtime.leases) >= spec.max_concurrency:
            raise Conflict("model not admissible")
        if any(item.request_id == request_id for candidate in self.runtime.values() for item in candidate.leases.values()):
            raise Conflict("request already owns a lease")
        lease = Lease(uuid4().hex, request_id, model_id, runtime.generation)
        runtime.leases[lease.lease_id] = lease
        runtime.total_requests, runtime.idle_since = runtime.total_requests + 1, None
        self._add_heat(model_id, now, self.request_weight)
        return lease

    def release(self, lease: Lease, outcome: Outcome, now: float, tokens: int | None = None) -> bool:
        runtime = self.runtime[lease.model_id]
        if runtime.leases.get(lease.lease_id) != lease or runtime.generation != lease.generation:
            return False
        if tokens is not None and (type(tokens) is not int or tokens < 0):
            raise ValueError("tokens must be a nonnegative integer")
        del runtime.leases[lease.lease_id]
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

    def begin_eviction(self, model_ids: list[str], *, automatic: bool = True) -> list[Operation]:
        if self.recovering or not model_ids or len(set(model_ids)) != len(model_ids):
            raise Conflict("invalid eviction batch")
        for model_id in model_ids:
            runtime, spec = self.runtime[model_id], self.specs[model_id]
            if runtime.state is not State.READY or runtime.leases or runtime.operation_id or spec.pinned or (automatic and not spec.evictable):
                raise Conflict("protected or changed candidate")
        result: list[Operation] = []
        for model_id in model_ids:
            runtime = self.runtime[model_id]
            runtime.state, runtime.admission_blocked, runtime.operation_id = State.EVICTING, True, uuid4().hex
            result.append(Operation(runtime.operation_id, model_id, runtime.generation, self.epoch))
        return result

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

    def stopped(self, operation: Operation) -> None:
        runtime = self._operation(operation)
        if runtime.state is not State.EVICTING or runtime.leases:
            raise Conflict("not safely stopping")
        runtime.state, runtime.operation_id, runtime.reservation = State.UNLOADED, None, 0
        runtime.admission_blocked, runtime.idle_since, runtime.last_error = False, None, None

    def ttl_due(self, model_id: str, now: float) -> bool:
        runtime, spec = self.runtime[model_id], self.specs[model_id]
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
        self.recovering = False
