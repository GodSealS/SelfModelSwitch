"""Read-only observation of a managed instance: whose container is serving a model.

A `load` case must show the instance it created, and the control API's session
view does not carry one. The deployment itself labels every container it starts
(`io.self-model-switch.deployment|model|runtime|profile|mode|config-sha256`), so
the run can *observe* which container belongs to the deployment it is testing.

Nothing here starts, stops or reconfigures anything: it lists containers by the
deployment's own labels and reads one container's identity. Docker is reached
through an injected runner, so the probe is testable and the device only appears
at the composition root.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any, Callable, Mapping, Sequence

LABEL_PREFIX = "io.self-model-switch"
DEPLOYMENT_LABEL = f"{LABEL_PREFIX}.deployment"
MODEL_LABEL = f"{LABEL_PREFIX}.model"


def _run(argv: Sequence[str]) -> str:
    result = subprocess.run(list(argv), capture_output=True, text=True, check=False, timeout=60)  # noqa: S603
    if result.returncode != 0:
        raise OSError(f"{argv[0]} exited {result.returncode}: {result.stderr.strip()[:200]}")
    return result.stdout


class ManagedInstanceProbe:
    """Which instance of this deployment is serving one model, as observed."""

    def __init__(self, deployment_id: str, *, runner: Callable[[Sequence[str]], str] = _run) -> None:
        if not isinstance(deployment_id, str) or not deployment_id:
            raise ValueError("the probe needs the deployment identity it observes")
        self.deployment_id = deployment_id
        self._run = runner

    def of(self, model_id: str) -> Mapping[str, Any] | None:
        """The container identity of this model, or None when nothing is serving it.

        None is a fact, not a failure: a model that is not loaded has no instance,
        and the case that needed one stays unattributable rather than borrowing
        another model's container.
        """
        try:
            listed = self._run(["docker", "ps", "--no-trunc",
                                "--filter", f"label={DEPLOYMENT_LABEL}={self.deployment_id}",
                                "--filter", f"label={MODEL_LABEL}={model_id}",
                                "--format", "{{.ID}}"])
        except OSError:
            return None
        container_id = next((line.strip() for line in listed.splitlines() if line.strip()), None)
        if container_id is None:
            return None
        try:
            detail = self._run(["docker", "inspect", "--format", "{{json .}}", container_id])
            document = json.loads(detail)
        except (OSError, ValueError):
            return None
        if not isinstance(document, dict):
            return None
        config = document.get("Config") if isinstance(document.get("Config"), Mapping) else {}
        state = document.get("State") if isinstance(document.get("State"), Mapping) else {}
        labels = config.get("Labels") if isinstance(config.get("Labels"), Mapping) else {}
        identity: dict[str, Any] = {"container_id": str(document.get("Id") or container_id),
                                    "image": config.get("Image"),
                                    "started_at": state.get("StartedAt"),
                                    "deployment_id": self.deployment_id, "model_id": model_id}
        runtime_id = labels.get(f"{LABEL_PREFIX}.runtime")
        if isinstance(runtime_id, str) and runtime_id:
            identity["runtime_id"] = runtime_id
        return identity
