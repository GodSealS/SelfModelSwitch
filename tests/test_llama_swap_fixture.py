from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from model_scheduler import llama_swap_contract as contract
from model_scheduler.llama_swap_client import LlamaSwapResponse

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "llama_swap_contract.json"


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_pinned_release_and_binary_are_recorded_with_their_measured_hashes() -> None:
    data = _fixture()

    assert data["schema_version"] == 2
    assert data["acceptance_status"] == "measured_on_target"
    assert data["release"] == {
        "version": "v217",
        "asset": "llama-swap_217_linux_arm64.tar.gz",
        "sha256": "36c58cf69f1422e999acba0b7bff0d47d5b955cb95e8d3875c461d814a74cc29",
    }
    assert data["binary"]["sha256"] == "0f86f5869d167b406c9e81bdc4b268f23dd96a4983ea57b8f2b561ac8bb82e80"
    assert data["binary"]["listen_address_source"].startswith("command-line --listen")
    assert data["captured"]["evidence_directory"].endswith("llama-swap-control-20260918T002718Z")
    assert data["captured"]["stop"] == {"exit_code": 0, "port_in_use": False}


def test_every_recorded_response_body_matches_its_recorded_hash() -> None:
    data = _fixture()
    control = data["control"]

    for name in ("health", "running_empty", "load_trigger", "unload", "running_empty_after_unload"):
        entry = control[name]
        body = entry["body_utf8"].encode("utf-8")
        assert hashlib.sha256(body).hexdigest() == entry["body_sha256"], name
        assert entry["status"] == 200


def test_the_measured_load_path_is_the_upstream_proxy_and_the_old_probe_stays_a_404() -> None:
    data = _fixture()

    assert data["control"]["load_trigger"]["request"] == {"method": "GET", "path": "/upstream/{model_id}/health"}
    assert data["control"]["load_trigger"]["body_utf8"] == '{"status":"ok"}'
    assert data["control"]["load_trigger"]["content_type"] == "application/json; charset=utf-8"
    assert data["control"]["unload"]["request"] == {"method": "POST", "path": "/api/models/unload/{model_id}"}
    assert data["control"]["unload"]["body_utf8"] == "OK"

    rejected = data["negative_samples"][0]
    assert rejected["name"] == "rejected_load_probe"
    assert rejected["request"]["path"] == "/props?model={model_id}"
    assert rejected["status"] == 404
    assert rejected["counts_as_success"] is False


def test_the_measured_running_shape_lists_upstream_objects() -> None:
    data = _fixture()
    loaded = data["control"]["running_loaded"]

    assert set(loaded["entry_keys"]) == {"cmd", "description", "model", "name", "proxy", "state", "ttl"}
    assert loaded["entry_example"]["model"] == "qwen25vl-7b-q4"
    assert loaded["entry_example"]["state"] == "ready"


def test_contract_is_derived_from_the_fixture_and_validates_the_measured_bodies() -> None:
    assert contract.CONTROL_CONTRACT.load_request("qwen25vl-7b-q4").path == "/upstream/qwen25vl-7b-q4/health"
    assert contract.CONTROL_CONTRACT.unload_path == "/api/models/unload/{model_id}"

    measured = contract.load_fixture()
    load_body = measured.load_trigger["body_utf8"].encode("utf-8")
    contract.CONTROL_CONTRACT.validate_load_response(
        LlamaSwapResponse(200, "application/json; charset=utf-8", load_body)
    )
    contract.CONTROL_CONTRACT.validate_unload_response(LlamaSwapResponse(200, "text/plain; charset=utf-8", b"OK"))

    with pytest.raises(Exception):
        contract.CONTROL_CONTRACT.validate_load_response(
            LlamaSwapResponse(404, None, measured.negative_samples[0]["body_utf8"].encode("utf-8"))
        )
    with pytest.raises(Exception):
        contract.CONTROL_CONTRACT.validate_unload_response(LlamaSwapResponse(500, "text/plain", b"error"))
    with pytest.raises(contract.FixtureError):
        contract.CONTROL_CONTRACT.running_parser({"running": ["qwen25vl-7b-q4"]})


def test_running_parser_accepts_the_measured_object_shape() -> None:
    parsed = contract.running_parser(
        {
            "running": [
                {
                    "model": "qwen25vl-7b-q4",
                    "name": "",
                    "proxy": "http://localhost:5800",
                    "state": "ready",
                    "ttl": 0,
                    "cmd": "docker run ...",
                    "description": "",
                }
            ]
        }
    )

    assert parsed == ["qwen25vl-7b-q4"]
    assert contract.running_parser({"running": []}) == []
    with pytest.raises(contract.FixtureError):
        contract.running_parser({"running": "qwen25vl-7b-q4"})


def test_a_fixture_without_the_measured_shape_is_refused(tmp_path: Path) -> None:
    data = _fixture()
    data["control"]["running_loaded"]["entry_keys"] = ["model", "state"]
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(contract.FixtureError, match="entry shape"):
        contract.load_fixture(broken)

    missing = tmp_path / "missing.json"
    with pytest.raises(contract.FixtureError, match="cannot read"):
        contract.load_fixture(missing)
