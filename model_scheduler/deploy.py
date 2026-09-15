"""Deterministic deployment-input validation and manifest rendering."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any


class DeployError(ValueError):
    pass


_HASH = re.compile(r"[0-9a-f]{64}\Z")
_MODELS = {"embedding", "reranker", "qwen-small", "qwen-large"}


def _require(value: Any, name: str) -> str:
    if type(value) is not str or not value or "REQUIRED" in value:
        raise DeployError(f"{name} is required")
    return value


def validate(data: Any, mode: str) -> dict[str, Any]:
    if mode not in {"lab", "production"} or not isinstance(data, dict):
        raise DeployError("invalid deployment input")
    required = {"deployment_id", "ssd_uuid", "ssd_filesystem", "jetpack_version", "llama_swap_version", "llama_swap_sha256", "image", "validation_report", "models"}
    if set(data) != required or data["ssd_filesystem"] != "ext4": raise DeployError("invalid deployment fields")
    for key in required - {"validation_report", "models", "ssd_filesystem"}: _require(data[key], key)
    if not _HASH.fullmatch(data["llama_swap_sha256"]): raise DeployError("invalid llama_swap_sha256")
    if not re.fullmatch(r"[^@]+@sha256:[0-9a-f]{64}", data["image"]): raise DeployError("image must be digest pinned")
    if not isinstance(data["models"], dict) or set(data["models"]) != _MODELS: raise DeployError("invalid model set")
    for model_id, model in data["models"].items():
        if not isinstance(model, dict) or set(model) != {"file", "sha256", "context_size", "parallel", "pooling", "reserved_bytes", "measured"}: raise DeployError(f"invalid {model_id}")
        if "/" in _require(model["file"], f"{model_id}.file") or not _HASH.fullmatch(_require(model["sha256"], f"{model_id}.sha256")): raise DeployError(f"unsafe {model_id} file")
        if type(model["context_size"]) is not int or model["context_size"] <= 0 or type(model["parallel"]) is not int or not 1 <= model["parallel"] <= 16 or model["context_size"] % model["parallel"] or type(model["reserved_bytes"]) is not int or model["reserved_bytes"] <= 0: raise DeployError(f"invalid {model_id} sizing")
        if mode == "production" and model["measured"] is not True: raise DeployError(f"{model_id} must be measured for production")
    if mode == "production" and (not isinstance(data["validation_report"], str) or not data["validation_report"]): raise DeployError("production requires validation_report")
    return data


def render(source: str | Path, mode: str, output: str | Path) -> dict[str, Any]:
    path, destination = Path(source), Path(output)
    try: data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc: raise DeployError("cannot read deployment input") from exc
    manifest = validate(data, mode)
    if destination.exists() and any(destination.iterdir()): raise DeployError("output directory must be empty")
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(); sub = parser.add_subparsers(dest="command", required=True)
    render_parser = sub.add_parser("render"); render_parser.add_argument("--input", required=True); render_parser.add_argument("--mode", choices=("lab", "production"), required=True); render_parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try: render(args.input, args.mode, args.output)
    except DeployError as exc: print(f"deployment error: {exc}", file=sys.stderr); return 78
    return 0


if __name__ == "__main__": raise SystemExit(main())
