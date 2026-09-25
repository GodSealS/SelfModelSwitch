"""CT09: the frozen lab candidate and the checks that refuse a fake one.

A freeze is only worth its name when the values it pins are real: a floating
image tag, a placeholder hash, an undefined effort set or a request budget too
small for the required suite all stay visible here instead of being discovered
on the device. Nothing in this module invents a measurement: it checks the
record the previous tasks left behind.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

from model_scheduler.acceptance import chat_freeze as cf

CODE_SHA = "0" * 39 + "a"
MODEL_SHA = "3f4513330aa7f109922bd701d773575484ae2b4a4090d6511260a2a4f8e3d069"
TEMPLATE_SHA = "a0bc6f6fc7a29a80017a433e8f03a1cc1236e838a944a2d034295a60c4f2fddb"
OTHER_MODEL_SHA = "f1e1b337fda4ec8e39974f69a385f970381488e9bb41d039d85959aeb5350457"
OTHER_TEMPLATE_SHA = "e84f32a23fdda27689f868aa4a1a5621f41133e51a48d7f3efcbea2839574259"
IMAGE = "sms-llama-cpp@sha256:" + "8" * 64
FROZEN_AT = "2026-09-25T00:00:00+00:00"


def _record(**changes) -> dict:
    document = {
        "schema_version": 1,
        "code_sha": CODE_SHA,
        "lab_input": {"path": "/home/jtzn/self-model-switch-evidence/ct01/site/site-input.json",
                      "sha256": "6b1baf519eb676f7665c9582967270fb09fbd8b54a79c745bc9dae78932598c4"},
        "policy_source_sha256": "a" * 64,
        "fixture_set_sha256": "b" * 64,
        "template_hashes": {"qwen25vl-7b": TEMPLATE_SHA, "qwen36-27b": OTHER_TEMPLATE_SHA},
        "service_base_url": "http://127.0.0.1:8090",
        "timeouts": {"connect_seconds": 5, "read_idle_seconds": 60, "total_seconds": 900},
        "request_limit": 64,
        "budget_caps": {"qwen25vl-7b": 4096, "qwen36-27b": 1024},
        "models": {
            "qwen25vl-7b": {"capabilities": ["chat", "vision"], "image_digest": IMAGE,
                            "model_sha256": MODEL_SHA, "template_sha256": TEMPLATE_SHA,
                            "max_parallel": 2, "effort_values": []},
            "qwen36-27b": {"capabilities": ["chat", "tools", "thinking"], "image_digest": IMAGE,
                           "model_sha256": OTHER_MODEL_SHA, "template_sha256": OTHER_TEMPLATE_SHA,
                           "max_parallel": 2, "effort_values": []},
        },
        "rollback": {"input_path": "/home/jtzn/self-model-switch-evidence/ct00/deploy2/manifest.json",
                     "input_sha256": "c" * 64, "code_sha": None},
        "exceptions": ["the 27B chat-features policy entry is still unregistered (CT10 measures it)"],
        "frozen_at_utc": FROZEN_AT,
    }
    document.update(changes)
    return document


def test_the_freeze_record_has_a_closed_set_and_real_types() -> None:
    parsed = cf.parse_freeze(_record())

    assert parsed["code_sha"] == CODE_SHA and parsed["request_limit"] == 64
    with pytest.raises(cf.FreezeError, match="unknown fields"):
        cf.parse_freeze({**_record(), "extra": 1})
    with pytest.raises(cf.FreezeError, match="missing"):
        cf.parse_freeze({key: value for key, value in _record().items() if key != "timeouts"})
    with pytest.raises(cf.FreezeError, match="code_sha"):
        cf.parse_freeze(_record(code_sha="not-a-sha"))
    with pytest.raises(cf.FreezeError, match="service_base_url"):
        cf.parse_freeze(_record(service_base_url="http://127.0.0.1:8090/v1"))
    with pytest.raises(cf.FreezeError, match="service_base_url"):
        cf.parse_freeze(_record(service_base_url="http://user:pw@127.0.0.1:8090"))
    with pytest.raises(cf.FreezeError, match="total_seconds"):
        cf.parse_freeze(_record(timeouts={"connect_seconds": 5, "read_idle_seconds": 60, "total_seconds": 0}))


def test_a_frozen_candidate_without_placeholders_or_floating_images_passes() -> None:
    assert cf.check_freeze(cf.parse_freeze(_record())) == []


def test_a_floating_image_tag_is_refused() -> None:
    document = _record()
    document["models"]["qwen36-27b"]["image_digest"] = "sms-llama-cpp:latest"

    problems = cf.check_freeze(cf.parse_freeze(document))
    assert any("image_digest" in problem and "digest" in problem for problem in problems)


def test_placeholder_and_zero_hashes_are_refused() -> None:
    document = _record()
    document["models"]["qwen36-27b"]["template_sha256"] = "REQUIRED_64_HEX"
    assert any("placeholder" in problem for problem in cf.check_freeze(cf.parse_freeze(document)))

    document = _record()
    document["models"]["qwen25vl-7b"]["model_sha256"] = "0" * 64
    assert any("model_sha256" in problem for problem in cf.check_freeze(cf.parse_freeze(document)))


def test_the_template_map_must_agree_with_every_frozen_model() -> None:
    document = _record()
    document["template_hashes"]["qwen25vl-7b"] = "0" * 64  # an all-zero template is a placeholder
    assert any("template_hashes" in problem for problem in cf.check_freeze(cf.parse_freeze(document)))

    document = _record()
    document["template_hashes"]["qwen25vl-7b"] = "b" * 64  # a template the model entry does not carry
    assert any("template_hashes" in problem and "qwen25vl-7b" in problem
               for problem in cf.check_freeze(cf.parse_freeze(document)))

    document = _record()
    document["template_hashes"].pop("qwen36-27b")
    assert any("template_hashes" in problem and "qwen36-27b" in problem
               for problem in cf.check_freeze(cf.parse_freeze(document)))


def test_the_budget_caps_must_cover_every_frozen_model() -> None:
    document = _record()
    document["budget_caps"].pop("qwen36-27b")
    assert any("budget_caps" in problem and "qwen36-27b" in problem
               for problem in cf.check_freeze(cf.parse_freeze(document)))

    document = _record()
    document["budget_caps"]["qwen36-27b"] = 0
    with pytest.raises(cf.FreezeError, match="budget_caps"):
        cf.parse_freeze(document)


def test_an_undefined_value_is_refused() -> None:
    document = _record()
    document["models"]["qwen36-27b"]["effort_values"] = None

    assert any("effort_values" in problem for problem in cf.check_freeze(cf.parse_freeze(document)))


def test_a_request_limit_below_the_required_suite_is_refused() -> None:
    enough = cf.minimum_requests(_record()["models"])
    document = _record()

    assert cf.check_freeze(cf.parse_freeze(document)) == []
    assert cf.check_freeze(cf.parse_freeze(_record(request_limit=enough))) == []
    assert any("request_limit" in problem for problem in
               cf.check_freeze(cf.parse_freeze(_record(request_limit=enough - 1))))


def test_the_required_suite_grows_with_every_declared_capability() -> None:
    from model_scheduler.acceptance.chat_compat import ROUNDS, VARIANTS

    tools_only = {"qwen36-27b": {"capabilities": ["chat", "tools"], "max_parallel": 2, "effort_values": []}}
    both = {"qwen36-27b": {"capabilities": ["chat", "tools", "thinking"], "max_parallel": 2, "effort_values": []}}
    legacy = {"qwen25vl-7b": {"capabilities": ["chat", "vision"], "max_parallel": 3, "effort_values": []}}

    compat = len(VARIANTS) * ROUNDS  # one two-round case per variant and capability

    assert cf.minimum_requests(tools_only) == compat + cf.LEGACY_VARIANTS + 2  # 27B still owes its legacy regression
    assert cf.minimum_requests(both) == 3 * compat + cf.LEGACY_VARIANTS + 2  # tools, thinking, combination
    assert cf.minimum_requests(legacy) == cf.LEGACY_VARIANTS + 3  # four legacy variants plus one budget batch
    assert cf.minimum_requests({**both, **legacy}) == 3 * compat + cf.LEGACY_VARIANTS + 2 + cf.LEGACY_VARIANTS + 3


def test_the_policy_source_digest_is_reproducible_and_sensitive() -> None:
    from model_scheduler import chat_counting

    first = chat_counting.policy_source_digest()
    assert first == chat_counting.policy_source_digest()
    assert len(first) == 64 and all(character in "0123456789abcdef" for character in first)

    registry = {key: value for key, value in chat_counting.POLICY_REGISTRY.items()}
    assert len(registry) >= 2
    # A different registry is a different source: the digest is not cosmetic.
    assert first != chat_counting.policy_source_digest(registry={})


def test_the_freeze_carries_the_utc_stamp_it_was_frozen_at() -> None:
    parsed = cf.parse_freeze(_record())

    assert datetime.fromisoformat(parsed["frozen_at_utc"]).tzinfo is not None
    with pytest.raises(cf.FreezeError, match="frozen_at_utc"):
        cf.parse_freeze(_record(frozen_at_utc="yesterday"))
    copied = copy.deepcopy(parsed)
    assert copied == parsed  # the record is plain data, never live state
