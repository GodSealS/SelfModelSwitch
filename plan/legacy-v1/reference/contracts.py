"""修复计划规范类型；仅标准库。Protocol 是后续实现的边界，不是运行服务。"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import AsyncIterator, Mapping, Protocol


class State(str, Enum):
    UNKNOWN = "unknown"
    UNLOADED = "unloaded"
    LOADING = "loading"
    READY = "ready"
    EVICTING = "evicting"
    ERROR = "error"


class Capability(str, Enum):
    CHAT = "chat"
    EMBEDDINGS = "embeddings"
    RERANK = "rerank"


class Outcome(str, Enum):
    SUCCESS = "success"
    REJECTED = "rejected"  # 已完整收到有效上游 4xx/429；没有未决推理
    ABORTED = "aborted"  # 取消、断连、超时、协议或传输不确定


class Presence(str, Enum):
    RUNNING = "running"
    STOPPED = "stopped"  # 必须满足 02 的停止证据，不只是 /running 缺席
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    upstream_url: str
    capabilities: frozenset[Capability]
    reserved_bytes: int
    priority: int = 0
    max_concurrency: int = 1
    pinned: bool = False
    evictable: bool = True
    ttl_seconds: float = 0


@dataclass(frozen=True)
class MemorySample:
    total_bytes: int
    available_bytes: int
    sampled_at: float  # monotonic


@dataclass(frozen=True)
class Lease:
    lease_id: str
    request_id: str
    model_id: str
    generation: int


@dataclass(frozen=True)
class Operation:
    operation_id: str
    model_id: str
    generation: int
    epoch: int  # 全局控制面恢复时递增，使旧启动/停止/探测回写失效


class GatewayError(RuntimeError):
    def __init__(self, http_status: int, code: str, outcome: Outcome):
        super().__init__(code)
        self.http_status = http_status
        self.code = code
        self.outcome = outcome


@dataclass(frozen=True)
class Observation:
    presence: Presence
    instance_id: str | None  # Docker container ID + StartedAt
    healthy: bool
    observed_at: float
    detail_code: str | None = None


@dataclass(frozen=True)
class Waiter:
    request_id: str
    model_id: str
    priority: int
    sequence: int
    enqueued_at: float
    deadline: float  # 等待 lease 的绝对 monotonic deadline，含共享加载


class BackendControl(Protocol):
    async def observe(self, model_id: str) -> Observation: ...

    async def load(self, operation: Operation, deadline: float) -> Observation:
        """只有确认目标实例健康才返回 RUNNING；超时不能据此释放预算。"""
        ...

    async def stop(self, operation: Operation, deadline: float) -> Observation:
        """先封闭该模型启动入口，再停止；不能得到证据时返回 UNKNOWN 或抛异常。"""
        ...


class OpenedResponse(Protocol):
    status_code: int
    headers: Mapping[str, str]

    def iter_bytes(self) -> AsyncIterator[bytes]: ...

    async def aclose(self) -> None:
        """幂等，只关闭本响应；不得关闭其他请求共享的 AsyncClient。"""
        ...


class InferenceGateway(Protocol):
    async def open(
        self, lease: Lease, capability: Capability,
        payload: Mapping[str, object], deadline: float,
    ) -> OpenedResponse:
        """直连配置端口、不重试；失败抛GatewayError，outcome规定lease释放方式。"""
        ...


class SchedulerPort(Protocol):
    async def acquire(self, model_id: str, request_id: str, deadline: float) -> Lease:
        """取消或超时必须移除 waiter/回收未领取 lease；不得留下后台 Future。"""
        ...

    async def release(self, lease: Lease, outcome: Outcome, tokens: int | None = None) -> None:
        """幂等；ABORTED隔离；None表示usage未知，仅SUCCESS计未知次数。"""
        ...

    async def unload(self, model_id: str, deadline: float) -> None:
        """忙碌/固定模型拒绝；返回成功必须已经证实停止。"""
        ...
