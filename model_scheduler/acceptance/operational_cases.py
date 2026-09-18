"""P24 O01—O06: the operational cases, driven through injectable fault/lab ports.

Every case is orchestrated here and every hard rule of plan/06-acceptance.md §3
is checked explicitly, because these are the rules a "green" report is most
tempting to fake:

* O02 — the model-disk fault must run inside this deployment's **private mount
  namespace** (never unmounting a machine-wide disk) and the scratch fault must
  use a **dedicated small-quota filesystem** (never filling the root disk); the
  recovery must re-hash the material.
* O03 — Docker unreachable, unknown instances/ports and a stalled stop must all
  keep the budget (UNKNOWN/BLOCKED) and answer health with 503, without touching
  other containers.
* O04 — a restart must clean residual instances, an old token must be refused,
  admission must work again after cleanup, and no inference may be replayed.
* O05 — the correct candidate is accepted and every tampered variant is rejected
  *before* loading; only the P26 primitive is used, never a full production gate
  that would depend on O05's own report.
* O06 — graceful shutdown, log capacity/redaction, and a backup→restore→rollback
  round trip whose digests match, including the refusal to roll back to a release
  with no accepted evidence.

A case whose condition is not available is `not_run`, never a placeholder pass.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Mapping, Protocol, Sequence

from .workload import ArrivalPlan, WorkloadDriver, build_arrival_plan, evaluate_workload, run_workload, validate_arrival_plan

OUTCOMES = ("passed", "failed", "unknown", "not_run")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class OperationalCaseError(RuntimeError):
    """The orchestrator refuses a case it cannot evidence."""


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    status: str
    facts: Mapping[str, Any] = field(default_factory=dict)
    problems: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in OUTCOMES:
            raise OperationalCaseError(f"unknown case status {self.status!r}")
        if self.status == "passed" and (self.problems or self.failures):
            raise OperationalCaseError("a passed case cannot carry problems or failures")

    def document(self) -> dict:
        return {"case_id": self.case_id, "status": self.status, "facts": dict(self.facts),
                "problems": list(self.problems), "failures": list(self.failures)}


class DiskFaultPort(Protocol):
    """O02: the deployment-private storage faults."""

    def isolate_mount_namespace(self, deployment_id: str) -> Mapping[str, Any]: ...

    def fail_model_disk(self) -> Mapping[str, Any]: ...

    def fill_scratch(self, quota_bytes: int) -> Mapping[str, Any]: ...

    def recover_model_disk(self) -> Mapping[str, Any]: ...


class FaultPort(Protocol):
    """O03: Docker/instance/port/stop faults."""

    def make_docker_unreachable(self) -> Mapping[str, Any]: ...

    def present_unknown_instance(self) -> Mapping[str, Any]: ...

    def stall_stop(self, model_id: str) -> Mapping[str, Any]: ...


class RecoveryPort(Protocol):
    """O04: restart, residuals, tokens and re-admission."""

    def restart_service(self) -> Mapping[str, Any]: ...

    def residual_instances(self) -> Sequence[str]: ...

    def attempt_old_token(self) -> Mapping[str, Any]: ...

    def admission_after_cleanup(self) -> Mapping[str, Any]: ...


class PreflightPort(Protocol):
    """O05: the P26 preflight primitive plus tamper scenarios."""

    def preflight(self, candidate: Mapping[str, Any], evidence: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def tamper_scenarios(self) -> Sequence[Mapping[str, Any]]: ...


class LabReleasePort(Protocol):
    """O06: graceful shutdown, logs and the isolated lab release drill."""

    def graceful_shutdown(self) -> Mapping[str, Any]: ...

    def log_report(self) -> Mapping[str, Any]: ...

    def backup(self) -> Mapping[str, Any]: ...

    def restore(self, snapshot: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def rollback(self, snapshot: Mapping[str, Any], *, has_accepted_evidence: bool) -> Mapping[str, Any]: ...


def _require(facts: Mapping[str, Any], key: str, *, expected: Any, problems: list[str]) -> None:
    seen = facts.get(key)
    if seen != expected:
        problems.append(f"{key}={seen!r} but {expected!r} is required")


def _unavailable(facts: Mapping[str, Any]) -> bool:
    return facts.get("available") is False


def _step(problems: list[str], failures: list[str], label: str, action) -> Mapping[str, Any]:
    try:
        return dict(action() or {})
    except Exception as exc:  # noqa: BLE001 - a crashed step is material, not a passed step
        failures.append(f"{label}: {type(exc).__name__}: {exc}")
        return {}


def _finish(case_id: str, *, problems: list[str], failures: list[str], facts: Mapping[str, Any],
            unavailable: bool = False) -> CaseResult:
    if failures:
        return CaseResult(case_id=case_id, status="failed", facts=facts, problems=tuple(problems),
                          failures=tuple(failures))
    if unavailable:
        return CaseResult(case_id=case_id, status="not_run", facts=facts, problems=tuple(problems))
    return CaseResult(case_id=case_id, status="passed" if not problems else "failed", facts=facts,
                      problems=tuple(problems))


# ---------------------------------------------------------------------------
# O01


def run_o01(*, plan: ArrivalPlan, driver: WorkloadDriver, policy: Mapping[str, Any] | None,
            final_state: Mapping[str, Any], clock=None, wait=None, collector=None) -> CaseResult:
    """The mixed-load run; without operational thresholds the case is `not_run`."""
    problems = validate_arrival_plan(plan)
    if policy is None:
        return CaseResult(case_id="O01", status="not_run", facts={"plan": plan.document()},
                          problems=tuple(problems + ["no operational policy was supplied: performance values are "
                                                     "required, a placeholder pass is not allowed"]))
    if problems:
        return CaseResult(case_id="O01", status="failed", facts={"plan": plan.document()}, problems=tuple(problems))
    sent = run_workload(plan, driver, clock=clock, wait=wait, collector=collector)
    metrics = evaluate_workload(sent, final_state=final_state, policy=policy)
    return _finish("O01", problems=list(metrics["problems"]),
                   failures=[], facts={"plan": plan.document(), "metrics": metrics})


def build_o01_plan(*, models: Sequence[str], duration_seconds: float, requests: int) -> ArrivalPlan:
    return build_arrival_plan(models=models, duration_seconds=duration_seconds, requests=requests)


# ---------------------------------------------------------------------------
# O02 — O06


def run_o02(port: DiskFaultPort, *, deployment_id: str, quota_bytes: int) -> CaseResult:
    problems: list[str] = []
    failures: list[str] = []
    facts: dict[str, Any] = {}
    isolation = _step(problems, failures, "isolate_mount_namespace", lambda: port.isolate_mount_namespace(deployment_id))
    facts["isolation"] = isolation
    if _unavailable(isolation):
        return _finish("O02", problems=problems, failures=failures, facts=facts, unavailable=True)
    _require(isolation, "private_mount_namespace", expected=True, problems=problems)
    if isolation.get("unmounted_shared_disk") is True:
        problems.append("a machine-wide disk was unmounted: the fault must stay inside this deployment's namespace")
    failure = _step(problems, failures, "fail_model_disk", port.fail_model_disk)
    facts["model_disk"] = failure
    _require(failure, "model_disk_unavailable", expected=True, problems=problems)
    _require(failure, "root_disk_writes", expected=0, problems=problems)
    scratch = _step(problems, failures, "fill_scratch", lambda: port.fill_scratch(quota_bytes))
    facts["scratch"] = scratch
    _require(scratch, "dedicated_quota_fs", expected=True, problems=problems)
    _require(scratch, "scratch_full", expected=True, problems=problems)
    _require(scratch, "root_disk_writes", expected=0, problems=problems)
    recovery = _step(problems, failures, "recover_model_disk", port.recover_model_disk)
    facts["recovery"] = recovery
    _require(recovery, "recovered", expected=True, problems=problems)
    _require(recovery, "rehashed", expected=True, problems=problems)
    return _finish("O02", problems=problems, failures=failures, facts=facts)


def run_o03(port: FaultPort, *, model_id: str) -> CaseResult:
    problems: list[str] = []
    failures: list[str] = []
    facts: dict[str, Any] = {}
    docker = _step(problems, failures, "make_docker_unreachable", port.make_docker_unreachable)
    facts["docker_unreachable"] = docker
    if _unavailable(docker):
        return _finish("O03", problems=problems, failures=failures, facts=facts, unavailable=True)
    _require(docker, "health_status", expected=503, problems=problems)
    _require(docker, "budget_kept", expected=True, problems=problems)
    _require(docker, "other_containers_untouched", expected=True, problems=problems)
    unknown = _step(problems, failures, "present_unknown_instance", port.present_unknown_instance)
    facts["unknown_instance"] = unknown
    _require(unknown, "unknown_recorded", expected=True, problems=problems)
    _require(unknown, "health_status", expected=503, problems=problems)
    _require(unknown, "other_containers_untouched", expected=True, problems=problems)
    stalled = _step(problems, failures, "stall_stop", lambda: port.stall_stop(model_id))
    facts["stop_timeout"] = stalled
    _require(stalled, "stop_timeout_recorded", expected=True, problems=problems)
    _require(stalled, "budget_kept", expected=True, problems=problems)
    _require(stalled, "health_status", expected=503, problems=problems)
    return _finish("O03", problems=problems, failures=failures, facts=facts)


def run_o04(port: RecoveryPort) -> CaseResult:
    problems: list[str] = []
    failures: list[str] = []
    facts: dict[str, Any] = {}
    restart = _step(problems, failures, "restart_service", port.restart_service)
    facts["restart"] = restart
    if _unavailable(restart):
        return _finish("O04", problems=problems, failures=failures, facts=facts, unavailable=True)
    _require(restart, "restarted", expected=True, problems=problems)
    _require(restart, "residual_instances_cleaned", expected=True, problems=problems)
    if restart.get("inference_replay") is not False:
        problems.append("the restart must not replay inference")
    try:
        leftovers = [str(container) for container in port.residual_instances()]
    except Exception as exc:  # noqa: BLE001 - an unreadable residual list is material, not a pass
        failures.append(f"residual_instances: {type(exc).__name__}: {exc}")
        leftovers = []
    facts["residual_after_restart"] = leftovers
    if leftovers:
        problems.append(f"instances survived the restart: {leftovers}")
    token = _step(problems, failures, "attempt_old_token", port.attempt_old_token)
    facts["old_token"] = token
    if token.get("accepted") is not False:
        problems.append(f"an old token was accepted: {token!r}")
    elif not str(token.get("reason") or "").strip():
        problems.append("an old token was refused without a reason")
    admission = _step(problems, failures, "admission_after_cleanup", port.admission_after_cleanup)
    facts["admission"] = admission
    _require(admission, "admitted", expected=True, problems=problems)
    return _finish("O04", problems=problems, failures=failures, facts=facts)


def run_o05(port: PreflightPort, *, candidate: Mapping[str, Any], evidence: Mapping[str, Any]) -> CaseResult:
    problems: list[str] = []
    failures: list[str] = []
    facts: dict[str, Any] = {}
    correct = _step(problems, failures, "preflight(candidate)", lambda: port.preflight(candidate, evidence))
    facts["correct"] = correct
    if _unavailable(correct):
        return _finish("O05", problems=problems, failures=failures, facts=facts, unavailable=True)
    _require(correct, "accepted", expected=True, problems=problems)
    if correct.get("production_gate") is True:
        problems.append("O05 must use the preflight primitive: the full production gate depends on O05's own report")
    scenarios = port.tamper_scenarios()
    if not scenarios:
        problems.append("no tamper scenario was supplied: tamper rejection cannot be claimed")
    tampered: list[dict[str, Any]] = []
    for scenario in scenarios:
        name = str(scenario.get("name") or "unnamed")
        result = _step(problems, failures, f"preflight(tampered:{name})",
                       lambda scenario=scenario: port.preflight(scenario.get("candidate"), scenario.get("evidence")))
        tampered.append({"name": name, "facts": result})
        if result.get("accepted") is not False:
            problems.append(f"the tampered scenario {name!r} was accepted")
        elif not str(result.get("reason") or "").strip():
            problems.append(f"the tampered scenario {name!r} was refused without a reason")
        if result.get("loaded") is True:
            problems.append(f"the tampered scenario {name!r} reached the load step: rejection must happen before")
        if result.get("production_gate") is True:
            problems.append(f"the tampered scenario {name!r} invoked the production gate")
    facts["tampered"] = tampered
    return _finish("O05", problems=problems, failures=failures, facts=facts)


def run_o06(port: LabReleasePort) -> CaseResult:
    problems: list[str] = []
    failures: list[str] = []
    facts: dict[str, Any] = {}
    shutdown = _step(problems, failures, "graceful_shutdown", port.graceful_shutdown)
    facts["shutdown"] = shutdown
    if _unavailable(shutdown):
        return _finish("O06", problems=problems, failures=failures, facts=facts, unavailable=True)
    _require(shutdown, "graceful", expected=True, problems=problems)
    _require(shutdown, "exit_code", expected=0, problems=problems)
    logs = _step(problems, failures, "log_report", port.log_report)
    facts["logs"] = logs
    _require(logs, "capacity_ok", expected=True, problems=problems)
    _require(logs, "redaction_ok", expected=True, problems=problems)
    backup = _step(problems, failures, "backup", port.backup)
    facts["backup"] = backup
    snapshot_digest = backup.get("snapshot_sha256")
    if not isinstance(snapshot_digest, str) or not _SHA256.fullmatch(snapshot_digest):
        problems.append("the backup must report a lowercase 64-hex snapshot_sha256")
    if not backup.get("files"):
        problems.append("the backup reports no files")
    restore = _step(problems, failures, "restore", lambda: port.restore(backup))
    facts["restore"] = restore
    _require(restore, "restored", expected=True, problems=problems)
    _require(restore, "snapshot_sha256", expected=snapshot_digest, problems=problems)
    rollback = _step(problems, failures, "rollback(accepted)", lambda: port.rollback(backup, has_accepted_evidence=True))
    facts["rollback"] = rollback
    _require(rollback, "applied", expected=True, problems=problems)
    refused = _step(problems, failures, "rollback(no evidence)",
                    lambda: port.rollback(backup, has_accepted_evidence=False))
    facts["rollback_refused"] = refused
    if refused.get("applied") is not False:
        problems.append("a rollback to a release without accepted evidence must be refused")
    elif not str(refused.get("reason") or "").strip():
        problems.append("the refused rollback carries no reason")
    return _finish("O06", problems=problems, failures=failures, facts=facts)
