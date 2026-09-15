#!/usr/bin/env python3
"""Root-installed manifest-only Docker runner for llama-swap commands.

The command accepts only ``start|stop`` plus one fixed model id.  It never takes
an image, path, container, or shell command from its caller.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from model_scheduler.config import ConfigError, load_config
from model_scheduler.model_runner import RunnerError, docker_run_argv, docker_stop_argv, require_manifest_config_digest, require_storage_ready, run_child_with_signal_forwarding
from model_scheduler.storage_monitor import StorageMonitor


MANIFEST = Path("/etc/self-model-switch/manifest.json")
CONFIG = Path("/etc/self-model-switch/config.yaml")
MODELS = frozenset({"embedding", "reranker", "qwen-small", "qwen-large"})


def _manifest() -> dict[str, object]:
    try:
        payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunnerError("manifest unavailable") from exc
    if not isinstance(payload, dict):
        raise RunnerError("invalid manifest")
    return payload


def _config_digest() -> str:
    try:
        return hashlib.sha256(Path("/etc/self-model-switch/config.yaml").read_bytes()).hexdigest()
    except OSError as exc:
        raise RunnerError("scheduler config unavailable") from exc


def _verify_storage() -> None:
    try:
        config = load_config(CONFIG)
    except ConfigError as exc:
        raise RunnerError("scheduler config unavailable") from exc
    monitor = StorageMonitor(
        config.storage.mount_path,
        config.storage.model_directory,
        config.storage.expected_uuid,
        config.storage.filesystem,
    )
    require_storage_ready(monitor, config.models)


def _stop(name: str, deployment_id: str, model_id: str) -> int:
    inspect = subprocess.run(["docker", "inspect", name], capture_output=True, text=True, timeout=10)
    if inspect.returncode:
        return 0
    try:
        record = json.loads(inspect.stdout)[0]
        labels = record["Config"]["Labels"]
    except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RunnerError("cannot verify container labels") from exc
    if (labels.get("io.self-model-switch.deployment") != deployment_id
            or labels.get("io.self-model-switch.model") != model_id):
        raise RunnerError("refusing to stop unmanaged container")
    return subprocess.run(["docker", "stop", "--time", "30", name], timeout=45).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("start", "stop"))
    parser.add_argument("model_id", choices=sorted(MODELS))
    args = parser.parse_args(argv)
    try:
        manifest = _manifest()
        models = manifest.get("models")
        if not isinstance(models, dict) or not isinstance(models.get(args.model_id), dict):
            raise RunnerError("model missing from manifest")
        name = models[args.model_id].get("container_name")
        if not isinstance(name, str):
            raise RunnerError("invalid manifest container")
        if args.action == "stop":
            command = docker_stop_argv(name, name)
            if command is None:
                raise RunnerError("invalid manifest container")
            deployment_id = manifest.get("deployment_id")
            if not isinstance(deployment_id, str):
                raise RunnerError("invalid manifest deployment")
            return _stop(name, deployment_id, args.model_id)
        _verify_storage()
        config_digest = _config_digest()
        require_manifest_config_digest(manifest, config_digest)
        return run_child_with_signal_forwarding(docker_run_argv(manifest, args.model_id, config_digest))
    except (OSError, RunnerError, subprocess.SubprocessError) as exc:
        print(f"model runner error: {exc}", file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
