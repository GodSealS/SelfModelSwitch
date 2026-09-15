from __future__ import annotations

import json
from unittest.mock import patch

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


def test_default_health_probe_accepts_only_loopback_http_200() -> None:
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    with patch("model_scheduler.process_observer.urlopen", return_value=Response()) as request:
        assert ProcessObserver._health_endpoint(10003) is True
    assert request.call_args.args[0].full_url == "http://127.0.0.1:10003/health"
