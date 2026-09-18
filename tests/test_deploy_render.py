from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from model_scheduler.config import load_config
from model_scheduler.deploy import DeployError, collect_facts, migrate, preflight, render, render_lab
from model_scheduler.storage_monitor import StorageSnapshot


def lab_v2_config(port: int = 18081) -> dict:
    """Schema-v2 registration of the M00 model, used by the lab render tests."""
    return {
        "schema_version": 2,
        "registration": {
            "runtimes": [
                {
                    "runtime_id": "llama-cpp-cuda-sm87-4bc272f",
                    "profile_id": "llama-cpp-gguf-v1",
                    "image_digest": "sms-llama-cpp@sha256:" + "8" * 64,
                    "adapter_sha256": "b" * 64,
                    "lock_sha256": "c" * 64,
                    "startup_args": [
                        "--load-mode", "--parallel", "--kv-unified-per-slot", "--image-max-tokens",
                        "--n-gpu-layers", "--flash-attn", "--no-warmup", "--no-webui", "--host", "--port",
                    ],
                }
            ],
            "models": [
                {
                    "model_id": "qwen25vl-7b-q4",
                    "runtime_id": "llama-cpp-cuda-sm87-4bc272f",
                    "capabilities": ["chat", "vision"],
                    "assets": [
                        {"role": "model", "path": "qwen25vl-7b-q4/model.gguf", "sha256": "3f" + "0" * 62, "size_bytes": 4683072320},
                        {"role": "projector", "path": "qwen25vl-7b-q4/mmproj.gguf", "sha256": "d1" + "0" * 62, "size_bytes": 1354162912},
                    ],
                    "port": port,
                    "envelope": {
                        "ctx_size": 32768, "max_input_tokens": 28672, "max_output_tokens": 4096,
                        "max_parallel": 2, "max_image_tokens": 1280, "max_image_edge_pixels": 1024, "max_images": 1,
                    },
                    "timeout_seconds": 3600,
                    "reserved_bytes": 6106148045,
                    "measured": False,
                    "measurement_ref": None,
                    "physical_resident_peak_bytes": None,
                }
            ],
        },
        "server": {"host": "127.0.0.1", "port": 8090, "workers": 1, "max_request_body_bytes": 4194304, "body_timeout_seconds": 30, "shutdown_grace_seconds": 30},
        "scheduler": {
            "poll_interval_seconds": 2, "request_queue_timeout_seconds": 1800, "queue_capacity": 128,
            "priority_aging_seconds": 30, "switch_drain_timeout_seconds": 30, "switch_retry_seconds": 30,
            "resource_safety_margin": 0.15, "min_free_memory_bytes": 2147483648, "max_evictions_per_request": 8,
            "memory_reclaim_timeout_seconds": 10,
            "heat": {"half_life_seconds": 1800, "request_weight": 1.0, "token_weight": 0.0001},
            "thrash": {"switch_window_seconds": 10, "max_switches_in_window": 3, "cooldown_seconds": 15},
        },
        "resources": {"provider": "psutil", "system_reserve_bytes": 8589934592, "sample_interval_seconds": 1, "sample_max_age_seconds": 2, "model_budget_bytes": 16000000000},
        "storage": {"mount_path": "/media/jtzn/sandisk-ext4", "model_directory": "/media/jtzn/sandisk-ext4/models", "expected_uuid": "0e0a0f2e-1111-2222-3333-444455556666", "filesystem": "ext4"},
        "gateway": {"connect_timeout_seconds": 5, "pool_timeout_seconds": 5, "read_idle_timeout_seconds": 60, "write_idle_timeout_seconds": 60, "inference_timeout_seconds": 900, "close_timeout_seconds": 5, "max_response_body_bytes": 16777216, "max_sse_event_bytes": 1048576},
        "control": {"allowed_uids": [1000]},
        "blobs": {"root": "/home/jtzn/self-model-switch-blobs"},
    }


def test_lab_render_writes_dynamic_scheduler_swap_and_runner_artifacts(tmp_path) -> None:
    source = tmp_path / "scheduler-v2.json"
    source.write_text(json.dumps(lab_v2_config()), encoding="utf-8")
    output = tmp_path / "lab"

    manifest = render_lab(source, output, deployment_id="lab-orin", container_runtime="nvidia")

    assert manifest["mode"] == "lab"
    assert manifest["lab_only"] is True
    assert manifest["deployment_id"] == "lab-orin"
    assert manifest["config_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    entry = manifest["models"]["qwen25vl-7b-q4"]
    assert entry["container_name"] == "sms-lab-orin-qwen25vl-7b-q4"
    assert entry["registered_port"] == 18081
    assert "--runtime=nvidia" in entry["argv"]
    assert entry["argv_sha256"] == hashlib.sha256("\x00".join(entry["argv"]).encode("utf-8")).hexdigest()
    assert entry["image_digest"].startswith("sms-llama-cpp@sha256:")
    assert entry["runtime_id"] == "llama-cpp-cuda-sm87-4bc272f"
    assert entry["profile_id"] == "llama-cpp-gguf-v1"
    assert entry["measured"] is False

    assert (output / "manifest.json").is_file()
    assert (output / "scheduler-v2.json").is_file()
    swap = (output / "llama-swap.yaml").read_text(encoding="utf-8")
    assert "${PORT}" in swap
    assert "sms-lab-orin-qwen25vl-7b-q4" in swap

    with pytest.raises(DeployError, match="empty"):
        render_lab(source, output, deployment_id="lab-orin", container_runtime="nvidia")


def test_lab_render_refuses_production_mode_and_unmeasured_production_branch(tmp_path) -> None:
    source = tmp_path / "scheduler-v2.json"
    source.write_text(json.dumps(lab_v2_config()), encoding="utf-8")

    with pytest.raises(DeployError, match="lab"):
        render_lab(source, tmp_path / "out", deployment_id="lab-orin", container_runtime="nvidia", mode="production")


def test_lab_manifest_binds_the_config_and_serves_the_rendered_argv(tmp_path) -> None:
    from model_scheduler.runtime import load_lab_manifest, lab_launch_argv

    source = tmp_path / "scheduler-v2.json"
    source.write_text(json.dumps(lab_v2_config()), encoding="utf-8")
    output = tmp_path / "lab"
    render_lab(source, output, deployment_id="lab-orin", container_runtime="nvidia")

    manifest = load_lab_manifest(output / "manifest.json", config_path=source)
    assert lab_launch_argv(manifest, "qwen25vl-7b-q4")[0] == "docker"

    tampered = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    tampered["config_sha256"] = "f" * 64
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(DeployError, match="config"):
        load_lab_manifest(broken, config_path=source)


def test_lab_runner_requires_the_manifest_budget_and_contract(tmp_path, monkeypatch) -> None:
    import importlib.util

    module_path = Path(__file__).resolve().parent.parent / "deploy" / "model-runner.py"
    spec = importlib.util.spec_from_file_location("deploy_model_runner_lab", module_path)
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)

    source = tmp_path / "scheduler-v2.json"
    source.write_text(json.dumps(lab_v2_config()), encoding="utf-8")
    output = tmp_path / "lab"
    render_lab(source, output, deployment_id="lab-orin", container_runtime="nvidia")

    executed: list[list[str]] = []
    monkeypatch.setattr(runner, "_lab_exec", lambda argv, budget, manifest: executed.append(argv) or 0)

    assert runner.main(["start", "qwen25vl-7b-q4", "--lab-manifest", str(output / "manifest.json"), "--temporary-budget-bytes", "16000000000"]) == 0
    assert executed and executed[0][0] == "docker"

    with pytest.raises(SystemExit):  # a lab start without a temporary budget is refused
        runner.main(["start", "qwen25vl-7b-q4", "--lab-manifest", str(output / "manifest.json")])
    with pytest.raises(SystemExit):  # the legacy entry still refuses a model outside the fixed set
        runner.main(["start", "dynamic-model"])




def input_data(measured: bool = False) -> dict:
    models = {}
    for name, file, pooling in (("embedding", "embedding.gguf", "mean"), ("reranker", "reranker.gguf", "rank"), ("qwen-small", "qwen-small.gguf", None), ("qwen-large", "qwen-large.gguf", None)):
        models[name] = {"file": file, "sha256": "a" * 64, "context_size": 2048, "parallel": 1, "pooling": pooling, "reserved_bytes": 1024, "measured": measured}
    return {"deployment_id": "orin-local", "hardware": {"model": "NVIDIA Jetson AGX Orin Developer Kit", "compatible": ["nvidia,p3737-0000+p3701-0005", "nvidia,p3701-0005", "nvidia,tegra234"]}, "ssd_uuid": "uuid", "ssd_filesystem": "ext4", "jetpack_version": "7", "llama_swap_version": "v1", "llama_swap_sha256": "b" * 64, "image": "repo/image@sha256:" + "c" * 64, "validation_report": None, "models": models}


def validation_report(payload: dict) -> dict:
    return {
        "schema_version": 2,
        "timestamp_utc": "2026-09-16T00:00:00Z",
        "source_commit": "a" * 40,
        "deployment_id": payload["deployment_id"],
        "hardware": payload["hardware"],
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
    assert manifest["deployment_id"] == "orin-local"
    assert json.loads((output / "manifest.json").read_text())["models"]["embedding"]["file"] == "embedding.gguf"
    config = load_config(output / "config.yaml")
    assert config.models["qwen-small"].upstream_url == "http://127.0.0.1:10003"
    assert "sms-model-runner start qwen-small" in (output / "llama-swap.yaml").read_text()
    assert json.loads((output / "manifest.json").read_text())["models"]["qwen-small"]["container_name"] == "sms-orin-local-qwen-small"
    assert manifest["config_sha256"] == hashlib.sha256((output / "config.yaml").read_bytes()).hexdigest()
    assert (output / "fstab.fragment").read_text() == "UUID=uuid /mnt/model-ssd ext4 defaults,nofail,x-systemd.device-timeout=10s 0 2\n"
    scheduler_unit = (output / "model-scheduler.service").read_text()
    assert "BindsTo=mnt-model\\x2dssd.mount" in scheduler_unit
    assert "BindsTo=llama-swap.service" not in scheduler_unit
    # P17/C08: the runtime dir that hosts control.sock is provisioned group-private
    assert "RuntimeDirectory=model-scheduler self-model-switch" in scheduler_unit
    assert "RuntimeDirectoryMode=0750" in scheduler_unit


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
    manifest = render(source, "production", tmp_path / "out")
    assert manifest["validation_report"] == "hardware-report.json"
    assert (tmp_path / "out" / "hardware-report.json").read_text() == report.read_text()
    assert manifest["hardware_report_sha256"] == hashlib.sha256(report.read_bytes()).hexdigest()
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


def test_production_rejects_hardware_report_for_a_different_device(tmp_path) -> None:
    payload = input_data(True)
    report = validation_report(payload)
    report["hardware"] = {"model": "NVIDIA Jetson AGX Thor", "compatible": ["nvidia,tegra264"]}
    report_path = tmp_path / "measurements.json"; report_path.write_text(json.dumps(report))
    payload["validation_report"] = str(report_path)
    source = tmp_path / "input.json"; source.write_text(json.dumps(payload))

    with pytest.raises(DeployError, match="validation report"):
        render(source, "production", tmp_path / "out")


def test_production_rejects_legacy_report_without_hardware_identity(tmp_path) -> None:
    payload = input_data(True)
    report = validation_report(payload)
    report["schema_version"] = 1
    del report["hardware"]
    report_path = tmp_path / "measurements.json"; report_path.write_text(json.dumps(report))
    payload["validation_report"] = str(report_path)
    source = tmp_path / "input.json"; source.write_text(json.dumps(payload))

    with pytest.raises(DeployError, match="validation report"):
        render(source, "production", tmp_path / "out")


def test_production_rejects_unknown_or_placeholder_hardware_evidence(tmp_path) -> None:
    payload = input_data(True)
    payload["hardware"]["compatible"] = ["REQUIRED_REAL_COMPATIBLE"]
    source = tmp_path / "input.json"; source.write_text(json.dumps(payload))
    with pytest.raises(DeployError, match="hardware"):
        render(source, "lab", tmp_path / "placeholder")

    payload = input_data(True)
    report = validation_report(payload)
    report["unreviewed"] = "value"
    report_path = tmp_path / "measurements.json"; report_path.write_text(json.dumps(report))
    payload["validation_report"] = str(report_path)
    source.write_text(json.dumps(payload))
    with pytest.raises(DeployError, match="validation report"):
        render(source, "production", tmp_path / "unknown")


def test_preflight_cross_checks_rendered_config_storage_and_hardware(tmp_path) -> None:
    source = tmp_path / "input.json"; source.write_text(json.dumps(input_data()))
    output = tmp_path / "out"; render(source, "lab", output)
    class Storage:
        def check(self, models):
            assert models["embedding"].sha256 == "a" * 64
            return StorageSnapshot(True, None, 0, {})
    result = preflight(output / "manifest.json", storage=Storage(), hardware=input_data()["hardware"])
    assert result["ok"] is True


def test_preflight_rejects_a_config_file_that_no_longer_matches_the_manifest_digest(tmp_path) -> None:
    source = tmp_path / "input.json"; source.write_text(json.dumps(input_data()))
    output = tmp_path / "out"; render(source, "lab", output)
    config_path = output / "config.yaml"
    config_path.write_text(config_path.read_text() + "\n")

    with pytest.raises(DeployError, match="config digest"):
        preflight(output / "manifest.json", hardware=input_data()["hardware"])


def test_collect_writes_read_only_device_facts_once(tmp_path) -> None:
    output = tmp_path / "facts.json"
    device_tree = tmp_path / "device-tree"
    device_tree.mkdir()
    (device_tree / "model").write_bytes(b"NVIDIA Jetson AGX Orin Developer Kit\0")
    (device_tree / "compatible").write_bytes(b"nvidia,p3737-0000+p3701-0005\0nvidia,p3701-0005\0nvidia,tegra234\0")
    calls: list[list[str]] = []
    facts = collect_facts(output, runner=lambda argv: calls.append(argv) or "value", device_tree_root=device_tree)
    assert facts["uname"] == "value"
    assert facts["llama_swap_version"] == "value"
    assert facts["gpu_runtime"] == "value"
    assert facts["hardware"] == input_data()["hardware"]
    assert ["lsblk", "--json", "--output", "NAME,UUID,FSTYPE,MOUNTPOINTS"] in calls
    with pytest.raises(DeployError, match="already exists"):
        collect_facts(output, runner=lambda _: "value", device_tree_root=device_tree)


def test_preflight_rejects_a_manifest_for_a_different_target_device(tmp_path) -> None:
    source = tmp_path / "input.json"; source.write_text(json.dumps(input_data()))
    output = tmp_path / "out"; render(source, "lab", output)

    with pytest.raises(DeployError, match="hardware"):
        preflight(output / "manifest.json", hardware={"model": "NVIDIA Jetson AGX Thor", "compatible": ["nvidia,tegra264"]})


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

# ---------------------------------------------------------------------------
# P26: the v3 production render (see plan/08-execution-plan.md)
# ---------------------------------------------------------------------------


import importlib.util  # noqa: E402
from model_scheduler import deploy as deploy_module  # noqa: E402
from model_scheduler.acceptance import EXIT_INPUT  # noqa: E402


def _p26_helpers(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_for_p26", Path(__file__).resolve().parent / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _p26_verified_site(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A production-renderable candidate (every model measured) plus verified evidence.

    A production render requires *every* registration to be measured, so the
    fixture keeps the measured vision model and drops the unmeasured one — the
    render logic itself is per model.
    """
    import yaml

    candidate_helpers = _p26_helpers("test_candidate")
    site = candidate_helpers._site(tmp_path)
    document = yaml.safe_load(site["config"].read_text(encoding="utf-8"))
    document["registration"]["models"] = [model for model in document["registration"]["models"]
                                          if model["model_id"] == "qwen-small"]
    for key in ("pinned_models", "preload_models"):
        document["scheduler"][key] = [model_id for model_id in document["scheduler"].get(key, [])
                                      if model_id == "qwen-small"]
    runtime = document["registration"]["runtimes"][0]
    runtime["startup_args"] = [*runtime["startup_args"], "--parallel", "--kv-unified-per-slot",
                               "--image-max-tokens"]
    site["config"].write_text(yaml.safe_dump(document), encoding="utf-8")
    policy = json.loads(site["policy"].read_text(encoding="utf-8"))
    policy["performance"] = [entry for entry in policy["performance"] if entry["model_id"] == "qwen-small"]
    site["policy"].write_text(json.dumps(policy), encoding="utf-8")
    candidate_helpers._build(site)
    candidate_path = site["output"]
    evidence = _p26_helpers("test_verify")._build_evidence(tmp_path, candidate_path)
    return candidate_path, evidence, tmp_path / "ssd" / "models"


def test_a_production_render_freezes_the_verified_identity(tmp_path) -> None:
    candidate_path, evidence, model_directory = _p26_verified_site(tmp_path)
    output = tmp_path / "build" / "deploy"

    result = deploy_module.render_v3(candidate_path=candidate_path, evidence_dir=evidence, output=output,
                              mode="production", model_directory=model_directory)

    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert result["production"] is True and manifest["production"] is True
    assert manifest["schema_version"] == 3 and manifest["identity_sha256"]
    assert manifest["model_directory"] == str(model_directory)
    assert manifest["evidence"]["run_id"] == "run-1"
    assert not (output / "NOT-PRODUCTION").exists()
    for model_id, entry in manifest["models"].items():
        assert entry["container_name"] == f"sms-{manifest['deployment_id']}-{model_id}"
        assert f"--mount=type=bind,src={model_directory}" in entry["argv"] or \
               any(f"src={model_directory}" in token for token in entry["argv"])
        assert entry["image_digest"].endswith("a" * 64)


def test_a_production_render_refuses_unverified_material_and_writes_nothing(tmp_path) -> None:
    import yaml

    candidate_helpers = _p26_helpers("test_candidate")
    site = candidate_helpers._site(tmp_path)
    document = yaml.safe_load(site["config"].read_text(encoding="utf-8"))
    document["registration"]["models"] = [model for model in document["registration"]["models"]
                                          if model["model_id"] == "qwen-small"]
    for key in ("pinned_models", "preload_models"):
        document["scheduler"][key] = [model_id for model_id in document["scheduler"].get(key, [])
                                      if model_id == "qwen-small"]
    runtime = document["registration"]["runtimes"][0]
    runtime["startup_args"] = [*runtime["startup_args"], "--parallel", "--kv-unified-per-slot",
                               "--image-max-tokens"]
    site["config"].write_text(yaml.safe_dump(document), encoding="utf-8")
    policy = json.loads(site["policy"].read_text(encoding="utf-8"))
    policy["performance"] = [entry for entry in policy["performance"] if entry["model_id"] == "qwen-small"]
    site["policy"].write_text(json.dumps(policy), encoding="utf-8")
    candidate_helpers._build(site)
    candidate_path = site["output"]
    broken = _p26_helpers("test_verify")._build_evidence(tmp_path / "broken", candidate_path, drop_final="S04")
    output = tmp_path / "build" / "deploy"

    with pytest.raises(deploy_module.DeployError, match="does not verify offline"):
        deploy_module.render_v3(candidate_path=candidate_path, evidence_dir=broken, output=output, mode="production",
                         model_directory=tmp_path / "ssd" / "models")

    assert not output.exists() or not any(output.iterdir())  # no launchable directory was produced

    with pytest.raises(deploy_module.DeployError, match="requires --evidence"):
        deploy_module.render_v3(candidate_path=candidate_path, evidence_dir=None, output=output, mode="production",
                         model_directory=tmp_path / "ssd" / "models")


def test_a_non_empty_target_is_refused(tmp_path) -> None:
    candidate_path, evidence, model_directory = _p26_verified_site(tmp_path)
    output = tmp_path / "build"
    output.mkdir()
    (output / "keep.txt").write_text("occupant", encoding="utf-8")

    with pytest.raises(deploy_module.DeployError, match="non-empty"):
        deploy_module.render_v3(candidate_path=candidate_path, evidence_dir=evidence, output=output, mode="production",
                         model_directory=model_directory)


def test_a_lab_render_is_marked_non_production(tmp_path) -> None:
    candidate_path, _evidence, model_directory = _p26_verified_site(tmp_path)
    output = tmp_path / "lab-deploy"

    result = deploy_module.render_v3(candidate_path=candidate_path, evidence_dir=None, output=output, mode="lab",
                              model_directory=model_directory, temporary_budget_bytes=16_000_000_000)

    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert result["production"] is False and manifest["mode"] == "lab" and manifest["evidence"] is None
    assert (output / "NOT-PRODUCTION").is_file()

    with pytest.raises(deploy_module.DeployError, match="temporary-budget-bytes"):
        deploy_module.render_v3(candidate_path=candidate_path, evidence_dir=None, output=tmp_path / "lab-2", mode="lab",
                         model_directory=model_directory)


def test_the_deploy_cli_routes_the_v3_render_and_requires_the_model_directory(tmp_path, capsys) -> None:
    candidate_path, evidence, model_directory = _p26_verified_site(tmp_path)
    output = tmp_path / "cli-deploy"

    code = deploy_module.main(["render", "--candidate", str(candidate_path), "--evidence", str(evidence),
                        "--mode", "production", "--output", str(output), "--model-directory", str(model_directory)])
    assert code == 0
    assert json.loads((output / "manifest.json").read_text(encoding="utf-8"))["mode"] == "production"

    missing = deploy_module.main(["render", "--candidate", str(candidate_path), "--evidence", str(evidence),
                           "--mode", "production", "--output", str(tmp_path / "cli-2")])
    assert missing == EXIT_INPUT
    assert "model-directory" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# P27: service rendering, the switch sequence and the rollback
# ---------------------------------------------------------------------------


def _p27_inputs(**overrides):
    base = dict(service_user="model-scheduler", service_group="model-scheduler", client_uid=1003,
                client_group="sms-client", socket_path="/run/self-model-switch/control.sock",
                model_mount="/media/jtzn/sandisk-ext4", model_directory="/media/jtzn/sandisk-ext4/models",
                mount_unit="media-jtzn-sandisk\\x2dext4.mount", blob_root="/var/lib/self-model-switch/blobs",
                blob_disk_uuid="25e77400-6e42-470b-bf06-1a5e6dd2b548", blob_quota_bytes=17179869184,
                release_root="/opt/self-model-switch/releases",
                config_path="/etc/self-model-switch/config.yaml",
                swap_config_path="/etc/self-model-switch/llama-swap.yaml")
    base.update(overrides)
    return deploy_module.ServiceInputs(**base)


class FakeOps:
    """A scripted site: every step can be broken and every call is recorded."""

    def __init__(self, **overrides) -> None:
        self.calls: list[str] = []
        self.release: str | None = "release-old"
        self.closed = {"closed": True}
        self.drained = {"queue_depth": 0, "leases": 0, "sessions": 0}
        self.stopped = {"stopped": True, "instances": []}
        self.preflight = {"accepted": True}
        self.started = {"running": True}
        self.smoke_ok = {"ok": True}
        self.restored = {"restored": True}
        for key, value in overrides.items():
            setattr(self, key, value)

    def current_release(self):
        self.calls.append("current_release")
        return self.release

    def close_admission(self):
        self.calls.append("close_admission")
        return self.closed

    def drain(self):
        self.calls.append("drain")
        return self.drained

    def prove_instances_stopped(self):
        self.calls.append("prove_instances_stopped")
        return self.stopped

    def preflight_release(self, release):
        self.calls.append("preflight_release")
        return self.preflight

    def switch_current(self, release):
        self.calls.append("switch_current")
        self.release = release
        return {"current": release}

    def start(self):
        self.calls.append("start")
        return self.started

    def smoke(self):
        self.calls.append("smoke")
        return self.smoke_ok

    def reopen_admission(self):
        self.calls.append("reopen_admission")
        return {"closed": False}

    def restore_blob_metadata(self, backup):
        self.calls.append(f"restore_blob_metadata:{backup}")
        return self.restored


def test_the_service_units_render_every_site_value_from_the_inputs(tmp_path) -> None:
    result = deploy_module.render_service_units(inputs=_p27_inputs(), output=tmp_path / "units")

    scheduler = (tmp_path / "units" / "model-scheduler.service").read_text(encoding="utf-8")
    assert result["video_units"] == 0 and result["socket_mode"] == "0660"
    assert "@SSD_MOUNT_UNIT@" not in scheduler and "/mnt/model-ssd" not in scheduler
    assert "media-jtzn-sandisk\\x2dext4.mount" in scheduler
    assert "User=model-scheduler" in scheduler and "Group=model-scheduler" in scheduler
    assert "ReadOnlyPaths=/media/jtzn/sandisk-ext4/models" in scheduler
    assert "RequiresMountsFor=/media/jtzn/sandisk-ext4/models" in scheduler
    assert "SMS_CONTROL_SOCKET_MODE=0660" in scheduler and "SMS_CLIENT_UID=1003" in scheduler
    assert "SMS_CLIENT_GROUP=sms-client" in scheduler and "SMS_BLOB_QUOTA_BYTES=17179869184" in scheduler
    assert "SMS_BLOB_DISK_UUID=25e77400-6e42-470b-bf06-1a5e6dd2b548" in scheduler
    assert "/opt/self-model-switch/releases/current/.venv/bin/python" in scheduler
    sudoers = tmp_path / "units" / "sudoers.model-scheduler"
    assert sudoers.is_file() and (sudoers.stat().st_mode & 0o777) == 0o440
    facts = json.loads((tmp_path / "units" / "service-facts.json").read_text(encoding="utf-8"))
    assert facts["video_units"] == 0 and facts["socket_mode"] == "0660"


def test_a_video_unit_is_never_rendered(tmp_path) -> None:
    with pytest.raises(deploy_module.DeployError, match="never renders a video unit"):
        deploy_module.render_service_units(inputs=_p27_inputs(video_unit=True), output=tmp_path / "units")

    templates = tmp_path / "templates"
    templates.mkdir()
    for name in ("model-scheduler.service.in", "llama-swap.service.in", "sudoers.model-scheduler"):
        (templates / name).write_text((Path("deploy") / name).read_text(encoding="utf-8"), encoding="utf-8")
    (templates / "video.service.in").write_text("[Unit]\nDescription=nope\n", encoding="utf-8")
    with pytest.raises(deploy_module.DeployError, match="forbidden unit template"):
        deploy_module.render_service_units(inputs=_p27_inputs(), output=tmp_path / "units-2",
                                           template_root=templates)


def test_the_service_inputs_are_validated(tmp_path) -> None:
    for override, message in (({"socket_mode": "0664"}, "client group contract"),
                              ({"model_directory": "relative/models"}, "absolute path"),
                              ({"blob_disk_uuid": "not-a-uuid"}, "filesystem UUID"),
                              ({"blob_quota_bytes": 0}, "positive integer"),
                              ({"client_uid": 0}, "positive integer")):
        with pytest.raises(deploy_module.DeployError, match=message):
            deploy_module.render_service_units(inputs=_p27_inputs(**override), output=tmp_path / "never")

    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "keep").write_text("x", encoding="utf-8")
    with pytest.raises(deploy_module.DeployError, match="non-empty"):
        deploy_module.render_service_units(inputs=_p27_inputs(), output=occupied)


def test_the_switch_sequence_proves_stopped_before_touching_current() -> None:
    ops = FakeOps()

    result = deploy_module.switch_release(ops=ops, release="release-new", metadata_backup="blob-backup-1")

    assert result["ok"] is True and result["switched"] is True
    assert ops.release == "release-new"
    assert ops.calls.index("prove_instances_stopped") < ops.calls.index("switch_current")
    assert ops.calls.index("preflight_release") < ops.calls.index("switch_current")
    assert "reopen_admission" in ops.calls


def test_a_switch_aborts_before_current_when_anything_is_unproven() -> None:
    blocked = FakeOps(stopped={"stopped": False, "instances": ["sms-lab-qwen-small"]})
    result = deploy_module.switch_release(ops=blocked, release="release-new")
    assert result["ok"] is False and result["switched"] is False
    assert "switch_current" not in blocked.calls  # current was never touched
    assert "reopen_admission" in blocked.calls

    busy = FakeOps(drained={"queue_depth": 1, "leases": 0, "sessions": 0})
    assert deploy_module.switch_release(ops=busy, release="release-new")["switched"] is False
    assert "switch_current" not in busy.calls

    refused = FakeOps(preflight={"accepted": False})
    result = deploy_module.switch_release(ops=refused, release="release-new")
    assert result["switched"] is False and "switch_current" not in refused.calls
    assert any("preflight" in problem for problem in result["problems"])


def test_a_failure_after_the_switch_is_reported_for_repair() -> None:
    ops = FakeOps(smoke_ok={"ok": False})

    result = deploy_module.switch_release(ops=ops, release="release-new", metadata_backup="blob-backup-1")

    assert result["switched"] is True and result["ok"] is False and result["repair_required"] is True
    assert result["rollback_release"] == "release-old" and result["metadata_backup"] == "blob-backup-1"


def test_a_rollback_restores_the_accepted_release_and_compatible_metadata() -> None:
    ops = FakeOps()

    result = deploy_module.rollback_release(ops=ops, accepted_release="release-old", metadata_backup="blob-backup-1",
                                            downgrade_compatible=False)

    assert result["ok"] is True and result["rolled_back"] is True and ops.release == "release-old"
    assert "restore_blob_metadata:blob-backup-1" in ops.calls

    compatible = FakeOps()
    deploy_module.rollback_release(ops=compatible, accepted_release="release-old", metadata_backup=None,
                                   downgrade_compatible=True)
    assert not any(call.startswith("restore_blob_metadata") for call in compatible.calls)


def test_a_rollback_without_an_accepted_release_or_backup_is_refused() -> None:
    ops = FakeOps()

    result = deploy_module.rollback_release(ops=ops, accepted_release=None, metadata_backup=None,
                                            downgrade_compatible=True)

    assert result["status"] == "stopped_for_repair" and result["rolled_back"] is False
    assert "switch_current" not in ops.calls and "start" not in ops.calls

    incompatible = FakeOps()
    result = deploy_module.rollback_release(ops=incompatible, accepted_release="release-old", metadata_backup=None,
                                            downgrade_compatible=False)
    assert result["ok"] is False and any("backup" in problem for problem in result["problems"])
    assert "switch_current" not in incompatible.calls
