"""Narrow implementation behind the root-owned no-argument recovery helper."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Callable


class RecoveryHelper:
    def __init__(self, manifest: str | Path, *, runner: Callable[[list[str]], str] | None = None):
        self.manifest = Path(manifest)
        self.runner = runner or self._run

    @staticmethod
    def _run(argv: list[str]) -> str:
        return subprocess.check_output(argv, text=True, timeout=90)

    def recover(self) -> dict[str, object]:
        try:
            data = json.loads(self.manifest.read_text(encoding="utf-8"))
            deployment_id, models = data["deployment_id"], data["models"]
            if not isinstance(deployment_id, str) or not isinstance(models, dict): raise ValueError("invalid manifest")
            self.runner(["systemctl", "stop", "llama-swap.service"])
            stopped = []
            for model_id in sorted(models):
                name = models[model_id].get("container_name")
                if not isinstance(name, str) or not name.startswith(f"sms-{deployment_id}-"): raise ValueError("unsafe container name")
                self.runner(["docker", "stop", "--time", "30", name])
                stopped.append(model_id)
            self.runner(["systemctl", "start", "llama-swap.service"])
            return {"ok": True, "phase": "complete", "error_code": None, "stopped_models": stopped}
        except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.SubprocessError):
            return {"ok": False, "phase": "failed", "error_code": "control_recovery_failed", "stopped_models": []}
