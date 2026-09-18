"""P25 offline verification and merge.

`verify` never starts a model, never talks to Docker and never opens a socket:
it only reads the candidate and the evidence directory, checks every artifact
byte-for-byte, re-derives the report's identity and mapping, enforces the
validity window (a report is good for seven days **from its start**, and exactly
seven days is already expired, and repackaging never refreshes it) and hands the
material to the evaluator for recomputation.

Exit codes (plan/08 §5): 0 passed, 2 missing material or structural error,
3 complete material that fails semantically.

`merge` joins runs of the same candidate and device into one evidence
directory: it copies material while re-checking every digest, requires exactly
one final attempt per case across the runs, and recomputes the report's
start/end from all attempts instead of refreshing the window.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any, Mapping, Sequence

from ..evidence_contracts import (AcceptanceReportV3, ArtifactRef, CaseFinal, ContractError,
                                  REPORT_FUTURE_TOLERANCE_SECONDS, REPORT_VALIDITY_SECONDS, candidate_digest,
                                  device_digest, parse_acceptance_report, parse_candidate, validate_report_identity,
                                  validate_report_mapping)
from . import EXIT_FAILED, EXIT_INPUT, EXIT_OK
from . import evaluator


class VerifyError(RuntimeError):
    """A verification refusal that carries its CLI exit code."""

    def __init__(self, message: str, *, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _utc(text: str, label: str) -> datetime:
    try:
        value = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError as exc:
        raise VerifyError(f"{label}: not a valid timestamp: {exc}", exit_code=EXIT_INPUT) from exc
    if value.tzinfo is None:
        raise VerifyError(f"{label}: the timestamp must carry a UTC offset", exit_code=EXIT_INPUT)
    return value.astimezone(timezone.utc)


def _read_json(path: Path, label: str) -> Mapping[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerifyError(f"{label}: {path} cannot be read: {exc}", exit_code=EXIT_INPUT) from exc
    if not isinstance(document, dict):
        raise VerifyError(f"{label}: {path} must be a JSON object", exit_code=EXIT_INPUT)
    return document


def _verify_artifacts(base: Path, references: Sequence[ArtifactRef], label: str) -> None:
    for reference in references:
        path = base / reference.relative_path
        if path.is_symlink():
            raise VerifyError(f"{label}: {reference.relative_path} is a symlink", exit_code=EXIT_INPUT)
        if not path.is_file():
            raise VerifyError(f"{label}: {reference.relative_path} is missing", exit_code=EXIT_INPUT)
        if path.stat().st_size != reference.size_bytes:
            raise VerifyError(f"{label}: {reference.relative_path} size differs from the manifest",
                              exit_code=EXIT_INPUT)
        if _sha256_file(path) != reference.sha256:
            raise VerifyError(f"{label}: {reference.relative_path} hash differs from the manifest",
                              exit_code=EXIT_INPUT)


def _load_candidate(candidate_path: Path):
    document = _read_json(Path(candidate_path), "candidate")
    try:
        return parse_candidate(document)
    except ContractError as exc:
        raise VerifyError(f"candidate: {exc}", exit_code=EXIT_INPUT) from exc


def _load_report(evidence_dir: Path) -> AcceptanceReportV3:
    document = _read_json(Path(evidence_dir) / "report.json", "report")
    try:
        return parse_acceptance_report(document)
    except ContractError as exc:
        raise VerifyError(f"report: {exc}", exit_code=EXIT_INPUT) from exc


def verify_evidence(*, candidate_path: Path, evidence_dir: Path, now: datetime | None = None) -> tuple[int, dict]:
    """Recompute one evidence directory against one candidate. Never touches a model."""
    moment = now if now is not None else datetime.now(timezone.utc)
    try:
        candidate = _load_candidate(Path(candidate_path))
        report = _load_report(Path(evidence_dir))
        base = Path(evidence_dir)
        _verify_artifacts(base, report.artifacts, "evidence")
        _verify_artifacts(base, [reference for attempt in report.attempts for reference in attempt.event_refs],
                          "evidence")
    except VerifyError as exc:
        return exc.exit_code, {"error": str(exc), "case": None}

    try:
        validate_report_identity(report, candidate)
    except ContractError as exc:
        return EXIT_FAILED, {"error": str(exc), "case": None}
    try:
        validate_report_mapping(report, candidate)
    except ContractError as exc:
        return EXIT_INPUT, {"error": str(exc), "case": None}

    digest = candidate_digest(candidate)
    device = device_digest(candidate.device)
    problems: list[str] = []
    for attempt in report.attempts:
        if attempt.candidate_sha256 != digest:
            problems.append(f"{attempt.case_id}: the attempt is bound to another candidate")
        if attempt.device_digest != device:
            problems.append(f"{attempt.case_id}: the attempt is bound to another device")
        if attempt.collector_sha256 != candidate.collector_sha256:
            problems.append(f"{attempt.case_id}: the collector hash does not match the candidate")
        if attempt.evaluator_sha256 != candidate.evaluator_sha256:
            problems.append(f"{attempt.case_id}: the evaluator hash does not match the candidate")

    started = _utc(report.started_at, "report.started_at")
    if started > moment + timedelta(seconds=REPORT_FUTURE_TOLERANCE_SECONDS):
        problems.append("the report starts in the future beyond the allowed tolerance")
    if moment - started >= timedelta(seconds=REPORT_VALIDITY_SECONDS):
        problems.append("the report is expired: validity ends seven days after its start, and repackaging "
                        "never refreshes it")

    recomputed = evaluator.evaluate_report(candidate=candidate, report=report, evidence_dir=base)
    problems.extend(f"{case_id}: {verdict['reasons']}" for case_id, verdict in recomputed["cases"].items()
                    if not verdict["passed"])
    if problems:
        return EXIT_FAILED, {"error": problems[0], "problems": problems, "cases": recomputed["cases"]}
    return EXIT_OK, {"candidate_sha256": digest, "device_digest": device, "run_id": report.run_id,
                     "cases": recomputed["cases"], "verdict": "passed"}


def _attempt_name(attempt) -> str:
    return f"{attempt.case_id.replace(':', '_')}/{attempt.run_id}-attempt-{attempt.attempt}"


def merge_runs(*, candidate_path: Path, run_dirs: Sequence[Path], output: Path) -> dict:
    """Join runs of the same candidate/device; digests are re-checked, never refreshed."""
    candidate = _load_candidate(Path(candidate_path))
    digest = candidate_digest(candidate)
    device = device_digest(candidate.device)
    reports: list[tuple[Path, AcceptanceReportV3]] = []
    for run_dir in run_dirs:
        directory = Path(run_dir)
        report = _load_report(directory)
        if report.candidate_sha256 != digest or report.device_digest != device:
            raise VerifyError(f"{directory}: the run belongs to another candidate or device", exit_code=EXIT_FAILED)
        _verify_artifacts(directory, report.artifacts, "run")
        reports.append((directory, report))

    seen_attempts: set[tuple[str, str, int]] = set()
    finals: dict[str, CaseFinal] = {}
    for _directory, report in reports:
        for attempt in report.attempts:
            key = (attempt.run_id, attempt.case_id, attempt.attempt)
            if key in seen_attempts:
                raise VerifyError(f"duplicate attempt {key}", exit_code=EXIT_INPUT)
            seen_attempts.add(key)
        for final in report.finals:
            if final.case_id in finals:
                raise VerifyError(f"case {final.case_id!r} has a final attempt in more than one run",
                                  exit_code=EXIT_INPUT)
            finals[final.case_id] = final
    if not finals:
        raise VerifyError("no final attempt was found in the supplied runs", exit_code=EXIT_INPUT)

    output.mkdir(parents=True, exist_ok=True)
    artifacts: list[ArtifactRef] = []
    attempts: list[dict] = []
    for directory, report in reports:
        for attempt in report.attempts:
            target_dir = output / "cases" / _attempt_name(attempt)
            # the material directory keeps its internal structure (case.json next to samples/)
            material_root = Path(attempt.event_refs[0].relative_path).parent
            copied: list[ArtifactRef] = []
            for reference in attempt.event_refs:
                source = directory / reference.relative_path
                if not source.is_file() or _sha256_file(source) != reference.sha256:
                    raise VerifyError(f"{source}: the material does not match its manifest", exit_code=EXIT_INPUT)
                target = target_dir / Path(reference.relative_path).relative_to(material_root)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                copied.append(ArtifactRef(relative_path=str(target.relative_to(output)),
                                          size_bytes=target.stat().st_size, sha256=reference.sha256))
            artifacts.extend(copied)
            body = asdict(attempt)
            body["event_refs"] = [asdict(reference) for reference in copied]
            attempts.append(body)

    started = min(attempt["started_at"] for attempt in attempts)
    ended = max(attempt["ended_at"] for attempt in attempts)
    merged = {"schema_version": 3, "candidate_sha256": digest, "device_digest": device,
              "run_id": f"merged-{reports[0][1].run_id}", "started_at": started, "ended_at": ended,
              "case_attempt_refs": attempts,
              "final_attempts": [asdict(final) for final in sorted(finals.values(), key=lambda item: item.case_id)],
              "artifact_manifest": [asdict(reference) for reference in
                                    sorted(artifacts, key=lambda item: item.relative_path)]}
    (output / "report.json").write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"output": str(output), "attempts": len(attempts), "cases": len(finals),
            "started_at": started, "ended_at": ended}
