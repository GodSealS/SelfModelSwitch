"""P24 workload: the plan meets §4, sends are measured, and the metrics are honest.

Percentiles are nearest-rank, a successful request is queue+load+execute, the
429/504 ratios divide by *all sent requests*, and the end state must report every
zero-tolerance counter — a missing counter cannot be assumed zero.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from model_scheduler.acceptance import workload as wl

POLICY = {"duration_seconds": 1800, "arrival_requests": 120, "arrival_gap_seconds_max": 15.0,
          "send_deviation_milliseconds_max": 1000.0, "error_rate_max": 0.1, "queue_full_rate_max": 0.1,
          "timeout_rate_max": 0.1}

CLEAN_STATE = {"oom": 0, "unexpected_500": 0, "unsafe_evictions": 0, "residual": 0, "queue_depth": 0,
               "leases": 0, "sessions": 0}


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ScriptedDriver:
    """One scripted response per request, keyed by index."""

    def __init__(self, responses: list[dict]) -> None:
        self.responses = responses
        self.seen: list[wl.PlannedRequest] = []

    def send(self, request: wl.PlannedRequest) -> dict:
        self.seen.append(request)
        return self.responses[request.index % len(self.responses)]


def _ok(latency: tuple[float, float, float] = (1.0, 2.0, 3.0)) -> dict:
    return {"status_code": 200, "queue_seconds": latency[0], "load_seconds": latency[1],
            "execute_seconds": latency[2]}


def _drive(sent_over: dict | None = None) -> list[wl.SentRequest]:
    clock = FakeClock()
    plan = wl.build_arrival_plan(models=("qwen-small",), duration_seconds=30.0, requests=4)
    driver = ScriptedDriver([sent_over or _ok()])
    return wl.run_workload(plan, driver, clock=clock, wait=clock.advance)


def test_the_plan_meets_the_required_floor() -> None:
    plan = wl.build_arrival_plan(models=("embedding", "qwen-small"), duration_seconds=1800.0, requests=120)

    assert wl.validate_arrival_plan(plan) == []
    assert plan.per_model() == {"embedding": 60, "qwen-small": 60}
    assert max(plan.gaps()) <= wl.MAX_ARRIVAL_GAP_SECONDS
    assert len(plan.document()["entries"]) == 120


def test_a_plan_below_the_floor_is_flagged_or_refused() -> None:
    with pytest.raises(wl.WorkloadError, match="maximum is"):
        wl.build_arrival_plan(models=("qwen-small",), duration_seconds=1800.0, requests=50)  # 36s gaps

    short = wl.build_arrival_plan(models=("qwen-small",), duration_seconds=600.0, requests=90)
    problems = wl.validate_arrival_plan(short)
    assert any("fewer than the required 100" in problem for problem in problems)
    assert any("shorter than the required 1800" in problem for problem in problems)

    with pytest.raises(wl.WorkloadError, match="3 arrivals"):
        wl.build_arrival_plan(models=("a", "b"), duration_seconds=100.0, requests=5)


def test_send_deviation_is_measured_against_the_plan() -> None:
    clock = FakeClock()
    plan = wl.build_arrival_plan(models=("qwen-small",), duration_seconds=30.0, requests=4)
    on_time = wl.run_workload(plan, ScriptedDriver([_ok()]), clock=clock, wait=clock.advance)
    assert max(record.deviation_milliseconds for record in on_time) < 1.0

    late_clock = FakeClock()
    late = wl.run_workload(plan, ScriptedDriver([_ok()]),
                           clock=late_clock, wait=lambda seconds: late_clock.advance(seconds + 1.2))
    metrics = wl.evaluate_workload(late, final_state=CLEAN_STATE, policy=POLICY)
    assert metrics["send_deviation_milliseconds_max"] > wl.MAX_SEND_DEVIATION_MILLISECONDS
    assert any("send偏差" in problem for problem in metrics["problems"])
    assert metrics["verdict"] == "failed"


def test_nearest_rank_percentiles_follow_the_definition() -> None:
    values = [float(index) for index in range(1, 101)]

    assert wl.nearest_rank(values, 95) == 95.0
    assert wl.nearest_rank(values, 99) == 99.0
    assert wl.nearest_rank([7.0], 95) == 7.0
    with pytest.raises(wl.WorkloadError, match="at least one value"):
        wl.nearest_rank([], 95)
    with pytest.raises(wl.WorkloadError, match=r"\(0, 100\]"):
        wl.nearest_rank(values, 0)


def test_successful_latency_is_queue_plus_load_plus_execute() -> None:
    sent = _drive({"status_code": 200, "queue_seconds": 0.5, "load_seconds": 4.0, "execute_seconds": 2.5})

    assert all(record.outcome == "ok" for record in sent)
    assert sent[0].total_seconds == 7.0

    metrics = wl.evaluate_workload(sent, final_state=CLEAN_STATE, policy=POLICY)
    assert metrics["latency_p95_seconds"] == 7.0 and metrics["per_model_latency_p95_seconds"] == {"qwen-small": 7.0}

    refused = _drive({"status_code": 429})
    assert refused[0].outcome == "queue_full" and refused[0].total_seconds is None


def _sent(index: int, outcome: str = "ok") -> wl.SentRequest:
    ok = outcome == "ok"
    return wl.SentRequest(index=index, model_id="qwen-small", capability="chat",
                          planned_offset_seconds=float(index), sent_offset_seconds=float(index),
                          deviation_milliseconds=0.0, outcome=outcome,
                          status_code=200 if ok else {"queue_full": 429, "timeout": 504}[outcome],
                          queue_seconds=1.0 if ok else None, load_seconds=2.0 if ok else None,
                          execute_seconds=3.0 if ok else None)


def test_ratios_use_all_sent_requests_as_the_denominator() -> None:
    sent = [_sent(index) for index in range(88)]
    sent += [_sent(index, "queue_full") for index in range(88, 98)]  # 10 of 100
    sent += [_sent(index, "timeout") for index in range(98, 100)]  # 2 of 100

    metrics = wl.evaluate_workload(sent, final_state=CLEAN_STATE, policy=POLICY)

    assert metrics["sent"] == 100
    assert metrics["queue_full_rate"] == pytest.approx(0.10)  # denominator = all sent, not only answers
    assert metrics["timeout_rate"] == pytest.approx(0.02)
    assert metrics["verdict"] == "passed"

    breached = sent[:89] + [_sent(index, "queue_full") for index in range(89, 100)]  # 12 of 100
    metrics = wl.evaluate_workload(breached, final_state=CLEAN_STATE, policy=POLICY)
    assert metrics["queue_full_rate"] == pytest.approx(0.12)
    assert metrics["verdict"] == "failed"


def test_a_policy_may_only_tighten_the_acceptance_caps() -> None:
    sent = _drive()
    strict = wl.evaluate_workload(sent, final_state=CLEAN_STATE, policy={**POLICY, "queue_full_rate_max": 0.05})
    assert strict["verdict"] == "passed"

    looser = wl.evaluate_workload(sent, final_state=CLEAN_STATE, policy={**POLICY, "queue_full_rate_max": 0.5})
    assert any("looser than the acceptance cap" in problem for problem in looser["problems"])
    assert looser["verdict"] == "failed"


def test_the_end_state_must_report_every_zero_tolerance_counter() -> None:
    sent = _drive()

    missing = wl.evaluate_workload(sent, final_state={"oom": 0}, policy=POLICY)
    assert any("does not report 'leases'" in problem for problem in missing["problems"])

    dirty = wl.evaluate_workload(sent, final_state={**CLEAN_STATE, "residual": 1, "oom": 2}, policy=POLICY)
    assert any("must be 0 at the end of the run" in problem for problem in dirty["problems"])
    assert any("OOM occurred" in problem for problem in dirty["problems"])
    assert dirty["verdict"] == "failed"


def test_every_send_is_recorded_through_the_collector(tmp_path) -> None:
    from model_scheduler.acceptance.collector import FileCollector

    collector = FileCollector(tmp_path, run_id="run-1", candidate_sha256="a" * 64, device_digest="b" * 64,
                              boot_id="boot-1")
    sent = _drive({"status_code": 200, "queue_seconds": 0.1, "load_seconds": 0.2, "execute_seconds": 0.3,
                   "note": "through the collector"})
    clock = FakeClock()
    plan = wl.build_arrival_plan(models=("qwen-small",), duration_seconds=30.0, requests=4)
    sent = wl.run_workload(plan, ScriptedDriver([_ok()]), clock=clock, wait=clock.advance, collector=collector)

    rows = [json.loads(line) for line in (tmp_path / "samples" / "workload.jsonl").read_text().splitlines()]
    assert len(rows) == len(sent) == 4
    assert rows[0]["raw"]["outcome"] == "ok" and rows[0]["kind"] == "workload"
    assert rows[0]["run_id"] == "run-1"
