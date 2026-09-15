from __future__ import annotations

import json

from model_scheduler.contracts import Presence
from model_scheduler.process_observer import ProcessObserver


def test_running_requires_matching_identity_labels_and_direct_health() -> None:
    payload = [{"Id": "abc", "State": {"Running": True, "StartedAt": "2026-01-01T00:00:00Z"}, "Config": {"Image": "image@sha256:abc", "Labels": {"io.self-model-switch.deployment": "thor-local", "io.self-model-switch.model": "qwen-small", "io.self-model-switch.config-sha256": "cfg"}}}]
    observer = ProcessObserver("thor-local", "image@sha256:abc", "cfg", inspect=lambda _: json.dumps(payload), port_open=lambda _: True, health=lambda _: True)

    result = observer.observe("qwen-small", "sms-thor-local-qwen-small", 10003)

    assert result.presence is Presence.RUNNING
    assert result.instance_id == "abc:2026-01-01T00:00:00Z"


def test_stopped_needs_both_no_container_and_closed_port() -> None:
    observer = ProcessObserver("thor-local", "image", "cfg", inspect=lambda _: "[]", port_open=lambda _: True, health=lambda _: False)

    result = observer.observe("qwen-small", "sms-thor-local-qwen-small", 10003)

    assert result.presence is Presence.UNKNOWN
