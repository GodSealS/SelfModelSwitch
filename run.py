"""Supported single-worker process entry point."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

import uvicorn

from app import create_app
from model_scheduler.config import ConfigError, load_config
from model_scheduler.instance_lock import InstanceLocked, acquire
from model_scheduler.runtime import RuntimeCompositionError, build_backend


_MANIFEST_PATH = Path("/etc/self-model-switch/manifest.json")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path)
    parser.add_argument("--check-config", action="store_true")
    args = parser.parse_args(argv)
    path = args.config or Path(os.environ.get("MODEL_SCHEDULER_CONFIG", "config.yaml"))
    try:
        config_bytes = path.read_bytes()
        config = load_config(path)
        if path.read_bytes() != config_bytes:
            raise ConfigError("configuration changed while loading")
    except (ConfigError, OSError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 78
    if args.check_config:
        print(f"schema_version={config.schema_version} models={','.join(sorted(config.models))}")
        return 0
    try:
        try:
            from model_scheduler.llama_swap_contract import CONTROL_CONTRACT
        except ImportError:
            print("configuration error: fixed llama-swap control contract is not installed", file=sys.stderr)
            return 78
        config_digest = hashlib.sha256(config_bytes).hexdigest()
        manifest = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
        backend = build_backend(config, manifest, config_digest, CONTROL_CONTRACT)
        with acquire(Path("/run/model-scheduler/scheduler.lock")):
            uvicorn.run(create_app(path, config=config, backend=backend), host=config.server.host, port=config.server.port, workers=1, reload=False)
    except (OSError, ValueError, json.JSONDecodeError, RuntimeCompositionError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 78
    except InstanceLocked:
        print("scheduler instance already running", file=sys.stderr)
        return 73
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
