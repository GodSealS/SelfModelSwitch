"""Pure reference transitions. Caller holds the single scheduler condition.

No I/O here. StopEvidence is supplied only by the independently verified observer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math

from contracts import PermitToken


class Conflict(ValueError):
    pass


def positive_int(value: int) -> bool:
    return type(value) is int and value > 0


def reserve_bytes(measured: int) -> int:
    if not positive_int(measured):
        raise ValueError("measured peak must be positive integer")
    return (measured * 115 + 99) // 100


def can_admit(*, total: int, available: int, sampled_at: float, now: float,
              committed: int, new_bytes: int, budget: int, floor: int,
              system_reserve: int, ready: bool, slot_free: bool) -> bool:
    values = (total, available, committed, new_bytes, budget, floor, system_reserve)
    if any(type(v) is not int or v < 0 for v in values) or total <= 0 or budget <= 0 or new_bytes <= 0:
        return False
    if not all(type(v) in (int, float) and math.isfinite(v) for v in (sampled_at, now)):
        return False
    return (ready is True and slot_free is True and 0 <= now - sampled_at <= 2
            and available <= total and budget <= total - system_reserve
            and committed + new_bytes <= budget and available >= new_bytes + floor)


@dataclass(frozen=True)
class StopEvidence:
    instance_id: str
    stopped_or_absent: bool
    launch_finished: bool
    port_closed: bool

    def confirms(self, instance_id: str) -> bool:
        return (self.instance_id == instance_id and self.stopped_or_absent is True
                and self.launch_finished is True and self.port_closed is True)


@dataclass
class Phase:
    token: PermitToken
    reservation: int
    hard_deadline: float
    expires_at: float
    instance_id: str
    state: str = "preparing"
    execution_id: str | None = None
    finished_executions: set[str] = field(default_factory=set)

    def match(self, token: PermitToken) -> None:
        if token != self.token:
            raise Conflict("stale_token")

    def activate(self, token: PermitToken, now: float) -> None:
        self.match(token)
        self.expire(now)
        if self.state != "preparing" or now >= self.hard_deadline:
            raise Conflict("not_preparing_or_expired")
        self.state = "active"
        self.expires_at = min(now + 30, self.hard_deadline)

    def renew(self, token: PermitToken, now: float) -> None:
        self.match(token)
        self.expire(now)
        if self.state != "active":
            raise Conflict("permit_not_active")
        self.expires_at = min(now + 30, self.hard_deadline)

    def expire(self, now: float) -> None:
        if self.state in {"preparing", "active"} and (now >= self.expires_at or now >= self.hard_deadline):
            self.state = "draining"

    def submit(self, token: PermitToken, execution_id: str, now: float) -> None:
        self.match(token)
        self.expire(now)
        if self.state != "active" or self.execution_id is not None or execution_id in self.finished_executions:
            raise Conflict("execution_conflict")
        self.execution_id = execution_id

    def terminal(self, token: PermitToken, execution_id: str, *, compute_quiescent: bool) -> None:
        self.match(token)
        if execution_id in self.finished_executions:
            return
        if self.execution_id != execution_id or compute_quiescent is not True:
            raise Conflict("unverified_execution_terminal")
        self.execution_id = None
        self.finished_executions.add(execution_id)

    def close(self, token: PermitToken) -> None:
        self.match(token)
        if self.state not in {"closed", "blocked"}:
            self.state = "draining"

    def confirm_stopped(self, token: PermitToken, evidence: StopEvidence) -> None:
        self.match(token)
        if self.state == "closed":
            return
        if self.state not in {"draining", "blocked"}:
            raise Conflict("not_draining")
        if not evidence.confirms(self.instance_id):
            self.state = "blocked"
            raise Conflict("stop_unverified")
        # Proven process exit also proves any orphaned execution has ended.
        if self.execution_id is not None:
            self.finished_executions.add(self.execution_id)
        self.execution_id = None
        self.reservation = 0
        self.state = "closed"


JOB_TRANSITIONS = {
    "queued": {"running", "cancelling"},
    "running": {"paused", "failed", "cancelling", "succeeded"},
    "paused": {"queued", "cancelling"},
    "failed": {"queued", "cancelling"},
    "cancelling": {"cancelled", "paused"},
    "cancelled": set(),
    "succeeded": set(),
}


def job_transition(old: str, new: str, *, resources_stopped: bool = False) -> str:
    if new not in JOB_TRANSITIONS.get(old, set()):
        raise Conflict("invalid_job_transition")
    if new in {"cancelled", "succeeded"} and resources_stopped is not True:
        raise Conflict("resources_not_stopped")
    return new
