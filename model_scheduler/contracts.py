"""Framework-free contracts for scheduler state and external adapters."""
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
    REJECTED = "rejected"
    ABORTED = "aborted"


class Presence(str, Enum):
    RUNNING = "running"
    STOPPED = "stopped"
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
    preload: bool = False
    ttl_seconds: float = 0


@dataclass(frozen=True)
class MemorySample:
    total_bytes: int
    available_bytes: int
    sampled_at: float


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
    epoch: int


@dataclass(frozen=True)
class Observation:
    presence: Presence
    instance_id: str | None
    healthy: bool
    observed_at: float
    detail_code: str | None = None


@dataclass(frozen=True)
class RecoveryResult:
    """Validated result from the root-owned control-plane recovery helper."""
    ok: bool
    phase: str
    error_code: str | None
    stopped_models: tuple[str, ...]


class GatewayError(RuntimeError):
    def __init__(self, http_status: int, code: str, outcome: Outcome, *, retry_after: int | None = None):
        super().__init__(code)
        self.http_status = http_status
        self.code = code
        self.outcome = outcome
        self.retry_after = retry_after


class BackendControl(Protocol):
    async def observe(self, model_id: str) -> Observation: ...
    async def load(self, operation: Operation, deadline: float) -> Observation: ...
    async def stop(self, operation: Operation, deadline: float) -> Observation: ...


class ControlRecoveryPort(Protocol):
    """The scheduler may request recovery but never executes sudo/systemctl itself."""
    async def recover(self, deadline: float) -> RecoveryResult: ...


class OpenedResponse(Protocol):
    status_code: int
    headers: Mapping[str, str]
    def iter_bytes(self) -> AsyncIterator[bytes]: ...
    async def aclose(self) -> None: ...


class InferenceGateway(Protocol):
    async def open(self, lease: Lease, capability: Capability, payload: Mapping[str, object], deadline: float) -> OpenedResponse: ...


class SchedulerPort(Protocol):
    async def acquire(self, model_id: str, request_id: str, deadline: float) -> Lease: ...
    async def release(self, lease: Lease, outcome: Outcome, tokens: int | None = None) -> None: ...
    async def unload(self, model_id: str, deadline: float) -> None: ...
