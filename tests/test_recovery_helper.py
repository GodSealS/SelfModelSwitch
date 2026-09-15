from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from model_scheduler.recovery_helper import RecoveryHelper


_CONFIG_SHA256 = "c" * 64


def _models() -> dict[str, dict[str, str]]:
    return {model_id: {"container_name": f"sms-thor-local-{model_id}"} for model_id in RecoveryHelper._PORTS}


def _manifest(models: dict[str, dict[str, str]]) -> dict[str, object]:
    return {"deployment_id": "thor-local", "config_sha256": _CONFIG_SHA256, "models": models}


def test_recovery_rejects_an_incomplete_manifest_before_stopping_control_plane(tmp_path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(_manifest({"embedding": {"container_name": "sms-thor-local-embedding"}})))
    calls: list[list[str]] = []
    helper = RecoveryHelper(manifest, runner=lambda argv: calls.append(argv) or "", inspector=lambda _: None, port_open=lambda _: False, control_stopped=lambda: True)

    assert helper.recover()["ok"] is False
    assert calls == []


def test_recovery_rejects_a_noncanonical_container_name_before_stopping_control_plane(tmp_path) -> None:
    manifest = tmp_path / "manifest.json"
    models = _models()
    models["qwen-small"]["container_name"] = "sms-thor-local-qwen-small-stale"
    manifest.write_text(json.dumps(_manifest(models)))
    calls: list[list[str]] = []
    helper = RecoveryHelper(manifest, runner=lambda argv: calls.append(argv) or "", inspector=lambda _: None, port_open=lambda _: False, control_stopped=lambda: True)

    assert helper.recover()["ok"] is False
    assert calls == []


def test_recovery_only_stops_manifest_named_matching_containers(tmp_path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(_manifest(_models())))
    calls: list[list[str]] = []
    record = {"Config": {"Labels": {"io.self-model-switch.deployment": "thor-local", "io.self-model-switch.model": "embedding", "io.self-model-switch.config-sha256": _CONFIG_SHA256}}, "State": {"Running": True}}
    stopped = {**record, "State": {"Running": False}}
    inspections = iter((record, stopped))
    helper = RecoveryHelper(manifest, runner=lambda argv: calls.append(argv) or "", inspector=lambda name: next(inspections) if name.endswith("embedding") else None, port_open=lambda _: False, control_stopped=lambda: True)
    result = helper.recover()
    assert result["ok"] is True
    assert ["docker", "stop", "--time", "30", "sms-thor-local-embedding"] in calls
    assert all("ps" not in command for command in calls)


def test_recovery_refuses_same_prefix_container_with_wrong_labels(tmp_path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(_manifest(_models())))
    helper = RecoveryHelper(manifest, runner=lambda _: "", inspector=lambda _: {"Config": {"Labels": {"io.self-model-switch.deployment": "other", "io.self-model-switch.model": "embedding"}}}, port_open=lambda _: False, control_stopped=lambda: True)
    assert helper.recover()["ok"] is False


def test_recovery_refuses_a_container_with_a_stale_config_digest_label(tmp_path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(_manifest(_models())))
    calls: list[list[str]] = []
    record = {"Config": {"Labels": {"io.self-model-switch.deployment": "thor-local", "io.self-model-switch.model": "embedding", "io.self-model-switch.config-sha256": "d" * 64}}, "State": {"Running": True}}
    helper = RecoveryHelper(manifest, runner=lambda argv: calls.append(argv) or "", inspector=lambda _: record, port_open=lambda _: False, control_stopped=lambda: True)

    assert helper.recover()["ok"] is False
    assert not any(command[:2] == ["docker", "stop"] for command in calls)


def test_recovery_refuses_to_claim_stop_while_manifest_port_is_open(tmp_path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(_manifest(_models())))
    helper = RecoveryHelper(manifest, runner=lambda _: "", inspector=lambda _: None, port_open=lambda port: port == 10001, control_stopped=lambda: True)
    assert helper.recover()["ok"] is False


def test_recovery_rejects_docker_stop_success_when_container_stays_running(tmp_path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(_manifest(_models())))
    record = {"Config": {"Labels": {"io.self-model-switch.deployment": "thor-local", "io.self-model-switch.model": "embedding", "io.self-model-switch.config-sha256": _CONFIG_SHA256}}, "State": {"Running": True}}
    helper = RecoveryHelper(manifest, runner=lambda _: "", inspector=lambda _: record, port_open=lambda _: False, control_stopped=lambda: True)
    assert helper.recover()["ok"] is False


def test_control_recovery_helper_rejects_all_arguments() -> None:
    script = Path(__file__).resolve().parent.parent / "deploy" / "control-recover.py"
    result = subprocess.run([sys.executable, str(script), "unexpected"], capture_output=True, text=True)
    assert result.returncode == 64


def test_sudoers_rule_allows_only_the_no_argument_recovery_helper() -> None:
    rule = (Path(__file__).resolve().parent.parent / "deploy" / "sudoers.model-scheduler").read_text(encoding="utf-8")
    assert rule == 'model-scheduler ALL=(root) NOPASSWD: /usr/local/libexec/sms-control-recover ""\n'


def test_recovery_refuses_to_clean_containers_until_control_cgroup_is_empty(tmp_path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(_manifest(_models())))
    calls: list[list[str]] = []
    helper = RecoveryHelper(
        manifest,
        runner=lambda argv: calls.append(argv) or "",
        inspector=lambda _: None,
        port_open=lambda _: False,
        control_stopped=lambda: False,
    )

    assert helper.recover()["ok"] is False
    assert calls == [["systemctl", "stop", "llama-swap.service"]]
