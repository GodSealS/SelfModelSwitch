from __future__ import annotations

import json

from model_scheduler.recovery_helper import RecoveryHelper


def test_recovery_only_stops_manifest_named_matching_containers(tmp_path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"deployment_id": "thor-local", "models": {"embedding": {"container_name": "sms-thor-local-embedding"}}}))
    calls: list[list[str]] = []
    helper = RecoveryHelper(manifest, runner=lambda argv: calls.append(argv) or "")
    result = helper.recover()
    assert result["ok"] is True
    assert ["docker", "stop", "--time", "30", "sms-thor-local-embedding"] in calls
    assert all("ps" not in command for command in calls)
