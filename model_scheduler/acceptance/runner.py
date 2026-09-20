"""The real layer run: drive the registered cases and persist the v3 report.

Two layers run here, in one run directory and one report:

* **S** — the software checks (`software_cases`), in-process, no socket and no
  model: fake ports drive the real scheduler/Book, the real session/queue/
  idempotency objects, a real BlobStore over scratch directories and the real
  parsers/preflight;
* **B** — `backend_cases.CaseExecutor` (P23), which only ever touches the
  deployment through `CaseDriver`. The real driver (`driver.ControlApiCaseDriver`)
  talks to the control socket.

Three rules shape it:

* **A run never starts a deployment.** The B layer without a live control socket
  fails before anything is executed, and no partial output is written.
* **Material is written once, in the shape recomputation reads** (`materials`):
  one directory per attempt with `case.json` and `samples/`, which is exactly
  what `evaluator` and `verify.merge_runs` re-read.
* **Unknown is never a pass.** Every attempt is kept and the report's verdict is
  a pass only when every case's final attempt passed.
"""
from __future__ import annotations

from pathlib import Path
import atexit
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import shutil
import tempfile
import time
from itertools import count
from typing import Any, Callable, Mapping, Sequence
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
from .collector import collector_sha256
from .device_activity import ManagedComputeSampler
from .fixtures import FillerSpec
from .instances import ManagedInstanceProbe
from .materials import CaseMaterialStore

CONTROL_SOCKET_ENV = "SMS_CONTROL_SOCKET"
FIXTURE_MATERIAL_NAME = "fillers.json"
FIXTURE_MATERIAL_KEYS = frozenset({"schema_version", "fillers"})
FILLER_ENTRY_KEYS = frozenset({"model_id", "tokens_per_unit", "template_overhead_tokens",
                               "vision_template_overhead_tokens", "instruction", "instruction_tokens", "unit"})
LAYERS = ("S", "B", "O")
EXIT_INPUT = 2
EXIT_FAILED = 3
# O02's scratch fault runs on a dedicated small-quota filesystem, never the root disk.
SCRATCH_FAULT_BYTES = 64 * 1024 ** 2


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


def run_layers(*, candidate_path: Path, layers: Sequence[str], output: Path, fixtures_root: Path | None = None,
               inventory: Path | None = None, legacy_config: Path | None = None,
               socket_path: str | None = None, transport: Any = None, executor: Any = None,
               driver: Any = None, run_id: str | None = None, sampler_factory: Callable[[], Any] | None = None,
               instance_probe: Callable[[str], Mapping[str, Any] | None] | None = None,
               software_runner: Callable[..., Any] | None = None,
               check_config: Callable[[Path], int] | None = None,
               filler_of: Mapping[str, Any] | None = None,
               config_path: Path | None = None, inference_url: str | None = None,
               service_log: Path | None = None, ports: Mapping[str, Any] | None = None,
               operational_driver: Any = None, final_state_probe: Any = None,
               deployment_boot_id: str | None = None, clock: Callable[[], float] | None = None,
               wait: Callable[[float], None] | None = None) -> dict:
    """Execute the requested layers against one frozen candidate and persist the run."""
    requested = _requested_layers(layers)
    candidate = _load_candidate(candidate_path)
    digest, device = candidate_digest(candidate), device_digest(candidate.device)
    run = run_id or uuid4().hex
    store = CaseMaterialStore(output, run_id=run, candidate_sha256=digest, device_digest=device)

    records: list[tuple[Any, str]] = []
    if "S" in requested:
        s_attempts, software_boot = run_s_layer(
            candidate=candidate, candidate_path=Path(candidate_path), store=store, inventory=inventory,
            legacy_config=legacy_config, check_config=check_config, runner=software_runner)
        records.extend((item, software_boot) for item in s_attempts)
    if "B" in requested:
        b_attempts, deployment_boot = run_backend_layer(
            candidate=candidate, store=store, socket_path=socket_path, transport=transport,
            executor=executor, driver=driver, sampler_factory=sampler_factory, instance_probe=instance_probe,
            fixtures_root=fixtures_root, filler_of=filler_of)
        records.extend((item, deployment_boot) for item in b_attempts)
    if "O" in requested:
        o_attempts, operations_boot = run_o_layer(
            candidate=candidate, candidate_path=Path(candidate_path), store=store, output=Path(output),
            config_path=config_path, inference_url=inference_url, service_log=service_log, ports=ports,
            workload_driver=operational_driver, final_state_probe=final_state_probe,
            deployment_boot_id=deployment_boot_id, clock=clock, wait=wait)
        records.extend((item, operations_boot) for item in o_attempts)
    if not records:
        raise LayerError("no layer produced any case attempt", exit_code=EXIT_FAILED)

    manifest = store.close()
    artifacts = tuple(ArtifactRef(relative_path=entry["relative_path"], size_bytes=entry["size_bytes"],
                                  sha256=entry["sha256"]) for entry in manifest["files"])
    case_attempts = tuple(_case_attempt(item, boot_id, run, digest, device, candidate, store)
                          for item, boot_id in records)
    finals = tuple(CaseFinal(case_id=item.case_id, run_id=run, attempt=item.attempt)
                   for item in _finals_of(records))
    started_at = min(item.started_utc for item, _boot in records)
    ended_at = max(item.ended_utc for item, _boot in records)
    report = AcceptanceReportV3(schema_version=EVIDENCE_SCHEMA_VERSION, candidate_sha256=digest,
                                device_digest=device, run_id=run, started_at=started_at, ended_at=ended_at,
                                attempts=case_attempts, finals=finals, artifacts=artifacts)
    document = _document_of(report)
    try:
        parse_acceptance_report(document)  # a run that cannot be re-read was never produced
    except ContractError as exc:
        raise LayerError(f"the report does not satisfy the v3 contract: {exc}", exit_code=EXIT_FAILED) from exc

    (Path(output) / "report.json").write_text(_dump(document), encoding="utf-8")
    passed = [item for item, _boot in records if item.status == "passed"]
    return {
        "run_id": run,
        "candidate_sha256": digest,
        "device_digest": device,
        "layers": list(requested),
        "boot_ids": sorted({boot_id for _item, boot_id in records}),
        "cases": len(finals),
        "attempts": len(records),
        "passed": len(passed),
        "failed": len(records) - len(passed),
        "artifacts": len(artifacts),
        "output": str(Path(output) / "report.json"),
        "verdict": "passed" if len(passed) == len(records) else "blocked",
    }


def run_s_layer(*, candidate: Any, candidate_path: Path, store: CaseMaterialStore,
                inventory: Path | None = None, legacy_config: Path | None = None,
                check_config: Callable[[Path], int] | None = None,
                runner: Callable[..., Any] | None = None) -> tuple[list[Any], str]:
    """Every S case, in-process; the material goes straight into its own directory.

    The S layer needs no deployment: it runs the real objects with fake ports and
    scratch directories. A check whose explicit input is missing fails *its own*
    case and is never skipped into a pass.
    """
    from .software_cases import SOFTWARE_CASES, run_software_case

    execute = runner if runner is not None else run_software_case
    boot_id = f"software-{uuid4().hex}"
    results: list[Any] = []
    for case_id in SOFTWARE_CASES:
        directory = store.begin_case(case_id, attempt=1)
        result = execute(case_id, candidate=candidate, material_dir=directory, candidate_path=candidate_path,
                         inventory=inventory, legacy_config=legacy_config, check_config=check_config)
        store.end_case(status=result.status, facts=result.facts,
                       failure="; ".join(result.problems) or None, problems=result.problems)
        results.append(result)
    return results, boot_id


def run_backend_layer(*, candidate: Any, store: CaseMaterialStore, socket_path: str | None = None,
                      transport: Any = None, executor: Any = None, driver: Any = None,
                      sampler_factory: Callable[[], Any] | None = None,
                      instance_probe: Callable[[str], Mapping[str, Any] | None] | None = None,
                      fixtures_root: Path | None = None,
                      filler_of: Mapping[str, Any] | None = None) -> tuple[list[Any], str]:
    """The B matrix of every registered model, through the official control API."""
    from .backend_cases import CaseExecutor
    from .driver import ControlApiCaseDriver, UnixControlTransport

    if driver is None:
        socket = socket_path or os.environ.get(CONTROL_SOCKET_ENV)
        if not isinstance(socket, str) or not socket:
            raise LayerError(f"the control socket is required: set {CONTROL_SOCKET_ENV} (a run never starts "
                             "a deployment)", exit_code=EXIT_INPUT)
        if not Path(socket).exists():
            raise LayerError(f"the control socket {socket} is not present: the deployment must be running",
                             exit_code=EXIT_INPUT)
        link = transport if transport is not None else UnixControlTransport(socket)
        scratch = None if sampler_factory is not None else Path(tempfile.mkdtemp(prefix="sms-acceptance-device-"))
        factory = sampler_factory if sampler_factory is not None else compute_sampler_factory(scratch)
        if scratch is not None:
            atexit.register(shutil.rmtree, scratch, True)
        probe = instance_probe if instance_probe is not None else ManagedInstanceProbe(candidate.deployment_id).of
        driver = ControlApiCaseDriver(link, sampler_factory=factory, provider_identity=candidate.deployment_id,
                                      instance_probe=probe)
    try:
        boot_id = driver.boot_id()
    except Exception as exc:
        raise LayerError(f"cannot read the deployment identity over the control socket: {exc}",
                         exit_code=EXIT_FAILED) from exc

    fillers = dict(filler_of) if filler_of is not None else (
        load_filler_specs(Path(fixtures_root), candidate) if fixtures_root is not None else None)
    if executor is None and fillers is None:
        raise LayerError("the fixture material is required: a run may not build cases from the package's own "
                         "fixtures", exit_code=EXIT_INPUT)
    machine = executor if executor is not None else CaseExecutor(driver, collector=store, filler_of=fillers)
    attempts: list[Any] = []
    for model in candidate.models:
        attempts.extend(machine.run_model(model_id=model.model_id, capabilities=tuple(model.capabilities),
                                          envelope=model.envelope))
    return attempts, boot_id


def run_b_layer(*, candidate_path: Path, output: Path, socket_path: str | None = None,
                transport: Any = None, executor: Any = None, driver: Any = None, run_id: str | None = None,
                sampler_factory: Callable[[], Any] | None = None,
                instance_probe: Callable[[str], Mapping[str, Any] | None] | None = None,
                fixtures_root: Path | None = None, filler_of: Mapping[str, Any] | None = None) -> dict:
    """The B layer alone (the entry point that predates the S orchestration)."""
    return run_layers(candidate_path=candidate_path, layers=("B",), output=output, fixtures_root=fixtures_root,
                      socket_path=socket_path, transport=transport, executor=executor, driver=driver,
                      run_id=run_id, sampler_factory=sampler_factory, instance_probe=instance_probe,
                      filler_of=filler_of)


def run_o_layer(*, candidate: Any, candidate_path: Path, store: CaseMaterialStore, output: Path,
                config_path: Path | None = None, inference_url: str | None = None,
                service_log: Path | None = None, ports: Mapping[str, Any] | None = None,
                workload_driver: Any = None, final_state_probe: Any = None,
                deployment_boot_id: str | None = None,
                clock: Callable[[], float] | None = None,
                wait: Callable[[float], None] | None = None) -> tuple[list[Any], str]:
    """O01—O06 through the injected ports; an unwired port yields `not_run`.

    O01 needs the compat surface and the end-state probe, O05 the live site
    facts; the remaining ports are supplied by whoever wires the faults. A case
    whose port is missing never becomes a pass — it is `not_run` with the
    missing condition recorded as its problem.
    """
    from . import operational_cases as oc
    from .operational_ports import (FinalStateProbe, HttpWorkloadDriver, NotWiredPort, read_boot_id)

    wired = dict(ports or {})
    policy = _operational_policy(candidate)
    results: list[Any] = []

    # -- O01: the mixed workload and the end state it must end on ----------
    store.begin_case("O01", attempt=1)
    o01_started = _utc_now()
    result = None
    plan = None
    if policy is None:
        result = oc.CaseResult(case_id="O01", status="not_run", facts={},
                               problems=("no operational policy: the run would have no thresholds to meet",))
    else:
        try:
            plan = oc.build_o01_plan(models=[model.model_id for model in candidate.models],
                                     duration_seconds=float(policy["duration_seconds"]),
                                     requests=int(policy["arrival_requests"]))
        except Exception as exc:  # noqa: BLE001 - a policy that cannot plan is a failed case, not a crash
            plan = None
            result = oc.CaseResult(case_id="O01", status="failed", facts={},
                                   problems=(f"the frozen operational policy cannot build a plan that meets the "
                                             f"acceptance bounds: {exc}",))
    if plan is not None:
        workload = workload_driver
        if workload is None and inference_url:
            workload = HttpWorkloadDriver(inference_url)
        probe = final_state_probe
        if probe is None and inference_url:
            probe = FinalStateProbe(inference_url, candidate.deployment_id, log_path=service_log)
        if workload is None or probe is None:
            missing = "--inference-url (the compat surface)" if workload is None else "the end-state probe"
            result = oc.CaseResult(case_id="O01", status="not_run", facts={"plan": plan.document()},
                                   problems=(f"O01 is not wired: {missing} is missing, so neither the workload "
                                             "nor its end state can be measured",))
        else:
            probe.baseline()
            result = oc.run_o01(plan=plan, driver=workload, policy=policy,
                                final_state=probe.mapping(), collector=store, clock=clock, wait=wait,
                                quiesce=_quiesce_of(probe, candidate, inference_url, wait=wait))
    document = dict(result.facts)
    if policy is not None:
        document["policy"] = dict(policy)
    metrics = document.get("metrics")
    if isinstance(metrics, Mapping) and isinstance(metrics.get("final_state"), Mapping):
        document["final_state"] = dict(metrics["final_state"])
    store.end_case(status=result.status, facts=document, problems=result.problems,
                   failure="; ".join(result.failures) or None)
    results.append(_OperationalAttempt(case_id=result.case_id, status=result.status,
                                       started_utc=o01_started, ended_utc=_utc_now(),
                                       problems=tuple(result.problems), failures=tuple(result.failures)))

    # -- O02 — O06: the fault, recovery, preflight and release ports -------
    disk = wired.get("disk") or _namespace_disk_port(config_path)
    if disk is None:
        disk = NotWiredPort(
            "the deployment-private storage fault port is not wired (O02 needs a private mount namespace "
            "so the model disk can fail without touching the machine)")
    try:
        results.append(_record_o(store, oc.run_o02(disk, deployment_id=candidate.deployment_id,
                                                   quota_bytes=SCRATCH_FAULT_BYTES)))
    finally:
        if hasattr(disk, "close"):
            disk.close()
    fault = wired.get("fault") or _docker_fault_port(candidate=candidate, config_path=config_path,
                                                     inference_url=inference_url) or NotWiredPort(
        "the Docker fault port is not wired (O03 needs Docker to become unreachable for this deployment only)")
    results.append(_record_o(store, oc.run_o03(fault, model_id=candidate.models[0].model_id)))
    recovery = wired.get("recovery") or NotWiredPort(
        "the restart port is not wired (O04 needs the deployment's stop/start control)")
    results.append(_record_o(store, oc.run_o04(recovery)))
    preflight = wired.get("preflight") or _preflight_from_site(
        candidate=candidate, candidate_path=candidate_path, config_path=config_path)
    if preflight is None:
        results.append(_record_o(store, oc.run_o05(
            NotWiredPort("the preflight port is not wired (O05 needs --config for the live site facts)"),
            candidate=json.loads(Path(candidate_path).read_text(encoding="utf-8")), evidence={})))
    else:
        candidate_document = json.loads(Path(candidate_path).read_text(encoding="utf-8"))
        evidence = {"started_at": datetime.now(timezone.utc).isoformat()}
        results.append(_record_o(store, oc.run_o05(preflight, candidate=candidate_document, evidence=evidence)))
    lab_release = wired.get("lab_release") or NotWiredPort(
        "the lab release port is not wired (O06 needs the release switch and the blob-metadata backup)")
    results.append(_record_o(store, oc.run_o06(lab_release)))

    boot = deployment_boot_id or (read_boot_id(inference_url) if inference_url else None) or \
        f"operations-{uuid4().hex}"
    return results, boot


def _record_o(store: CaseMaterialStore, result: Any) -> Any:
    """O02—O06 material: the collected facts under `observations`, as the evaluator reads them."""
    started = _utc_now()
    store.begin_case(result.case_id, attempt=1)
    store.end_case(status=result.status, facts={"observations": dict(result.facts)},
                   problems=result.problems, failure="; ".join(result.failures) or None)
    return _OperationalAttempt(case_id=result.case_id, status=result.status, started_utc=started,
                               ended_utc=_utc_now(), problems=tuple(result.problems),
                               failures=tuple(result.failures))


@dataclass(frozen=True)
class _OperationalAttempt:
    """One O case as the report reads it; `CaseResult` carries no attempt identity."""

    case_id: str
    status: str
    started_utc: str
    ended_utc: str
    attempt: int = 1
    problems: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _quiesce_of(probe: Any, candidate: Any, inference_url: str | None, *, wait: Any) -> Any:
    """Release what the workload loaded, so the run can end with nothing running.

    `None` when there is no compat surface to ask, in which case O01 still reads
    whatever end state exists and the zero-tolerance check judges it as read.
    """
    if not inference_url:
        return None
    from .operational_ports import DeploymentQuiescer

    return DeploymentQuiescer(base_url=inference_url, probe=probe,
                              models=tuple(model.model_id for model in candidate.models),
                              wait=wait if wait is not None else time.sleep)


def _operational_policy(candidate: Any) -> Mapping[str, Any] | None:
    operational = getattr(getattr(candidate, "policy", None), "operational", None)
    if operational is None:
        return None
    try:
        return asdict(operational)
    except TypeError:
        return dict(operational) if isinstance(operational, Mapping) else None


def _namespace_disk_port(config_path: Path | None) -> Any:
    """O02's port over a mount namespace this run creates for itself.

    `None` means there is no configuration and therefore no model disk to shadow;
    the caller then reports the case as `not_run`.
    """
    if config_path is None:
        return None
    from ..config import load_config
    from .operational_ports import NamespaceDiskFaultPort, _filesystem_of

    config = load_config(Path(config_path))
    directory = Path(config.storage.model_directory)
    return NamespaceDiskFaultPort(config_path=Path(config_path), filesystem=_filesystem_of(directory))


def _docker_fault_port(*, candidate: Any, config_path: Path | None, inference_url: str | None) -> Any:
    """O03's port over the live machine; without the compat surface there is nothing to fault."""
    if config_path is None or not inference_url:
        return None
    from .operational_ports import DockerFaultPort

    image = next((runtime.image_digest for runtime in candidate.runtimes
                  if getattr(runtime, "image_digest", None)), None)
    if image is None or not candidate.models:
        return None
    return DockerFaultPort(deployment_id=candidate.deployment_id, base_url=inference_url,
                           model_id=candidate.models[0].model_id, image=image)


def _preflight_from_site(*, candidate: Any, candidate_path: Path, config_path: Path | None) -> Any:
    """O05's port over the live site; without a configuration there is no site to read."""
    if config_path is None:
        return None
    from ..config import load_config
    from .operational_ports import PreflightPrimitivePort, read_site_facts, tamper_scenarios

    config = load_config(Path(config_path))
    site = read_site_facts(candidate=candidate, config_path=Path(config_path),
                           model_directory=Path(config.storage.model_directory),
                           source_archive_sha256=candidate.source_archive_sha256,
                           scratch_path=Path(config.blobs.root))
    document = json.loads(Path(candidate_path).read_text(encoding="utf-8"))
    return PreflightPrimitivePort(site=site, tamper=tamper_scenarios(document))


def _requested_layers(layers: Sequence[str]) -> tuple[str, ...]:
    items = tuple(str(layer).strip().upper() for layer in layers)
    if not items or any(layer not in LAYERS for layer in items):
        raise LayerError(f"layers must be a comma-separated subset of {sorted(LAYERS)}, got {layers!r}",
                         exit_code=EXIT_INPUT)
    if len(set(items)) != len(items):
        raise LayerError(f"layers repeats a layer: {layers!r}", exit_code=EXIT_INPUT)
    return items


def _case_attempt(item: Any, boot_id: str, run: str, digest: str, device: str, candidate: Any,
                  store: CaseMaterialStore) -> CaseAttempt:
    return CaseAttempt(case_id=item.case_id, run_id=run, attempt=item.attempt, candidate_sha256=digest,
                       device_digest=device, started_at=item.started_utc, ended_at=item.ended_utc,
                       boot_id=boot_id, event_refs=store.refs(item.case_id, item.attempt),
                       collector_sha256=collector_sha256(), evaluator_sha256=candidate.evaluator_sha256,
                       exit_code=0 if item.status == "passed" else 1)


def _finals_of(records: Sequence[tuple[Any, str]]) -> list[Any]:
    """One final per case: the last attempt recorded for it (failures included)."""
    last: dict[str, Any] = {}
    for item, _boot in records:
        last[item.case_id] = item
    return list(last.values())


def _load_candidate(path: Path):
    document = _read_json(path)
    try:
        return parse_candidate(document)
    except ContractError as exc:
        raise LayerError(f"{path}: {exc}", exit_code=EXIT_INPUT) from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path) -> Mapping[str, Any]:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LayerError(f"cannot read {path}: {exc}", exit_code=EXIT_INPUT) from exc
    if not isinstance(document, dict):
        raise LayerError(f"{path} must be a JSON object", exit_code=EXIT_INPUT)
    return document


def _dump(document: Mapping[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=True) + "\n"
