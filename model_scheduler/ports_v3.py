"""Structural ports and observation DTOs for the v3 scheduler (M01/P03, C03).

This module fixes the shape downstream modules depend on without importing each
other: the scheduler talks to a `BackendPort` and an `ObserverPort`, reads time
through a `Clock`, injects memory samples as `MemorySample` facts and publishes
`EventRecord`s through an `EventSink`. The registration types (`ModelSpec`) and
the identity/fence types come from the two pure contracts modules, so there is
exactly one definition of a fence or an instance identity in the service.

Facts, not verdicts:

* `Observation` reports what was seen (`running|stopped|unknown`, the port
  state, the launch operation and the subprocess state) and never claims a
  stop by itself;
* `TerminationEvidence` carries the complete fence, and for a dispatched
  execution the full instance identity, the backend reason, the device-sync
  fact and `compute_quiescent=true`; a locally terminated `not_started`
  request must not invent a container identity;
* `stopped_is_proven` is the single C03 computation: container absent AND
  launch operation terminal AND subprocess exited AND port not listening. A
  port occupied by an unknown process keeps the observation UNKNOWN.

`CancelAck`/`StopAck` only mean the command was processed, never that the
container or computation stopped.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal, Mapping, Protocol, Sequence, runtime_checkable

from .contracts_v2 import ContractError, ModelSpec
from .control_protocol_v1 import Fence, InstanceIdentity

RUNNING = "running"
STOPPED = "stopped"
UNKNOWN = "unknown"
OBSERVATION_STATES = frozenset({RUNNING, STOPPED, UNKNOWN})

PORT_STATES = frozenset({"listening", "closed", "unknown"})
SUBPROCESS_STATES = frozenset({"running", "exited", "absent", "unknown"})
LAUNCH_STATES = frozenset({"starting", "completed", "failed"})
DISPATCH_STATES = frozenset({"not_started", "dispatched"})

# C02 sample freshness window.
SAMPLE_MAX_AGE_SECONDS = 2.0

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_REFERENCE = re.compile(r"^(?:[^\s@]+@)?sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class LifecyclePolicy:
    """The one immutable timing policy for witnesses and recovery (K1).

    These are first-version *software* values, not figures derived from the C02
    hardware evidence; they may only be overridden by an explicit short value in
    a test. Nothing is clamped: an inconsistent policy is refused at construction.
    """

    verify_window_seconds: float = 10.0
    poll_seconds: float = 0.5
    observation_timeout_seconds: float = 2.0
    observation_max_age_seconds: float = 2.0
    recovery_seconds: float = 60.0

    def __post_init__(self) -> None:
        for name in (
            "verify_window_seconds",
            "poll_seconds",
            "observation_timeout_seconds",
            "observation_max_age_seconds",
            "recovery_seconds",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ContractError(f"lifecycle_policy.{name}: must be a finite positive number")
        if self.poll_seconds > self.verify_window_seconds:
            raise ContractError("lifecycle_policy: poll_seconds must not exceed verify_window_seconds")
        if not self.observation_timeout_seconds <= self.observation_max_age_seconds <= self.verify_window_seconds:
            raise ContractError(
                "lifecycle_policy: observation_timeout_seconds <= observation_max_age_seconds <= verify_window_seconds"
            )


@dataclass(frozen=True)
class ExpectedInstance:
    """What this deployment expects one boot to have started (K4).

    The expectation is assembled from the raw configuration bytes by the
    composition root, never derived from an observation: a fact cannot be the
    source of the rule that judges it.
    """

    deployment_id: str
    model_id: str
    runtime_id: str
    image_digest: str
    identity_digest: str
    digest_kind: Literal["config"] = "config"

    def __post_init__(self) -> None:
        _check_identifier(self.deployment_id, "expected_instance.deployment_id")
        _check_identifier(self.model_id, "expected_instance.model_id")
        _check_identifier(self.runtime_id, "expected_instance.runtime_id")
        if not isinstance(self.image_digest, str) or not _IMAGE_REFERENCE.match(self.image_digest):
            raise ContractError("expected_instance.image_digest: must be `name@sha256:<64 hex>`")
        if not isinstance(self.identity_digest, str) or not _HEX64.match(self.identity_digest):
            raise ContractError("expected_instance.identity_digest: must be 64 lowercase hex digits")
        if self.digest_kind != "config":
            raise ContractError("expected_instance.digest_kind: only 'config' is rendered this release")


def _check_identifier(value: object, where: str) -> None:
    if not isinstance(value, str) or not value:
        raise ContractError(f"{where}: must be a non-empty string")


def _check_finite(value: object, where: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ContractError(f"{where}: must be a finite number")


def _check_optional_positive_int(value: object, where: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ContractError(f"{where}: must be a positive integer or null")


def _check_enum(value: object, where: str, allowed: frozenset[str]) -> None:
    if value not in allowed:
        raise ContractError(f"{where}: must be one of {', '.join(sorted(allowed))}")


def _check_aware_utc(value: object, where: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ContractError(f"{where}: must be a timezone-aware UTC datetime")


@dataclass(frozen=True)
class LaunchOperation:
    """The scheduler's own record of a supervised startup (P06 produces it).

    `state=starting` means the operation has not reached a terminal state yet;
    a later observation must not report STOPPED while the launcher is still
    running, even if a timeout has elapsed.
    """

    operation_id: str
    fence: Fence
    started_at_monotonic: float
    state: str
    pid: int | None = None
    process_group_id: int | None = None
    terminal_at_monotonic: float | None = None

    def __post_init__(self) -> None:
        _check_identifier(self.operation_id, "launch_operation.operation_id")
        if not isinstance(self.fence, Fence):
            raise ContractError("launch_operation.fence: must be a Fence")
        _check_finite(self.started_at_monotonic, "launch_operation.started_at_monotonic")
        _check_enum(self.state, "launch_operation.state", LAUNCH_STATES)
        _check_optional_positive_int(self.pid, "launch_operation.pid")
        _check_optional_positive_int(self.process_group_id, "launch_operation.process_group_id")
        if self.terminal_at_monotonic is not None:
            _check_finite(self.terminal_at_monotonic, "launch_operation.terminal_at_monotonic")
        if self.state == "starting" and self.terminal_at_monotonic is not None:
            raise ContractError("launch_operation: a starting operation has no terminal timestamp")
        if self.state != "starting" and self.terminal_at_monotonic is None:
            raise ContractError("launch_operation: a terminal operation requires a terminal timestamp")

    @property
    def is_terminal(self) -> bool:
        return self.state != "starting"


@dataclass(frozen=True)
class MemorySample:
    """One raw memory fact (C02); `system_nonfree_upper_bound_v1` keeps both fields."""

    mem_total_bytes: int
    mem_free_bytes: int
    mem_available_bytes: int
    sampled_at_monotonic: float

    def __post_init__(self) -> None:
        for name in ("mem_total_bytes", "mem_free_bytes", "mem_available_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ContractError(f"memory_sample.{name}: must be a non-negative integer")
        _check_finite(self.sampled_at_monotonic, "memory_sample.sampled_at_monotonic")


def sample_is_fresh(sample: MemorySample, now_monotonic: float, *, max_age_seconds: float = SAMPLE_MAX_AGE_SECONDS) -> bool:
    """C02 freshness: the sample age must be within 0..max_age_seconds, inclusive."""
    _check_finite(now_monotonic, "now_monotonic")
    age = now_monotonic - sample.sampled_at_monotonic
    return 0.0 <= age <= max_age_seconds


@dataclass(frozen=True)
class EventRecord:
    """Structured event for the P22 collector.

    `sequence` is strictly increasing within one boot; the sink is responsible
    for ordering. The payload is JSON-shaped and owned by the emitting module.
    """

    schema_version: int
    event_id: str
    sequence: int
    utc_time: datetime
    monotonic_time: float
    fence: Fence
    type: str
    payload: Mapping = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ContractError("event.schema_version: the structured event schema is version 1")
        _check_identifier(self.event_id, "event.event_id")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 1:
            raise ContractError("event.sequence: must be a positive integer")
        _check_aware_utc(self.utc_time, "event.utc_time")
        _check_finite(self.monotonic_time, "event.monotonic_time")
        if not isinstance(self.fence, Fence):
            raise ContractError("event.fence: must be a Fence")
        _check_identifier(self.type, "event.type")
        if not isinstance(self.payload, Mapping):
            raise ContractError("event.payload: must be a mapping")


@dataclass(frozen=True)
class ObservationTarget:
    """What to observe: a deployment and, when known, the exact instance."""

    deployment_id: str
    container_id: str | None = None
    process_group_id: int | None = None

    def __post_init__(self) -> None:
        _check_identifier(self.deployment_id, "observation_target.deployment_id")
        if self.container_id is not None:
            _check_identifier(self.container_id, "observation_target.container_id")
        _check_optional_positive_int(self.process_group_id, "observation_target.process_group_id")


@dataclass(frozen=True)
class Observation:
    """A fact set sampled from the world; never a stop verdict by itself."""

    state: str
    sampled_at_monotonic: float
    sampled_at_utc: datetime
    port_state: str
    subprocess_state: str
    instance: InstanceIdentity | None = None
    launch_operation: LaunchOperation | None = None
    # False when the observer had no launch record source at all, so nothing about
    # the launch could be proven either way (K4 `launch_unresolved`). A caller must
    # not turn such an observation into STOPPED even if the other facts line up.
    launch_resolved: bool = True

    def __post_init__(self) -> None:
        _check_enum(self.state, "observation.state", OBSERVATION_STATES)
        _check_finite(self.sampled_at_monotonic, "observation.sampled_at_monotonic")
        _check_aware_utc(self.sampled_at_utc, "observation.sampled_at_utc")
        _check_enum(self.port_state, "observation.port_state", PORT_STATES)
        _check_enum(self.subprocess_state, "observation.subprocess_state", SUBPROCESS_STATES)
        if self.instance is not None and not isinstance(self.instance, InstanceIdentity):
            raise ContractError("observation.instance: must be an InstanceIdentity or null")
        if self.launch_operation is not None and not isinstance(self.launch_operation, LaunchOperation):
            raise ContractError("observation.launch_operation: must be a LaunchOperation or null")


def stopped_is_proven(
    *,
    container_absent: bool,
    launch_operation_terminal: bool,
    subprocess_exited: bool,
    port_listening: bool | None,
) -> bool:
    """The single C03 STOPPED computation.

    A port occupied by an unknown process is `None`, which never proves a stop.
    `container_absent` must come from a successful listing or a structured
    not-found, never from an arbitrary non-zero inspect exit code.
    """
    return bool(container_absent) and bool(launch_operation_terminal) and bool(subprocess_exited) and port_listening is False


@dataclass(frozen=True)
class ExecutionRequest:
    """A backend execution request: either inline input or a verified blob path."""

    execution_id: str
    operation: str
    parameters: Mapping = field(default_factory=dict)
    inline_input: Mapping | None = None
    blob_path: str | None = None
    output_directory: str | None = None

    def __post_init__(self) -> None:
        _check_identifier(self.execution_id, "execution_request.execution_id")
        _check_identifier(self.operation, "execution_request.operation")
        if not isinstance(self.parameters, Mapping):
            raise ContractError("execution_request.parameters: must be a mapping")
        if self.inline_input is not None and not isinstance(self.inline_input, Mapping):
            raise ContractError("execution_request.inline_input: must be a mapping or null")
        if (self.inline_input is None) == (self.blob_path is None):
            raise ContractError("execution_request: exactly one of inline_input or blob_path is required")
        if self.blob_path is not None:
            _check_identifier(self.blob_path, "execution_request.blob_path")


@dataclass(frozen=True)
class ExecutionHandle:
    """Identifies one dispatched execution for cancel purposes."""

    execution_id: str
    instance: InstanceIdentity

    def __post_init__(self) -> None:
        _check_identifier(self.execution_id, "execution_handle.execution_id")
        if not isinstance(self.instance, InstanceIdentity):
            raise ContractError("execution_handle.instance: must be an InstanceIdentity")


@dataclass(frozen=True)
class CancelAck:
    """The cancel command was processed; it does not mean the computation stopped."""

    execution_id: str
    accepted: bool

    def __post_init__(self) -> None:
        _check_identifier(self.execution_id, "cancel_ack.execution_id")
        if not isinstance(self.accepted, bool):
            raise ContractError("cancel_ack.accepted: must be a boolean")


@dataclass(frozen=True)
class StopAck:
    """The stop command was processed; it does not mean the instance stopped."""

    accepted: bool

    def __post_init__(self) -> None:
        if not isinstance(self.accepted, bool):
            raise ContractError("stop_ack.accepted: must be a boolean")


@dataclass(frozen=True)
class TerminationEvidence:
    """Execution terminal proof (C03).

    A dispatched execution requires the full instance identity, a backend
    reason, the device-sync fact and `compute_quiescent=true`; a locally
    terminated request uses `dispatch_state=not_started` and must not carry a
    container identity.
    """

    fence: Fence
    dispatch_state: str
    compute_quiescent: bool
    device_synchronized: bool
    reason: str | None = None
    instance: InstanceIdentity | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.fence, Fence):
            raise ContractError("termination.fence: must be a Fence")
        _check_enum(self.dispatch_state, "termination.dispatch_state", DISPATCH_STATES)
        if not isinstance(self.compute_quiescent, bool) or self.compute_quiescent is not True:
            raise ContractError("termination.compute_quiescent: terminal evidence must state compute_quiescent=true")
        if not isinstance(self.device_synchronized, bool):
            raise ContractError("termination.device_synchronized: must be a boolean")
        if self.dispatch_state == "dispatched":
            if self.fence.execution_id is None or self.fence.attempt is None:
                raise ContractError("termination.fence: a dispatched execution requires an execution fence")
            if not isinstance(self.instance, InstanceIdentity):
                raise ContractError("termination.instance: a dispatched execution requires the full instance identity")
            _check_identifier(self.reason, "termination.reason")
        else:
            if self.instance is not None:
                raise ContractError("termination.instance: a not_started termination must not carry a container identity")
            if self.reason is not None:
                _check_identifier(self.reason, "termination.reason")


@runtime_checkable
class BackendPort(Protocol):
    """Lifecycle and execution port implemented by runtime adapters (P15/P16)."""

    async def load(self, spec: ModelSpec, fence: Fence, deadline: float) -> Observation: ...

    async def execute(self, request: ExecutionRequest, fence: Fence, deadline: float) -> ExecutionHandle: ...

    async def cancel(self, handle: ExecutionHandle, deadline: float) -> CancelAck: ...

    async def stop(self, identity: InstanceIdentity, fence: Fence, deadline: float) -> StopAck: ...


@runtime_checkable
class ObserverPort(Protocol):
    """Independent observation port; never reads scheduler memory (C03)."""

    async def observe(self, target: ObservationTarget, deadline: float) -> Observation: ...


@runtime_checkable
class DeadlineDocker(Protocol):
    """A docker CLI whose every call is bounded by one absolute deadline (K4).

    Each invocation re-derives the time that is left from the same deadline, so
    no stage of one sample ever gets a fresh allowance of its own.
    """

    def __call__(self, argv: Sequence[str], *, deadline: float) -> tuple[int, str, str]: ...


@runtime_checkable
class Clock(Protocol):
    """Time source: monotonic for deadlines and samples, UTC for evidence."""

    def monotonic(self) -> float: ...

    def utc_now(self) -> datetime: ...


@runtime_checkable
class EventSink(Protocol):
    """Structured event sink; ordering is the sink's responsibility (P22 persists)."""

    def emit(self, event: EventRecord) -> None: ...
