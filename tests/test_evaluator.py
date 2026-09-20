"""P25: the evaluator recomputes from raw material — a stored verdict means nothing.

Every test rewrites the material (a raw sample, a boundary number, an
observation) and asserts the conclusion follows the *material*, not a `status`
or `passed` flag that was left in the document.
"""
from __future__ import annotations

import json
from pathlib import Path

from model_scheduler.acceptance import evaluator as ev

TEGRA_ACTIVE = "RAM 1234/32000MB SWAP 0/16000MB GR3D_FREQ 87% cpu@1"
CUDA_MAP = "7f2b0000-7f2b1000 r-xp /usr/lib/aarch64-linux-gnu/libcuda.so.1"
CLEAN_STATE = {"oom": 0, "unexpected_500": 0, "unsafe_evictions": 0, "residual": 0, "queue_depth": 0,
               "leases": 0, "sessions": 0, "instances_running": 0}
POLICY = {"duration_seconds": 1800, "arrival_requests": 120, "arrival_gap_seconds_max": 15.0,
          "send_deviation_milliseconds_max": 1000.0, "error_rate_max": 0.1, "queue_full_rate_max": 0.1,
          "timeout_rate_max": 0.1}


def _material(directory: Path, case_id: str, *, samples: dict[str, list[str]] | None = None, **document) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "case.json").write_text(json.dumps({"case_id": case_id, **document}), encoding="utf-8")
    for kind, lines in (samples or {}).items():
        target = directory / "samples" / f"{kind}.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("".join(json.dumps({"kind": kind, "raw": line}) + "\n" for line in lines),
                          encoding="utf-8")
    return directory


def _backend_document(case_id: str, **overrides) -> dict:
    document = {"provider": "runtime-1",
               "output": {"choices": [{"message": {"role": "assistant", "content": "tok out"}}]},
                "observed": {"input_tokens": 512, "output_tokens": 64, "parallel": 2},
                "fixture": {"capability": "chat", "fixture_id": "qwen-small-chat",
                            "boundary": {"input_tokens": 512, "output_tokens": 64, "parallel": 2}}}
    document.update(overrides)
    return document


def test_a_backend_case_is_recomputed_from_raw_samples_and_the_boundary(tmp_path) -> None:
    directory = _material(tmp_path / "B_qwen-small_envelope", "B:qwen-small:envelope",
                          samples={"tegrastats": [TEGRA_ACTIVE], "proc_maps": [CUDA_MAP]},
                          **_backend_document("B:qwen-small:envelope"))

    verdict = ev.evaluate_case(case_id="B:qwen-small:envelope", directory=directory)

    assert verdict.passed is True
    assert verdict.evidence["attribution"]["gr3d_peak_pct"] == 87


def test_a_case_without_raw_samples_or_below_the_boundary_fails_even_if_it_claims_success(tmp_path) -> None:
    without_samples = _material(tmp_path / "no-samples", "B:qwen-small:envelope",
                                status="passed", gpu_verified=True,
                                device_activity={"gr3d_peak_pct": 99, "raw_samples": {"tegrastats": 3}},
                                **_backend_document("B:qwen-small:envelope"))

    verdict = ev.evaluate_case(case_id="B:qwen-small:envelope", directory=without_samples)

    assert verdict.passed is False
    assert any("no raw device samples" in reason for reason in verdict.reasons)

    short_boundary = _material(tmp_path / "short", "B:qwen-small:envelope", status="passed",
                               samples={"tegrastats": [TEGRA_ACTIVE]},
                               **_backend_document("B:qwen-small:envelope",
                                                   observed={"input_tokens": 256, "output_tokens": 64, "parallel": 2}))

    verdict = ev.evaluate_case(case_id="B:qwen-small:envelope", directory=short_boundary)

    assert verdict.passed is False
    assert any("did not reach the declared boundary" in reason for reason in verdict.reasons)


def test_a_capability_case_is_recomputed_from_its_real_output(tmp_path) -> None:
    healthy = _material(tmp_path / "cap-ok", "B:qwen-small:cap:embeddings", samples={"tegrastats": [TEGRA_ACTIVE]},
                        provider="runtime-1", observed={"batch": 2},
                        output={"data": [{"embedding": [0.5, 1.0]}, {"embedding": [1.5, 2.0]}]})
    assert ev.evaluate_case(case_id="B:qwen-small:cap:embeddings", directory=healthy).passed is True

    nan = _material(tmp_path / "cap-nan", "B:qwen-small:cap:embeddings", samples={"tegrastats": [TEGRA_ACTIVE]},
                    status="passed", provider="runtime-1", observed={"batch": 2},
                    output={"data": [{"embedding": [float("nan"), 1.0]}, {"embedding": [1.5, 2.0]}]})
    verdict = ev.evaluate_case(case_id="B:qwen-small:cap:embeddings", directory=nan)
    assert verdict.passed is False and any("non-finite" in reason for reason in verdict.reasons)


def test_o01_metrics_are_recomputed_from_the_raw_rows(tmp_path) -> None:
    def row(index: int, outcome: str) -> dict:
        ok = outcome == "ok"
        return {"index": index, "model_id": "qwen-small", "capability": "chat", "planned_offset_seconds": float(index),
                "sent_offset_seconds": float(index), "deviation_milliseconds": 1.0, "outcome": outcome,
                "status_code": 200 if ok else 429, "queue_seconds": 1.0 if ok else None,
                "load_seconds": 2.0 if ok else None, "execute_seconds": 3.0 if ok else None, "error": None}

    clean = _material(tmp_path / "o01-ok", "O01",
                      samples={"workload": [row(index, "ok") for index in range(20)]},
                      policy=POLICY, final_state=CLEAN_STATE, verdict="passed")
    assert ev.evaluate_case(case_id="O01", directory=clean).passed is True

    # 3 of 20 = 0.15 breaches the 0.1 cap (2 of 20 sits exactly on it and must pass)
    breached_rows = [row(index, "ok") for index in range(17)] + [row(index, "queue_full") for index in range(17, 20)]
    breached = _material(tmp_path / "o01-bad", "O01", samples={"workload": breached_rows},
                         policy=POLICY, final_state=CLEAN_STATE, verdict="passed", status="passed")
    verdict = ev.evaluate_case(case_id="O01", directory=breached)
    assert verdict.passed is False and any("queue_full_rate" in reason for reason in verdict.reasons)

    dirty_state = _material(tmp_path / "o01-dirty", "O01",
                            samples={"workload": [row(index, "ok") for index in range(20)]},
                            policy=POLICY, final_state={**CLEAN_STATE, "residual": 1})
    assert ev.evaluate_case(case_id="O01", directory=dirty_state).passed is False


def test_operational_facts_are_recomputed_with_the_p24_predicates(tmp_path) -> None:
    healthy = _material(
        tmp_path / "o02-ok", "O02",
        observations={"isolation": {"private_mount_namespace": True, "unmounted_shared_disk": False},
                      "model_disk": {"model_disk_unavailable": True, "root_disk_writes": 0},
                      "scratch": {"dedicated_quota_fs": True, "scratch_full": True, "root_disk_writes": 0},
                      "recovery": {"recovered": True, "rehashed": True}})
    assert ev.evaluate_case(case_id="O02", directory=healthy).passed is True

    root_writes = _material(
        tmp_path / "o02-bad", "O02", status="passed",
        observations={"isolation": {"private_mount_namespace": True, "unmounted_shared_disk": False},
                      "model_disk": {"model_disk_unavailable": True, "root_disk_writes": 2},
                      "scratch": {"dedicated_quota_fs": True, "scratch_full": True, "root_disk_writes": 0},
                      "recovery": {"recovered": True, "rehashed": True}})
    verdict = ev.evaluate_case(case_id="O02", directory=root_writes)
    assert verdict.passed is False and any("root_disk_writes" in reason for reason in verdict.reasons)

    unknown = _material(tmp_path / "o99", "O07", observations={})
    verdict = ev.evaluate_case(case_id="O07", directory=unknown)
    assert verdict.passed is False and any("no recomputation rule" in reason for reason in verdict.reasons)


def test_s03_arithmetic_is_recomputed_at_the_boundary_and_one_byte_below(tmp_path) -> None:
    peak = 5_300_000_000
    reserved = (peak * 115 + 99) // 100
    numbers = {"measured_peak_bytes": peak, "model_budget_bytes": reserved, "mem_available_bytes": reserved + 4096,
               "free_floor_bytes": 4096, "committed_bytes": 0, "instance_already_reserved": False}
    healthy = _material(tmp_path / "s03-ok", "S03", numbers=numbers,
                        observations={name: True for name in ev.S_CASE_OBSERVATIONS["S03"]})
    assert ev.evaluate_case(case_id="S03", directory=healthy).passed is True

    inside = _material(tmp_path / "s03-inside", "S03", status="passed",
                       numbers={**numbers, "mem_available_bytes": reserved + 4097},
                       observations={name: True for name in ev.S_CASE_OBSERVATIONS["S03"]})
    verdict = ev.evaluate_case(case_id="S03", directory=inside)
    assert verdict.passed is False and any("equality sample" in reason for reason in verdict.reasons)

    missing = _material(tmp_path / "s03-missing", "S03", numbers={"measured_peak_bytes": peak},
                        observations={name: True for name in ev.S_CASE_OBSERVATIONS["S03"]})
    assert ev.evaluate_case(case_id="S03", directory=missing).passed is False


def test_software_cases_need_their_declared_observations(tmp_path) -> None:
    complete = _material(tmp_path / "s05", "S05",
                         observations={name: True for name in ev.S_CASE_OBSERVATIONS["S05"]})
    assert ev.evaluate_case(case_id="S05", directory=complete).passed is True

    incomplete = _material(tmp_path / "s05-bad", "S05",
                           observations={name: True for name in ev.S_CASE_OBSERVATIONS["S05"] if name != "quota_enforced"})
    verdict = ev.evaluate_case(case_id="S05", directory=incomplete)
    assert verdict.passed is False and any("quota_enforced" in reason for reason in verdict.reasons)

    lied = _material(tmp_path / "s06", "S06", passed=True,
                     observations={**{name: True for name in ev.S_CASE_OBSERVATIONS["S06"]},
                                   "tampered_asset_rejected": False})
    assert ev.evaluate_case(case_id="S06", directory=lied).passed is False


def test_material_for_another_case_is_refused(tmp_path) -> None:
    directory = _material(tmp_path / "wrong", "S01", observations={})

    verdict = ev.evaluate_case(case_id="S02", directory=directory)

    assert verdict.passed is False and any("belongs to" in reason for reason in verdict.reasons)


def test_missing_material_is_an_evaluator_error(tmp_path) -> None:
    import pytest

    with pytest.raises(ev.EvaluatorError, match="case.json is missing"):
        ev.evaluate_case(case_id="S01", directory=tmp_path / "nothing")
