"""The instance probe: observation only, and an absent instance stays absent."""

from __future__ import annotations

import json

from model_scheduler.acceptance.instances import ManagedInstanceProbe


def _runner(responses: list[str]):
    calls: list[list[str]] = []

    def run(argv):
        calls.append(list(argv))
        return responses.pop(0) if responses else ""

    run.calls = calls  # type: ignore[attr-defined]
    return run


CONTAINER = "4fa3253c59348a4caa610bb6082ad8f0aea554fe65cbc92e2b1e4e6708f88dc8"


def test_it_reports_the_container_the_deployment_labelled() -> None:
    document = {"Id": CONTAINER, "Config": {"Image": "sms-llama-cpp@sha256:" + "a" * 64,
                                            "Labels": {"io.self-model-switch.runtime": "llama-cpp-1"}},
                "State": {"StartedAt": "2026-09-19T18:47:00Z"}}
    runner = _runner([CONTAINER + "\n", json.dumps(document)])
    probe = ManagedInstanceProbe("sms-orin-lab", runner=runner)

    identity = probe.of("qwen-small")

    assert identity["container_id"] == CONTAINER and identity["runtime_id"] == "llama-cpp-1"
    assert identity["model_id"] == "qwen-small" and identity["deployment_id"] == "sms-orin-lab"
    listing, inspecting = runner.calls  # type: ignore[attr-defined]
    assert listing[:3] == ["docker", "ps", "--no-trunc"]
    assert "label=io.self-model-switch.deployment=sms-orin-lab" in listing
    assert "label=io.self-model-switch.model=qwen-small" in listing
    assert inspecting[1] == "inspect"  # read-only: neither start, stop nor config


def test_nothing_serving_the_model_is_an_absence_not_a_borrowed_instance() -> None:
    probe = ManagedInstanceProbe("sms-orin-lab", runner=_runner(["\n"]))

    assert probe.of("qwen-small") is None


def test_an_unreadable_container_or_docker_is_absent_rather_than_guessed() -> None:
    assert ManagedInstanceProbe("sms-orin-lab", runner=_runner([CONTAINER, "not json"])).of("m") is None

    def unreachable(argv):
        raise OSError("docker is not reachable")

    assert ManagedInstanceProbe("sms-orin-lab", runner=unreachable).of("m") is None


def test_the_probe_refuses_to_observe_without_a_deployment_identity() -> None:
    import pytest

    with pytest.raises(ValueError, match="deployment identity"):
        ManagedInstanceProbe("")
