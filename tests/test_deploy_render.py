from __future__ import annotations

import json

import pytest

from model_scheduler.config import load_config
from model_scheduler.deploy import DeployError, collect_facts, preflight, render
from model_scheduler.storage_monitor import StorageSnapshot


def input_data(measured: bool = False) -> dict:
    models = {}
    for name, file, pooling in (("embedding", "embedding.gguf", "mean"), ("reranker", "reranker.gguf", "rank"), ("qwen-small", "qwen-small.gguf", None), ("qwen-large", "qwen-large.gguf", None)):
        models[name] = {"file": file, "sha256": "a" * 64, "context_size": 2048, "parallel": 1, "pooling": pooling, "reserved_bytes": 1024, "measured": measured}
    return {"deployment_id": "thor-local", "ssd_uuid": "uuid", "ssd_filesystem": "ext4", "jetpack_version": "7", "llama_swap_version": "v1", "llama_swap_sha256": "b" * 64, "image": "repo/image@sha256:" + "c" * 64, "validation_report": None, "models": models}


def test_lab_render_writes_consistent_deployment_artifacts(tmp_path) -> None:
    source = tmp_path / "input.json"; source.write_text(json.dumps(input_data()))
    output = tmp_path / "out"
    manifest = render(source, "lab", output)
    assert manifest["deployment_id"] == "thor-local"
    assert json.loads((output / "manifest.json").read_text())["models"]["embedding"]["file"] == "embedding.gguf"
    config = load_config(output / "config.yaml")
    assert config.models["qwen-small"].upstream_url == "http://127.0.0.1:10003"
    assert "sms-model-runner start qwen-small" in (output / "llama-swap.yaml").read_text()
    assert json.loads((output / "manifest.json").read_text())["models"]["qwen-small"]["container_name"] == "sms-thor-local-qwen-small"
    assert (output / "fstab.fragment").read_text() == "UUID=uuid /mnt/model-ssd ext4 defaults,nofail,x-systemd.device-timeout=10s 0 2\n"
    scheduler_unit = (output / "model-scheduler.service").read_text()
    assert "BindsTo=mnt-model\\x2dssd.mount" in scheduler_unit
    assert "BindsTo=llama-swap.service" not in scheduler_unit


def test_production_rejects_unmeasured_or_placeholder_input(tmp_path) -> None:
    source = tmp_path / "input.json"; source.write_text(json.dumps(input_data(False)))
    with pytest.raises(DeployError, match="measured"):
        render(source, "production", tmp_path / "out")


def test_rejects_capability_pooling_mismatch(tmp_path) -> None:
    payload = input_data(); payload["models"]["embedding"]["pooling"] = "rank"
    source = tmp_path / "input.json"; source.write_text(json.dumps(payload))
    with pytest.raises(DeployError, match="pooling"):
        render(source, "lab", tmp_path / "out")


def test_production_report_must_match_manifest_model_measurements(tmp_path) -> None:
    payload = input_data(True)
    report = tmp_path / "measurements.json"
    report.write_text(json.dumps({"image": payload["image"], "models": {name: {"sha256": model["sha256"], "context_size": model["context_size"], "parallel": model["parallel"]} for name, model in payload["models"].items()}}))
    payload["validation_report"] = str(report)
    source = tmp_path / "input.json"; source.write_text(json.dumps(payload))
    render(source, "production", tmp_path / "out")
    payload["models"]["qwen-small"]["parallel"] = 2
    source.write_text(json.dumps(payload))
    with pytest.raises(DeployError, match="validation report"):
        render(source, "production", tmp_path / "out-2")


def test_preflight_cross_checks_rendered_config_and_storage(tmp_path) -> None:
    source = tmp_path / "input.json"; source.write_text(json.dumps(input_data()))
    output = tmp_path / "out"; render(source, "lab", output)
    class Storage:
        def check(self, models):
            assert models["embedding"].sha256 == "a" * 64
            return StorageSnapshot(True, None, 0, {})
    result = preflight(output / "manifest.json", storage=Storage())
    assert result["ok"] is True


def test_collect_writes_read_only_device_facts_once(tmp_path) -> None:
    output = tmp_path / "facts.json"
    calls: list[list[str]] = []
    facts = collect_facts(output, runner=lambda argv: calls.append(argv) or "value")
    assert facts["uname"] == "value"
    assert ["lsblk", "--json", "--output", "NAME,UUID,FSTYPE,MOUNTPOINTS"] in calls
    with pytest.raises(DeployError, match="already exists"):
        collect_facts(output, runner=lambda _: "value")
