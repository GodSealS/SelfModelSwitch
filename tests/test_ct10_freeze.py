"""CT10: the freeze the candidate suite runs against (the CT09 record stays historical).

The CT09 record predates the CT10c registration, so it is kept as the historical freeze it
is. This one is the *current* identity: its policy digest must equal the code's, its
fixtures must still derive from this code, and its site input must agree with the record,
the render and its own digest.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from model_scheduler import chat_counting
from model_scheduler.acceptance import chat_compat as cc
from model_scheduler.acceptance import chat_freeze as cf
from model_scheduler.contracts_v2 import CHAT_FEATURES_PROFILE, Envelope

FROZEN = Path(__file__).resolve().parents[1] / "plan" / "tool-calling-and-reasoning" / "ct10-freeze"
CANDIDATE = FROZEN.parent / "ct09-candidate"
DEPLOYMENT_ID = "sms-orin-lab2"
CANDIDATE_RUNTIME = "llama-cpp-chat-features-4bc272f"


def _record() -> dict:
    return json.loads((FROZEN / "freeze.json").read_text(encoding="utf-8"))


def _envelope(cap: int, parallel: int) -> Envelope:
    return Envelope(ctx_size=8192, max_input_tokens=4096, max_output_tokens=cap, max_parallel=parallel,
                    max_image_tokens=0, max_image_edge_pixels=0, max_images=0)


def _scenarios(record: dict) -> tuple[cc.CompatScenario, ...]:
    scenarios: list[cc.CompatScenario] = []
    deadline = float(record["timeouts"]["total_seconds"])
    for model_id, entry in sorted(record["models"].items()):
        declared = set(entry["capabilities"])
        envelope = _envelope(record["budget_caps"][model_id], entry["max_parallel"])
        for capability in cc.COMPAT_CAPABILITIES:
            if capability not in declared:
                continue
            builder = cc.tools_scenario if capability == "tools" else cc.thinking_scenario
            for stream in (False, True):
                scenarios.append(builder(model_id, envelope=envelope, stream=stream, deadline_seconds=deadline))
        if set(cc.COMPAT_CAPABILITIES) <= declared:
            for stream in (False, True):
                scenarios.append(cc.tools_thinking_scenario(model_id, envelope=envelope, stream=stream,
                                                            deadline_seconds=deadline))
    return tuple(scenarios)


def test_the_current_freeze_is_freezable_and_its_suite_fits_the_request_budget() -> None:
    parsed = cf.parse_freeze(_record())

    assert cf.check_freeze(parsed) == []
    assert cf.minimum_requests(parsed["models"]) <= parsed["request_limit"]
    assert len(cf.freeze_digest(parsed)) == 64


def test_the_current_freeze_names_the_registered_policy_and_the_derived_fixtures() -> None:
    record = _record()

    # This is the *current* identity: unlike the CT09 record it must carry today's policy source,
    # and the shipped fixtures must still be the ones this code derives.
    assert record["policy_source_sha256"] == chat_counting.policy_source_digest()
    assert record["fixture_set_sha256"] == cc.fixture_set_digest(_scenarios(record))

    shipped = sorted((CANDIDATE / "fixtures").glob("*.json"))
    documents = [json.loads(path.read_text(encoding="utf-8")) for path in shipped]
    recomputed = hashlib.sha256(cc.canonical_json_bytes(
        sorted(document["scenario_sha256"] for document in documents))).hexdigest()
    assert recomputed == record["fixture_set_sha256"]

    # The read idle has to outlast this deployment's own answers: the pre-check measured
    # 4.56 tok/s on the 27B, where a non-streamed round needs minutes, not 60 seconds.
    assert record["timeouts"]["read_idle_seconds"] >= 300


def test_the_current_site_input_matches_the_record_the_render_and_its_own_digest() -> None:
    record, site_path = _record(), FROZEN / "site-input.json"
    site = json.loads(site_path.read_text(encoding="utf-8"))
    manifest = json.loads((CANDIDATE / "manifest.json").read_text(encoding="utf-8"))

    assert site_path.name == Path(record["lab_input"]["path"]).name
    assert hashlib.sha256(site_path.read_bytes()).hexdigest() == record["lab_input"]["sha256"]
    assert site["expected_sha"] == record["code_sha"] and site["phase"] == "candidate"
    assert site["service_base_url"] == record["service_base_url"]
    assert site["timeouts"] == record["timeouts"] and site["request_limit"] == record["request_limit"]
    assert site["policy_source_sha256"] == record["policy_source_sha256"]
    assert site["fixture_set_sha256"] == record["fixture_set_sha256"]
    assert site["deployment_id"] == DEPLOYMENT_ID

    assert set(site["models"]) == set(record["models"])
    for model_id, entry in record["models"].items():
        registered = site["models"][model_id]
        assert registered["capabilities"] == entry["capabilities"]
        assert registered["launch_argv"] == manifest["models"][model_id]["argv"]
        assert registered["envelope"]["max_output_tokens"] == record["budget_caps"][model_id]
        assert registered["template_sha256"] == record["template_hashes"][model_id]
    assert manifest["models"]["qwen36-27b"]["runtime_id"] == CANDIDATE_RUNTIME
