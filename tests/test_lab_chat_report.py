"""CT10: the lab-chat-report-v1 contract — attempts, finals and artifacts (A12/§7).

A verdict is never stored: the report records which attempt produced which material,
and `final` may only cite an attempt that exists. A stored `summary`, a renamed status,
a final that points at a missing attempt, a forbidden field or an artifact whose bytes
no longer match is refused — including when the hash is rewritten along with the file.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from model_scheduler.acceptance import lab_report as lr
from model_scheduler.acceptance import lab_suite as ls

CODE_SHA = "a" * 40
DIGEST = "b" * 64
START = "2026-09-25T15:00:00+00:00"
END = "2026-09-25T15:30:00+00:00"


def _required() -> tuple[tuple[str, str], ...]:
    return (("L:qwen25vl-7b:legacy", "chat-json"), ("L:qwen36-27b:legacy", "cold-count"))


def _write_material(root: Path) -> list[dict]:
    (root / "cases" / "legacy").mkdir(parents=True, exist_ok=True)
    (root / "cases" / "legacy" / "request.json").write_bytes(b'{"model":"qwen36-27b"}')
    return lr.collect_artifacts(root)


def _report(root: Path, **changes) -> dict:
    if "artifacts" not in changes:
        changes["artifacts"] = _write_material(root)
    document = {
        "schema": "lab-chat-report-v1",
        "suite": ls.SUITE_CANDIDATE,
        "site_sha256": DIGEST,
        "code_sha": CODE_SHA,
        "policy_source_sha256": DIGEST,
        "fixture_set_sha256": DIGEST,
        "candidate_report_sha256": None,
        "started_at_utc": START,
        "ended_at_utc": END,
        "attempts": [
            {"case_id": "L:qwen25vl-7b:legacy", "variant": "chat-json", "attempt": 1, "status": "passed",
             "reason": None, "request_files": ["cases/legacy/request.json"], "response_files": [],
             "observation_files": [], "cleanup_files": [], "start": START, "end": END},
            {"case_id": "L:qwen36-27b:legacy", "variant": "cold-count", "attempt": 1, "status": "failed",
             "reason": "the cold count did not move", "request_files": [], "response_files": [],
             "observation_files": [], "cleanup_files": [], "start": START, "end": END},
            {"case_id": "L:qwen36-27b:legacy", "variant": "cold-count", "attempt": 2, "status": "passed",
             "reason": None, "request_files": ["cases/legacy/request.json"], "response_files": [],
             "observation_files": [], "cleanup_files": [], "start": START, "end": END},
        ],
        "final": [{"case_id": "L:qwen25vl-7b:legacy", "variant": "chat-json", "attempt": 1},
                  {"case_id": "L:qwen36-27b:legacy", "variant": "cold-count", "attempt": 2}],
    }
    document.update(changes)
    return document


def _problems(document: dict, root: Path) -> list[str]:
    return lr.report_problems(document, required=_required(), root=root)


def test_a12_a_complete_report_is_structurally_sound(tmp_path: Path) -> None:
    document = _report(tmp_path)

    assert _problems(document, tmp_path) == []
    path = lr.write_report(tmp_path, suite=ls.SUITE_CANDIDATE, site_sha256=DIGEST, code_sha=CODE_SHA,
                           policy_source_sha256=DIGEST, fixture_set_sha256=DIGEST,
                           candidate_report_sha256=None, started_at_utc=START, ended_at_utc=END,
                           attempts=document["attempts"], final=document["final"])

    loaded = lr.load_report(tmp_path)
    assert path.name == lr.REPORT_FILE and loaded["suite"] == ls.SUITE_CANDIDATE
    assert loaded["artifacts"] == document["artifacts"]
    assert [row["attempt"] for row in loaded["attempts"]] == [1, 1, 2]  # the failed attempt is kept

    with pytest.raises(lr.ReportError, match="exists"):
        lr.write_report(tmp_path, suite=ls.SUITE_CANDIDATE, site_sha256=DIGEST, code_sha=CODE_SHA,
                        policy_source_sha256=DIGEST, fixture_set_sha256=DIGEST,
                        candidate_report_sha256=None, started_at_utc=START, ended_at_utc=END,
                        attempts=[], final=[])


def test_a12_a_stored_verdict_or_a_forbidden_field_is_refused(tmp_path: Path) -> None:
    document = _report(tmp_path)
    document["summary"] = {"passed": 3}
    assert any("summary" in problem for problem in _problems(document, tmp_path))

    for forbidden in ("production_ready", "device_backend_ready"):
        document = _report(tmp_path)
        document[forbidden] = True
        assert any(forbidden in problem for problem in _problems(document, tmp_path))

    document = _report(tmp_path)
    document["extra"] = 1
    assert any("unknown" in problem for problem in _problems(document, tmp_path))

    document = _report(tmp_path)
    document["attempts"][0]["status"] = "probably"
    assert any("status" in problem for problem in _problems(document, tmp_path))

    document = _report(tmp_path)
    document["started_at_utc"] = "yesterday"
    assert any("started_at_utc" in problem for problem in _problems(document, tmp_path))


def test_a12_final_must_cite_one_existing_attempt_per_required_case(tmp_path: Path) -> None:
    document = _report(tmp_path)
    document["final"] = document["final"][:1]
    assert any("cold-count" in problem for problem in _problems(document, tmp_path))

    document = _report(tmp_path)
    document["final"][1]["attempt"] = 7
    assert any("attempt" in problem for problem in _problems(document, tmp_path))

    document = _report(tmp_path)
    document["final"].append(dict(document["final"][0]))
    assert any("more than once" in problem for problem in _problems(document, tmp_path))

    # Every attempt number of one case/variant is unique: renumbering is not a legal edit.
    document = _report(tmp_path)
    document["attempts"][2]["attempt"] = 1
    assert any("duplicate" in problem for problem in _problems(document, tmp_path))


def test_a12_artifacts_must_exist_with_the_recorded_bytes(tmp_path: Path) -> None:
    document = _report(tmp_path)
    assert _problems(document, tmp_path) == []

    (tmp_path / "cases" / "legacy" / "request.json").write_bytes(b'{"model":"other"}')
    assert any("request.json" in problem for problem in _problems(document, tmp_path))

    document = _report(tmp_path, artifacts=[{"path": "../escape.json", "bytes": 1, "sha256": DIGEST}])
    assert any("escape" in problem or "relative" in problem for problem in _problems(document, tmp_path))

    document = _report(tmp_path)
    (tmp_path / "cases" / "legacy" / "missing.json").unlink(missing_ok=True)
    document["artifacts"].append({"path": "cases/legacy/missing.json", "bytes": 1, "sha256": DIGEST})
    assert any("missing.json" in problem for problem in _problems(document, tmp_path))

    document = _report(tmp_path)
    document["attempts"][0]["request_files"] = ["cases/legacy/no-such-request.json"]
    assert any("no-such-request.json" in problem for problem in _problems(document, tmp_path))


def test_a12_a_symlinked_material_file_is_never_an_artifact(tmp_path: Path) -> None:
    (tmp_path / "cases").mkdir()
    (tmp_path / "cases" / "request.json").write_bytes(b"{}")
    (tmp_path / "cases" / "link.json").symlink_to(tmp_path / "cases" / "request.json")

    with pytest.raises(lr.ReportError, match="symlink"):
        lr.collect_artifacts(tmp_path)
