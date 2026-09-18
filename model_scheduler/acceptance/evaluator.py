"""P25: recompute every case conclusion from raw material.

The evaluator never reads a stored verdict: no `status`, no `passed` flag, no
"gpu_verified" boolean and no summary can decide a case. It re-derives each
conclusion from the material a case left behind:

* device attribution is recomputed from the raw `tegrastats` / `proc_maps`
  samples (a missing raw sample means no attribution, never an assumed one);
* B inference cases are re-checked against the declared fixture boundary and the
  real output (shape, finiteness, range), using the same predicates as the P23
  executor;
* O01 metrics (percentiles, ratios, send deviation, end state) are recomputed
  from the raw workload rows, and O02—O06 conclusions from the collected facts
  with the same predicates as the P24 orchestrator;
* S01—S06 are recomputed from their declared observations, and S03's memory
  arithmetic is recomputed from the raw numbers at the boundary and one byte
  under it.

Anything that cannot be recomputed (missing rule, missing material) is a
failure, not a pass.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..evidence_contracts import CandidateV3, AcceptanceReportV3, CaseAttempt, ContractError
from . import backend_cases as bc
from . import operational_cases as oc
from . import workload as wl
from .fixtures import Fixture

GR3D_PERCENT = re.compile(r"GR3D_FREQ\s+(\d+)%")
CUDA_LIBRARY_MARKERS = ("libcuda.so", "libcudart.so", "libcublas")
S_CASE_OBSERVATIONS: dict[str, tuple[str, ...]] = {
    "S01": ("runtime_registered", "strict_schema_enforced", "legacy_config_migrated", "no_business_coupling"),
    "S02": ("load_failure_recorded", "late_load_rejected", "old_boot_rejected",
            "stop_returned_instance_alive_recorded", "unknown_keeps_budget"),
    "S03": ("boundary_equality_holds", "boundary_one_byte_rejected", "sample_freshness_enforced",
            "no_double_counting"),
    "S04": ("interactive_priority_holds", "drain_keeps_lease", "ttl_enforced", "hard_deadline_enforced",
            "cancel_observed", "idempotency_enforced"),
    "S05": ("compat_api_accepts", "blob_owner_enforced", "blob_hash_enforced", "quota_enforced",
            "expiry_enforced", "restart_discovery_works", "late_output_rejected"),
    "S06": ("tampered_candidate_rejected", "tampered_evidence_rejected", "tampered_asset_rejected",
            "tampered_device_rejected", "duplicate_final_rejected", "forged_summary_rejected"),
}


class EvaluatorError(RuntimeError):
    """The evaluator refuses material it cannot recompute."""


@dataclass(frozen=True)
class CaseVerdict:
    case_id: str
    passed: bool
    reasons: tuple[str, ...] = ()
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def document(self) -> dict:
        return {"case_id": self.case_id, "passed": self.passed, "reasons": list(self.reasons),
                "evidence": dict(self.evidence)}


def load_case_material(directory: Path) -> Mapping[str, Any]:
    path = Path(directory) / "case.json"
    if not path.is_file():
        raise EvaluatorError(f"{directory}: case.json is missing: the case cannot be recomputed")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvaluatorError(f"cannot read {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise EvaluatorError(f"{path}: the case material must be a JSON object")
    return document


def raw_samples(directory: Path, kind: str) -> list[dict]:
    path = Path(directory) / "samples" / f"{kind}.jsonl"
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def recompute_device_attribution(directory: Path) -> dict:
    """Device activity derived from the raw samples only."""
    tegrastats = raw_samples(directory, "tegrastats")
    proc_maps = raw_samples(directory, "proc_maps")
    gr3d_peak: int | None = None
    for row in tegrastats:
        found = GR3D_PERCENT.search(str(row.get("raw", "")))
        if found is not None:
            gr3d_peak = max(gr3d_peak or 0, int(found.group(1)))
    cuda_mapped = any(any(marker in str(row.get("raw", "")) for marker in CUDA_LIBRARY_MARKERS)
                      for row in proc_maps)
    return {"gr3d_peak_pct": gr3d_peak, "cuda_library_mapped": cuda_mapped,
            "raw_samples": {"tegrastats": len(tegrastats), "proc_maps": len(proc_maps)}}


def _fixture_of(material: Mapping[str, Any]) -> Fixture | None:
    fixture = material.get("fixture")
    if not isinstance(fixture, Mapping) or not isinstance(fixture.get("boundary"), Mapping):
        return None
    return Fixture(capability=str(fixture.get("capability") or "chat"), fixture_id=str(fixture.get("fixture_id") or ""),
                   payload={}, boundary=dict(fixture["boundary"]), artifact_name="")


def _evaluate_backend(case_id: str, material: Mapping[str, Any], directory: Path) -> CaseVerdict:
    parts = case_id.split(":")
    kind = "capability" if parts[2] == "cap" else parts[2]
    capability = parts[3] if kind == "capability" else None
    facts = dict(material)
    reasons: list[str] = []
    attribution = recompute_device_attribution(directory)
    if not attribution["raw_samples"]["tegrastats"] and not attribution["raw_samples"]["proc_maps"]:
        reasons.append("no raw device samples are present: attribution cannot be recomputed")
    if kind in bc.INFERENCE_KINDS:
        if attribution["gr3d_peak_pct"] is None and not attribution["cuda_library_mapped"]:
            reasons.append("the raw samples show no attributable device activity")
    facts["device_activity"] = attribution
    if material.get("failure"):
        reasons.append(f"the attempt recorded a failure: {material['failure']}")
    reasons.extend(bc.attribution_problems(kind, facts))
    if kind == "envelope":
        fixture = _fixture_of(material)
        if fixture is None:
            reasons.append("the declared fixture boundary is missing: the round cannot be recomputed")
        else:
            observed = material.get("observed")
            reasons.extend(f"did not reach the declared boundary: {item}"
                           for item in bc_boundary_shortfalls(fixture, observed))
    if capability is not None and isinstance(material.get("output"), Mapping):
        reasons.extend(bc.capability_output_problems(capability, material["output"]))
    elif capability is not None:
        reasons.append("no real output is present: the capability case cannot be recomputed")
    return CaseVerdict(case_id=case_id, passed=not reasons, reasons=tuple(reasons),
                       evidence={"kind": kind, "attribution": attribution,
                                 "observed": material.get("observed")})


def bc_boundary_shortfalls(fixture: Fixture, observed: Any) -> list[str]:
    from .fixtures import boundary_shortfalls

    return boundary_shortfalls(fixture, observed if isinstance(observed, Mapping) else {})


def _evaluate_operational(case_id: str, material: Mapping[str, Any], directory: Path) -> CaseVerdict:
    if case_id == "O01":
        rows = raw_samples(directory, "workload")
        if not rows:
            return CaseVerdict(case_id=case_id, passed=False,
                               reasons=("no raw workload rows are present: the metrics cannot be recomputed",))
        fields = set(wl.SentRequest.__dataclass_fields__)
        sent = [wl.SentRequest(**{key: value for key, value in row.get("raw", {}).items() if key in fields})
                for row in rows]
        policy = material.get("policy")
        final_state = material.get("final_state")
        if not isinstance(policy, Mapping) or not isinstance(final_state, Mapping):
            return CaseVerdict(case_id=case_id, passed=False,
                               reasons=("the policy or end state is missing: the metrics cannot be recomputed",))
        metrics = wl.evaluate_workload(sent, final_state=final_state, policy=policy)
        return CaseVerdict(case_id=case_id, passed=metrics["verdict"] == "passed",
                           reasons=tuple(metrics["problems"]),
                           evidence={key: metrics[key] for key in ("sent", "queue_full_rate", "timeout_rate",
                                                                   "error_rate", "latency_p95_seconds",
                                                                   "latency_p99_seconds",
                                                                   "send_deviation_milliseconds_max")})
    facts = material.get("observations")
    if not isinstance(facts, Mapping):
        facts = material
    objections = oc.recompute_objections(case_id, facts)
    return CaseVerdict(case_id=case_id, passed=not objections, reasons=tuple(objections),
                       evidence={"recomputed_from": "collected facts"})


def _recompute_s03(material: Mapping[str, Any]) -> list[str]:
    numbers = material.get("numbers")
    if not isinstance(numbers, Mapping):
        return ["S03: the raw memory numbers are missing: the arithmetic cannot be recomputed"]
    required = ("measured_peak_bytes", "model_budget_bytes", "mem_available_bytes", "free_floor_bytes",
                "committed_bytes")
    missing = [key for key in required
               if isinstance(numbers.get(key), bool) or not isinstance(numbers.get(key), int)]
    if not isinstance(numbers.get("instance_already_reserved"), bool):
        missing.append("instance_already_reserved")
    if missing:
        return [f"S03: the raw numbers {', '.join(sorted(missing))} are missing or not integers"]
    peak = numbers["measured_peak_bytes"]
    reserved = (peak * 115 + 99) // 100
    new = 0 if numbers["instance_already_reserved"] else reserved
    floor = numbers["free_floor_bytes"]
    available = numbers["mem_available_bytes"]
    budget = numbers["model_budget_bytes"]
    committed = numbers["committed_bytes"]
    problems: list[str] = []
    if committed + new <= budget:
        if available != new + floor:
            problems.append("S03: the equality sample was not taken exactly on the MemAvailable boundary")
        if not (committed + new <= budget and available >= new + floor):
            problems.append("S03: the boundary sample must be admissible")
    else:
        problems.append("S03: the supplied numbers never sit on the budget boundary")
    if available - 1 >= new + floor:
        problems.append("S03: one byte less than the boundary was still admissible")
    return problems


def _evaluate_software(case_id: str, material: Mapping[str, Any]) -> CaseVerdict:
    required = S_CASE_OBSERVATIONS.get(case_id)
    if required is None:
        return CaseVerdict(case_id=case_id, passed=False,
                           reasons=(f"no recomputation rule exists for case {case_id!r}",))
    observations = material.get("observations")
    reasons: list[str] = []
    if not isinstance(observations, Mapping):
        reasons.append(f"{case_id}: no observations are present: the case cannot be recomputed")
    else:
        missing = [name for name in required if name not in observations]
        if missing:
            reasons.append(f"{case_id}: the observations {', '.join(missing)} are missing")
        reasons.extend(f"{case_id}: the observation {name!r} is not evidenced"
                       for name in required if name in observations and observations[name] is not True)
    if case_id == "S03":
        reasons.extend(_recompute_s03(material))
    return CaseVerdict(case_id=case_id, passed=not reasons, reasons=tuple(reasons),
                       evidence={"observations": sorted((observations or {}).keys())})


def evaluate_case(*, case_id: str, directory: Path) -> CaseVerdict:
    """Recompute one case from its material directory."""
    material = load_case_material(directory)
    recorded = material.get("case_id")
    if isinstance(recorded, str) and recorded != case_id:
        return CaseVerdict(case_id=case_id, passed=False,
                           reasons=(f"the material belongs to {recorded!r}, not {case_id!r}",))
    if case_id.startswith("B:"):
        return _evaluate_backend(case_id, material, directory)
    if case_id in oc.OUTCOMES or re.fullmatch(r"O0[1-6]", case_id):
        return _evaluate_operational(case_id, material, directory)
    return _evaluate_software(case_id, material)


def evaluate_report(*, candidate: CandidateV3, report: AcceptanceReportV3,
                    evidence_dir: Path) -> dict:
    """Recompute every final attempt of a report; the stored verdicts are ignored."""
    by_key = {(attempt.run_id, attempt.case_id, attempt.attempt): attempt for attempt in report.attempts}
    verdicts: dict[str, dict] = {}
    for final in report.finals:
        attempt: CaseAttempt = by_key[(final.run_id, final.case_id, final.attempt)]
        if attempt.exit_code != 0:
            verdicts[final.case_id] = CaseVerdict(final.case_id, False,
                                                  (f"the attempt exited with {attempt.exit_code}",)).document()
            continue
        directory = _material_directory(evidence_dir, attempt)
        try:
            verdict = evaluate_case(case_id=final.case_id, directory=directory)
        except EvaluatorError as exc:
            verdict = CaseVerdict(final.case_id, False, (str(exc),))
        verdicts[final.case_id] = verdict.document()
    failed = sorted(case_id for case_id, verdict in verdicts.items() if not verdict["passed"])
    return {"cases": verdicts, "failed": failed, "verdict": "passed" if verdicts and not failed else "failed"}


def _material_directory(evidence_dir: Path, attempt: CaseAttempt) -> Path:
    if not attempt.event_refs:
        raise EvaluatorError(f"case {attempt.case_id!r}: the attempt carries no material references")
    reference = attempt.event_refs[0]
    base = Path(evidence_dir) / reference.relative_path
    directory = base if base.is_dir() else base.parent
    if not directory.is_dir():
        raise EvaluatorError(f"case {attempt.case_id!r}: {reference.relative_path} is not present")
    return directory
