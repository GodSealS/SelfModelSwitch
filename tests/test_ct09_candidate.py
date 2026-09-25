"""CT09: the frozen lab candidate — the record, its inputs and the render behind them.

A freeze is only as good as the material it names, so this module recomputes what it
can from the frozen bytes: the policy-source digest from the registered policies, the
fixture-set digest from the frozen scenarios *and* from the shipped fixture documents,
the deployment render from the committed candidate configuration, and the site input
against the record, the render and its own digest. Nothing here reads a stored verdict
or a measurement that was never taken.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from model_scheduler import chat_counting
from model_scheduler.acceptance import chat_compat as cc
from model_scheduler.acceptance import chat_freeze as cf
from model_scheduler.contracts_v2 import CHAT_FEATURES_PROFILE, Envelope

CANDIDATE = Path(__file__).resolve().parents[1] / "plan" / "tool-calling-and-reasoning" / "ct09-candidate"
DEPLOYMENT_ID = "sms-orin-lab2"
CANDIDATE_RUNTIME = "llama-cpp-chat-features-4bc272f"
LEGACY_RUNTIME = "llama-cpp-1"
LEGACY_PROFILE = "llama-cpp-gguf-v1"


def _record() -> dict:
    return json.loads((CANDIDATE / "freeze.json").read_text(encoding="utf-8"))


def _envelope(cap: int) -> Envelope:
    """Only the output budget shapes a compat fixture; the rest is registration detail."""
    return Envelope(ctx_size=8192, max_input_tokens=4096, max_output_tokens=cap, max_parallel=1,
                    max_image_tokens=0, max_image_edge_pixels=0, max_images=0)


def _scenarios(record: dict) -> tuple[cc.CompatScenario, ...]:
    """The fixture set the frozen capabilities must produce, in the frozen order."""
    scenarios: list[cc.CompatScenario] = []
    deadline = float(record["timeouts"]["total_seconds"])
    for model_id, entry in sorted(record["models"].items()):
        declared = set(entry["capabilities"])
        envelope = _envelope(record["budget_caps"][model_id])
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


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_the_frozen_record_is_freezable_and_the_suite_fits_the_request_budget() -> None:
    record = _record()
    parsed = cf.parse_freeze(record)

    assert cf.check_freeze(parsed) == []
    assert cf.minimum_requests(parsed["models"]) <= parsed["request_limit"]
    assert len(cf.freeze_digest(parsed)) == 64  # the record has one stable identity


def test_the_frozen_digests_come_from_the_policy_source_and_the_fixture_set() -> None:
    record = _record()
    scenarios = _scenarios(record)
    shipped = sorted((CANDIDATE / "fixtures").glob("*.json"))

    # The CT09 record is the historical freeze: its fixtures still derive from this code, while its
    # policy source was superseded by the CT10c registration — a run must not reuse it as current.
    assert record["policy_source_sha256"] != chat_counting.policy_source_digest()
    assert record["fixture_set_sha256"] == cc.fixture_set_digest(scenarios)
    assert len(shipped) == len(scenarios)

    # Recompute the set digest from the *shipped documents*, not from the builders:
    # a rewritten fixture cannot hide behind a re-derived one.
    documents = [json.loads(path.read_text(encoding="utf-8")) for path in shipped]
    recomputed = hashlib.sha256(cc.canonical_json_bytes(
        sorted(document["scenario_sha256"] for document in documents))).hexdigest()
    assert recomputed == record["fixture_set_sha256"]
    assert {document["fixture_id"] for document in documents} == {scenario.fixture_id for scenario in scenarios}


def test_the_frozen_candidate_configuration_renders_the_frozen_manifest(tmp_path: Path) -> None:
    from model_scheduler.deploy import render_lab

    render = render_lab(CANDIDATE / "scheduler-v2.json", tmp_path / "render", deployment_id=DEPLOYMENT_ID,
                        container_runtime="nvidia")

    assert (tmp_path / "render" / "manifest.json").read_bytes() == (CANDIDATE / "manifest.json").read_bytes()
    assert (tmp_path / "render" / "llama-swap.yaml").read_bytes() == (CANDIDATE / "llama-swap.yaml").read_bytes()
    assert (tmp_path / "render" / "scheduler-v2.json").read_bytes() == \
        (CANDIDATE / "scheduler-v2.json").read_bytes()

    twenty_seven_b = render["models"]["qwen36-27b"]
    assert twenty_seven_b["runtime_id"] == CANDIDATE_RUNTIME
    assert twenty_seven_b["profile_id"] == CHAT_FEATURES_PROFILE
    assert twenty_seven_b["argv"][-3:] == ["--jinja", "--reasoning-format", "deepseek"]
    assert twenty_seven_b["argv"].count("--jinja") == 1 and twenty_seven_b["argv"].count("--reasoning-format") == 1

    seven_b = render["models"]["qwen25vl-7b"]
    assert seven_b["runtime_id"] == LEGACY_RUNTIME and seven_b["profile_id"] == LEGACY_PROFILE
    assert "--jinja" not in seven_b["argv"] and "--reasoning-format" not in seven_b["argv"]


def test_the_frozen_site_input_matches_the_record_the_render_and_its_own_digest() -> None:
    record = _record()
    site_path = CANDIDATE / "site-input.json"
    site = json.loads(site_path.read_text(encoding="utf-8"))
    manifest = json.loads((CANDIDATE / "manifest.json").read_text(encoding="utf-8"))

    assert site_path.name == Path(record["lab_input"]["path"]).name
    assert _sha256_file(site_path) == record["lab_input"]["sha256"]
    assert site["expected_sha"] == record["code_sha"]
    assert site["branch"] and site["phase"] == "candidate" and site["mode"] == "lab"
    assert site["deployment_id"] == DEPLOYMENT_ID
    assert site["service_base_url"] == record["service_base_url"]
    assert site["timeouts"] == record["timeouts"]
    assert site["request_limit"] == record["request_limit"]
    assert site["policy_source_sha256"] == record["policy_source_sha256"]
    assert site["fixture_set_sha256"] == record["fixture_set_sha256"]
    assert site["rollback_input"] == record["rollback"]["input_path"]
    assert site["gateway_base_url"] is None and site["rollback_sha"] is None

    assert set(site["models"]) == set(record["models"])
    for model_id, entry in record["models"].items():
        registered = site["models"][model_id]
        assert registered["capabilities"] == entry["capabilities"]
        assert registered["image_digest"] == entry["image_digest"]
        assert registered["model_sha256"] == entry["model_sha256"]
        assert registered["template_sha256"] == entry["template_sha256"]
        assert registered["template_sha256"] == record["template_hashes"][model_id]
        assert registered["envelope"]["max_output_tokens"] == record["budget_caps"][model_id]
        assert registered["envelope"]["max_parallel"] == entry["max_parallel"]
        # The frozen launch is the render of the frozen configuration, not a retyped argv.
        assert registered["launch_argv"] == manifest["models"][model_id]["argv"]
        assert registered["runtime_id"] == manifest["models"][model_id]["runtime_id"]
        assert registered["profile_id"] == manifest["models"][model_id]["profile_id"]

    floating = {**record, "models": {**record["models"],
                                     "qwen36-27b": {**record["models"]["qwen36-27b"],
                                                    "image_digest": "sms-llama-cpp:latest"}}}
    problems = cf.check_freeze(cf.parse_freeze(floating))
    assert any("image_digest" in problem and "digest" in problem for problem in problems)
