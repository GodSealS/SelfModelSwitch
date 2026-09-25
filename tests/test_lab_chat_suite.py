"""CT10: the candidate suite's own case list, its registration gate and its budget (A08/A10).

The suite is derived from the registration the site input declares and from the frozen
fixtures, never typed case by case: a missing capability, a chat feature on the wrong
model, a fixture set that is not the frozen one or a budget too small for the derived
list must be visible before a single request is sent.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from model_scheduler import chat_counting
from model_scheduler.acceptance import chat_compat as cc
from model_scheduler.acceptance import chat_freeze as cf
from model_scheduler.acceptance import lab_suite as ls
from model_scheduler.contracts_v2 import Envelope

MODELS = {"qwen25vl-7b": ("chat", "vision"), "qwen36-27b": ("chat", "vision", "tools", "thinking")}
ENVELOPES = {
    "qwen25vl-7b": Envelope(ctx_size=32768, max_input_tokens=8192, max_output_tokens=4096, max_parallel=2,
                            max_image_tokens=1280, max_image_edge_pixels=1024, max_images=1),
    "qwen36-27b": Envelope(ctx_size=8192, max_input_tokens=4096, max_output_tokens=1024, max_parallel=1,
                           max_image_tokens=1280, max_image_edge_pixels=1024, max_images=1),
}
DEADLINE = 900.0


def _suite(models=MODELS) -> ls.LabSuite:
    return ls.candidate_suite(models=models, envelope_of=ENVELOPES, deadline_seconds=DEADLINE)


def _minimum(models=MODELS) -> int:
    return cf.minimum_requests({model_id: {"capabilities": list(capabilities),
                                           "max_parallel": ENVELOPES[model_id].max_parallel}
                                for model_id, capabilities in models.items()})


def test_a10_the_candidate_suite_is_derived_from_the_registration() -> None:
    suite = _suite()

    assert sorted({case.case_id for case in suite.cases}) == [
        "B:qwen36-27b:cap:thinking", "B:qwen36-27b:cap:tools", "L:qwen25vl-7b:legacy",
        "L:qwen36-27b:legacy", "L:qwen36-27b:tools-thinking",
    ]
    for capability in cc.COMPAT_CAPABILITIES:
        variants = {case.variant for case in suite.cases if case.case_id == f"B:qwen36-27b:cap:{capability}"}
        assert variants == set(cc.VARIANTS)
    assert {case.variant for case in suite.cases if case.case_id == "L:qwen36-27b:tools-thinking"} == set(cc.VARIANTS)
    for model_id in MODELS:
        assert {case.variant for case in suite.cases if case.case_id == f"L:{model_id}:legacy"} == set(ls.LEGACY_VARIANTS)

    # A id/variant pair is unique, and the compat cases really carry the frozen scenarios.
    assert len(suite.cases) == 3 * len(cc.VARIANTS) + 2 * len(ls.LEGACY_VARIANTS)
    assert len({(case.case_id, case.variant) for case in suite.cases}) == len(suite.cases)
    compat = [case for case in suite.cases if case.kind == "compat"]
    assert all(case.scenario is not None and case.scenario.model_id == case.model_id for case in compat)
    assert len(suite.scenarios()) == 6  # two streams for tools, thinking and the combination
    assert all(case.kind == "legacy" and case.scenario is None for case in suite.cases if case.kind == "legacy")


def test_a10_the_suite_request_budget_matches_the_frozen_minimum() -> None:
    suite = _suite()

    assert suite.requests() == _minimum()

    # The boundary variant is the registered parallelism, not a guess.
    boundary = [case for case in suite.cases if case.variant == "budget-boundary"]
    assert sorted(case.requests() for case in boundary) == [1, 2]
    assert all(case.requests() == cc.ROUNDS for case in suite.cases if case.kind == "compat")
    assert not ls.budget_problems(suite=suite, request_limit=suite.requests())
    assert ls.budget_problems(suite=suite, request_limit=suite.requests() - 1)


def test_a10_the_fixture_set_and_policy_source_must_be_the_frozen_ones() -> None:
    suite = _suite()

    assert ls.identity_problems(suite=suite, policy_source_sha256=chat_counting.policy_source_digest(),
                                fixture_set_sha256=cc.fixture_set_digest(suite.scenarios())) == []
    assert ls.identity_problems(suite=suite, policy_source_sha256="a" * 64,
                                fixture_set_sha256=cc.fixture_set_digest(suite.scenarios()))
    assert ls.identity_problems(suite=suite, policy_source_sha256=chat_counting.policy_source_digest(),
                                fixture_set_sha256="b" * 64)


@pytest.mark.parametrize("candidate", ["ct09-candidate", "ct10-candidate"])
def test_a10_the_frozen_candidate_derives_exactly_the_suite_the_record_froze(candidate: str) -> None:
    directory = Path(__file__).resolve().parents[1] / "plan/tool-calling-and-reasoning" / candidate
    site = json.loads((directory / "site-input.json").read_text(encoding="utf-8"))
    record = json.loads((directory / "freeze.json").read_text(encoding="utf-8"))
    models = {model_id: entry["capabilities"] for model_id, entry in site["models"].items()}
    envelopes = {model_id: Envelope(**entry["envelope"]) for model_id, entry in site["models"].items()}

    suite = ls.candidate_suite(models=models, envelope_of=envelopes,
                               deadline_seconds=site["timeouts"]["total_seconds"])

    assert ls.candidate_registration_problems(models) == []
    assert ls.identity_problems(suite=suite, policy_source_sha256=site["policy_source_sha256"],
                                fixture_set_sha256=site["fixture_set_sha256"]) == []
    assert ls.budget_problems(suite=suite, request_limit=site["request_limit"]) == []
    assert cc.fixture_set_digest(suite.scenarios()) == record["fixture_set_sha256"]
    assert suite.requests() == cf.minimum_requests(record["models"]) <= site["request_limit"]
    assert cf.check_freeze(cf.parse_freeze(record)) == []
    # The record names the site it froze, and the site names the commit under test.
    assert record["lab_input"]["sha256"] == hashlib.sha256((directory / "site-input.json").read_bytes()).hexdigest()
    assert record["code_sha"] == site["expected_sha"]


def test_a10_the_ct10_refreeze_moves_only_the_code_sha() -> None:
    base = Path(__file__).resolve().parents[1] / "plan/tool-calling-and-reasoning"
    ct09 = json.loads((base / "ct09-candidate/freeze.json").read_text(encoding="utf-8"))
    ct10 = json.loads((base / "ct10-candidate/freeze.json").read_text(encoding="utf-8"))

    assert ct10["code_sha"] != ct09["code_sha"]
    unchanged = ("schema_version", "policy_source_sha256", "fixture_set_sha256", "template_hashes",
                 "service_base_url", "timeouts", "request_limit", "budget_caps", "models", "rollback")
    assert {key: ct10[key] for key in unchanged} == {key: ct09[key] for key in unchanged}
    assert ct10["exceptions"] and all(isinstance(item, str) and item for item in ct10["exceptions"])
    assert ct10["lab_input"] != ct09["lab_input"]  # a re-freeze names its own staged input


def test_a10_the_first_release_registration_is_required() -> None:
    assert ls.candidate_registration_problems(MODELS) == []

    only_legacy = {"qwen25vl-7b": ("chat", "vision")}
    assert any("chat features" in problem for problem in ls.candidate_registration_problems(only_legacy))

    one_capability = {"qwen25vl-7b": ("chat", "vision"), "qwen36-27b": ("chat", "tools")}
    assert any("tools and thinking" in problem for problem in ls.candidate_registration_problems(one_capability))

    wrong_model = {"qwen25vl-7b": ("chat", "vision", "tools", "thinking"),
                   "qwen36-27b": ("chat", "vision", "tools", "thinking")}
    assert any("qwen25vl-7b" in problem for problem in ls.candidate_registration_problems(wrong_model))

    suite = _suite(only_legacy)
    assert suite.case_ids() == ("L:qwen25vl-7b:legacy",)
    assert {case.variant for case in suite.cases} == set(ls.LEGACY_VARIANTS)
    assert not any(case.kind == "compat" for case in suite.cases)
    with pytest.raises(ls.SuiteError, match="variant"):
        ls.LabCase(case_id="L:qwen25vl-7b:legacy", variant="chat-hot", model_id="qwen25vl-7b", kind="legacy")
    with pytest.raises(ls.SuiteError, match="case id"):
        ls.LabCase(case_id="qwen25vl-7b:legacy", variant="chat-json", model_id="qwen25vl-7b", kind="legacy")
