"""Evidence envelope gate; scenario evaluation is an explicitly trusted port.

This reference does NOT implement GPU runners or production quality evaluators.
Without an evaluator it fails closed. Tests use synthetic evidence only.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Callable

from contracts import AcceptanceReport, Candidate, ScenarioRecord


def canonical_digest(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def configuration_digest(config: dict) -> str:
    """Only the top-level derived candidate binding is excluded, no other keys."""
    return canonical_digest({key: value for key, value in config.items() if key != "candidate_sha256"})


def required_scenarios(candidate: Candidate) -> frozenset[str]:
    cases = {f"S{i:02d}" for i in range(1, 9)} | {f"O{i:02d}" for i in range(1, 7)}
    for model in candidate.models:
        cases.update(f"B:{model.model_id}:{name}" for name in ("load", "infer", "envelope", "cancel", "stop", "reload"))
        cases.update(f"B:{model.model_id}:cap:{cap}" for cap in model.capabilities)
    if candidate.scope != "scheduler-only":
        cases.update(f"P{i:02d}" for i in range(1, 9))
        cases.update(f"Q{i:02d}" for i in range(1, 7))
    if candidate.scope == "full-pipeline":
        cases.update(f"E{i:02d}" for i in range(1, 4))
    return frozenset(cases)


@dataclass(frozen=True)
class GateResult:
    eligible: bool
    reasons: tuple[str, ...]


# Evaluator recomputes assertions AND compares raw run identity/times to case fields.
Evaluator = Callable[[Candidate, ScenarioRecord, dict[str, bytes]], bool]


def utc(value: str) -> datetime:
    if not value.endswith("Z"):
        raise ValueError("UTC Z timestamp required")
    result = datetime.fromisoformat(value[:-1] + "+00:00")
    if result.tzinfo is None:
        raise ValueError("timezone required")
    return result


def verify(candidate: Candidate, report: AcceptanceReport, *, now: datetime,
           read_evidence: Callable[[str], bytes], evaluate: Evaluator | None = None) -> GateResult:
    reasons: list[str] = []
    digest = canonical_digest(candidate.model_dump(mode="json"))
    expected = {
        "candidate_sha256": digest,
        "device_identity_sha256": canonical_digest(candidate.device.model_dump(mode="json")),
        "source_tree_sha256": candidate.source_tree_sha256,
        "evaluator_sha256": candidate.evaluator_sha256,
        "collector_sha256": candidate.collector_sha256,
        "scope": candidate.scope,
    }
    for key, value in expected.items():
        if getattr(report, key) != value:
            reasons.append(f"identity:{key}")
    if now.tzinfo is None:
        return GateResult(False, ("invalid_clock",))
    now = now.astimezone(timezone.utc)
    try:
        start, end = utc(report.started_at), utc(report.finished_at)
        if start > end or end > now + timedelta(minutes=5) or now - start > timedelta(days=7):
            reasons.append("expired_or_invalid_time")
    except ValueError:
        reasons.append("invalid_time")
    ids = [case.scenario_id for case in report.scenarios]
    if len(ids) != len(set(ids)):
        reasons.append("duplicate_scenarios")
    if set(ids) != required_scenarios(candidate):
        reasons.append("scenario_set_mismatch")
    try:
        starts = [utc(case.started_at) for case in report.scenarios]
        ends = [utc(case.finished_at) for case in report.scenarios]
        if (not starts or any(a > b for a, b in zip(starts, ends))
                or utc(report.started_at) != min(starts) or utc(report.finished_at) != max(ends)):
            reasons.append("case_time_range_mismatch")
    except ValueError:
        reasons.append("invalid_case_time")
    if evaluate is None:
        reasons.append("missing_trusted_evaluator")
    for case in report.scenarios:
        if case.candidate_sha256 != digest or case.disposition != "executed":
            reasons.append(f"unexecuted_or_mismatched:{case.scenario_id}")
            continue
        paths = [ref.path for ref in case.evidence]
        if len(paths) != len(set(paths)):
            reasons.append(f"duplicate_evidence:{case.scenario_id}")
            continue
        payloads: dict[str, bytes] = {}
        for ref in case.evidence:
            try:
                raw = read_evidence(ref.path)
            except (OSError, ValueError):
                reasons.append(f"missing_evidence:{ref.path}")
                continue
            if type(raw) is not bytes or len(raw) != ref.size_bytes or hashlib.sha256(raw).hexdigest() != ref.sha256:
                reasons.append(f"corrupt_evidence:{ref.path}")
                continue
            payloads[ref.path] = raw
        if len(payloads) == len(case.evidence) and evaluate is not None:
            try:
                passed = evaluate(candidate, case, payloads)
            except Exception:
                passed = False
            if passed is not True:
                reasons.append(f"assertions_failed:{case.scenario_id}")
    return GateResult(not reasons, tuple(reasons))
