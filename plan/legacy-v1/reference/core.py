"""规范核心：调用者必须持有同一 asyncio.Condition；所有方法均无 IO/await。

此实现覆盖状态转换、lease、批量淘汰、预算、热度和排序。
异步工作循环及 HTTP/Docker 适配器依照 02/03 实现，不在此假装已有完整服务。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from uuid import uuid4

from contracts import Lease, MemorySample, ModelSpec, Operation, Outcome, State, Waiter


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
    # 设定后禁止新 lease；用于切换意图、取消后清理和停止阶段。
    admission_blocked: bool = False
    reservation: int = 0
    idle_since: float | None = None
    last_error: str | None = None
    heat_value: float = 0
    heat_updated_at: float = 0
    total_requests: int = 0
    total_tokens: int = 0
    usage_unknown_requests: int = 0


def waiter_key(waiter: Waiter, now: float, aging_seconds: float = 30) -> tuple[int, int]:
    if aging_seconds <= 0:
        raise ValueError("aging_seconds must be positive")
    effective = waiter.priority + math.floor(max(0, now - waiter.enqueued_at) / aging_seconds)
    return -effective, waiter.sequence


class Book:
    def __init__(
        self, specs: dict[str, ModelSpec], *, model_budget: int,
        free_floor: int, margin: float = 0.15, max_sample_age: float = 2,
        half_life: float = 1800, request_weight: float = 1,
        token_weight: float = 0.0001,
    ):
        if model_budget <= 0 or free_floor < 0 or not 0 <= margin <= 1:
            raise ValueError("invalid memory policy")
        if max_sample_age <= 0 or half_life <= 0 or request_weight < 0 or token_weight < 0:
            raise ValueError("invalid time or heat policy")
        for mid, spec in specs.items():
            if mid != spec.model_id or spec.reserved_bytes <= 0 or spec.max_concurrency <= 0:
                raise ValueError("invalid model spec")
        self.specs = specs.copy()
        self.runtime = {mid: Runtime() for mid in specs}
        self.model_budget = model_budget
        self.free_floor = free_floor
        self.margin = margin
        self.max_sample_age = max_sample_age
        self.half_life = half_life
        self.request_weight = request_weight
        self.token_weight = token_weight
        self.epoch = 0
        self.recovering = False
        # 启动时 UNKNOWN 也计预算；全部确认停止前不开放准入。
        for mid, runtime in self.runtime.items():
            runtime.reservation = self.required(mid)

    def required(self, mid: str) -> int:
        return math.ceil(self.specs[mid].reserved_bytes * (1 + self.margin))

    @property
    def committed(self) -> int:
        return sum(runtime.reservation for runtime in self.runtime.values())

    def bootstrap_stopped(self, mid: str) -> None:
        """仅在全局 readiness=false 且已取得 STOPPED 证据后调用一次。"""
        runtime = self.runtime[mid]
        if runtime.state != State.UNKNOWN or runtime.operation_id or runtime.leases:
            raise Conflict("not a bootstrap state")
        runtime.state = State.UNLOADED
        runtime.reservation = 0

    def sample_valid(self, sample: MemorySample, now: float) -> bool:
        return (
            0 <= now - sample.sampled_at <= self.max_sample_age
            and sample.total_bytes > 0
            and 0 <= sample.available_bytes <= sample.total_bytes
        )

    def can_load(self, mid: str, sample: MemorySample, now: float) -> bool:
        runtime = self.runtime[mid]
        required = self.required(mid)
        return (
            not self.recovering and runtime.state == State.UNLOADED and not runtime.leases
            and not runtime.admission_blocked
            and self.sample_valid(sample, now)
            and sample.available_bytes >= self.free_floor + required
            and self.committed + required <= self.model_budget
        )

    def begin_load(self, mid: str, sample: MemorySample, now: float) -> Operation:
        if not self.can_load(mid, sample, now):
            raise Conflict("load not admissible")
        runtime = self.runtime[mid]
        runtime.generation += 1
        runtime.state = State.LOADING
        runtime.reservation = self.required(mid)
        runtime.operation_id = uuid4().hex
        runtime.idle_since = None
        runtime.last_error = None
        return Operation(runtime.operation_id, mid, runtime.generation, self.epoch)

    def _operation(self, operation: Operation) -> Runtime:
        runtime = self.runtime[operation.model_id]
        if operation.epoch != self.epoch or (runtime.generation, runtime.operation_id) != (operation.generation, operation.operation_id):
            raise StaleOperation(operation.operation_id)
        return runtime

    def loaded(self, operation: Operation, now: float) -> None:
        runtime = self._operation(operation)
        if runtime.state != State.LOADING:
            raise Conflict("not loading")
        runtime.state = State.READY
        runtime.operation_id = None
        runtime.admission_blocked = False
        runtime.idle_since = now

    def failed(self, operation: Operation, code: str) -> None:
        runtime = self._operation(operation)
        runtime.state = State.ERROR
        runtime.operation_id = None
        runtime.admission_blocked = True
        runtime.last_error = code
        # 未取得停止证据，不释放 reservation。

    def heat(self, mid: str, now: float) -> float:
        runtime = self.runtime[mid]
        elapsed = max(0, now - runtime.heat_updated_at)
        return runtime.heat_value * math.pow(0.5, elapsed / self.half_life)

    def _add_heat(self, mid: str, now: float, delta: float) -> None:
        runtime = self.runtime[mid]
        runtime.heat_value = self.heat(mid, now) + delta
        runtime.heat_updated_at = now

    def acquire_ready(self, mid: str, request_id: str, now: float) -> Lease:
        runtime, spec = self.runtime[mid], self.specs[mid]
        if (
            self.recovering or runtime.state != State.READY or runtime.admission_blocked
            or len(runtime.leases) >= spec.max_concurrency
        ):
            raise Conflict("model not admissible")
        if any(lease.request_id == request_id for r in self.runtime.values() for lease in r.leases.values()):
            raise Conflict("request already owns a lease")
        lease = Lease(uuid4().hex, request_id, mid, runtime.generation)
        runtime.leases[lease.lease_id] = lease
        runtime.total_requests += 1
        runtime.idle_since = None
        self._add_heat(mid, now, self.request_weight)
        return lease

    def release(self, lease: Lease, outcome: Outcome, now: float, tokens: int | None = None) -> bool:
        runtime = self.runtime[lease.model_id]
        stored = runtime.leases.get(lease.lease_id)
        if stored != lease or runtime.generation != lease.generation:
            return False
        if tokens is not None and (type(tokens) is not int or tokens < 0):
            raise ValueError("tokens must be a nonnegative integer")
        del runtime.leases[lease.lease_id]
        if outcome == Outcome.SUCCESS:
            if tokens is None:
                runtime.usage_unknown_requests += 1
            else:
                runtime.total_tokens += tokens
                self._add_heat(lease.model_id, now, tokens * self.token_weight)
        if outcome == Outcome.ABORTED:
            runtime.state = State.ERROR
            runtime.admission_blocked = True
            runtime.last_error = "request_aborted"
        if not runtime.leases:
            runtime.idle_since = now
        # 不把 ERROR/EVICTING 等状态无条件改回 READY。
        return True

    def candidates(self, now: float) -> list[str]:
        eligible = [mid for mid, runtime in self.runtime.items() if (
            runtime.state == State.READY and not runtime.leases
            and not self.specs[mid].pinned and self.specs[mid].evictable
        )]
        def key(mid: str) -> tuple[float, int, str]:
            spec = self.specs[mid]
            keep = self.heat(mid, now) * 100 + spec.priority * 5
            return keep / max(self.required(mid) / 1024**3, 0.25), -self.required(mid), mid
        return sorted(eligible, key=key)

    def begin_eviction(self, mids: list[str], *, automatic: bool = True) -> list[Operation]:
        if self.recovering:
            raise Conflict("control recovering")
        if not mids or len(set(mids)) != len(mids):
            raise Conflict("empty or duplicate eviction batch")
        # 校验所有候选后再改任意状态；必须在同一临界区调用。
        for mid in mids:
            runtime, spec = self.runtime[mid], self.specs[mid]
            if runtime.state != State.READY or runtime.leases or runtime.operation_id:
                raise Conflict("candidate changed")
            if spec.pinned or (automatic and not spec.evictable):
                raise Conflict("protected model")
        operations = []
        for mid in mids:
            runtime = self.runtime[mid]
            runtime.state = State.EVICTING
            runtime.admission_blocked = True
            runtime.operation_id = uuid4().hex
            operations.append(Operation(runtime.operation_id, mid, runtime.generation, self.epoch))
        return operations

    def rollback_unsent(self, operation: Operation) -> None:
        """仅适用于完全未发出 stop 的批内候选；一旦发出则只能对账。"""
        runtime = self._operation(operation)
        if runtime.state != State.EVICTING or runtime.leases:
            raise Conflict("cannot roll back")
        runtime.state = State.READY
        runtime.admission_blocked = False
        runtime.operation_id = None

    def begin_cleanup(self, mid: str) -> Operation:
        """故障清理可终止已经取消的工作，但不得伤及其他有效 lease。"""
        runtime = self.runtime[mid]
        if self.recovering:
            raise Conflict("control recovering")
        if runtime.state not in {State.ERROR, State.UNKNOWN} or runtime.leases or runtime.operation_id:
            raise Conflict("cleanup not allowed")
        runtime.state = State.EVICTING
        runtime.admission_blocked = True
        runtime.operation_id = uuid4().hex
        return Operation(runtime.operation_id, mid, runtime.generation, self.epoch)

    def stopped(self, operation: Operation) -> None:
        """调用方已得到 STOPPED 证据；方法本身不进行外部探测。"""
        runtime = self._operation(operation)
        if runtime.state != State.EVICTING or runtime.leases:
            raise Conflict("not safely stopping")
        runtime.state = State.UNLOADED
        runtime.operation_id = None
        runtime.reservation = 0
        runtime.admission_blocked = False
        runtime.idle_since = None
        runtime.last_error = None

    def ttl_due(self, mid: str, now: float) -> bool:
        runtime, spec = self.runtime[mid], self.specs[mid]
        return (
            runtime.state == State.READY and not runtime.leases
            and not runtime.admission_blocked and not spec.pinned and spec.evictable
            and spec.ttl_seconds > 0 and runtime.idle_since is not None
            and now - runtime.idle_since >= spec.ttl_seconds
        )

    def begin_recovery(self) -> int:
        """原子封闭准入并使所有旧operation失效；现有lease仍可正常结束。"""
        if self.recovering:
            return self.epoch
        self.epoch += 1
        self.recovering = True
        for runtime in self.runtime.values():
            runtime.operation_id = None
            runtime.admission_blocked = True
            if runtime.state != State.UNLOADED:
                runtime.state = State.ERROR
                runtime.last_error = "control_recovering"
        return self.epoch

    def finish_recovery(self, epoch: int, confirmed_stopped: frozenset[str]) -> None:
        """helper已封闭旧控制cgroup、确认全部模型停止、重启无preload控制器。"""
        if not self.recovering or epoch != self.epoch:
            raise StaleOperation("recovery epoch")
        if confirmed_stopped != frozenset(self.specs) or any(r.leases for r in self.runtime.values()):
            raise Conflict("recovery lacks stop evidence or still owns leases")
        for runtime in self.runtime.values():
            runtime.state = State.UNLOADED
            runtime.reservation = 0
            runtime.operation_id = None
            runtime.admission_blocked = False
            runtime.idle_since = None
            runtime.last_error = None
        self.recovering = False
