"""Safe Docker argv construction for manifest-managed model containers."""
from __future__ import annotations

import re
from typing import Any, Mapping


class RunnerError(ValueError):
    pass


_PORTS = {"embedding": 10001, "reranker": 10002, "qwen-small": 10003, "qwen-large": 10004}
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def docker_stop_argv(requested_name: str, manifest_name: str) -> list[str] | None:
    if requested_name != manifest_name or not manifest_name.startswith("sms-"):
        return None
    return ["docker", "stop", "--time", "30", manifest_name]


def docker_run_argv(manifest: Mapping[str, Any], model_id: str, config_sha256: str) -> list[str]:
    """Build the exact argv for one declared model; never accept shell fragments."""
    if model_id not in _PORTS or not _HASH.fullmatch(config_sha256):
        raise RunnerError("invalid model or config digest")
    deployment_id, image, models = manifest.get("deployment_id"), manifest.get("image"), manifest.get("models")
    if not isinstance(deployment_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", deployment_id):
        raise RunnerError("invalid deployment")
    if not isinstance(image, str) or not re.fullmatch(r"[^@]+@sha256:[0-9a-f]{64}", image) or not isinstance(models, Mapping):
        raise RunnerError("invalid image or models")
    model = models.get(model_id)
    if not isinstance(model, Mapping):
        raise RunnerError("unknown model")
    name, filename, context, parallel = (model.get(key) for key in ("container_name", "file", "context_size", "parallel"))
    expected_name = f"sms-{deployment_id}-{model_id}"
    if name != expected_name:
        raise RunnerError("invalid container name")
    if not isinstance(filename, str) or "/" in filename or filename in {"", ".", ".."}:
        raise RunnerError("invalid model file")
    if type(context) is not int or context <= 0 or type(parallel) is not int or not 1 <= parallel <= 16 or context % parallel:
        raise RunnerError("invalid model sizing")
    argv = ["docker", "run", "--name", name, "--init", "--rm", "--restart=no", "--gpus", "all", "--label", f"io.self-model-switch.deployment={deployment_id}", "--label", f"io.self-model-switch.model={model_id}", "--label", f"io.self-model-switch.config-sha256={config_sha256}", "--publish", f"127.0.0.1:{_PORTS[model_id]}:8080", "--mount", "type=bind,src=/mnt/model-ssd/models,dst=/models,readonly", image, "--model", f"/models/{filename}", "--alias", model_id, "--host", "0.0.0.0", "--port", "8080", "--ctx-size", str(context), "--batch-size", "512", "--ubatch-size", "128", "--cache-type-k", "f16", "--cache-type-v", "f16"]
    if model_id == "embedding":
        argv.extend(["--embedding", "--pooling", "mean"])
    elif model_id == "reranker":
        argv.extend(["--embedding", "--pooling", "rank"])
    argv.extend(["--parallel", str(parallel), "--n-gpu-layers", "99", "--fit", "off"])
    if model_id.startswith("qwen-"):
        argv.append("--jinja")
    return argv
