"""Deterministic deployment-input validation and manifest rendering."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import platform
import re
import subprocess
import sys
from typing import Any

import yaml

from .config import ConfigError, load_config
from .migration_v2 import MissingInventory, MigrationError, migrate_v2
from .storage_monitor import StorageMonitor


class DeployError(ValueError):
    pass


_HASH = re.compile(r"[0-9a-f]{64}\Z")
_MODELS = {"embedding", "reranker", "qwen-small", "qwen-large"}
_MODEL_SETTINGS = {
    "embedding": (10001, ["embeddings"], 100, False, True, 0, "mean"),
    "reranker": (10002, ["rerank"], 80, True, False, 900, "rank"),
    "qwen-small": (10003, ["chat"], 50, True, False, 900, None),
    "qwen-large": (10004, ["chat"], 40, True, False, 1800, None),
}
_SSD_MOUNT_UNIT = "mnt-model\\x2dssd.mount"
_LEGACY_MODEL_SETTINGS = {
    "embedding": (10001, ["embeddings"], "embedding.gguf"),
    "reranker": (10002, ["rerank"], "reranker.gguf"),
    "qwen-small": (10003, ["chat"], "qwen-small.gguf"),
    "qwen-large": (10004, ["chat"], "qwen-large.gguf"),
}
_SCENARIOS = frozenset(f"A{index:02d}" for index in range(1, 21))
_REPORT_MODEL_FIELDS = frozenset({
    "sha256", "context_size", "parallel", "batch_size", "ubatch_size",
    "cache_type_k", "cache_type_v", "gpu_layers", "fit", "reserved_bytes",
    "peak_deltas_bytes", "gpu_verified", "capability_verified", "cold_load_seconds",
})
_REPORT_SOAK_FIELDS = frozenset({
    "duration_seconds", "http_500_count", "oom_count", "lease_leaks",
    "unsafe_evictions", "request_count", "http_429_count", "http_504_count",
    "queue_final", "leases_final",
})


def _require(value: Any, name: str) -> str:
    if type(value) is not str or not value or "REQUIRED" in value:
        raise DeployError(f"{name} is required")
    return value


def _positive_int(value: Any) -> bool:
    return type(value) is int and value > 0


def _nonnegative_finite_number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _validate_hardware_identity(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"model", "compatible"}:
        raise DeployError("invalid hardware identity")
    model = _require(value["model"], "hardware model")
    compatible = value["compatible"]
    if not isinstance(compatible, list) or not compatible or any(type(item) is not str or not item or "REQUIRED" in item for item in compatible):
        raise DeployError("invalid hardware compatible")
    return {"model": model, "compatible": list(compatible)}


def validate_hardware_report(report: Any) -> dict[str, Any]:
    """Validate prompt-free, device-bound evidence needed for production render."""
    required = {
        "schema_version", "timestamp_utc", "source_commit", "deployment_id",
        "jetpack_version", "image_digest", "llama_swap_version", "llama_swap_sha256",
        "ssd_uuid", "hardware", "models", "scenarios", "soak",
    }
    if not isinstance(report, dict) or set(report) != required or report.get("schema_version") != 2:
        raise DeployError("invalid hardware report")
    for key in required - {"schema_version", "hardware", "models", "scenarios", "soak"}:
        _require(report[key], f"hardware report {key}")
    _validate_hardware_identity(report["hardware"])
    timestamp = report["timestamp_utc"]
    try:
        if not isinstance(timestamp, str) or not timestamp.endswith("Z"):
            raise ValueError
        parsed_timestamp = datetime.fromisoformat(timestamp.removesuffix("Z") + "+00:00")
        if parsed_timestamp.tzinfo is None:
            raise ValueError
    except ValueError as exc:
        raise DeployError("invalid hardware report provenance") from exc
    if not isinstance(report["source_commit"], str) or not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", report["source_commit"]):
        raise DeployError("invalid hardware report provenance")
    if not _HASH.fullmatch(report["llama_swap_sha256"]) or not re.fullmatch(r"[^@]+@sha256:[0-9a-f]{64}", report["image_digest"]):
        raise DeployError("invalid hardware report identity")
    models = report["models"]
    if not isinstance(models, dict) or set(models) != _MODELS:
        raise DeployError("invalid hardware report models")
    for model in models.values():
        if not isinstance(model, dict) or not _REPORT_MODEL_FIELDS <= set(model):
            raise DeployError("invalid hardware report model")
        if (
            not isinstance(model["sha256"], str)
            or not _HASH.fullmatch(model["sha256"])
            or not _positive_int(model["context_size"])
            or not _positive_int(model["parallel"])
            or model["batch_size"] != 512
            or model["ubatch_size"] != 128
            or model["cache_type_k"] != "f16"
            or model["cache_type_v"] != "f16"
            or model["gpu_layers"] != 99
            or model["fit"] is not False
            or not _positive_int(model["reserved_bytes"])
            or not isinstance(model["peak_deltas_bytes"], list)
            or len(model["peak_deltas_bytes"]) < 3
            or not all(_positive_int(value) for value in model["peak_deltas_bytes"])
            or model["gpu_verified"] is not True
            or model["capability_verified"] is not True
            or not _nonnegative_finite_number(model["cold_load_seconds"])
        ):
            raise DeployError("invalid hardware report model")
    scenarios = report["scenarios"]
    if not isinstance(scenarios, dict) or set(scenarios) != _SCENARIOS or any(value != "passed" for value in scenarios.values()):
        raise DeployError("hardware acceptance scenarios are incomplete")
    soak = report["soak"]
    if not isinstance(soak, dict) or not _REPORT_SOAK_FIELDS <= set(soak):
        raise DeployError("invalid hardware soak report")
    zero_fields = {"http_500_count", "oom_count", "lease_leaks", "unsafe_evictions", "queue_final", "leases_final"}
    nonnegative_fields = {"http_429_count", "http_504_count"}
    if (
        not _nonnegative_finite_number(soak["duration_seconds"])
        or soak["duration_seconds"] < 1800
        or not _positive_int(soak["request_count"])
        or any(type(soak[key]) is not int or soak[key] != 0 for key in zero_fields)
        or any(type(soak[key]) is not int or soak[key] < 0 for key in nonnegative_fields)
    ):
        raise DeployError("hardware soak report did not pass")
    return report


def _validate_report(path_value: Any, data: dict[str, Any]) -> None:
    if not isinstance(path_value, str) or not path_value:
        raise DeployError("production requires validation_report")
    try:
        report = json.loads(Path(path_value).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeployError("cannot read validation report") from exc
    try:
        report = validate_hardware_report(report)
    except DeployError as exc:
        raise DeployError("validation report does not meet hardware acceptance") from exc
    expected_identity = {
        "deployment_id": data["deployment_id"],
        "hardware": data["hardware"],
        "jetpack_version": data["jetpack_version"],
        "image_digest": data["image"],
        "llama_swap_version": data["llama_swap_version"],
        "llama_swap_sha256": data["llama_swap_sha256"],
        "ssd_uuid": data["ssd_uuid"],
    }
    if any(report[key] != value for key, value in expected_identity.items()):
        raise DeployError("validation report does not match deployment input")
    for model_id, model in data["models"].items():
        measured = report["models"].get(model_id)
        expected = {"sha256": model["sha256"], "context_size": model["context_size"], "parallel": model["parallel"], "reserved_bytes": model["reserved_bytes"]}
        if not isinstance(measured, dict) or any(measured.get(key) != value for key, value in expected.items()):
            raise DeployError("validation report does not match deployment input")


def validate(data: Any, mode: str) -> dict[str, Any]:
    if mode not in {"lab", "production"} or not isinstance(data, dict):
        raise DeployError("invalid deployment input")
    required = {"deployment_id", "hardware", "ssd_uuid", "ssd_filesystem", "jetpack_version", "llama_swap_version", "llama_swap_sha256", "image", "validation_report", "models"}
    if set(data) != required or data["ssd_filesystem"] != "ext4": raise DeployError("invalid deployment fields")
    for key in required - {"hardware", "validation_report", "models", "ssd_filesystem"}: _require(data[key], key)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", data["deployment_id"]):
        raise DeployError("invalid deployment_id")
    _validate_hardware_identity(data["hardware"])
    if not _HASH.fullmatch(data["llama_swap_sha256"]): raise DeployError("invalid llama_swap_sha256")
    if not re.fullmatch(r"[^@]+@sha256:[0-9a-f]{64}", data["image"]): raise DeployError("image must be digest pinned")
    if not isinstance(data["models"], dict) or set(data["models"]) != _MODELS: raise DeployError("invalid model set")
    for model_id, model in data["models"].items():
        if not isinstance(model, dict) or set(model) != {"file", "sha256", "context_size", "parallel", "pooling", "reserved_bytes", "measured"}: raise DeployError(f"invalid {model_id}")
        if "/" in _require(model["file"], f"{model_id}.file") or not _HASH.fullmatch(_require(model["sha256"], f"{model_id}.sha256")): raise DeployError(f"unsafe {model_id} file")
        if type(model["context_size"]) is not int or model["context_size"] <= 0 or type(model["parallel"]) is not int or not 1 <= model["parallel"] <= 16 or model["context_size"] % model["parallel"] or type(model["reserved_bytes"]) is not int or model["reserved_bytes"] <= 0: raise DeployError(f"invalid {model_id} sizing")
        if model["pooling"] != _MODEL_SETTINGS[model_id][6]:
            raise DeployError(f"invalid {model_id} pooling")
        if mode == "production" and model["measured"] is not True: raise DeployError(f"{model_id} must be measured for production")
    if mode == "production":
        _validate_report(data["validation_report"], data)
    return data


def _scheduler_config(manifest: dict[str, Any]) -> dict[str, Any]:
    models = {}
    for model_id in sorted(_MODELS):
        port, capabilities, priority, evictable, pinned, ttl, _ = _MODEL_SETTINGS[model_id]
        source = manifest["models"][model_id]
        models[model_id] = {
            "upstream_url": f"http://127.0.0.1:{port}", "capabilities": capabilities,
            "file": source["file"], "sha256": source["sha256"], "container_name": source["container_name"],
            "memory": {"reserved_bytes": source["reserved_bytes"]},
            "scheduling": {"priority": priority, "max_concurrency": source["parallel"], "evictable": evictable, "pinned": pinned},
            "lifecycle": {"preload": pinned, "ttl_seconds": ttl},
        }
    return {
        "schema_version": 1,
        "server": {"host": "127.0.0.1", "port": 8090, "workers": 1, "max_request_body_bytes": 4194304, "body_timeout_seconds": 10, "shutdown_grace_seconds": 30},
        "llama_swap": {"base_url": "http://127.0.0.1:8080", "connect_timeout_seconds": 5, "control_timeout_seconds": 30, "load_timeout_seconds": 900, "unload_timeout_seconds": 45},
        "scheduler": {"poll_interval_seconds": 2, "request_queue_timeout_seconds": 1800, "queue_capacity": 128, "priority_aging_seconds": 30, "switch_drain_timeout_seconds": 30, "switch_retry_seconds": 30, "resource_safety_margin": 0.15, "min_free_memory_bytes": 2147483648, "max_evictions_per_request": 8, "memory_reclaim_timeout_seconds": 10, "heat": {"half_life_seconds": 1800, "request_weight": 1.0, "token_weight": 0.0001}, "thrash": {"switch_window_seconds": 10, "max_switches_in_window": 3, "cooldown_seconds": 15}},
        "resources": {"provider": "psutil", "system_reserve_bytes": 8589934592, "sample_interval_seconds": 1, "sample_max_age_seconds": 2},
        "storage": {"mount_path": "/mnt/model-ssd", "model_directory": "/mnt/model-ssd/models", "expected_uuid": manifest["ssd_uuid"], "filesystem": "ext4"},
        "gateway": {"connect_timeout_seconds": 5, "pool_timeout_seconds": 5, "read_idle_timeout_seconds": 60, "write_idle_timeout_seconds": 60, "inference_timeout_seconds": 900, "close_timeout_seconds": 5, "max_response_body_bytes": 16777216, "max_sse_event_bytes": 1048576},
        "models": models,
    }


def _llama_swap_config(manifest: dict[str, Any]) -> dict[str, Any]:
    models = {}
    for model_id in sorted(_MODELS):
        port = _MODEL_SETTINGS[model_id][0]
        models[model_id] = {"proxy": f"http://127.0.0.1:{port}", "cmd": f"/usr/local/libexec/sms-model-runner start {model_id}", "cmdStop": f"/usr/local/libexec/sms-model-runner stop {model_id}", "checkEndpoint": "/health", "ttl": 0}
    return {"healthCheckTimeout": 900, "globalTTL": 0, "unloadTimeout": 45, "models": models, "routing": {"router": {"use": "group", "settings": {"groups": {"all-managed": {"swap": False, "exclusive": False, "members": sorted(_MODELS)}}}}}}


def _render_unit(name: str) -> str:
    template = Path(__file__).resolve().parent.parent / "deploy" / f"{name}.in"
    try:
        return template.read_text(encoding="utf-8").replace("@SSD_MOUNT_UNIT@", _SSD_MOUNT_UNIT)
    except OSError as exc:
        raise DeployError(f"cannot read {name} template") from exc


def render(source: str | Path, mode: str, output: str | Path) -> dict[str, Any]:
    path, destination = Path(source), Path(output)
    try: data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc: raise DeployError("cannot read deployment input") from exc
    validated = validate(data, mode)
    report_bytes = Path(validated["validation_report"]).read_bytes() if mode == "production" else None
    manifest = json.loads(json.dumps(validated))
    if report_bytes is not None:
        manifest["validation_report"] = "hardware-report.json"
        manifest["hardware_report_sha256"] = hashlib.sha256(report_bytes).hexdigest()
    for model_id in _MODELS:
        manifest["models"][model_id]["container_name"] = f"sms-{manifest['deployment_id']}-{model_id}"
    if destination.exists() and any(destination.iterdir()): raise DeployError("output directory must be empty")
    destination.mkdir(parents=True, exist_ok=True)
    config_text = yaml.safe_dump(_scheduler_config(manifest), sort_keys=False)
    manifest["config_sha256"] = hashlib.sha256(config_text.encode("utf-8")).hexdigest()
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (destination / "config.yaml").write_text(config_text, encoding="utf-8")
    if report_bytes is not None:
        (destination / "hardware-report.json").write_bytes(report_bytes)
    (destination / "llama-swap.yaml").write_text(yaml.safe_dump(_llama_swap_config(manifest), sort_keys=False), encoding="utf-8")
    (destination / "fstab.fragment").write_text(f"UUID={manifest['ssd_uuid']} /mnt/model-ssd ext4 defaults,nofail,x-systemd.device-timeout=10s 0 2\n", encoding="utf-8")
    (destination / "model-scheduler.service").write_text(_render_unit("model-scheduler.service"), encoding="utf-8")
    (destination / "llama-swap.service").write_text(_render_unit("llama-swap.service"), encoding="utf-8")
    return manifest


def _read_hardware_identity(device_tree_root: Path = Path("/proc/device-tree")) -> dict[str, Any]:
    try:
        model_parts = [part.decode("utf-8") for part in (device_tree_root / "model").read_bytes().split(b"\0") if part]
        compatible = [part.decode("utf-8") for part in (device_tree_root / "compatible").read_bytes().split(b"\0") if part]
    except (OSError, UnicodeDecodeError) as exc:
        raise DeployError("cannot read hardware identity") from exc
    if len(model_parts) != 1:
        raise DeployError("invalid hardware identity")
    return _validate_hardware_identity({"model": model_parts[0], "compatible": compatible})


def preflight(manifest_path: str | Path, *, storage: Any | None = None, hardware: Any | None = None) -> dict[str, Any]:
    """Perform only read-only cross checks on a rendered deployment directory."""
    path = Path(manifest_path)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(manifest, dict) or not isinstance(manifest.get("deployment_id"), str)
                or not isinstance(manifest.get("config_sha256"), str) or not _HASH.fullmatch(manifest["config_sha256"])
                or set(manifest.get("models", {})) != _MODELS):
            raise DeployError("invalid rendered manifest")
        expected_hardware = _validate_hardware_identity(manifest.get("hardware"))
        config_path = path.parent / "config.yaml"
        config_text = config_path.read_bytes()
        if hashlib.sha256(config_text).hexdigest() != manifest["config_sha256"]:
            raise DeployError("rendered config digest mismatch")
        config = load_config(config_path)
    except (OSError, json.JSONDecodeError, ConfigError) as exc:
        raise DeployError("cannot read rendered deployment") from exc
    actual_hardware = _validate_hardware_identity(hardware) if hardware is not None else _read_hardware_identity()
    if actual_hardware != expected_hardware:
        raise DeployError("hardware preflight failed: target identity mismatch")
    for model_id, model in config.models.items():
        rendered = manifest["models"].get(model_id)
        if not isinstance(rendered, dict) or rendered.get("file") != model.file or rendered.get("sha256") != model.sha256:
            raise DeployError("manifest/config model mismatch")
    monitor = storage or StorageMonitor(config.storage.mount_path, config.storage.model_directory, config.storage.expected_uuid, config.storage.filesystem)
    snapshot = monitor.check(config.models)
    if not snapshot.ready:
        raise DeployError(f"storage preflight failed: {snapshot.reason}")
    return {"ok": True, "deployment_id": manifest["deployment_id"], "models": sorted(config.models)}


def collect_facts(output: str | Path, *, runner: Any | None = None, device_tree_root: Path = Path("/proc/device-tree")) -> dict[str, Any]:
    """Record only read-only host facts; this command starts no models or services."""
    destination = Path(output)
    if destination.exists():
        raise DeployError("facts output already exists")
    command = runner or (lambda argv: subprocess.check_output(argv, text=True, timeout=10).strip())
    def optional(argv: list[str]) -> str | None:
        try:
            return command(argv)
        except (OSError, subprocess.SubprocessError):
            return None

    try:
        facts = {
            "uname": command(["uname", "-a"]),
            "python": platform.python_version(),
            "docker": command(["docker", "version", "--format", "{{.Server.Version}}"]),
            "storage": command(["lsblk", "--json", "--output", "NAME,UUID,FSTYPE,MOUNTPOINTS"]),
            "hardware": _read_hardware_identity(device_tree_root),
            "jetpack_release": Path("/etc/nv_tegra_release").read_text(encoding="utf-8").strip() if Path("/etc/nv_tegra_release").is_file() else None,
            "llama_swap_version": optional(["llama-swap", "--version"]),
            "gpu_runtime": optional(["nvidia-smi", "--query-gpu=driver_version,name", "--format=csv,noheader"]),
        }
    except (OSError, subprocess.SubprocessError) as exc:
        raise DeployError("cannot collect host facts") from exc
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(facts, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return facts


def migrate(source: str | Path, output: str | Path) -> dict[str, Any]:
    """Convert only the known pre-v1 layout into an explicit unsafe template."""
    destination = Path(output)
    if destination.exists():
        raise DeployError("migration output already exists")
    try:
        legacy = yaml.safe_load(Path(source).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise DeployError("cannot read legacy configuration") from exc
    if not isinstance(legacy, dict) or set(legacy) != {"server", "llama_swap", "scheduler", "resources", "models"}:
        raise DeployError("unsupported legacy configuration")
    try:
        server, swap, scheduler, resources, models = (legacy[key] for key in ("server", "llama_swap", "scheduler", "resources", "models"))
        if not all(isinstance(value, dict) for value in (server, swap, scheduler, resources, models)) or set(models) != _MODELS:
            raise ValueError
        if resources.get("provider") != "auto" or resources.get("total_memory_bytes") != 0:
            raise ValueError
        heat, thrash = scheduler["heat"], scheduler["thrash"]
        if not isinstance(heat, dict) or not isinstance(thrash, dict):
            raise ValueError
        migrated_models = {}
        for model_id, old_model in models.items():
            if not isinstance(old_model, dict):
                raise ValueError
            port, capabilities, filename = _LEGACY_MODEL_SETTINGS[model_id]
            old_scheduling, old_lifecycle, old_memory = old_model["scheduling"], old_model["lifecycle"], old_model["memory"]
            if not all(isinstance(value, dict) for value in (old_scheduling, old_lifecycle, old_memory)):
                raise ValueError
            pinned = old_scheduling["pinned"]
            migrated_models[model_id] = {"upstream_url": f"http://127.0.0.1:{port}", "capabilities": capabilities, "file": filename, "sha256": "REQUIRED_64_HEX", "container_name": f"sms-REQUIRED_DEPLOYMENT-{model_id}", "memory": {"reserved_bytes": old_memory["reserved_bytes"]}, "scheduling": {"priority": old_scheduling["priority"], "max_concurrency": 1, "evictable": old_scheduling["evictable"], "pinned": pinned}, "lifecycle": {"preload": pinned, "ttl_seconds": old_lifecycle["ttl_seconds"]}}
        migrated = {"schema_version": 1, "server": {"host": server["host"], "port": server["port"], "workers": 1, "max_request_body_bytes": 4194304, "body_timeout_seconds": 10, "shutdown_grace_seconds": 30}, "llama_swap": {"base_url": swap["base_url"], "connect_timeout_seconds": 5, "control_timeout_seconds": swap["timeout_seconds"], "load_timeout_seconds": swap["load_timeout_seconds"], "unload_timeout_seconds": 45}, "scheduler": {"poll_interval_seconds": scheduler["poll_interval_seconds"], "request_queue_timeout_seconds": scheduler["request_queue_timeout_seconds"], "queue_capacity": 128, "priority_aging_seconds": 30, "switch_drain_timeout_seconds": 30, "switch_retry_seconds": 30, "resource_safety_margin": scheduler["resource_safety_margin"], "min_free_memory_bytes": scheduler["min_free_memory_bytes"], "max_evictions_per_request": scheduler["max_evictions_per_request"], "memory_reclaim_timeout_seconds": 10, "heat": {"half_life_seconds": heat["half_life_seconds"], "request_weight": heat["request_weight"], "token_weight": heat["token_weight"]}, "thrash": thrash}, "resources": {"provider": "psutil", "system_reserve_bytes": 8589934592, "sample_interval_seconds": 1, "sample_max_age_seconds": 2}, "storage": {"mount_path": "/mnt/model-ssd", "model_directory": "/mnt/model-ssd/models", "expected_uuid": "REQUIRED_REAL_UUID", "filesystem": "ext4"}, "gateway": {"connect_timeout_seconds": 5, "pool_timeout_seconds": 5, "read_idle_timeout_seconds": 60, "write_idle_timeout_seconds": 60, "inference_timeout_seconds": 900, "close_timeout_seconds": 5, "max_response_body_bytes": 16777216, "max_sse_event_bytes": 1048576}, "models": migrated_models}
    except (KeyError, TypeError, ValueError) as exc:
        raise DeployError("unsupported legacy configuration") from exc
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(yaml.safe_dump(migrated, sort_keys=False), encoding="utf-8")
    return migrated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(); sub = parser.add_subparsers(dest="command", required=True)
    render_parser = sub.add_parser("render"); render_parser.add_argument("--input", required=True); render_parser.add_argument("--mode", choices=("lab", "production"), required=True); render_parser.add_argument("--output", required=True)
    preflight_parser = sub.add_parser("preflight"); preflight_parser.add_argument("--manifest", required=True)
    collect_parser = sub.add_parser("collect"); collect_parser.add_argument("--output", required=True)
    migrate_parser = sub.add_parser("migrate"); migrate_parser.add_argument("--input", required=True); migrate_parser.add_argument("--output", required=True)
    migrate_v2_parser = sub.add_parser("migrate-v2"); migrate_v2_parser.add_argument("--input", required=True); migrate_v2_parser.add_argument("--inventory", required=True); migrate_v2_parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "render":
            render(args.input, args.mode, args.output)
        elif args.command == "preflight":
            print(json.dumps(preflight(args.manifest), sort_keys=True))
        elif args.command == "collect":
            print(json.dumps(collect_facts(args.output), sort_keys=True))
        elif args.command == "migrate-v2":
            document = migrate_v2(args.input, args.inventory, args.output)
            print(json.dumps({"ok": True, "output": args.output, "models": sorted(model["model_id"] for model in document["registration"]["models"])}, sort_keys=True))
        else:
            migrate(args.input, args.output)
    except MissingInventory as exc:
        print(json.dumps(exc.report, sort_keys=True))
        return 2
    except MigrationError as exc: print(f"migration error: {exc}", file=sys.stderr); return 2
    except DeployError as exc: print(f"deployment error: {exc}", file=sys.stderr); return 78
    return 0


if __name__ == "__main__": raise SystemExit(main())
