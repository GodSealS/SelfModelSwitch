"""P30: the O layer — real ports where they exist, `not_run` where they do not.

The orchestration is pinned here: O01's workload and end state are measured (the
clock and the driver are injected), O05 uses the P26 primitive against a live-
site document, and a port that is not wired yields `not_run` with the missing
condition — never a placeholder pass. A policy that cannot even build a §4 plan
is a *failed* case with the reason, not a silent skip.
"""
from __future__ import annotations

import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from model_scheduler.acceptance import evaluator as ev
from model_scheduler.acceptance import materials as mt
from model_scheduler.acceptance import operational_ports as op
from model_scheduler.acceptance import runner as rn
from model_scheduler.acceptance import workload as wl
from model_scheduler.evidence_contracts import parse_candidate, parse_policy

CLEAN_STATE = {"oom": 0, "unexpected_500": 0, "unsafe_evictions": 0, "residual": 0, "queue_depth": 0,
               "leases": 0, "sessions": 0, "instances_running": 0}


def _load_module(name: str, path: Path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _candidate(tmp_path: Path):
    module = _load_module("tc_for_o_layer", Path(__file__).resolve().parent / "test_candidate.py")
    site = module._site(tmp_path)
    module._build(site)
    document = json.loads(site["output"].read_text(encoding="utf-8"))
    return site["output"], parse_candidate(document)


def _candidate_with_arrivals(candidate, requests: int):
    document = json.loads(json.dumps(asdict(candidate.policy)))  # tuples become lists for the parser
    document["operational"]["arrival_requests"] = requests
    return replace(candidate, policy=parse_policy(document))


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += max(seconds, 0.0)


class _HealthyDriver:
    def __init__(self) -> None:
        self.sent = 0

    def send(self, request: wl.PlannedRequest) -> dict:
        self.sent += 1
        return {"status_code": 200, "queue_seconds": 1.0, "load_seconds": 2.0, "execute_seconds": 3.0}


class _CleanProbe:
    def __init__(self) -> None:
        self.baselined = False

    def baseline(self) -> None:
        self.baselined = True

    def read(self) -> dict:
        return dict(CLEAN_STATE)

    def mapping(self) -> dict:
        return dict(CLEAN_STATE)


def _store(tmp_path: Path) -> mt.CaseMaterialStore:
    return mt.CaseMaterialStore(tmp_path / "run", run_id="run-o", candidate_sha256="a" * 64,
                                device_digest="b" * 64)


def test_o01_passes_with_a_measured_run_and_a_clean_end_state(tmp_path) -> None:
    candidate_path, candidate = _candidate(tmp_path)
    candidate = _candidate_with_arrivals(candidate, 120)
    store = _store(tmp_path)
    clock = _Clock()
    driver = _HealthyDriver()

    results, boot = rn.run_o_layer(candidate=candidate, candidate_path=candidate_path, store=store,
                                   output=tmp_path / "run", workload_driver=driver,
                                   final_state_probe=_CleanProbe(), clock=clock, wait=clock.advance)

    by_case = {result.case_id: result for result in results}
    assert by_case["O01"].status == "passed", by_case["O01"].problems
    assert driver.sent == 120
    assert boot.startswith("operations-")

    material = json.loads((store.case_directory("O01", 1) / "case.json").read_text(encoding="utf-8"))
    assert material["policy"]["arrival_requests"] == 120
    assert material["final_state"] == CLEAN_STATE
    assert (store.case_directory("O01", 1) / "samples" / "workload.jsonl").is_file()

    verdict = ev.evaluate_case(case_id="O01", directory=store.case_directory("O01", 1))
    assert verdict.passed is True, verdict.reasons


def test_the_o_layer_reports_unwired_ports_as_not_run(tmp_path) -> None:
    candidate_path, candidate = _candidate(tmp_path)
    candidate = _candidate_with_arrivals(candidate, 120)
    store = _store(tmp_path)
    clock = _Clock()

    results, _boot = rn.run_o_layer(candidate=candidate, candidate_path=candidate_path, store=store,
                                    output=tmp_path / "run", workload_driver=_HealthyDriver(),
                                    final_state_probe=_CleanProbe(), clock=clock, wait=clock.advance)

    by_case = {result.case_id: result for result in results}
    for case_id in ("O02", "O03", "O04", "O05", "O06"):
        assert by_case[case_id].status == "not_run", case_id
        material = json.loads((store.case_directory(case_id, 1) / "case.json").read_text(encoding="utf-8"))
        unavailability = [entry for entry in material["observations"].values()
                          if isinstance(entry, dict) and entry.get("available") is False]
        assert unavailability, case_id
        assert "not wired" in unavailability[0]["reason"], case_id
    # `not_run` is not a pass: recomputation refuses it
    assert ev.evaluate_case(case_id="O02", directory=store.case_directory("O02", 1)).passed is False


def test_a_policy_that_cannot_build_a_plan_fails_with_its_reason(tmp_path) -> None:
    candidate_path, candidate = _candidate(tmp_path)  # 100 arrivals over 1800s leaves 18s gaps
    store = _store(tmp_path)
    clock = _Clock()

    results, _boot = rn.run_o_layer(candidate=candidate, candidate_path=candidate_path, store=store,
                                    output=tmp_path / "run", workload_driver=_HealthyDriver(),
                                    final_state_probe=_CleanProbe(), clock=clock, wait=clock.advance)

    by_case = {result.case_id: result for result in results}
    assert by_case["O01"].status == "failed"
    assert any("gap" in problem for problem in by_case["O01"].problems)


def test_o05_primitive_accepts_the_registration_and_refuses_every_tamper(tmp_path) -> None:
    candidate_path, candidate = _candidate(tmp_path)
    document = json.loads(candidate_path.read_text(encoding="utf-8"))
    device = asdict(candidate.device)
    site = {**{field: device[field] for field in op.SITE_DEVICE_FIELDS},
            "filesystem": "ext4",
            "images": {runtime.image_digest: True for runtime in candidate.runtimes},
            "model_files": {asset.path: {"size_bytes": asset.size_bytes, "sha256": asset.sha256}
                            for model in candidate.models for asset in model.assets},
            "config_sha256": candidate.config_sha256,
            "source_archive_sha256": candidate.source_archive_sha256}
    port = op.PreflightPrimitivePort(site=site, tamper=op.tamper_scenarios(document))
    evidence = {"started_at": op._utc_now()}

    accepted = port.preflight(document, evidence)

    assert accepted["accepted"] is True, accepted
    scenarios = port.tamper_scenarios()
    assert [scenario["name"] for scenario in scenarios] == ["asset hash", "device change", "image digest",
                                                            "expired evidence"]
    for scenario in scenarios:
        result = port.preflight(scenario["candidate"], scenario["evidence"] or evidence)
        assert result["accepted"] is False, scenario["name"]
        assert result["reason"], scenario["name"]
        assert result["loaded"] == 0, scenario["name"]


def test_the_end_state_mapping_reads_only_when_the_workload_is_over() -> None:
    reads: list[int] = []

    def reader() -> dict:
        reads.append(1)
        return dict(CLEAN_STATE)

    mapping = op._LazyMapping(reader)

    assert reads == []
    assert dict(mapping)["queue_depth"] == 0
    assert len(reads) == 1


def test_the_workload_driver_records_the_real_call() -> None:
    class _Response:
        status_code = 200

        def json(self) -> dict:
            return {"usage": {"total_tokens": 3}, "model": "qwen-small"}

    calls: list[tuple[str, dict]] = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return _Response()

    clock = _Clock()
    driver = op.HttpWorkloadDriver("http://127.0.0.1:8090", post=post, clock=clock)
    request = wl.PlannedRequest(index=1, offset_seconds=0.0, model_id="qwen-small", capability="chat")

    document = driver.send(request)

    assert calls and calls[0][0] == "http://127.0.0.1:8090/v1/chat/completions"
    assert calls[0][1]["json"]["model"] == "qwen-small"
    assert document["status_code"] == 200 and document["execute_seconds"] == 0.0
    assert document["usage"] == {"total_tokens": 3}


def test_the_not_wired_port_reports_itself_unavailable() -> None:
    port = op.NotWiredPort("the private mount namespace is not wired")

    assert port.anything(at_all=True) == {"available": False, "action": "anything",
                                          "reason": "the private mount namespace is not wired"}
