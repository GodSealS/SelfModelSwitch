"""Supported single-worker process entry point."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

import uvicorn

from app import create_app
from model_scheduler.config import ConfigError, load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path)
    parser.add_argument("--check-config", action="store_true")
    args = parser.parse_args(argv)
    path = args.config or Path(os.environ.get("MODEL_SCHEDULER_CONFIG", "config.yaml"))
    try:
        config = load_config(path)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 78
    if args.check_config:
        print(f"schema_version={config.schema_version} models={','.join(sorted(config.models))}")
        return 0
    uvicorn.run(create_app(path), host=config.server.host, port=config.server.port, workers=1, reload=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
