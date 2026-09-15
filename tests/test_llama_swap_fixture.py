from __future__ import annotations

import json
from pathlib import Path


def test_v217_candidate_fixture_is_explicitly_incomplete_until_a_real_llama_cpp_load_is_captured() -> None:
    fixture = Path(__file__).resolve().parent / "fixtures" / "llama_swap_contract.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))

    assert data["schema_version"] == 1
    assert data["acceptance_status"] == "incomplete_no_llama_cpp_load_fixture"
    assert data["release"] == {
        "version": "v217",
        "asset": "llama-swap_217_linux_arm64.tar.gz",
        "sha256": "36c58cf69f1422e999acba0b7bff0d47d5b955cb95e8d3875c461d814a74cc29",
    }
    assert data["running"] == {
        "status": 200,
        "content_type": "application/json",
        "body": {"running": []},
    }
    assert data["unload"] == {
        "status": 200,
        "content_type": "text/plain",
        "body_utf8": "OK",
    }
    assert data["load_probe"] == {
        "path": "/props?model=fixture",
        "status": 404,
    }
