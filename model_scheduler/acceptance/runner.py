"""The real layer run: drive the registered cases through the official control API.

The B layer is executed by `backend_cases.CaseExecutor` (P23), which only ever
touches the deployment through `CaseDriver`. This module supplies the real driver
(`driver.ControlApiCaseDriver` over the control socket) and turns the recorded
attempts into the v3 report the merge/verify steps re-check.

Two rules shape it:

* **A run never starts a deployment.** Without a live control socket there is no
  run at all — and no partial output is written, so nothing can be mistaken for
  executed evidence.
* **Unknown is never a pass.** Every attempt is kept, and the report's verdict is
  a pass only when every case's final attempt passed.
"""

from __future__ import annotations

from pathlib import Path
import atexit
import hashlib
import math
import os
import shutil
import tempfile
from itertools import count
from typing import Any, Callable, Mapping
from uuid import uuid4

from ..evidence_contracts import (
    AcceptanceReportV3,
    ArtifactRef,
    CaseAttempt,
    CaseFinal,
    ContractError,
    EVIDENCE_SCHEMA_VERSION,
    candidate_digest,
    device_digest,
    parse_acceptance_report,
    parse_candidate,
)
from .collector import FileCollector, collector_sha256
from .device_activity import ManagedComputeSampler
from .fixtures import FillerSpec

CONTROL_SOCKET_ENV = "SMS_CONTROL_SOCKET"
FIXTURE_MATERIAL_NAME = "fillers.json"
FIXTURE_MATERIAL_KEYS = frozenset({"schema_version", "fillers"})
FILLER_ENTRY_KEYS = frozenset({"model_id", "tokens_per_unit", "template_overhead_tokens",
                               "vision_template_overhead_tokens", "instruction", "instruction_tokens", "unit"})
EXIT_INPUT = 2
EXIT_FAILED = 3


class LayerError(RuntimeError):
    """A layer refusal. `exit_code` keeps input errors apart from execution ones."""

    def __init__(self, message: str, *, exit_code: int = EXIT_FAILED) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _document_of(report: AcceptanceReportV3) -> dict:
    return {
        "schema_version": report.schema_version,
        "candidate_sha256": report.candidate_sha256,
        "device_digest": report.device_digest,
        "run_id": report.run_id,
        "started_at": report.started_at,
        "ended_at": report.ended_at,
        "case_attempt_refs": [
            {"case_id": attempt.case_id, "run_id": attempt.run_id, "attempt": attempt.attempt,
             "candidate_sha256": attempt.candidate_sha256, "device_digest": attempt.device_digest,
             "started_at": attempt.started_at, "ended_at": attempt.ended_at, "boot_id": attempt.boot_id,
             "event_refs": [{"relative_path": ref.relative_path, "size_bytes": ref.size_bytes,
                             "sha256": ref.sha256} for ref in attempt.event_refs],
             "collector_sha256": attempt.collector_sha256, "evaluator_sha256": attempt.evaluator_sha256,
             "exit_code": attempt.exit_code}
            for attempt in report.attempts],
        "final_attempts": [{"case_id": final.case_id, "run_id": final.run_id, "attempt": final.attempt}
                           for final in report.finals],
        "artifact_manifest": [{"relative_path": ref.relative_path, "size_bytes": ref.size_bytes,
                               "sha256": ref.sha256} for ref in report.artifacts],
    }


def load_filler_specs(root: Path, candidate: Any) -> dict[str, FillerSpec]:
    """Read the frozen fixture material and check it against the candidate's refs.

    A run may not fall back on the package's own fixtures: the candidate names the
    material it was frozen with, and the same directory supplies the measured
    tokens-per-unit ratio without which no text boundary can be built honestly.
    """
    directory = Path(root)
    if not directory.is_dir():
        raise LayerError(f"the fixture material {directory} is not a directory", exit_code=EXIT_INPUT)
    for reference in candidate.fixture_refs:
        path = directory / reference.relative_path
        if not path.is_file():
            raise LayerError(f"the fixture {reference.relative_path} the candidate names is missing under {directory}",
                             exit_code=EXIT_INPUT)
        if path.stat().st_size != reference.size_bytes or _sha256_file(path) != reference.sha256:
            raise LayerError(f"the fixture {reference.relative_path} differs from the candidate's reference: "
                             "a run may not use material the candidate does not name", exit_code=EXIT_INPUT)
    document = _read_json(directory / FIXTURE_MATERIAL_NAME)
    if not isinstance(document, dict) or set(document) != FIXTURE_MATERIAL_KEYS:
        raise LayerError(f"{directory / FIXTURE_MATERIAL_NAME}: keys must be exactly {sorted(FIXTURE_MATERIAL_KEYS)}",
                         exit_code=EXIT_INPUT)
    specs: dict[str, FillerSpec] = {}
    for index, entry in enumerate(document.get("fillers") or []):
        where = f"fillers[{index}]"
        if not isinstance(entry, dict) or set(entry) != FILLER_ENTRY_KEYS:
            raise LayerError(f"{where}: keys must be exactly {sorted(FILLER_ENTRY_KEYS)}", exit_code=EXIT_INPUT)
        model_id = entry.get("model_id")
        if not isinstance(model_id, str) or not model_id:
            raise LayerError(f"{where}: model_id is required", exit_code=EXIT_INPUT)
        ratio = entry.get("tokens_per_unit")
        if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not math.isfinite(ratio) or ratio <= 0:
            raise LayerError(f"{where}: tokens_per_unit must be a measured positive number, not an assumption",
                             exit_code=EXIT_INPUT)
        overhead = entry.get("template_overhead_tokens")
        if isinstance(overhead, bool) or not isinstance(overhead, int) or overhead < 0:
            raise LayerError(f"{where}: template_overhead_tokens must be the measured non-negative count",
                             exit_code=EXIT_INPUT)
        vision_overhead = entry.get("vision_template_overhead_tokens")
        if isinstance(vision_overhead, bool) or not isinstance(vision_overhead, int) or vision_overhead < 0:
            raise LayerError(f"{where}: vision_template_overhead_tokens must be the measured non-negative count",
                             exit_code=EXIT_INPUT)
        instruction, instruction_tokens = entry.get("instruction"), entry.get("instruction_tokens")
        if not isinstance(instruction, str) or not instruction.strip():
            raise LayerError(f"{where}: instruction is required: a boundary round is a real request",
                             exit_code=EXIT_INPUT)
        if isinstance(instruction_tokens, bool) or not isinstance(instruction_tokens, int) or instruction_tokens <= 0:
            raise LayerError(f"{where}: instruction_tokens must be the measured positive cost of the instruction",
                             exit_code=EXIT_INPUT)
        specs[model_id] = FillerSpec(unit=str(entry["unit"]), tokens_per_unit=float(ratio),
                                     template_overhead_tokens=overhead,
                                     vision_template_overhead_tokens=vision_overhead,
                                     instruction=instruction, instruction_tokens=instruction_tokens)
    return specs


def compute_sampler_factory(scratch: Path) -> Callable[[], ManagedComputeSampler]:
    """One sampler per execution, each over a scratch log it removes once read.

    The scratch sits outside the run output on purpose: only the rows the sampler
    harvests become case material, never the file they were captured in. The real
    tools (tegrastats, docker, /proc) are reached by the sampler itself, so this
    factory stays testable.
    """
    counter = count(1)

    def factory() -> ManagedComputeSampler:
        return ManagedComputeSampler(scratch / f"tegrastats-{next(counter)}.log")

    return factory


def run_b_layer(*, candidate_path: Path, output: Path, socket_path: str | None = None,
                transport: Any = None, executor: Any = None, run_id: str | None = None,
                sampler_factory: Callable[[], Any] | None = None,
                collector_factory: Callable[..., FileCollector] = FileCollector,
                fixtures_root: Path | None = None, filler_of: Mapping[str, Any] | None = None,
                clock: Callable[[], float] | None = None) -> dict:
    """Execute the B matrix of every registered model and persist its v3 report."""
    from .backend_cases import CaseExecutor
    from .driver import ControlApiCaseDriver, UnixControlTransport

    socket = socket_path or os.environ.get(CONTROL_SOCKET_ENV)
    if not isinstance(socket, str) or not socket:
        raise LayerError(f"the control socket is required: set {CONTROL_SOCKET_ENV} (a run never starts "
                         "a deployment)", exit_code=EXIT_INPUT)
    if not Path(socket).exists():
        raise LayerError(f"the control socket {socket} is not present: the deployment must be running",
                         exit_code=EXIT_INPUT)

    candidate_document = _read_json(candidate_path)
    try:
        candidate = parse_candidate(candidate_document)
    except ContractError as exc:
        raise LayerError(f"{candidate_path}: {exc}", exit_code=EXIT_INPUT) from exc
    digest = candidate_digest(candidate)
    device = device_digest(candidate.device)

    link = transport if transport is not None else UnixControlTransport(socket)
    if sampler_factory is None:
        scratch = Path(tempfile.mkdtemp(prefix="sms-acceptance-device-"))
    else:
        scratch = None
    factory = sampler_factory if sampler_factory is not None else compute_sampler_factory(scratch)
    driver = ControlApiCaseDriver(link, sampler_factory=factory)
    if scratch is not None:
        atexit.register(shutil.rmtree, scratch, True)  # a device window is scratch, never evidence
    try:
        boot_id = driver.boot_id()
    except Exception as exc:
        raise LayerError(f"cannot read the deployment identity over {socket}: {exc}",
                         exit_code=EXIT_FAILED) from exc

    run = run_id or uuid4().hex
    collector = collector_factory(output, run_id=run, candidate_sha256=digest, device_digest=device,
                                  boot_id=boot_id)
    fillers = dict(filler_of) if filler_of is not None else load_filler_specs(Path(fixtures_root), candidate) \
        if fixtures_root is not None else None
    if executor is None and fillers is None:
        raise LayerError("the fixture material is required: a run may not build cases from the package's own "
                         "fixtures", exit_code=EXIT_INPUT)
    machine = executor if executor is not None else CaseExecutor(driver, collector=collector, filler_of=fillers)

    attempts: list[Any] = []
    for model in candidate.models:
        attempts.extend(machine.run_model(model_id=model.model_id,
                                          capabilities=tuple(model.capabilities),
                                          envelope=model.envelope))
    manifest = collector.close()

    artifacts = tuple(ArtifactRef(relative_path=entry["relative_path"], size_bytes=entry["size_bytes"],
                                  sha256=entry["sha256"]) for entry in manifest["files"])
    # The collector appends every case to the run's two journals, so they are this
    # case's event material: the evaluator re-reads them and matches by case_id.
    journals = tuple(ref for ref in artifacts if ref.relative_path in ("events.jsonl", "cases.jsonl"))
    case_attempts = tuple(CaseAttempt(case_id=item.case_id, run_id=run, attempt=item.attempt,
                                      candidate_sha256=digest, device_digest=device,
                                      started_at=item.started_utc, ended_at=item.ended_utc, boot_id=boot_id,
                                      event_refs=journals, collector_sha256=collector_sha256(),
                                      evaluator_sha256=candidate.evaluator_sha256,
                                      exit_code=0 if item.status == "passed" else 1)
                          for item in attempts)
    finals = tuple(CaseFinal(case_id=item.case_id, run_id=run, attempt=item.attempt)
                   for item in _finals_of(attempts))
    started_at = min((item.started_utc for item in attempts), default="")
    ended_at = max((item.ended_utc for item in attempts), default="")
    report = AcceptanceReportV3(schema_version=EVIDENCE_SCHEMA_VERSION, candidate_sha256=digest,
                                device_digest=device, run_id=run, started_at=started_at, ended_at=ended_at,
                                attempts=case_attempts, finals=finals, artifacts=artifacts)
    document = _document_of(report)
    try:
        parse_acceptance_report(document)  # a run that cannot be re-read was never produced
    except ContractError as exc:
        raise LayerError(f"the report does not satisfy the v3 contract: {exc}", exit_code=EXIT_FAILED) from exc

    (Path(output) / "report.json").write_text(_dump(document), encoding="utf-8")
    passed = [item for item in attempts if item.status == "passed"]
    return {
        "run_id": run,
        "boot_id": boot_id,
        "candidate_sha256": digest,
        "device_digest": device,
        "cases": len(finals),
        "attempts": len(attempts),
        "passed": len(passed),
        "failed": len(attempts) - len(passed),
        "artifacts": len(artifacts),
        "output": str(Path(output) / "report.json"),
        "verdict": "passed" if attempts and len(passed) == len(attempts) else "blocked",
    }


def _finals_of(attempts: list[Any]) -> list[Any]:
    """One final per case: the last attempt recorded for it (failures included)."""
    last: dict[str, Any] = {}
    for item in attempts:
        last[item.case_id] = item
    return list(last.values())


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path) -> Mapping[str, Any]:
    import json

    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LayerError(f"cannot read the candidate {path}: {exc}", exit_code=EXIT_INPUT) from exc
    if not isinstance(document, dict):
        raise LayerError(f"the candidate {path} must be a JSON object", exit_code=EXIT_INPUT)
    return document


def _dump(document: Mapping[str, Any]) -> str:
    import json

    return json.dumps(document, indent=2, sort_keys=True) + "\n"
