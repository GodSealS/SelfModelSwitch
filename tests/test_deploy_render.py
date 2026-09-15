from __future__ import annotations

import hashlib
import json

import pytest

from model_scheduler.config import load_config
from model_scheduler.deploy import DeployError, collect_facts, migrate, preflight, render
from model_scheduler.storage_monitor import StorageSnapshot


def input_data(measured: bool = False) -> dict:
    models = {}
    for name, file, pooling in (("embedding", "embedding.gguf", "mean"), ("reranker", "reranker.gguf", "rank"), ("qwen-small", "qwen-small.gguf", None), ("qwen-large", "qwen-large.gguf", None)):
        models[name] = {"file": file, "sha256": "a" * 64, "context_size": 2048, "parallel": 1, "pooling": pooling, "reserved_bytes": 1024, "measured": measured}
    return {"deployment_id": "thor-local", "ssd_uuid": "uuid", "ssd_filesystem": "ext4", "jetpack_version": "7", "llama_swap_version": "v1", "llama_swap_sha256": "b" * 64, "image": "repo/image@sha256:" + "c" * 64, "validation_report": None, "models": models}


def validation_report(payload: dict) -> dict:
    return {
        "schema_version": 1,
        "timestamp_utc": "2026-09-16T00:00:00Z",
        "source_commit": "a" * 40,
        "deployment_id": payload["deployment_id"],
        "jetpack_version": payload["jetpack_version"],
        "image_digest": payload["image"],
        "llama_swap_version": payload["llama_swap_version"],
        "llama_swap_sha256": payload["llama_swap_sha256"],
        "ssd_uuid": payload["ssd_uuid"],
        "models": {
            name: {
                "sha256": model["sha256"],
                "context_size": model["context_size"],
                "parallel": model["parallel"],
                "batch_size": 512,
                "ubatch_size": 128,
                "cache_type_k": "f16",
                "cache_type_v": "f16",
                "gpu_layers": 99,
                "fit": False,
                "reserved_bytes": model["reserved_bytes"],
                "peak_deltas_bytes": [1, 2, 3],
                "gpu_verified": True,
                "capability_verified": True,
                "cold_load_seconds": 0.0,
            }
            for name, model in payload["models"].items()
        },
        "scenarios": {f"A{index:02d}": "passed" for index in range(1, 21)},
        "soak": {
            "duration_seconds": 1800,
            "http_500_count": 0,
            "oom_count": 0,
            "lease_leaks": 0,
            "unsafe_evictions": 0,
            "request_count": 1,
            "http_429_count": 0,
            "http_504_count": 0,
            "queue_final": 0,
            "leases_final": 0,
        },
    }

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
    assert manifest["config_sha256"] == hashlib.sha256((output / "config.yaml").read_bytes()).hexdigest()
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
    report.write_text(json.dumps(validation_report(payload)))
    payload["validation_report"] = str(report)
    source = tmp_path / "input.json"; source.write_text(json.dumps(payload))
    render(source, "production", tmp_path / "out")
    payload["models"]["qwen-small"]["parallel"] = 2
    source.write_text(json.dumps(payload))
    with pytest.raises(DeployError, match="validation report"):
        render(source, "production", tmp_path / "out-2")


def test_production_rejects_a_report_without_all_hardware_acceptance_evidence(tmp_path) -> None:
    payload = input_data(True)
    report = validation_report(payload)
    report["scenarios"]["A20"] = "not_run"
    report_path = tmp_path / "measurements.json"; report_path.write_text(json.dumps(report))
    payload["validation_report"] = str(report_path)
    source = tmp_path / "input.json"; source.write_text(json.dumps(payload))
    with pytest.raises(DeployError, match="validation report"):
        render(source, "production", tmp_path / "out")


def test_production_rejects_unparseable_hardware_report_provenance(tmp_path) -> None:
    payload = input_data(True)
    report = validation_report(payload)
    report["timestamp_utc"] = "not-a-timestamp"
    report["source_commit"] = "not-a-commit"
    report_path = tmp_path / "measurements.json"; report_path.write_text(json.dumps(report))
    payload["validation_report"] = str(report_path)
    source = tmp_path / "input.json"; source.write_text(json.dumps(payload))
    with pytest.raises(DeployError, match="validation report"):
        render(source, "production", tmp_path / "out")


def test_preflight_cross_checks_rendered_config_and_storage(tmp_path) -> None:
    source = tmp_path / "input.json"; source.write_text(json.dumps(input_data()))
    output = tmp_path / "out"; render(source, "lab", output)
    class Storage:
        def check(self, models):
            assert models["embedding"].sha256 == "a" * 64
            return StorageSnapshot(True, None, 0, {})
    result = preflight(output / "manifest.json", storage=Storage())
    assert result["ok"] is True


def test_preflight_rejects_a_config_file_that_no_longer_matches_the_manifest_digest(tmp_path) -> None:
    source = tmp_path / "input.json"; source.write_text(json.dumps(input_data()))
    output = tmp_path / "out"; render(source, "lab", output)
    config_path = output / "config.yaml"
    config_path.write_text(config_path.read_text() + "\n")

    with pytest.raises(DeployError, match="config digest"):
        preflight(output / "manifest.json")


def test_collect_writes_read_only_device_facts_once(tmp_path) -> None:
    output = tmp_path / "facts.json"
    calls: list[list[str]] = []
    facts = collect_facts(output, runner=lambda argv: calls.append(argv) or "value")
    assert facts["uname"] == "value"
    assert facts["llama_swap_version"] == "value"
    assert facts["gpu_runtime"] == "value"
    assert ["lsblk", "--json", "--output", "NAME,UUID,FSTYPE,MOUNTPOINTS"] in calls
    with pytest.raises(DeployError, match="already exists"):
        collect_facts(output, runner=lambda _: "value")


def test_migrate_legacy_config_writes_explicit_v1_template(tmp_path) -> None:
    legacy = tmp_path / "legacy.yaml"
    legacy.write_text("""
server: {host: 127.0.0.1, port: 8090}
llama_swap: {base_url: http://127.0.0.1:8080, timeout_seconds: 30, load_timeout_seconds: 900}
scheduler:
  poll_interval_seconds: 2
  resource_safety_margin: 0.15
  min_free_memory_bytes: 1
  max_evictions_per_request: 8
  request_queue_timeout_seconds: 1800
  heat: {half_life_seconds: 1800, request_weight: 1.0, token_weight: 0.0001, active_bonus: 2.0}
  thrash: {switch_window_seconds: 10, max_switches_in_window: 3, cooldown_seconds: 15}
resources: {provider: auto, total_memory_bytes: 0}
models:
  embedding: {memory: {reserved_bytes: 1}, scheduling: {priority: 100, evictable: false, pinned: true}, lifecycle: {ttl_seconds: 0}}
  reranker: {memory: {reserved_bytes: 1}, scheduling: {priority: 80, evictable: true, pinned: false}, lifecycle: {ttl_seconds: 1}}
  qwen-small: {memory: {reserved_bytes: 1}, scheduling: {priority: 50, evictable: true, pinned: false}, lifecycle: {ttl_seconds: 1}}
  qwen-large: {memory: {reserved_bytes: 1}, scheduling: {priority: 40, evictable: true, pinned: false}, lifecycle: {ttl_seconds: 1}}
""")
    output = tmp_path / "v1.yaml"
    migrated = migrate(legacy, output)
    assert migrated["resources"]["provider"] == "psutil"
    assert "active_bonus" not in migrated["scheduler"]["heat"]
    assert migrated["models"]["embedding"]["lifecycle"]["preload"] is True
    assert "REQUIRED_REAL_UUID" in output.read_text()
