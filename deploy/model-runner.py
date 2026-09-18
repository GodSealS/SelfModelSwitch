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
from model_scheduler.control_protocol_v1 import Fence
from model_scheduler.model_runner import RunnerError, SupervisedLaunch, docker_run_argv, docker_stop_argv, require_container_identity, require_manifest_config_digest, require_storage_ready, run_child_with_signal_forwarding
from model_scheduler.runtime import DeployError, lab_launch_argv, load_lab_manifest
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


def _stop(name: str, deployment_id: str, model_id: str, config_sha256: str) -> int:
    inspect = subprocess.run(["docker", "inspect", name], capture_output=True, text=True, timeout=10)
    if inspect.returncode:
        return 0
    try:
        record = json.loads(inspect.stdout)[0]
        labels = record["Config"]["Labels"]
    except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RunnerError("cannot verify container labels") from exc
    require_container_identity(labels, deployment_id, model_id, config_sha256)
    return subprocess.run(["docker", "stop", "--time", "30", name], timeout=45).returncode


def _require_v3_identity(model_id: str) -> None:
    """A v3 manifest must verify its own identity before anything is started (P26).

    The identity block covers the candidate/source/config/device digests and each
    model's pinned image digest, so a replaced image or an edited manifest is
    refused here instead of being launched.
    """
    try:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunnerError("manifest unavailable") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 3:
        return
    try:
        from model_scheduler.preflight_v3 import PreflightError, require_manifest_identity
    except ImportError as exc:  # pragma: no cover - a v3 manifest without the module cannot be trusted
        raise RunnerError("the v3 preflight module is not installed") from exc
    try:
        require_manifest_identity(manifest, model_id=model_id)
    except PreflightError as exc:
        raise RunnerError(str(exc)) from exc


def _require_pinned_contract() -> None:
    """A lab start refuses to run without the measured llama-swap contract (P06a)."""
    try:
        from model_scheduler.llama_swap_contract import CONTROL_CONTRACT  # noqa: F401
    except ImportError as exc:
        raise RunnerError("the pinned llama-swap control contract is not installed") from exc


def _lab_exec(argv: list[str], temporary_budget_bytes: int, manifest: dict[str, object]) -> int:
    """Run one lab launch through the observable supervised launcher (P06)."""
    del temporary_budget_bytes, manifest
    launch = SupervisedLaunch(argv, Fence("lab-boot", "lab", 1, "lab-launch-1", None, None))
    launch.start()
    exit_code = launch.wait(timeout=900.0)
    if exit_code is None:
        launch.terminate()
        exit_code = launch.wait(timeout=60.0)
    return 0 if exit_code is None else exit_code


def _lab_start(
    manifest_path: Path,
    model_id: str,
    *,
    temporary_budget_bytes: int | None,
    config_sha256: str | None,
) -> int:
    """Isolated maintenance start: explicit temporary budget and identity checks only."""
    if not isinstance(temporary_budget_bytes, int) or isinstance(temporary_budget_bytes, bool) or temporary_budget_bytes <= 0:
        raise RunnerError("a lab start requires --temporary-budget-bytes")
    _require_pinned_contract()
    try:
        manifest = load_lab_manifest(manifest_path, config_path=Path(manifest_path).parent / "scheduler-v2.json")
    except DeployError as exc:
        raise RunnerError(str(exc)) from exc
    if config_sha256 is not None and manifest.get("config_sha256") != config_sha256:
        raise RunnerError("the lab manifest does not match the supplied config digest")
    entry = manifest["models"].get(model_id)
    if not isinstance(entry, dict):
        raise RunnerError("model missing from the lab manifest")
    if entry.get("container_name") != f"sms-{manifest['deployment_id']}-{model_id}":
        raise RunnerError("invalid lab manifest container")
    return _lab_exec(lab_launch_argv(manifest, model_id), temporary_budget_bytes, manifest)


def _lab_stop(manifest_path: Path, model_id: str) -> int:
    try:
        manifest = load_lab_manifest(manifest_path, config_path=Path(manifest_path).parent / "scheduler-v2.json")
    except DeployError as exc:
        raise RunnerError(str(exc)) from exc
    entry = manifest["models"].get(model_id)
    if not isinstance(entry, dict):
        raise RunnerError("model missing from the lab manifest")
    return _stop(entry["container_name"], manifest["deployment_id"], model_id, manifest["config_sha256"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("start", "stop"))
    parser.add_argument("model_id")
    parser.add_argument("--lab-manifest", dest="lab_manifest", default=None)
    parser.add_argument("--temporary-budget-bytes", dest="temporary_budget_bytes", type=int, default=None)
    parser.add_argument("--config-sha256", dest="config_sha256", default=None)
    args = parser.parse_args(argv)
    if args.lab_manifest is None and args.model_id not in MODELS:
        parser.error("the legacy entry only accepts the fixed model ids")
    if args.lab_manifest is not None and args.action == "start" and args.temporary_budget_bytes is None:
        parser.error("a lab start requires --temporary-budget-bytes")
    try:
        if args.lab_manifest is not None:
            manifest_path = Path(args.lab_manifest)
            if args.action == "start":
                return _lab_start(
                    manifest_path,
                    args.model_id,
                    temporary_budget_bytes=args.temporary_budget_bytes,
                    config_sha256=args.config_sha256,
                )
            return _lab_stop(manifest_path, args.model_id)
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
            config_sha256 = manifest.get("config_sha256")
            if not isinstance(config_sha256, str):
                raise RunnerError("invalid manifest config digest")
            require_manifest_config_digest(manifest, config_sha256)
            return _stop(name, deployment_id, args.model_id, config_sha256)
        _require_v3_identity(args.model_id)
        _verify_storage()
        config_digest = _config_digest()
        require_manifest_config_digest(manifest, config_digest)
        return run_child_with_signal_forwarding(docker_run_argv(manifest, args.model_id, config_digest))
    except (OSError, RunnerError, DeployError, subprocess.SubprocessError) as exc:
        print(f"model runner error: {exc}", file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
