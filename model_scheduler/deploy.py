"""Deterministic deployment-input validation and manifest rendering."""
from __future__ import annotations

import shlex

import argparse
from datetime import datetime
import hashlib
import ipaddress
import json
import math
from pathlib import Path
import platform
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

import yaml

from .config import AppConfigV2, ConfigError, load_config
from .contracts_v2 import ContractError, DeploymentSpec, deployment_digest
from .migration_v2 import MissingInventory, MigrationError, migrate_v2
from .runtime_profiles import LaunchRenderError, render_container_launch
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


def render_lab(
    config_path: str | Path,
    output: str | Path,
    *,
    deployment_id: str,
    container_runtime: str,
    mode: str = "lab",
) -> dict[str, Any]:
    """Render the isolated lab deployment for one schema-v2 configuration (P06b).

    The lab manifest binds the configuration digest, every registered runtime
    profile and asset to the argv the P06 renderer produces; its digest is a test
    instance identity, never a production candidate. Production rendering for
    schema v2 arrives with P26.
    """
    if mode != "lab":
        raise DeployError("schema v2 production rendering is not available; lab mode only until P26")
    source, destination = Path(config_path), Path(output)
    try:
        config_bytes = source.read_bytes()
        config = load_config(source)
    except (ConfigError, OSError) as exc:
        raise DeployError(f"cannot read the schema-v2 configuration: {exc}") from exc
    if not isinstance(config, AppConfigV2):
        raise DeployError("the lab renderer requires a schema_version=2 configuration")
    if destination.exists() and any(destination.iterdir()):
        raise DeployError("output directory must be empty")
    deployment = DeploymentSpec(runtimes=tuple(config.runtimes.values()), models=tuple(config.models.values()))
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "mode": "lab",
        "lab_only": True,
        "deployment_id": deployment_id,
        "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
        "registration_digest": deployment_digest(deployment),
        "container_runtime": container_runtime,
        "storage": {"mount_path": str(config.storage.mount_path), "model_directory": str(config.storage.model_directory)},
        "runtimes": {
            runtime_id: {"profile_id": runtime.profile_id, "image_digest": runtime.image_digest}
            for runtime_id, runtime in config.runtimes.items()
        },
        "models": {},
    }
    swap_models: dict[str, Any] = {}
    for model_id in sorted(config.models):
        model = config.models[model_id]
        try:
            launch = render_container_launch(
                deployment,
                model_id,
                deployment_id=deployment_id,
                model_directory=config.storage.model_directory,
                config_sha256=manifest["config_sha256"],
                mode="lab",
                container_runtime=container_runtime,
                temporary_budget_bytes=model.reserved_bytes,
            )
        except (LaunchRenderError, ContractError) as exc:
            raise DeployError(f"cannot render the lab launch for {model_id!r}: {exc}") from exc
        probe_argv = [token.replace(f"127.0.0.1:{model.port}:", "127.0.0.1:${PORT}:") for token in launch.argv]
        manifest["models"][model_id] = {
            "container_name": launch.container_name,
            "registered_port": model.port,
            "argv": list(launch.argv),
            "argv_sha256": hashlib.sha256("\x00".join(launch.argv).encode("utf-8")).hexdigest(),
            "probe_argv": probe_argv,
            "image_digest": launch.image_digest,
            "runtime_id": launch.runtime_id,
            "profile_id": launch.profile_id,
            "measured": model.measured,
            "assets": [{"role": asset.role, "path": asset.path, "sha256": asset.sha256} for asset in model.assets],
        }
        swap_models[model_id] = {"cmd": " ".join(shlex.quote(token) for token in probe_argv)}
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (destination / "scheduler-v2.json").write_bytes(config_bytes)
    (destination / "llama-swap.yaml").write_text(
        yaml.safe_dump({"healthCheckTimeout": 900, "logLevel": "info", "models": swap_models}, sort_keys=False),
        encoding="utf-8",
    )
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


def render_v3(*, candidate_path: str | Path, evidence_dir: str | Path | None, output: str | Path, mode: str,
              model_directory: str | Path, container_runtime: str = "nvidia",
              temporary_budget_bytes: int | None = None, now: Any | None = None) -> dict[str, Any]:
    """Render a v3 deployment from one frozen candidate (P26).

    A production render first verifies the evidence offline; if it does not
    verify, nothing is written at all (no launchable directory is produced).
    A lab render is allowed without evidence but is marked non-production in the
    manifest and by a `NOT-PRODUCTION` file. The model directory is an explicit
    input: no old model-disk location is hardcoded anywhere.
    """
    from dataclasses import asdict

    from .contracts_v2 import DeploymentSpec
    from .evidence_contracts import candidate_digest, device_digest, parse_candidate
    from .preflight_v3 import manifest_identity
    from .runtime_profiles import render_container_launch

    if mode not in ("production", "lab"):
        raise DeployError("the render mode must be production or lab")
    try:
        candidate = parse_candidate(json.loads(Path(candidate_path).read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ContractError) as exc:
        raise DeployError(f"cannot read the candidate: {exc}") from exc
    target = Path(output)
    if target.exists() and any(target.iterdir()):
        raise DeployError("refusing to render into a non-empty directory")

    evidence: dict[str, Any] | None = None
    if mode == "production":
        from .acceptance.verify import verify_evidence

        if evidence_dir is None:
            raise DeployError("a production render requires --evidence: an unverified candidate is never rendered")
        code, document = verify_evidence(candidate_path=Path(candidate_path), evidence_dir=Path(evidence_dir), now=now)
        if code != 0:
            raise DeployError(f"the evidence does not verify offline (exit {code}): {document.get('error')}")
        report_path = Path(evidence_dir) / "report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        evidence = {"run_id": report["run_id"], "started_at": report["started_at"], "ended_at": report["ended_at"],
                    "report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest()}
    elif isinstance(temporary_budget_bytes, bool) or not isinstance(temporary_budget_bytes, int) \
            or temporary_budget_bytes <= 0:
        raise DeployError("a lab render requires an explicit --temporary-budget-bytes")

    registration = DeploymentSpec(runtimes=tuple(candidate.runtimes), models=tuple(candidate.models))
    deployment_id = f"sms-{candidate.deployment_id}"
    models: dict[str, Any] = {}
    for model in candidate.models:
        launch = render_container_launch(registration, model.model_id, deployment_id=candidate.deployment_id,
                                         model_directory=str(model_directory), config_sha256=candidate.config_sha256,
                                         mode=mode, container_runtime=container_runtime,
                                         temporary_budget_bytes=temporary_budget_bytes)
        models[model.model_id] = {"container_name": launch.container_name, "image_digest": launch.image_digest,
                                  "runtime_id": launch.runtime_id, "profile_id": launch.profile_id,
                                  "port": launch.port, "argv": list(launch.argv), "labels": dict(launch.labels),
                                  "assets": [asdict(asset) for asset in model.assets]}
    manifest: dict[str, Any] = {
        "schema_version": 3, "mode": mode, "production": mode == "production", "deployment_id": candidate.deployment_id,
        "candidate_sha256": candidate_digest(candidate), "source_archive_sha256": candidate.source_archive_sha256,
        "config_sha256": candidate.config_sha256, "device_digest": device_digest(candidate.device),
        "device": asdict(candidate.device), "runtime_stack": asdict(candidate.runtime_stack),
        "model_directory": str(model_directory), "container_prefix": deployment_id,
        "models": models, "evidence": evidence,
    }
    manifest["identity_sha256"] = manifest_identity(manifest)
    target.mkdir(parents=True, exist_ok=True)
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if mode == "lab":
        (target / "NOT-PRODUCTION").write_text("lab rendering: this directory is not a production deployment\n",
                                               encoding="utf-8")
    return {"ok": True, "output": str(target), "mode": mode, "candidate_sha256": manifest["candidate_sha256"],
            "models": sorted(models), "production": manifest["production"]}


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


SERVICE_TEMPLATES = ("model-scheduler.service.in", "llama-swap.service.in")
FORBIDDEN_UNIT_NAMES = ("video", "media", "transcode", "av1", "nvenc")
_SOCKET_MODE = "0660"
_UUIDISH = re.compile(r"[0-9A-Fa-f][0-9A-Fa-f-]{7,}\Z")


@dataclass(frozen=True)
class ServiceInputs:
    """Every value the service units are rendered from (no site path is hardcoded)."""

    service_user: str
    service_group: str
    client_uid: int
    client_group: str
    socket_path: str
    model_mount: str
    model_directory: str
    mount_unit: str
    blob_root: str
    blob_disk_uuid: str
    blob_quota_bytes: int
    release_root: str
    config_path: str
    swap_config_path: str
    listen: str = "127.0.0.1:8080"
    allow_cidr: str = "127.0.0.1/32"
    allow_public: bool = False
    scheduler_port: int = 8090
    socket_mode: str = _SOCKET_MODE
    video_unit: bool = False

    def validate(self) -> None:
        if self.video_unit:
            raise DeployError("this deployment never renders a video unit: the video project has its own release flow")
        for label, value in (("model_mount", self.model_mount), ("model_directory", self.model_directory),
                             ("release_root", self.release_root), ("config_path", self.config_path),
                             ("swap_config_path", self.swap_config_path), ("socket_path", self.socket_path)):
            if not isinstance(value, str) or not value.startswith("/"):
                raise DeployError(f"{label} must be an absolute path")
        if self.socket_mode != _SOCKET_MODE:
            raise DeployError(f"the control socket must be {_SOCKET_MODE}; it carries the client group contract")
        if isinstance(self.client_uid, bool) or not isinstance(self.client_uid, int) or self.client_uid <= 0:
            raise DeployError("client_uid must be a positive integer")
        if not self.client_group or not self.service_user or not self.service_group:
            raise DeployError("service user/group and client group are required")
        if not _UUIDISH.fullmatch(self.blob_disk_uuid):
            raise DeployError("blob_disk_uuid must be a filesystem UUID: the blob store never lands on an unverified disk")
        if isinstance(self.blob_quota_bytes, bool) or not isinstance(self.blob_quota_bytes, int) \
                or self.blob_quota_bytes <= 0:
            raise DeployError("blob_quota_bytes must be a positive integer")
        for name in FORBIDDEN_UNIT_NAMES:
            if name in Path(self.swap_config_path).name.lower() or name in Path(self.config_path).name.lower():
                raise DeployError(f"the configuration names a {name!r} unit: this deployment renders none")
        networks = [item.strip() for item in self.allow_cidr.split(",") if item.strip()]
        if not networks:
            raise DeployError("allow_cidr must name the network the compat port opens to")
        for item in networks:
            try:
                network = ipaddress.ip_network(item, strict=False)
            except ValueError as exc:
                raise DeployError(f"allow_cidr must be a network the deployment names: {exc}") from exc
            if network.num_addresses == 0:
                raise DeployError("allow_cidr must name at least one address")
            if item == "0.0.0.0/0" and not self.allow_public:
                raise DeployError("allow_cidr 0.0.0.0/0 needs allow_public=True: the internet is not a "
                                  "deployment input")
        if isinstance(self.scheduler_port, bool) or not isinstance(self.scheduler_port, int) \
                or not 1 <= self.scheduler_port <= 65535:
            raise DeployError("scheduler_port must be a port number")


def render_service_units(*, inputs: ServiceInputs, output: str | Path,
                         template_root: str | Path | None = None) -> dict[str, Any]:
    """Render the scheduler and llama-swap units plus the sudoers rule (P27).

    Every site value comes from `inputs`; the model directory is mounted
    read-only and the control socket contract (0660 + client group) is recorded
    in the unit environment. No video/media unit is ever produced.
    """
    inputs.validate()
    target = Path(output)
    if target.exists() and any(target.iterdir()):
        raise DeployError("refusing to render service units into a non-empty directory")
    templates = Path(template_root) if template_root is not None else Path(__file__).resolve().parent.parent / "deploy"
    stray = [path.name for path in templates.iterdir()
             if path.name.endswith(".service.in") and any(word in path.name.lower() for word in FORBIDDEN_UNIT_NAMES)]
    if stray:
        raise DeployError(f"a forbidden unit template is present: {', '.join(sorted(stray))}")

    rendered: dict[str, str] = {}
    for name in SERVICE_TEMPLATES:
        path = templates / name
        if not path.is_file():
            raise DeployError(f"the service template {name} is missing")
        text = path.read_text(encoding="utf-8")
        text = text.replace("@SSD_MOUNT_UNIT@", inputs.mount_unit)
        text = text.replace("/mnt/model-ssd", inputs.model_mount)
        text = text.replace("/opt/self-model-switch/current", f"{inputs.release_root}/current")
        text = text.replace("/etc/self-model-switch/config.yaml", inputs.config_path)
        text = text.replace("/etc/self-model-switch/llama-swap.yaml", inputs.swap_config_path)
        text = text.replace("User=model-scheduler", f"User={inputs.service_user}")
        text = text.replace("Group=model-scheduler", f"Group={inputs.service_group}")
        text = text.replace("RuntimeDirectory=model-scheduler self-model-switch",
                            f"RuntimeDirectory={inputs.service_user} {Path(inputs.socket_path).parent.name}")
        text = text.replace("127.0.0.1:8080", inputs.listen)
        extras = [
            # [Unit] already carries RequiresMountsFor (rewritten below with the site mount
            # path); systemd-analyze rejects a second copy inside [Service].
            f"ReadOnlyPaths={inputs.model_directory}",
            f"Environment=SMS_CONTROL_SOCKET={inputs.socket_path}",
            f"Environment=SMS_CONTROL_SOCKET_MODE={inputs.socket_mode}",
            f"Environment=SMS_CLIENT_UID={inputs.client_uid}",
            f"Environment=SMS_CLIENT_GROUP={inputs.client_group}",
            f"Environment=SMS_BLOB_ROOT={inputs.blob_root}",
            f"Environment=SMS_BLOB_DISK_UUID={inputs.blob_disk_uuid}",
            f"Environment=SMS_BLOB_QUOTA_BYTES={inputs.blob_quota_bytes}",
            # The compat surface is reachable only from the network the deployment
            # names; the rule is added idempotently and never touches the control socket.
            f"Environment=SMS_ALLOW_CIDR={inputs.allow_cidr}",
            f"Environment=SMS_SCHEDULER_PORT={inputs.scheduler_port}",
        ]
        marker = "\n[Install]"
        text = text.replace(marker, "\n" + "\n".join(extras) + marker) if marker in text else text + "\n" + "\n".join(extras) + "\n"
        rendered[name.removesuffix(".in")] = text

    sudoers = templates / "sudoers.model-scheduler"
    if not sudoers.is_file():
        raise DeployError("the sudoers template is missing")
    rendered["sudoers.model-scheduler"] = sudoers.read_text(encoding="utf-8").replace("model-scheduler",
                                                                                      inputs.service_user)

    target.mkdir(parents=True, exist_ok=True)
    for name, text in sorted(rendered.items()):
        path = target / name
        if any(word in name.lower() for word in FORBIDDEN_UNIT_NAMES):
            raise DeployError(f"refusing to write a forbidden unit {name!r}")
        path.write_text(text, encoding="utf-8")
        if name == "sudoers.model-scheduler":
            path.chmod(0o440)
    facts = {"schema_version": 1, "service_user": inputs.service_user, "service_group": inputs.service_group,
             "client_uid": inputs.client_uid, "client_group": inputs.client_group,
             "socket_path": inputs.socket_path, "socket_mode": inputs.socket_mode,
             "model_mount": inputs.model_mount, "model_directory": inputs.model_directory,
             "mount_unit": inputs.mount_unit, "blob_root": inputs.blob_root,
             "blob_disk_uuid": inputs.blob_disk_uuid, "blob_quota_bytes": inputs.blob_quota_bytes,
             "release_root": inputs.release_root, "allow_cidr": inputs.allow_cidr,
             "allow_public": inputs.allow_public, "scheduler_port": inputs.scheduler_port, "video_units": 0}
    (target / "service-facts.json").write_text(json.dumps(facts, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"ok": True, "output": str(target), "units": [name for name in sorted(rendered)],
            "socket_mode": inputs.socket_mode, "video_units": 0}


class OpsPort(Protocol):
    """The site operations the switch/rollback sequences are allowed to perform."""

    def current_release(self) -> str | None: ...

    def close_admission(self) -> Mapping[str, Any]: ...

    def drain(self) -> Mapping[str, Any]: ...

    def prove_instances_stopped(self) -> Mapping[str, Any]: ...

    def preflight_release(self, release: str) -> Mapping[str, Any]: ...

    def switch_current(self, release: str) -> Mapping[str, Any]: ...

    def start(self) -> Mapping[str, Any]: ...

    def smoke(self) -> Mapping[str, Any]: ...

    def reopen_admission(self) -> Mapping[str, Any]: ...

    def restore_blob_metadata(self, backup: str) -> Mapping[str, Any]: ...


def _ops_step(document: list[dict], failures: list[str], label: str, action) -> Mapping[str, Any]:
    try:
        result = dict(action() or {})
    except Exception as exc:  # noqa: BLE001 - a crashed step blocks the sequence, it never passes it
        failures.append(f"{label}: {type(exc).__name__}: {exc}")
        document.append({"step": label, "ok": False, "error": str(exc)})
        return {}
    document.append({"step": label, "ok": True, "facts": result})
    return result


def switch_release(*, ops: OpsPort, release: str, metadata_backup: str | None = None) -> dict[str, Any]:
    """admission closed → drained → stopped proven → preflight → switch → start → smoke.

    `current` is switched **only** after the old instances are proven stopped; a
    failure before that point leaves the running release untouched.
    """
    steps: list[dict] = []
    failures: list[str] = []
    previous = ops.current_release()
    admission = _ops_step(steps, failures, "close_admission", ops.close_admission)
    if admission.get("closed") is not True:
        failures.append("admission was not closed")
    drained = _ops_step(steps, failures, "drain", ops.drain)
    for key in ("queue_depth", "leases", "sessions"):
        if drained.get(key) != 0:
            failures.append(f"the drain still reports {key}={drained.get(key)!r}")
    stopped = _ops_step(steps, failures, "prove_instances_stopped", ops.prove_instances_stopped)
    if stopped.get("stopped") is not True:
        failures.append("the running instances were not proven stopped: current is not switched")
    preflight = _ops_step(steps, failures, "preflight_release", lambda: ops.preflight_release(release))
    if preflight.get("accepted") is not True:
        failures.append(f"the new release {release!r} did not pass preflight")
    if failures:
        _ops_step(steps, [], "reopen_admission", ops.reopen_admission)
        return {"ok": False, "switched": False, "previous_release": previous, "steps": steps,
                "problems": failures, "repair_required": False}

    switched = _ops_step(steps, failures, "switch_current", lambda: ops.switch_current(release))
    started = _ops_step(steps, failures, "start", ops.start)
    smoke = _ops_step(steps, failures, "smoke", ops.smoke)
    problems: list[str] = list(failures)
    if switched.get("current") != release:
        problems.append("current does not point at the new release")
    if started.get("running") is not True:
        problems.append("the scheduler did not start")
    if smoke.get("ok") is not True:
        problems.append("the smoke check failed")
    if problems:
        # the switch already happened: the site is left in a known, repairable state
        return {"ok": False, "switched": True, "previous_release": previous, "release": release, "steps": steps,
                "problems": problems, "repair_required": True, "rollback_release": previous,
                "metadata_backup": metadata_backup}
    _ops_step(steps, [], "reopen_admission", ops.reopen_admission)
    return {"ok": True, "switched": True, "previous_release": previous, "release": release, "steps": steps,
            "problems": [], "repair_required": False}


def rollback_release(*, ops: OpsPort, accepted_release: str | None, metadata_backup: str | None,
                     downgrade_compatible: bool) -> dict[str, Any]:
    """Restore the accepted release (and compatible metadata) or stop for repair.

    Without an accepted older candidate the site does **not** guess one: it stops
    and asks for a repair. Metadata is restored from the backup taken before the
    upgrade whenever the downgrade cannot read the upgraded format.
    """
    if accepted_release is None:
        stopped = _ops_step([], [], "stop_for_repair", ops.close_admission)
        return {"ok": False, "rolled_back": False, "status": "stopped_for_repair",
                "problems": ["no accepted older candidate exists: the site stops and is repaired by hand"],
                "admission": stopped}
    steps: list[dict] = []
    failures: list[str] = []
    _ops_step(steps, failures, "close_admission", ops.close_admission)
    _ops_step(steps, failures, "prove_instances_stopped", ops.prove_instances_stopped)
    if not downgrade_compatible:
        if metadata_backup is None:
            failures.append("the downgrade is not metadata-compatible and no pre-upgrade backup exists")
        else:
            restored = _ops_step(steps, failures, "restore_blob_metadata",
                                 lambda: ops.restore_blob_metadata(metadata_backup))
            if restored.get("restored") is not True:
                failures.append("the blob metadata backup was not restored")
    if failures:
        # an incompatible downgrade without its backup must not touch the running release
        return {"ok": False, "rolled_back": False, "release": accepted_release, "steps": steps,
                "problems": failures, "status": "repair_required"}
    switched = _ops_step(steps, failures, "switch_current", lambda: ops.switch_current(accepted_release))
    started = _ops_step(steps, failures, "start", ops.start)
    smoke = _ops_step(steps, failures, "smoke", ops.smoke)
    if switched.get("current") != accepted_release:
        failures.append("current does not point at the accepted release")
    if started.get("running") is not True:
        failures.append("the restored scheduler did not start")
    if smoke.get("ok") is not True:
        failures.append("the smoke check after the rollback failed")
    return {"ok": not failures, "rolled_back": not failures, "release": accepted_release, "steps": steps,
            "problems": failures, "status": "running" if not failures else "repair_required"}


def _is_v3_manifest(manifest_path: str | Path) -> bool:
    try:
        document = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(document, dict) and document.get("schema_version") == 3


def preflight_v3_command(manifest_path: str | Path, *, candidate: str | None, evidence: str | None) -> tuple[int, dict]:
    """Route a v3 manifest to layer 1, or to the full gate when evidence is supplied."""
    from .preflight_v3 import PreflightError, production_gate, verify_environment

    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return 2, {"ok": False, "error": f"cannot read the manifest: {exc}"}
    site = live_site(model_directory=manifest.get("model_directory"))
    try:
        if evidence is not None:
            if candidate is None:
                return 2, {"ok": False, "error": "--evidence requires --candidate for the production gate"}
            document = production_gate(manifest, candidate_path=Path(candidate), evidence_dir=Path(evidence), site=site)
        else:
            document = verify_environment(manifest, site=site)
    except PreflightError as exc:
        return exc.exit_code, {"ok": False, "layer": exc.layer, "error": str(exc)}
    return (0 if document["ok"] else 3), document


def live_site(*, model_directory: str | None, device_tree_root: Path = Path("/proc/device-tree"),
              image_checker: Any | None = None, model_files: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Read the live site facts the preflight compares against (no model is loaded)."""
    try:
        site = dict(_read_hardware_identity(device_tree_root))
    except DeployError as exc:
        # the preflight still runs and reports the mismatch instead of crashing;
        # nothing here ever loads a model.
        site = {"device_tree_unavailable": str(exc)}
    site["filesystem"] = "ext4"
    site["model_files"] = dict(model_files or {})
    site["images"] = {}
    site["model_directory"] = model_directory
    if image_checker is not None:
        site["image_checker"] = image_checker
    return site


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(); sub = parser.add_subparsers(dest="command", required=True)
    render_parser = sub.add_parser("render"); render_parser.add_argument("--input"); render_parser.add_argument("--config"); render_parser.add_argument("--candidate"); render_parser.add_argument("--evidence"); render_parser.add_argument("--model-directory", dest="model_directory"); render_parser.add_argument("--temporary-budget-bytes", dest="temporary_budget_bytes", type=int, default=None); render_parser.add_argument("--mode", choices=("lab", "production"), required=True); render_parser.add_argument("--output", required=True); render_parser.add_argument("--deployment-id", dest="deployment_id"); render_parser.add_argument("--container-runtime", default="nvidia", dest="container_runtime")
    preflight_parser = sub.add_parser("preflight"); preflight_parser.add_argument("--manifest", required=True); preflight_parser.add_argument("--candidate"); preflight_parser.add_argument("--evidence")
    collect_parser = sub.add_parser("collect"); collect_parser.add_argument("--output", required=True)
    migrate_parser = sub.add_parser("migrate"); migrate_parser.add_argument("--input", required=True); migrate_parser.add_argument("--output", required=True)
    migrate_v2_parser = sub.add_parser("migrate-v2"); migrate_v2_parser.add_argument("--input", required=True); migrate_v2_parser.add_argument("--inventory", required=True); migrate_v2_parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "render" and args.candidate:
            try:
                if not args.model_directory:
                    raise DeployError("--model-directory is required when rendering a v3 candidate: the site path is "
                                      "never guessed")
                result = render_v3(candidate_path=args.candidate, evidence_dir=args.evidence, output=args.output,
                                   mode=args.mode, model_directory=args.model_directory,
                                   container_runtime=args.container_runtime,
                                   temporary_budget_bytes=args.temporary_budget_bytes)
            except DeployError as exc:
                # the v3 commands follow the §5 exit codes: 2 for input/material errors
                print(f"deployment error: {exc}", file=sys.stderr)
                return 2
            print(json.dumps(result, sort_keys=True))
        elif args.command == "preflight" and _is_v3_manifest(args.manifest):
            code, document = preflight_v3_command(args.manifest, candidate=args.candidate, evidence=args.evidence)
            print(json.dumps(document, sort_keys=True))
            return code
        elif args.command == "render" and args.config:
            if not args.deployment_id:
                raise DeployError("--deployment-id is required when rendering a schema-v2 configuration")
            manifest = render_lab(args.config, args.output, deployment_id=args.deployment_id, container_runtime=args.container_runtime, mode=args.mode)
            print(json.dumps({"ok": True, "output": args.output, "mode": manifest["mode"], "models": sorted(manifest["models"])}, sort_keys=True))
        elif args.command == "render":
            if not args.input:
                raise DeployError("--input is required when rendering a legacy deployment input")
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
