"""Deterministic deployment-input validation and manifest rendering."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any

import yaml


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


def _require(value: Any, name: str) -> str:
    if type(value) is not str or not value or "REQUIRED" in value:
        raise DeployError(f"{name} is required")
    return value


def _validate_report(path_value: Any, data: dict[str, Any]) -> None:
    if not isinstance(path_value, str) or not path_value:
        raise DeployError("production requires validation_report")
    try:
        report = json.loads(Path(path_value).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeployError("cannot read validation report") from exc
    if not isinstance(report, dict) or report.get("image") != data["image"] or not isinstance(report.get("models"), dict):
        raise DeployError("validation report does not match deployment input")
    for model_id, model in data["models"].items():
        measured = report["models"].get(model_id)
        expected = {"sha256": model["sha256"], "context_size": model["context_size"], "parallel": model["parallel"]}
        if not isinstance(measured, dict) or any(measured.get(key) != value for key, value in expected.items()):
            raise DeployError("validation report does not match deployment input")


def validate(data: Any, mode: str) -> dict[str, Any]:
    if mode not in {"lab", "production"} or not isinstance(data, dict):
        raise DeployError("invalid deployment input")
    required = {"deployment_id", "ssd_uuid", "ssd_filesystem", "jetpack_version", "llama_swap_version", "llama_swap_sha256", "image", "validation_report", "models"}
    if set(data) != required or data["ssd_filesystem"] != "ext4": raise DeployError("invalid deployment fields")
    for key in required - {"validation_report", "models", "ssd_filesystem"}: _require(data[key], key)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", data["deployment_id"]):
        raise DeployError("invalid deployment_id")
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
    manifest = json.loads(json.dumps(validated))
    for model_id in _MODELS:
        manifest["models"][model_id]["container_name"] = f"sms-{manifest['deployment_id']}-{model_id}"
    if destination.exists() and any(destination.iterdir()): raise DeployError("output directory must be empty")
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (destination / "config.yaml").write_text(yaml.safe_dump(_scheduler_config(manifest), sort_keys=False), encoding="utf-8")
    (destination / "llama-swap.yaml").write_text(yaml.safe_dump(_llama_swap_config(manifest), sort_keys=False), encoding="utf-8")
    (destination / "fstab.fragment").write_text(f"UUID={manifest['ssd_uuid']} /mnt/model-ssd ext4 defaults,nofail,x-systemd.device-timeout=10s 0 2\n", encoding="utf-8")
    (destination / "model-scheduler.service").write_text(_render_unit("model-scheduler.service"), encoding="utf-8")
    (destination / "llama-swap.service").write_text(_render_unit("llama-swap.service"), encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(); sub = parser.add_subparsers(dest="command", required=True)
    render_parser = sub.add_parser("render"); render_parser.add_argument("--input", required=True); render_parser.add_argument("--mode", choices=("lab", "production"), required=True); render_parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try: render(args.input, args.mode, args.output)
    except DeployError as exc: print(f"deployment error: {exc}", file=sys.stderr); return 78
    return 0


if __name__ == "__main__": raise SystemExit(main())
