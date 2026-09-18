"""P24 mixed workload: a planned arrival list, real sends, and the §4 metrics.

The plan is built before the run and validated against 06-acceptance §4
(`MIN_OPERATIONAL_SECONDS`, `MIN_OPERATIONAL_REQUESTS`, `MAX_ARRIVAL_GAP_SECONDS`,
`MAX_SEND_DEVIATION_MILLISECONDS`); the send偏差 is measured against the plan,
not rounded away. Percentiles are **nearest-rank** over successful requests, and
a successful request's latency is queue + load + execute. The 429/504 ratios use
*all sent requests* as the denominator, and the zero-tolerance counters (OOM,
unexpected 500, unsafe evictions, residual instances/leases/sessions) come from
the end-of-run state probe, never from a summary.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import time
from typing import Any, Callable, Mapping, Protocol, Sequence

from ..evidence_contracts import (MAX_ARRIVAL_GAP_SECONDS, MAX_ERROR_RATE, MAX_QUEUE_FULL_RATE,
                                  MAX_SEND_DEVIATION_MILLISECONDS, MAX_TIMEOUT_RATE, MIN_OPERATIONAL_REQUESTS,
                                  MIN_OPERATIONAL_SECONDS)
from .collector import FileCollector

OUTCOMES = ("ok", "queue_full", "timeout", "server_error", "oom", "error")
# §3 O01: the run ends with an empty queue/lease/session set and every instance STOPPED;
# OOM, unexpected 500s, unsafe evictions and residual instances must all be zero.
ZERO_TOLERANCE_STATE = ("oom", "unexpected_500", "unsafe_evictions", "residual", "queue_depth", "leases",
                        "sessions", "instances_running")


class WorkloadError(RuntimeError):
    """The workload refuses a plan or a driver that cannot meet §4."""


@dataclass(frozen=True)
class PlannedRequest:
    index: int
    offset_seconds: float
    model_id: str
    capability: str


@dataclass(frozen=True)
class ArrivalPlan:
    entries: tuple[PlannedRequest, ...]
    duration_seconds: float

    def per_model(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self.entries:
            counts[entry.model_id] = counts.get(entry.model_id, 0) + 1
        return counts

    def gaps(self) -> list[float]:
        return [second.offset_seconds - first.offset_seconds
                for first, second in zip(self.entries, self.entries[1:])]

    def document(self) -> dict:
        return {"duration_seconds": self.duration_seconds, "requests": len(self.entries),
                "per_model": self.per_model(), "max_planned_gap_seconds": max(self.gaps()) if self.gaps() else 0.0,
                "entries": [{"index": entry.index, "offset_seconds": entry.offset_seconds,
                             "model_id": entry.model_id, "capability": entry.capability} for entry in self.entries]}


def build_arrival_plan(*, models: Sequence[str], duration_seconds: float, requests: int,
                       capability: str = "chat") -> ArrivalPlan:
    """Round-robin arrival list over the duration; the last request closes the run."""
    if not models:
        raise WorkloadError("an arrival plan needs at least one model")
    if isinstance(requests, bool) or not isinstance(requests, int) or requests < 1:
        raise WorkloadError("requests must be a positive integer")
    if requests < 3 * len(models):
        raise WorkloadError(f"{requests} requests cannot give every model 3 arrivals (needs >= {3 * len(models)})")
    if duration_seconds <= 0:
        raise WorkloadError("the duration must be positive")
    step = duration_seconds / requests if requests > 1 else 0.0
    if step > MAX_ARRIVAL_GAP_SECONDS:
        raise WorkloadError(f"a {duration_seconds}s run with {requests} requests leaves {step:.1f}s gaps: "
                            f"the maximum is {MAX_ARRIVAL_GAP_SECONDS}s")
    entries = tuple(PlannedRequest(index=index, offset_seconds=round(index * step, 3),
                                   model_id=models[index % len(models)], capability=capability)
                    for index in range(requests))
    return ArrivalPlan(entries=entries, duration_seconds=duration_seconds)


def validate_arrival_plan(plan: ArrivalPlan) -> list[str]:
    problems: list[str] = []
    if len(plan.entries) < MIN_OPERATIONAL_REQUESTS:
        problems.append(f"the plan has {len(plan.entries)} arrivals, fewer than the required {MIN_OPERATIONAL_REQUESTS}")
    if plan.duration_seconds < MIN_OPERATIONAL_SECONDS:
        problems.append(f"the plan lasts {plan.duration_seconds}s, shorter than the required {MIN_OPERATIONAL_SECONDS}s")
    for model_id, count in sorted(plan.per_model().items()):
        if count < 3:
            problems.append(f"model {model_id!r} has only {count} arrivals (needs >= 3)")
    if plan.gaps() and max(plan.gaps()) > MAX_ARRIVAL_GAP_SECONDS:
        problems.append(f"a planned gap of {max(plan.gaps()):.1f}s exceeds {MAX_ARRIVAL_GAP_SECONDS}s")
    return problems


@dataclass(frozen=True)
class SentRequest:
    index: int
    model_id: str
    capability: str
    planned_offset_seconds: float
    sent_offset_seconds: float
    deviation_milliseconds: float
    outcome: str
    status_code: int | None
    queue_seconds: float | None
    load_seconds: float | None
    execute_seconds: float | None
    error: str | None = None
    facts: Mapping[str, Any] = field(default_factory=dict)

    @property
    def total_seconds(self) -> float | None:
        if self.outcome != "ok":
            return None
        parts = [self.queue_seconds, self.load_seconds, self.execute_seconds]
        if any(part is None for part in parts):
            return None
        return float(sum(part for part in parts if part is not None))

    def document(self) -> dict:
        return {"index": self.index, "model_id": self.model_id, "capability": self.capability,
                "planned_offset_seconds": self.planned_offset_seconds,
                "sent_offset_seconds": self.sent_offset_seconds,
                "deviation_milliseconds": round(self.deviation_milliseconds, 3),
                "outcome": self.outcome, "status_code": self.status_code,
                "queue_seconds": self.queue_seconds, "load_seconds": self.load_seconds,
                "execute_seconds": self.execute_seconds, "total_seconds": self.total_seconds,
                "error": self.error}


class WorkloadDriver(Protocol):
    """The official API surface: send one request and report what happened."""

    def send(self, request: PlannedRequest) -> Mapping[str, Any]: ...


def _outcome_of(response: Mapping[str, Any]) -> tuple[str, int | None, str | None]:
    status = response.get("status_code")
    if isinstance(status, bool) or not isinstance(status, int):
        return "error", None, str(response.get("error") or "no status code was reported")
    if status == 429:
        return "queue_full", status, None
    if status == 504:
        return "timeout", status, None
    if status == 500:
        return "server_error", status, None
    if 200 <= status < 300:
        return "ok", status, None
    return "error", status, f"unexpected status {status}"


def run_workload(plan: ArrivalPlan, driver: WorkloadDriver, *, clock: Callable[[], float] | None = None,
                 wait: Callable[[float], None] | None = None, collector: FileCollector | None = None) -> list[SentRequest]:
    """Send every planned request at its planned offset and record what happened."""
    monotonic = clock if clock is not None else time.monotonic
    pause = wait if wait is not None else time.sleep
    started = monotonic()
    sent: list[SentRequest] = []
    for entry in plan.entries:
        remaining = (started + entry.offset_seconds) - monotonic()
        if remaining > 0:
            pause(remaining)
        sent_at = monotonic() - started
        try:
            response = dict(driver.send(entry) or {})
        except Exception as exc:  # noqa: BLE001 - a send failure is material, not a reason to stop the run
            response = {"error": f"{type(exc).__name__}: {exc}"}
        outcome, status, error = _outcome_of(response)
        record = SentRequest(index=entry.index, model_id=entry.model_id, capability=entry.capability,
                             planned_offset_seconds=entry.offset_seconds, sent_offset_seconds=round(sent_at, 3),
                             deviation_milliseconds=abs(sent_at - entry.offset_seconds) * 1000.0,
                             outcome=outcome, status_code=status,
                             queue_seconds=response.get("queue_seconds") if outcome == "ok" else None,
                             load_seconds=response.get("load_seconds") if outcome == "ok" else None,
                             execute_seconds=response.get("execute_seconds") if outcome == "ok" else None,
                             error=error, facts=response)
        sent.append(record)
        if collector is not None:
            collector.record_sample("workload", record.document())
    return sent


def nearest_rank(values: Sequence[float], percentile: float) -> float:
    """06-acceptance §4 nearest-rank percentile (1-based ceil)."""
    if not values:
        raise WorkloadError("a percentile needs at least one value")
    if not 0 < percentile <= 100:
        raise WorkloadError("the percentile must be in (0, 100]")
    ordered = sorted(values)
    rank = math.ceil(percentile / 100 * len(ordered))
    return float(ordered[max(rank, 1) - 1])


def _ratio(count: int, total: int) -> float:
    return 0.0 if total == 0 else count / total


def evaluate_workload(sent: Sequence[SentRequest], *, final_state: Mapping[str, Any],
                      policy: Mapping[str, Any] | None = None) -> dict:
    """The §4 metrics and the zero-tolerance end state. Returns the observed numbers."""
    total = len(sent)
    counts = {outcome: sum(1 for record in sent if record.outcome == outcome) for outcome in OUTCOMES}
    problems: list[str] = []
    deviations = [record.deviation_milliseconds for record in sent]
    if deviations and max(deviations) > MAX_SEND_DEVIATION_MILLISECONDS:
        problems.append(f"a send偏差 of {max(deviations):.1f}ms exceeds {MAX_SEND_DEVIATION_MILLISECONDS}ms")

    latencies = [record.total_seconds for record in sent if record.total_seconds is not None]
    per_model: dict[str, list[float]] = {}
    for record in sent:
        if record.total_seconds is not None:
            per_model.setdefault(record.model_id, []).append(record.total_seconds)

    metrics = {
        "sent": total,
        "outcomes": counts,
        "success_rate": _ratio(counts["ok"], total),
        "queue_full_rate": _ratio(counts["queue_full"], total),
        "timeout_rate": _ratio(counts["timeout"], total),
        "server_error_rate": _ratio(counts["server_error"], total),
        "error_rate": _ratio(counts["server_error"] + counts["error"] + counts["oom"], total),
        "send_deviation_milliseconds_max": round(max(deviations), 3) if deviations else None,
        "latency_p95_seconds": nearest_rank(latencies, 95) if latencies else None,
        "latency_p99_seconds": nearest_rank(latencies, 99) if latencies else None,
        "per_model_latency_p95_seconds": {model: nearest_rank(values, 95) for model, values in sorted(per_model.items())},
        "per_model_latency_p99_seconds": {model: nearest_rank(values, 99) for model, values in sorted(per_model.items())},
        "final_state": dict(final_state),
    }

    limits = {"queue_full_rate": MAX_QUEUE_FULL_RATE, "timeout_rate": MAX_TIMEOUT_RATE, "error_rate": MAX_ERROR_RATE}
    if policy is not None:
        # 06-acceptance §4: a policy may only be stricter than the acceptance caps.
        for name, cap in (("queue_full_rate_max", MAX_QUEUE_FULL_RATE), ("timeout_rate_max", MAX_TIMEOUT_RATE),
                          ("error_rate_max", MAX_ERROR_RATE)):
            declared = float(policy.get(name, cap))
            if declared > cap:
                problems.append(f"the policy {name}={declared} is looser than the acceptance cap {cap}")
        limits = {"queue_full_rate": float(policy.get("queue_full_rate_max", MAX_QUEUE_FULL_RATE)),
                  "timeout_rate": float(policy.get("timeout_rate_max", MAX_TIMEOUT_RATE)),
                  "error_rate": float(policy.get("error_rate_max", MAX_ERROR_RATE))}
    for name, limit in sorted(limits.items()):
        if metrics[name] > limit:
            problems.append(f"{name}={metrics[name]:.4f} exceeds {limit}")

    for key in ZERO_TOLERANCE_STATE:
        value = final_state.get(key)
        if value is None:
            problems.append(f"the end state does not report {key!r}: it cannot be proven zero")
        elif value:
            problems.append(f"an OOM occurred during the run" if key == "oom"
                            else f"{key}={value} must be 0 at the end of the run")
    metrics["problems"] = list(problems)  # every check above is included, not a stale copy
    metrics["verdict"] = "passed" if not problems else "failed"
    return metrics
