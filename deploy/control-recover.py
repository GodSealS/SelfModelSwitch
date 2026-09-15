#!/usr/bin/env python3
"""Root-owned, no-argument control-plane recovery entrypoint."""
from __future__ import annotations

import fcntl
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from model_scheduler.recovery_helper import RecoveryHelper


LOCK = Path("/run/model-scheduler/control-recover.lock")
MANIFEST = Path("/etc/self-model-switch/manifest.json")


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv:
        print("control recovery accepts no arguments", file=sys.stderr)
        return 64
    try:
        LOCK.parent.mkdir(parents=True, exist_ok=True)
        with LOCK.open("a+", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print(json.dumps({"ok": False, "phase": "already_running", "error_code": "control_recovery_busy", "stopped_models": []}))
                return 1
            result = RecoveryHelper(MANIFEST).recover()
    except OSError:
        result = {"ok": False, "phase": "failed", "error_code": "control_recovery_failed", "stopped_models": []}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
