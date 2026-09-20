"""P29: the S layer and the per-case material shape.

Two contracts are pinned here:

* every S observation is the result of a real call — the checks run the real
  scheduler/Book/session/queue/blob/parser objects, and a check whose explicit
  input is missing fails instead of being filled in;
* the material a run leaves behind is exactly what the evaluator and merge
  re-read: one directory per case with `case.json` and `samples/`, so both a B
  case and an S case can be recomputed offline.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from model_scheduler.acceptance import backend_cases as bc
from model_scheduler.acceptance import evaluator as ev
from model_scheduler.acceptance import fixtures as fx
from model_scheduler.acceptance import materials as mt
from model_scheduler.acceptance import runner as rn
from model_scheduler.acceptance import software_cases as sc
from model_scheduler.acceptance import verify as vf
from model_scheduler.contracts_v2 import Envelope
from model_scheduler.evidence_contracts import parse_acceptance_report, parse_candidate

ENVELOPE = Envelope(ctx_size=1024, max_input_tokens=512, max_output_tokens=64, max_parallel=2,
                    max_image_tokens=64, max_image_edge_pixels=64, max_images=1)
FILLER = fx.FillerSpec(unit="a", tokens_per_unit=1.0, template_overhead_tokens=19,
                       vision_template_overhead_tokens=50, instruction="Reply briefly.",
                       instruction_tokens=3)
TEGRASTATS = "RAM 1234/32000MB SWAP 0/16000MB GR3D_FREQ 87% cpu@1"
PROC_MAPS = "7f2b0000-7f2b1000 r-xp /usr/lib/aarch64-linux-gnu/libcuda.so.1"
SAMPLES = ({"kind": "tegrastats", "raw": TEGRASTATS}, {"kind": "proc_maps", "raw": PROC_MAPS})


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _candidate(tmp_path: Path):
    module = _load_module("tc_for_s_layer", Path(__file__).resolve().parent / "test_candidate.py")
    site = module._site(tmp_path)
    module._build(site)
    document = json.loads(site["output"].read_text(encoding="utf-8"))
    return site["output"], parse_candidate(document)


def _inventory(tmp_path: Path) -> Path:
    module = _load_module("tm_for_s_layer", Path(__file__).resolve().parent / "test_migration_v2.py")
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(module._inventory()), encoding="utf-8")
    return path


class _FakeDriver:
    """A scripted official-API surface that also brings its own raw device rows."""

    def __init__(self) -> None:
        self.loads: list[bool] = []

    def load(self, model_id: str, *, cold: bool):
        self.loads.append(cold)
        return {"provider": "runtime-1", "instance": {"container_id": "c1", "model_id": model_id},
                "cold": cold, "samples": SAMPLES}

    def start(self, model_id: str, request):
        return {"execution_id": "exec-1", "provider": "runtime-1"}

    def execute(self, model_id: str, request):
        return {"provider": "runtime-1", "output": {"choices": [{"message": {"role": "assistant",
                                                                             "content": "out"}}]},
                "observed": {"input_tokens": 1, "output_tokens": 1, "parallel": 1}, "samples": SAMPLES}

    def cancel(self, model_id: str, execution_id: str):
        return {"provider": "runtime-1", "cancelled": True, "samples": SAMPLES}

    def stop(self, model_id: str):
        return {"provider": "runtime-1", "stop_proven": True, "samples": SAMPLES}

    def cleanup(self, model_id: str):
        return {"stopped": True, "model_id": model_id}


# ---------------------------------------------------------------------------
# the checks themselves


def test_s02_records_every_failure_rule() -> None:
    observations, facts, problems = sc._check_s02()

    assert observations == {"load_failure_recorded": True, "late_load_rejected": True,
                            "old_boot_rejected": True, "stop_returned_instance_alive_recorded": True,
                            "unknown_keeps_budget": True}
    assert problems == []
    assert facts["evidence"]["old_boot_rejected"]["reason"] == "stale_boot"


def test_s03_uses_the_books_own_boundary_arithmetic() -> None:
    observations, facts, problems = sc._check_s03()

    assert observations == {"boundary_equality_holds": True, "boundary_one_byte_rejected": True,
                            "sample_freshness_enforced": True, "no_double_counting": True}
    assert problems == []
    numbers = facts["numbers"]
    peak = numbers["measured_peak_bytes"]
    assert numbers["mem_available_bytes"] == (peak * 115 + 99) // 100 + numbers["free_floor_bytes"]
    assert numbers["model_budget_bytes"] == numbers["committed_bytes"] + (peak * 115 + 99) // 100


def test_s04_real_session_queue_and_idempotency_rules() -> None:
    observations, _facts, problems = sc._check_s04()

    assert set(observations) == {"interactive_priority_holds", "drain_keeps_lease", "ttl_enforced",
                                 "hard_deadline_enforced", "cancel_observed", "idempotency_enforced"}
    assert all(value is True for value in observations.values()), observations
    assert problems == []


def test_s05_reads_the_capability_the_registration_really_has(tmp_path) -> None:
    _path, candidate = _candidate(tmp_path)
    observations, facts, problems = sc._check_s05(candidate=candidate, material_dir=tmp_path / "S05")

    assert all(value is True for value in observations.values()), (observations, problems)
    assert facts["evidence"]["compat"]["capability"] == "embeddings"


def test_s06_refuses_tampered_and_forged_material(tmp_path) -> None:
    candidate_path, candidate = _candidate(tmp_path)
    observations, facts, problems = sc._check_s06(candidate=candidate, material_dir=tmp_path / "S06",
                                                  candidate_path=candidate_path)

    assert all(value is True for value in observations.values()), (observations, problems)
    assert facts["evidence"]["tampered_evidence_rejected"]["exit"] == vf.EXIT_INPUT


def test_s06_without_the_candidate_document_claims_nothing(tmp_path) -> None:
    _path, candidate = _candidate(tmp_path)
    observations, _facts, problems = sc._check_s06(candidate=candidate, material_dir=tmp_path / "S06",
                                                   candidate_path=None)

    assert set(observations) == set(ev.S_CASE_OBSERVATIONS["S06"])
    assert not any(observations.values())
    assert problems and "candidate document" in problems[0]


def test_s01_needs_its_explicit_inputs_and_then_really_migrates(tmp_path) -> None:
    _path, candidate = _candidate(tmp_path)
    without, _facts, problems = sc._check_s01(candidate=candidate, material_dir=tmp_path / "without",
                                              inventory=None, legacy_config=None, check_config=None)

    assert without["legacy_config_migrated"] is False  # a missing input is never a placeholder pass
    assert without["runtime_registered"] is True and without["strict_schema_enforced"] is True
    assert problems

    inventory = _inventory(tmp_path)
    observations, facts, _problems = sc._check_s01(candidate=candidate, material_dir=tmp_path / "with",
                                                   inventory=inventory, legacy_config=None, check_config=None)

    assert all(value is True for value in observations.values()), observations
    assert facts["evidence"]["legacy_migration"]["check_config_exit"] == 0
    assert facts["evidence"]["legacy_migration"]["models"] == ["embedding", "qwen-large", "qwen-small",
                                                               "reranker"]


def test_the_interface_and_the_checks_cannot_drift() -> None:
    assert sc.SOFTWARE_CASES == tuple(sorted(ev.S_CASE_OBSERVATIONS))
    for case_id in sc.SOFTWARE_CASES:
        observations = {name: False for name in ev.S_CASE_OBSERVATIONS[case_id]}
        assert set(observations) == set(ev.S_CASE_OBSERVATIONS[case_id])


# ---------------------------------------------------------------------------
# the material the evaluator re-reads


def test_a_software_run_leaves_material_the_evaluator_recomputes(tmp_path) -> None:
    _path, candidate = _candidate(tmp_path)
    store = mt.CaseMaterialStore(tmp_path / "run", run_id="run-1", candidate_sha256="a" * 64,
                                 device_digest="b" * 64)
    for case_id in ("S01", "S02", "S03"):
        directory = store.begin_case(case_id, attempt=1)
        result = sc.run_software_case(case_id, candidate=candidate, material_dir=directory,
                                      candidate_path=None, inventory=None)
        store.end_case(status=result.status, facts=result.facts,
                       failure="; ".join(result.problems) or None, problems=result.problems)

        references = store.refs(case_id, 1)
        assert references[0].relative_path.endswith("/case.json")
        assert {ref.relative_path for ref in references} >= {references[0].relative_path}

        verdict = ev.evaluate_case(case_id=case_id, directory=directory)
        if case_id == "S01":
            assert verdict.passed is False  # the missing inventory is carried into the material
            assert any("legacy_config_migrated" in reason for reason in verdict.reasons)
        else:
            assert verdict.passed is True, verdict.reasons


def test_a_b_case_leaves_recomputable_material(tmp_path) -> None:
    """The per-case directory is the contract: `case.json` plus its raw samples."""
    store = mt.CaseMaterialStore(tmp_path / "run", run_id="run-1", candidate_sha256="a" * 64,
                                 device_digest="b" * 64)
    executor = bc.CaseExecutor(_FakeDriver(), collector=store, filler_of={"qwen-small": FILLER})
    attempts = executor.run_model(model_id="qwen-small", capabilities=("chat",), envelope=ENVELOPE)

    for attempt in attempts:
        references = store.refs(attempt.case_id, attempt.attempt)
        assert references[0].relative_path.endswith("/case.json")
        directory = store.case_directory(attempt.case_id, attempt.attempt)
        assert (directory / "samples" / "tegrastats.jsonl").is_file()
        if attempt.case_id.endswith((":load", ":stop", ":cancel", ":reload")):
            verdict = ev.evaluate_case(case_id=attempt.case_id, directory=directory)
            assert verdict.passed is True, (attempt.case_id, verdict.reasons)

    envelope = next(attempt for attempt in attempts if attempt.case_id.endswith(":envelope"))
    material = json.loads((store.case_directory(envelope.case_id, envelope.attempt) / "case.json")
                          .read_text(encoding="utf-8"))
    assert material["fixture"]["boundary"]  # the declared boundary travels with the case


def test_evaluate_case_refuses_a_rewritten_observation(tmp_path) -> None:
    _path, candidate = _candidate(tmp_path)
    store = mt.CaseMaterialStore(tmp_path / "run", run_id="run-1", candidate_sha256="a" * 64,
                                 device_digest="b" * 64)
    directory = store.begin_case("S03", attempt=1)
    result = sc.run_software_case("S03", candidate=candidate, material_dir=directory, candidate_path=None)
    store.end_case(status=result.status, facts=result.facts, problems=result.problems)

    document = json.loads((directory / "case.json").read_text(encoding="utf-8"))
    document["observations"]["boundary_one_byte_rejected"] = False
    (directory / "case.json").write_text(json.dumps(document), encoding="utf-8")

    verdict = ev.evaluate_case(case_id="S03", directory=directory)

    assert verdict.passed is False
    assert any("boundary_one_byte_rejected" in reason for reason in verdict.reasons)


# ---------------------------------------------------------------------------
# the run entry point


def test_run_of_the_s_layer_persists_a_v3_report(tmp_path) -> None:
    candidate_path, candidate = _candidate(tmp_path)
    inventory = _inventory(tmp_path)
    output = tmp_path / "run-s"

    summary = rn.run_layers(candidate_path=candidate_path, layers=("S",), output=output, inventory=inventory)

    assert summary["verdict"] == "passed", summary
    assert summary["cases"] == 6 and summary["passed"] == 6
    report = parse_acceptance_report(json.loads((output / "report.json").read_text(encoding="utf-8")))
    assert report.candidate_sha256 == summary["candidate_sha256"]
    for attempt in report.attempts:
        assert attempt.event_refs[0].relative_path.endswith("/case.json")
    assert {final.case_id for final in report.finals} == set(sc.SOFTWARE_CASES)


def test_a_run_without_its_explicit_input_fails_the_case_it_belongs_to(tmp_path) -> None:
    candidate_path, _candidate_value = _candidate(tmp_path)
    output = tmp_path / "run-s-no-inventory"

    summary = rn.run_layers(candidate_path=candidate_path, layers=("S",), output=output, inventory=None)

    assert summary["verdict"] == "blocked"
    material = json.loads((output / "cases" / "s01" / "attempt-1" / "case.json").read_text(encoding="utf-8"))
    assert material["observations"]["legacy_config_migrated"] is False
    assert material["observations"]["strict_schema_enforced"] is True
    assert summary["failed"] == 1  # only S01 is missing its explicit input


def test_a_run_never_starts_a_deployment(tmp_path) -> None:
    candidate_path, _candidate_value = _candidate(tmp_path)

    with pytest.raises(rn.LayerError) as refused:
        rn.run_layers(candidate_path=candidate_path, layers=("B",), output=tmp_path / "run-b")

    assert refused.value.exit_code == rn.EXIT_INPUT
    assert "SMS_CONTROL_SOCKET" in str(refused.value)
    assert not (tmp_path / "run-b" / "report.json").exists()


def test_the_merge_keeps_the_material_root_of_every_attempt(tmp_path) -> None:
    candidate_path, _candidate_value = _candidate(tmp_path)
    inventory = _inventory(tmp_path)
    run = tmp_path / "run-s"
    rn.run_layers(candidate_path=candidate_path, layers=("S",), output=run, inventory=inventory)

    merged = tmp_path / "merged"
    result = vf.merge_runs(candidate_path=candidate_path, run_dirs=[run], output=merged)

    assert result["cases"] == 6
    for attempt in json.loads((merged / "report.json").read_text(encoding="utf-8"))["case_attempt_refs"]:
        reference = attempt["event_refs"][0]["relative_path"]
        assert reference.endswith("/case.json")
        assert (merged / reference).is_file()
        assert hashlib.sha256((merged / reference).read_bytes()).hexdigest() == attempt["event_refs"][0]["sha256"]
