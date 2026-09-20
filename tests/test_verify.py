"""P25: offline verify recomputes the whole evidence set, then merge joins runs.

The evidence builder writes one material directory per required case, so every
test can break exactly one thing: a raw value, a file, a digest, a timestamp.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from model_scheduler import evidence_contracts as ec
from model_scheduler.acceptance import EXIT_FAILED, EXIT_INPUT, EXIT_OK
from model_scheduler.acceptance import evaluator as ev
from model_scheduler.acceptance import verify as vf

TEGRA = "RAM 1234/32000MB SWAP 0/16000MB GR3D_FREQ 87% cpu@1"
CUDA = "7f2b0000-7f2b1000 r-xp /usr/lib/aarch64-linux-gnu/libcuda.so.1"
CLEAN_STATE = {"oom": 0, "unexpected_500": 0, "unsafe_evictions": 0, "residual": 0, "queue_depth": 0,
               "leases": 0, "sessions": 0, "instances_running": 0}
POLICY = {"duration_seconds": 1800, "arrival_requests": 120, "arrival_gap_seconds_max": 15.0,
          "send_deviation_milliseconds_max": 1000.0, "error_rate_max": 0.1, "queue_full_rate_max": 0.1,
          "timeout_rate_max": 0.1}


def _candidate(tmp_path: Path):
    spec = importlib.util.spec_from_file_location("tc_for_p25",
                                                  Path(__file__).resolve().parent / "test_candidate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    site = module._site(tmp_path)
    module._build(site)
    document = json.loads(site["output"].read_text(encoding="utf-8"))
    return site["output"], ec.parse_candidate(document)


def _workload_row(index: int, outcome: str = "ok") -> dict:
    ok = outcome == "ok"
    return {"index": index, "model_id": "qwen-small", "capability": "chat", "planned_offset_seconds": float(index),
            "sent_offset_seconds": float(index), "deviation_milliseconds": 1.0, "outcome": outcome,
            "status_code": 200 if ok else 429, "queue_seconds": 1.0 if ok else None,
            "load_seconds": 2.0 if ok else None, "execute_seconds": 3.0 if ok else None, "error": None}


def _o_facts(case_id: str) -> dict:
    if case_id == "O02":
        return {"observations": {
            "isolation": {"private_mount_namespace": True, "unmounted_shared_disk": False},
            "model_disk": {"model_disk_unavailable": True, "root_disk_writes": 0},
            "scratch": {"dedicated_quota_fs": True, "scratch_full": True, "root_disk_writes": 0},
            "recovery": {"recovered": True, "rehashed": True}}}
    if case_id == "O03":
        return {"observations": {
            "docker_unreachable": {"health_status": 503, "budget_kept": True, "other_containers_untouched": True},
            "unknown_instance": {"unknown_recorded": True, "health_status": 503, "other_containers_untouched": True},
            "stop_timeout": {"stop_timeout_recorded": True, "budget_kept": True, "health_status": 503}}}
    if case_id == "O04":
        return {"observations": {
            "restart": {"restarted": True, "residual_instances_cleaned": True, "inference_replay": False},
            "residual_after_restart": [], "old_token": {"accepted": False, "reason": "previous boot"},
            "admission": {"admitted": True}}}
    if case_id == "O05":
        return {"observations": {
            "correct": {"accepted": True},
            "tampered": [{"name": "asset hash", "facts": {"accepted": False, "reason": "hash", "loaded": False}},
                         {"name": "device change", "facts": {"accepted": False, "reason": "device", "loaded": False}}]}}
    if case_id == "O06":
        digest = "c" * 64
        return {"observations": {
            "shutdown": {"graceful": True, "exit_code": 0}, "logs": {"capacity_ok": True, "redaction_ok": True},
            "backup": {"snapshot_sha256": digest, "files": 3},
            "restore": {"restored": True, "snapshot_sha256": digest}, "rollback": {"applied": True},
            "rollback_refused": {"applied": False, "reason": "no accepted evidence"}}}
    raise AssertionError(f"no O facts for {case_id}")


def _s03_numbers() -> dict:
    peak = 5_300_000_000
    reserved = (peak * 115 + 99) // 100
    return {"measured_peak_bytes": peak, "model_budget_bytes": reserved,
            "mem_available_bytes": reserved + 4096, "free_floor_bytes": 4096, "committed_bytes": 0,
            "instance_already_reserved": False}


def _case_material(case_id: str) -> tuple[dict, dict[str, object]]:
    """The case.json document and any raw samples it needs."""
    if case_id.startswith("B:"):
        parts = case_id.split(":")
        kind = parts[2]
        samples: dict[str, object] = {"tegrastats": [TEGRA], "proc_maps": [CUDA]}
        if kind in ("load", "stop", "cancel", "reload"):
            facts = {"provider": "runtime-1"}
            if kind == "load":
                facts["instance"] = {"container_id": "c1"}
            if kind == "stop":
                facts["stop_proven"] = True
            if kind == "cancel":
                facts["cancelled"] = True
            return facts, samples
        envelope = {"input_tokens": 512, "output_tokens": 64, "parallel": 2}
        document = {"provider": "runtime-1", "observed": dict(envelope),
                    "output": {"choices": [{"message": {"role": "assistant", "content": "tok out"}}]},
                    "fixture": {"capability": "chat", "fixture_id": "qwen-small-chat", "boundary": dict(envelope)}}
        if kind == "cap":
            capability = parts[3]
            if capability in ("chat", "vision"):
                document["output"] = {"choices": [{"message": {"role": "assistant", "content": "tok out"}}]}
            elif capability == "embeddings":
                document["output"] = {"data": [{"embedding": [0.5, 1.0]}, {"embedding": [1.5, 2.0]}]}
            else:
                document["output"] = {"results": [{"index": 0, "relevance_score": 0.5},
                                                  {"index": 1, "relevance_score": 0.25}]}
        return document, samples
    if case_id == "O01":
        return {"policy": POLICY, "final_state": CLEAN_STATE}, {
            "workload": [_workload_row(index) for index in range(20)]}
    if case_id.startswith("O"):
        return _o_facts(case_id), {}
    observations = {name: True for name in ev.S_CASE_OBSERVATIONS[case_id]}
    document: dict = {"observations": observations}
    if case_id == "S03":
        document["numbers"] = _s03_numbers()
    return document, {}


def _write_material(base: Path, case_id: str, *, overrides: dict | None = None,
                    raw_overrides: dict | None = None) -> tuple[list[dict], str, str]:
    directory = base / "cases" / case_id.replace(":", "_")
    directory.mkdir(parents=True, exist_ok=True)
    document, samples = _case_material(case_id)
    document = {**document, **(overrides or {})}
    references: list[dict] = []
    for name, payload in (("case.json", json.dumps({"case_id": case_id, **document}, sort_keys=True)),
                          *[(f"samples/{kind}.jsonl",
                             "".join(json.dumps({"kind": kind, "raw": line}) + "\n"
                                     for line in (raw_overrides or {}).get(kind, lines)))
                            for kind, lines in samples.items()]):
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        body = payload.encode("utf-8")
        path.write_bytes(body)
        references.append({"relative_path": str(path.relative_to(base)), "size_bytes": len(body),
                           "sha256": hashlib.sha256(body).hexdigest()})
    return references, directory.name, case_id


def _build_evidence(tmp_path: Path, candidate_path: Path, *, cases: set[str] | None = None,
                    case_overrides: dict[str, dict] | None = None, raw_overrides: dict[str, dict] | None = None,
                    started_at: datetime | None = None, drop_final: str | None = None,
                    bad_collector: bool = False) -> Path:
    candidate = ec.parse_candidate(json.loads(candidate_path.read_text(encoding="utf-8")))
    digest = ec.candidate_digest(candidate)
    device = ec.device_digest(candidate.device)
    required = sorted(ec.required_case_ids(candidate))
    selected = [case_id for case_id in required if cases is None or case_id in cases]
    start = started_at or datetime(2026, 9, 18, 0, 0, tzinfo=timezone.utc)
    base = tmp_path / "evidence"
    base.mkdir(parents=True, exist_ok=True)
    attempts = []
    finals = []
    artifacts: list[dict] = []
    for index, case_id in enumerate(selected):
        references, _directory_name, case = _write_material(
            base, case_id, overrides=(case_overrides or {}).get(case_id),
            raw_overrides=(raw_overrides or {}).get(case_id))
        artifacts.extend(references)
        started = (start + timedelta(minutes=index)).isoformat().replace("+00:00", "Z")
        ended = (start + timedelta(minutes=index, seconds=30)).isoformat().replace("+00:00", "Z")
        attempts.append({"case_id": case, "run_id": "run-1", "attempt": 1, "candidate_sha256": digest,
                         "device_digest": device, "started_at": started, "ended_at": ended,
                         "boot_id": "boot-1", "event_refs": references,
                         "collector_sha256": ("e" * 64 if bad_collector else candidate.collector_sha256),
                         "evaluator_sha256": candidate.evaluator_sha256, "exit_code": 0})
        if case_id != drop_final:
            finals.append({"case_id": case, "run_id": "run-1", "attempt": 1})
    report = {"schema_version": 3, "candidate_sha256": digest, "device_digest": device, "run_id": "run-1",
              "started_at": min(attempt["started_at"] for attempt in attempts),
              "ended_at": max(attempt["ended_at"] for attempt in attempts),
              "case_attempt_refs": attempts, "final_attempts": finals,
              "artifact_manifest": sorted(artifacts, key=lambda item: item["relative_path"])}
    (base / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return base


def _verify(tmp_path: Path, candidate_path: Path, evidence: Path, **kwargs):
    return vf.verify_evidence(candidate_path=candidate_path, evidence_dir=evidence, **kwargs)


def test_a_complete_evidence_set_passes_offline(tmp_path) -> None:
    candidate_path, _candidate_object = _candidate(tmp_path)
    evidence = _build_evidence(tmp_path, candidate_path)

    code, document = _verify(tmp_path, candidate_path, evidence)

    assert code == EXIT_OK
    assert document["verdict"] == "passed"
    assert len(document["cases"]) == len(ec.required_case_ids(ec.parse_candidate(
        json.loads(candidate_path.read_text(encoding="utf-8")))))


def test_missing_or_tampered_material_is_an_input_error(tmp_path) -> None:
    candidate_path, _ = _candidate(tmp_path)
    evidence = _build_evidence(tmp_path, candidate_path)
    victim = next(evidence.rglob("case.json"))
    victim.unlink()
    code, document = _verify(tmp_path, candidate_path, evidence)
    assert code == EXIT_INPUT and "missing" in document["error"]

    evidence = _build_evidence(tmp_path / "second", candidate_path)
    victim = next(evidence.rglob("case.json"))
    victim.write_text(json.dumps({"case_id": "S01", "tampered": True}), encoding="utf-8")
    code, document = _verify(tmp_path, candidate_path, evidence)
    assert code == EXIT_INPUT and "differs from the manifest" in document["error"]


def test_a_semantically_failing_case_exits_three(tmp_path) -> None:
    candidate_path, _ = _candidate(tmp_path)
    evidence = _build_evidence(tmp_path, candidate_path, case_overrides={
        "O02": _o_facts("O02") | {"observations": {**_o_facts("O02")["observations"],
                                                   "model_disk": {"model_disk_unavailable": True,
                                                                  "root_disk_writes": 4}}}})

    code, document = _verify(tmp_path, candidate_path, evidence)

    assert code == EXIT_FAILED
    assert any("root_disk_writes" in problem for problem in document["problems"])


def test_the_validity_window_is_seven_days_from_the_start(tmp_path) -> None:
    candidate_path, _ = _candidate(tmp_path)
    start = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    evidence = _build_evidence(tmp_path, candidate_path, started_at=start)

    fresh = _verify(tmp_path, candidate_path, evidence, now=start + timedelta(days=6))
    assert fresh[0] == EXIT_OK
    boundary = _verify(tmp_path, candidate_path, evidence, now=start + timedelta(days=7))
    assert boundary[0] == EXIT_FAILED and "expired" in boundary[1]["error"]
    late = _verify(tmp_path, candidate_path, evidence, now=start + timedelta(days=8))
    assert late[0] == EXIT_FAILED and "repackaging" in late[1]["error"]

    future = _build_evidence(tmp_path / "future", candidate_path,
                             started_at=(start + timedelta(days=1)))
    code, document = _verify(tmp_path, candidate_path, future, now=start)
    assert code == EXIT_FAILED and "future" in document["error"]


def test_identity_and_tool_bindings_are_enforced(tmp_path) -> None:
    candidate_path, _ = _candidate(tmp_path)
    evidence = _build_evidence(tmp_path, candidate_path)
    report = json.loads((evidence / "report.json").read_text(encoding="utf-8"))
    report["candidate_sha256"] = "f" * 64
    (evidence / "report.json").write_text(json.dumps(report), encoding="utf-8")
    code, document = _verify(tmp_path, candidate_path, evidence)
    assert code == EXIT_FAILED and "candidate" in document["error"]

    mismatched = _build_evidence(tmp_path / "collector", candidate_path, bad_collector=True)
    code, document = _verify(tmp_path, candidate_path, mismatched)
    assert code == EXIT_FAILED and "collector hash" in document["error"]


def test_an_incomplete_case_set_is_structurally_rejected(tmp_path) -> None:
    candidate_path, _ = _candidate(tmp_path)
    evidence = _build_evidence(tmp_path, candidate_path, drop_final="S04")

    code, document = _verify(tmp_path, candidate_path, evidence)

    assert code == EXIT_INPUT and "missing final attempts" in document["error"]


def test_a_rewritten_summary_cannot_save_a_tampered_raw_value(tmp_path) -> None:
    candidate_path, _ = _candidate(tmp_path)
    evidence = _build_evidence(tmp_path, candidate_path, raw_overrides={
        "B:qwen-small:cap:vision": {"tegrastats": ["RAM 1234/32000MB SWAP 0/16000MB GR3D_FREQ 5% cpu@1"]}})
    # the material still claims success in its own document
    case_file = evidence / "cases" / "B_qwen-small_cap_vision" / "case.json"
    document = json.loads(case_file.read_text(encoding="utf-8"))
    document.update({"status": "passed", "passed": True, "device_activity": {"gr3d_peak_pct": 99}})
    case_file.write_text(json.dumps(document), encoding="utf-8")

    code, verdict = _verify(tmp_path, candidate_path, evidence)

    assert code == EXIT_INPUT  # the rewritten file no longer matches the manifest...
    report = json.loads((evidence / "report.json").read_text(encoding="utf-8"))
    for reference in report["artifact_manifest"]:
        if reference["relative_path"].endswith("B_qwen-small_cap_vision/case.json"):
            body = case_file.read_bytes()
            reference["size_bytes"] = len(body)
            reference["sha256"] = hashlib.sha256(body).hexdigest()
    for attempt in report["case_attempt_refs"]:
        for reference in attempt["event_refs"]:
            if reference["relative_path"].endswith("B_qwen-small_cap_vision/case.json"):
                body = case_file.read_bytes()
                reference["size_bytes"] = len(body)
                reference["sha256"] = hashlib.sha256(body).hexdigest()
    (evidence / "report.json").write_text(json.dumps(report), encoding="utf-8")

    code, document = _verify(tmp_path, candidate_path, evidence)

    assert code == EXIT_OK  # GR3D 5% is still real device activity: the case is genuinely proven


def test_verify_never_opens_a_socket_or_starts_a_process(tmp_path, monkeypatch) -> None:
    import socket
    import subprocess

    candidate_path, _ = _candidate(tmp_path)
    evidence = _build_evidence(tmp_path, candidate_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("offline verification must not touch the network or spawn processes")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)

    code, document = _verify(tmp_path, candidate_path, evidence)

    assert code == EXIT_OK and document["verdict"] == "passed"


def test_merge_joins_runs_and_recomputes_the_window(tmp_path) -> None:
    candidate_path, _ = _candidate(tmp_path)
    candidate = ec.parse_candidate(json.loads(candidate_path.read_text(encoding="utf-8")))
    required = ec.required_case_ids(candidate)
    software = {case_id for case_id in required if case_id.startswith("S") or case_id.startswith("B:")}
    operational = {case_id for case_id in required if case_id.startswith("O")}
    run_a = _build_evidence(tmp_path / "run-a", candidate_path, cases=software)
    run_b = _build_evidence(tmp_path / "run-b", candidate_path, cases=operational,
                            started_at=datetime(2026, 9, 18, 6, 0, tzinfo=timezone.utc))

    merged = vf.merge_runs(candidate_path=candidate_path, run_dirs=[run_a, run_b], output=tmp_path / "merged")

    assert merged["cases"] == len(required)
    code, document = vf.verify_evidence(candidate_path=candidate_path, evidence_dir=tmp_path / "merged",
                                        now=datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc))
    assert code == EXIT_OK and document["verdict"] == "passed"

    with pytest.raises(vf.VerifyError, match="duplicate attempt"):
        vf.merge_runs(candidate_path=candidate_path, run_dirs=[run_a, run_a], output=tmp_path / "twice")


def test_the_cli_wires_verify_and_merge(tmp_path) -> None:
    from model_scheduler.acceptance.__main__ import main

    candidate_path, _ = _candidate(tmp_path)
    evidence = _build_evidence(tmp_path, candidate_path)

    assert main(["verify", "--candidate", str(candidate_path), "--evidence", str(evidence)]) == EXIT_OK
    output = tmp_path / "cli-merged"
    assert main(["merge", "--candidate", str(candidate_path), "--runs", str(evidence), "--output", str(output)]) == EXIT_OK
    assert (output / "report.json").is_file()
    assert main(["merge", "--candidate", str(candidate_path), "--runs", str(evidence), "--output", str(output)]) == EXIT_INPUT
