"""P24 O01—O06: the operational orchestrator enforces the hard rules of §3.

Each fake port is healthy by default and every test breaks exactly one rule, so
a passing case can only mean the rule was actually checked: private mount
namespace (never a machine-wide unmount), dedicated scratch quota (never the
root disk), 503 + kept budget for faults, old tokens refused, tamper rejected
before loading, and a rollback that refuses a release with no accepted evidence.
"""
from __future__ import annotations

import pytest

from model_scheduler.acceptance import operational_cases as oc
from model_scheduler.acceptance import workload as wl

POLICY = {"duration_seconds": 1800, "arrival_requests": 120, "arrival_gap_seconds_max": 15.0,
          "send_deviation_milliseconds_max": 1000.0, "error_rate_max": 0.1, "queue_full_rate_max": 0.1,
          "timeout_rate_max": 0.1}
CLEAN_STATE = {"oom": 0, "unexpected_500": 0, "unsafe_evictions": 0, "residual": 0, "queue_depth": 0,
               "leases": 0, "sessions": 0, "instances_running": 0}


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += max(seconds, 0.0)


class HealthyDriver:
    def __init__(self, response: dict | None = None) -> None:
        self.response = response or {"status_code": 200, "queue_seconds": 1.0, "load_seconds": 2.0,
                                     "execute_seconds": 3.0}
        self.sent = 0

    def send(self, request: wl.PlannedRequest) -> dict:
        self.sent += 1
        return dict(self.response)


class FakeDisk:
    def __init__(self, **overrides) -> None:
        self.isolation = {"available": True, "private_mount_namespace": True, "unmounted_shared_disk": False}
        self.failure = {"model_disk_unavailable": True, "root_disk_writes": 0}
        self.scratch = {"dedicated_quota_fs": True, "scratch_full": True, "root_disk_writes": 0}
        self.recovery = {"recovered": True, "rehashed": True}
        self.quota: int | None = None
        for key, value in overrides.items():
            setattr(self, key, value)

    def isolate_mount_namespace(self, deployment_id: str):
        return self.isolation

    def fail_model_disk(self):
        return self.failure

    def fill_scratch(self, quota_bytes: int):
        self.quota = quota_bytes
        return self.scratch

    def recover_model_disk(self):
        return self.recovery


class FakeFaults:
    def __init__(self, **overrides) -> None:
        self.docker = {"available": True, "health_status": 503, "budget_kept": True,
                       "other_containers_untouched": True}
        self.unknown = {"unknown_recorded": True, "health_status": 503, "other_containers_untouched": True}
        self.stalled = {"stop_timeout_recorded": True, "budget_kept": True, "health_status": 503}
        for key, value in overrides.items():
            setattr(self, key, value)

    def make_docker_unreachable(self):
        return self.docker

    def present_unknown_instance(self):
        return self.unknown

    def stall_stop(self, model_id: str):
        return self.stalled


class FakeRecovery:
    def __init__(self, **overrides) -> None:
        self.restart = {"available": True, "restarted": True, "residual_instances_cleaned": True,
                        "inference_replay": False}
        self.leftovers: list[str] = []
        self.token = {"accepted": False, "reason": "the token was issued for a previous boot"}
        self.admission = {"admitted": True}
        for key, value in overrides.items():
            setattr(self, key, value)

    def restart_service(self):
        return self.restart

    def residual_instances(self):
        return list(self.leftovers)

    def attempt_old_token(self):
        return self.token

    def admission_after_cleanup(self):
        return self.admission


class FakePreflight:
    def __init__(self, **overrides) -> None:
        self.correct = {"accepted": True}
        self.scenarios = [{"name": "asset hash", "candidate": {"tampered": "asset"}, "evidence": {}},
                          {"name": "device change", "candidate": {"tampered": "device"}, "evidence": {}}]
        for key, value in overrides.items():
            setattr(self, key, value)

    def preflight(self, candidate, evidence):
        if candidate and candidate.get("tampered"):
            return {"accepted": False, "reason": f"tampered {candidate['tampered']}", "loaded": False}
        return self.correct

    def tamper_scenarios(self):
        return self.scenarios


class FakeLab:
    def __init__(self, **overrides) -> None:
        self.shutdown = {"available": True, "graceful": True, "exit_code": 0}
        self.logs = {"capacity_ok": True, "redaction_ok": True}
        self.snapshot = {"snapshot_sha256": "c" * 64, "files": 12}
        self.applied = {"applied": True}
        self.refused = {"applied": False, "reason": "that release has no accepted evidence"}
        for key, value in overrides.items():
            setattr(self, key, value)

    def graceful_shutdown(self):
        return self.shutdown

    def log_report(self):
        return self.logs

    def backup(self):
        return self.snapshot

    def restore(self, snapshot):
        return {"restored": True, "snapshot_sha256": snapshot.get("snapshot_sha256")}

    def rollback(self, snapshot, *, has_accepted_evidence):
        return dict(self.applied) if has_accepted_evidence else dict(self.refused)


def _o01(driver=None, policy=POLICY, final_state=None):
    clock = FakeClock()
    plan = wl.build_arrival_plan(models=("embedding", "qwen-small"), duration_seconds=1800.0, requests=120)
    return oc.run_o01(plan=plan, driver=driver or HealthyDriver(), policy=policy,
                      final_state=final_state or CLEAN_STATE, clock=clock, wait=clock.advance)


def test_o01_passes_a_clean_1800_second_run() -> None:
    driver = HealthyDriver()
    result = _o01(driver)

    assert result.status == "passed"
    assert driver.sent == 120
    assert result.facts["metrics"]["sent"] == 120
    assert result.facts["metrics"]["latency_p95_seconds"] == 6.0  # queue+load+execute


def test_o01_is_not_run_without_operational_thresholds() -> None:
    result = _o01(policy=None)

    assert result.status == "not_run"
    assert any("no operational policy" in problem for problem in result.problems)


def test_o01_fails_when_a_ratio_breaches_the_cap() -> None:
    driver = HealthyDriver({"status_code": 429})

    result = _o01(driver)

    assert result.status == "failed"
    assert any("queue_full_rate" in problem for problem in result.problems)


def test_o02_keeps_the_fault_inside_the_deployment() -> None:
    assert oc.run_o02(FakeDisk(), deployment_id="sms-lab", quota_bytes=64 * 1024**2).status == "passed"

    unmounted = FakeDisk(isolation={"available": True, "private_mount_namespace": True,
                                    "unmounted_shared_disk": True})
    shared = oc.run_o02(unmounted, deployment_id="sms-lab", quota_bytes=1)
    assert shared.status == "failed" and any("machine-wide disk" in problem for problem in shared.problems)

    root_writes = FakeDisk(scratch={"dedicated_quota_fs": True, "scratch_full": True, "root_disk_writes": 3})
    assert oc.run_o02(root_writes, deployment_id="sms-lab", quota_bytes=1).status == "failed"

    no_quota = FakeDisk(scratch={"dedicated_quota_fs": False, "scratch_full": True, "root_disk_writes": 0})
    assert oc.run_o02(no_quota, deployment_id="sms-lab", quota_bytes=1).status == "failed"

    no_rehash = FakeDisk(recovery={"recovered": True, "rehashed": False})
    assert oc.run_o02(no_rehash, deployment_id="sms-lab", quota_bytes=1).status == "failed"

    missing = FakeDisk(isolation={"available": False})
    assert oc.run_o02(missing, deployment_id="sms-lab", quota_bytes=1).status == "not_run"


def test_o03_requires_503_and_a_kept_budget() -> None:
    assert oc.run_o03(FakeFaults(), model_id="qwen-small").status == "passed"

    healthy = FakeFaults(docker={"available": True, "health_status": 200, "budget_kept": True,
                                 "other_containers_untouched": True})
    assert oc.run_o03(healthy, model_id="qwen-small").status == "failed"

    released = FakeFaults(stalled={"stop_timeout_recorded": True, "budget_kept": False, "health_status": 503})
    result = oc.run_o03(released, model_id="qwen-small")
    assert result.status == "failed" and any("budget_kept" in problem for problem in result.problems)

    collateral = FakeFaults(unknown={"unknown_recorded": True, "health_status": 503,
                                     "other_containers_untouched": False})
    assert oc.run_o03(collateral, model_id="qwen-small").status == "failed"


def test_o04_cleans_residuals_refuses_old_tokens_and_never_replays() -> None:
    assert oc.run_o04(FakeRecovery()).status == "passed"

    leftover = FakeRecovery()
    leftover.leftovers = ["sms-lab-qwen-small"]
    result = oc.run_o04(leftover)
    assert result.status == "failed" and any("survived the restart" in problem for problem in result.problems)

    accepted = FakeRecovery(token={"accepted": True, "reason": ""})
    assert oc.run_o04(accepted).status == "failed"

    replay = FakeRecovery(restart={"available": True, "restarted": True, "residual_instances_cleaned": True,
                                   "inference_replay": True})
    result = oc.run_o04(replay)
    assert result.status == "failed" and any("must not replay" in problem for problem in result.problems)


def test_o05_rejects_every_tampered_variant_before_loading() -> None:
    result = oc.run_o05(FakePreflight(), candidate={"candidate": "good"}, evidence={"evidence": "good"})

    assert result.status == "passed"
    assert len(result.facts["tampered"]) == 2

    permissive = FakePreflight(correct={"accepted": True, "production_gate": True})
    gate = oc.run_o05(permissive, candidate={}, evidence={})
    assert gate.status == "failed" and any("production gate" in problem for problem in gate.problems)

    accepting = FakePreflight()
    accepting.preflight = lambda candidate, evidence: {"accepted": True}  # accepts anything
    bad = oc.run_o05(accepting, candidate={}, evidence={})
    assert bad.status == "failed" and any("was accepted" in problem for problem in bad.problems)

    no_scenarios = FakePreflight(scenarios=[])
    assert oc.run_o05(no_scenarios, candidate={}, evidence={}).status == "failed"


def test_o06_round_trips_metadata_and_refuses_an_unaccepted_rollback() -> None:
    assert oc.run_o06(FakeLab()).status == "passed"

    bad_digest = FakeLab(snapshot={"snapshot_sha256": "not-a-digest", "files": 1})
    assert oc.run_o06(bad_digest).status == "failed"

    mismatch = FakeLab()
    mismatch.restore = lambda snapshot: {"restored": True, "snapshot_sha256": "d" * 64}
    assert oc.run_o06(mismatch).status == "failed"

    eager = FakeLab(refused={"applied": True})
    result = oc.run_o06(eager)
    assert result.status == "failed" and any("must be refused" in problem for problem in result.problems)

    abrupt = FakeLab(shutdown={"available": True, "graceful": False, "exit_code": 137})
    assert oc.run_o06(abrupt).status == "failed"


def test_a_crashed_step_is_a_failed_case_with_the_failure_recorded() -> None:
    class Broken(FakeDisk):
        def fail_model_disk(self):
            raise RuntimeError("the storage channel is down")

    result = oc.run_o02(Broken(), deployment_id="sms-lab", quota_bytes=1)

    assert result.status == "failed"
    assert any("the storage channel is down" in failure for failure in result.failures)
    assert result.problems or result.failures  # never a passed case


def test_a_passed_case_cannot_carry_problems() -> None:
    with pytest.raises(oc.OperationalCaseError, match="cannot carry"):
        oc.CaseResult(case_id="O01", status="passed", problems=("something",))
    with pytest.raises(oc.OperationalCaseError, match="unknown case status"):
        oc.CaseResult(case_id="O01", status="maybe")
