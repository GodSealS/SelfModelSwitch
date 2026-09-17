"""Profile-driven container argv rendering for dynamic registrations (M02/P06).

One registered runtime profile owns the argv vocabulary of its launches:

* only flags enabled in `RuntimeSpec.startup_args` are rendered, each from the
  profile's declared value source (a fixed constant, the registered envelope, or
  the fixed in-container port); a flag without a value source is refused rather
  than guessed;
* the flags required to express the registered envelope, plus the flags a
  capability needs, must be enabled, so an unmeasured argument set can never be
  launched while claiming the measured one;
* the only bind mount is the verified model directory, read-only, at
  `/models`; arbitrary entrypoints, volumes, `--privileged` and the Docker
  socket are impossible because the renderer builds every token itself;
* the loopback port comes from the registered model, never from a fixed
  four-model table, and the deployment/model/runtime/profile/mode labels keep
  two runtime identities from ever sharing one container identity;
* `restart=no` plus a foreground child make the scheduler the only lifecycle
  authority: there is no auto-swap and no restart-driven eviction;
* production renders require the model's bound measurement material, while a
  lab render must carry an explicit temporary budget and stays marked as lab.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .contracts_v2 import (
    GGUF_PROFILE,
    ContractError,
    DeploymentSpec,
    ModelSpec,
    RuntimeSpec,
    require_production_openable,
    require_startable_profile,
)

CONTAINER_MODEL_ROOT = "/models"
CONTAINER_SERVER_PORT = "8080"
CONTROL_LABEL_PREFIX = "io.self-model-switch"

LAB = "lab"
PRODUCTION = "production"
LAUNCH_MODES = (LAB, PRODUCTION)

_DEPLOYMENT_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class LaunchRenderError(ValueError):
    """The registration cannot produce a safe, profile-bound container argv."""


@dataclass(frozen=True)
class FlagSource:
    """Where one enabled startup flag takes its value from.

    `kind` is `fixed` (the constant `value`, or a value-less switch when
    `value` is None) or `envelope` (the named `Envelope` field). A flag may
    additionally require a capability the model must declare.
    """

    kind: str
    value: str | None = None
    field: str | None = None
    requires_capability: str | None = None


@dataclass(frozen=True)
class LaunchRules:
    """The launch vocabulary of one registered profile, in canonical render order."""

    flag_sources: Mapping[str, FlagSource]
    required_flags: tuple[str, ...]


# The first GGUF profile follows the measured M00 runtime: the loading mode is
# fixed to `auto` (this runtime has no `--no-mmap`), the GPU layer count is
# fixed, and the envelope flags come from the registration. `--batch-size`,
# `--ubatch-size` and `--threads` stay enabled in the contract but have no value
# source here, so a registration that enables them is refused instead of guessed.
GGUF_LAUNCH_RULES = LaunchRules(
    flag_sources={
        "--parallel": FlagSource("envelope", field="max_parallel"),
        "--kv-unified-per-slot": FlagSource("envelope", field="ctx_size"),
        "--ctx-size": FlagSource("envelope", field="ctx_size"),
        "--image-max-tokens": FlagSource(
            "envelope", field="max_image_tokens", requires_capability="vision"
        ),
        "--n-gpu-layers": FlagSource("fixed", value="99"),
        "--flash-attn": FlagSource("fixed", value="auto"),
        "--no-warmup": FlagSource("fixed"),
        "--no-webui": FlagSource("fixed"),
        "--load-mode": FlagSource("fixed", value="auto"),
        "--host": FlagSource("fixed", value="0.0.0.0"),
        "--port": FlagSource("fixed", value=CONTAINER_SERVER_PORT),
    },
    required_flags=("--host", "--port", "--parallel", "--kv-unified-per-slot"),
)

# Flags a capability cannot be served without. The current GGUF profile has no
# value source for the embedding/pooling flags, so an embeddings or rerank
# registration is refused until its own profile, fixture and measurement exist.
CAPABILITY_FLAGS: Mapping[str, tuple[str, ...]] = {
    "chat": (),
    "vision": ("--image-max-tokens",),
    "embeddings": ("--embedding", "--pooling"),
    "rerank": ("--embedding", "--pooling"),
}

LAUNCH_RULES: Mapping[str, LaunchRules] = {GGUF_PROFILE: GGUF_LAUNCH_RULES}


@dataclass(frozen=True)
class ContainerLaunch:
    """A rendered, profile-bound container launch and its identity labels."""

    argv: tuple[str, ...]
    server_args: tuple[str, ...]
    labels: Mapping[str, str]
    container_name: str
    deployment_id: str
    model_id: str
    runtime_id: str
    profile_id: str
    image_digest: str
    port: int
    mode: str
    config_sha256: str
    model_directory: str

    @property
    def is_lab(self) -> bool:
        return self.mode == LAB


def render_container_launch(
    registration: DeploymentSpec,
    model_id: str,
    *,
    deployment_id: str,
    model_directory: str | Path,
    config_sha256: str,
    mode: str,
    temporary_budget_bytes: int | None = None,
) -> ContainerLaunch:
    """Render the exact argv for one registered model, or refuse to launch it."""
    if mode not in LAUNCH_MODES:
        raise LaunchRenderError(f"launch mode must be one of {', '.join(LAUNCH_MODES)}")
    if mode == PRODUCTION:
        if temporary_budget_bytes is not None:
            raise LaunchRenderError("a production launch must not carry a temporary budget")
    elif isinstance(temporary_budget_bytes, bool) or not isinstance(temporary_budget_bytes, int) or temporary_budget_bytes <= 0:
        raise LaunchRenderError("a lab launch requires an explicit temporary budget in bytes")

    deployment = _deployment_id(deployment_id)
    digest = _config_sha256(config_sha256)
    directory = _model_directory(model_directory)
    model, runtime = _lookup(registration, model_id)
    try:
        profile = require_startable_profile(runtime)
    except ContractError as exc:
        raise LaunchRenderError(str(exc)) from exc
    if mode == PRODUCTION:
        try:
            require_production_openable(model, runtime)
        except ContractError as exc:
            raise LaunchRenderError(str(exc)) from exc
    rules = LAUNCH_RULES.get(profile.profile_id)
    if rules is None:
        raise LaunchRenderError(f"profile {profile.profile_id!r} has no launch rules")

    flags = tuple(_render_flags(model, runtime, rules))
    assets = _asset_arguments(model)
    _assert_server_arguments(assets + flags, rules)

    container_name = f"sms-{deployment}-{model.model_id}"
    labels = {
        f"{CONTROL_LABEL_PREFIX}.deployment": deployment,
        f"{CONTROL_LABEL_PREFIX}.runtime": runtime.runtime_id,
        f"{CONTROL_LABEL_PREFIX}.profile": profile.profile_id,
        f"{CONTROL_LABEL_PREFIX}.model": model.model_id,
        f"{CONTROL_LABEL_PREFIX}.mode": mode,
        f"{CONTROL_LABEL_PREFIX}.config-sha256": digest,
    }
    label_args = tuple(token for item in labels.items() for token in ("--label", f"{item[0]}={item[1]}"))
    mount = f"type=bind,src={directory},dst={CONTAINER_MODEL_ROOT},readonly"
    argv = (
        "docker", "run",
        "--name", container_name,
        "--init",
        "--rm",
        "--restart=no",
        "--gpus", "all",
        *label_args,
        "--publish", f"127.0.0.1:{model.port}:{CONTAINER_SERVER_PORT}",
        "--mount", mount,
        runtime.image_digest,
        *assets,
        *flags,
    )
    _assert_no_host_escape(argv, mount)
    return ContainerLaunch(
        argv=argv,
        server_args=assets + flags,
        labels=MappingProxyType(labels),
        container_name=container_name,
        deployment_id=deployment,
        model_id=model.model_id,
        runtime_id=runtime.runtime_id,
        profile_id=profile.profile_id,
        image_digest=runtime.image_digest,
        port=model.port,
        mode=mode,
        config_sha256=digest,
        model_directory=directory,
    )


def _lookup(registration: DeploymentSpec, model_id: str) -> tuple[ModelSpec, RuntimeSpec]:
    if not isinstance(registration, DeploymentSpec):
        raise LaunchRenderError("a parsed deployment registration is required to render a launch")
    models = {model.model_id: model for model in registration.models}
    model = models.get(model_id)
    if model is None:
        raise LaunchRenderError(f"unknown model {model_id!r}")
    runtimes = {runtime.runtime_id: runtime for runtime in registration.runtimes}
    runtime = runtimes.get(model.runtime_id)
    if runtime is None:
        raise LaunchRenderError(f"model {model_id!r} references unknown runtime {model.runtime_id!r}")
    return model, runtime


def _render_flags(model: ModelSpec, runtime: RuntimeSpec, rules: LaunchRules) -> list[str]:
    enabled = tuple(runtime.startup_args)
    for flag in enabled:
        if flag not in rules.flag_sources:
            raise LaunchRenderError(
                f"profile {runtime.profile_id!r} has no value source for the enabled flag {flag!r}"
            )
    required: list[str] = list(rules.required_flags)
    for capability in model.capabilities:
        if capability not in CAPABILITY_FLAGS:
            raise LaunchRenderError(f"capability {capability!r} has no launch profile")
        required.extend(CAPABILITY_FLAGS[capability])
    required = list(dict.fromkeys(required))
    for flag in required:
        if flag not in rules.flag_sources:
            raise LaunchRenderError(
                f"profile {runtime.profile_id!r} has no value source for {flag!r}, "
                f"required by model {model.model_id!r}"
            )
    missing = [flag for flag in required if flag not in enabled]
    if missing:
        raise LaunchRenderError(
            f"runtime {runtime.runtime_id!r} must enable {', '.join(missing)} to launch "
            f"model {model.model_id!r}"
        )
    arguments: list[str] = []
    for flag, source in rules.flag_sources.items():
        if flag not in enabled:
            continue
        if source.requires_capability is not None and source.requires_capability not in model.capabilities:
            raise LaunchRenderError(
                f"flag {flag!r} requires the {source.requires_capability!r} capability of model {model.model_id!r}"
            )
        if source.kind == "fixed":
            arguments.extend((flag,) if source.value is None else (flag, source.value))
        elif source.kind == "envelope":
            value = getattr(model.envelope, source.field)
            arguments.extend((flag, str(value)))
        else:
            raise LaunchRenderError(f"profile {runtime.profile_id!r} declares an unknown flag source")
    return arguments


def _asset_arguments(model: ModelSpec) -> tuple[str, ...]:
    model_files = [asset for asset in model.assets if asset.role == "model"]
    projectors = [asset for asset in model.assets if asset.role == "projector"]
    if len(model_files) != 1:
        raise LaunchRenderError(f"model {model.model_id!r}: exactly one model asset is required to launch")
    if len(projectors) > 1:
        raise LaunchRenderError(f"model {model.model_id!r}: at most one projector asset can be mounted")
    if "vision" in model.capabilities and not projectors:
        raise LaunchRenderError(f"model {model.model_id!r}: the vision capability requires a projector asset")
    arguments = ["--model", _container_path(model_files[0].path)]
    if projectors:
        arguments.extend(("--mmproj", _container_path(projectors[0].path)))
    return tuple(arguments)


def _container_path(path: str) -> str:
    if path.startswith("/") or any(part in ("", ".", "..") for part in path.split("/")):
        raise LaunchRenderError(f"asset path cannot be mounted inside the container: {path!r}")
    return f"{CONTAINER_MODEL_ROOT}/{path}"


def _deployment_id(value: Any) -> str:
    if not isinstance(value, str) or not _DEPLOYMENT_RE.fullmatch(value):
        raise LaunchRenderError(f"deployment id {value!r} is not a lowercase DNS-style name")
    return value


def _config_sha256(value: Any) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise LaunchRenderError("config_sha256 must be a lowercase 64-hex digest")
    return value


def _model_directory(value: Any) -> str:
    if not isinstance(value, (str, Path)):
        raise LaunchRenderError("the model directory must be an absolute host path")
    text = str(value)
    if not text.startswith("/") or text == "/" or ".." in text.split("/") or "," in text or "\x00" in text:
        raise LaunchRenderError(
            "the model directory must be an absolute, comma-free host path below a verified mount"
        )
    return text


def _assert_server_arguments(server_args: tuple[str, ...], rules: LaunchRules) -> None:
    """Every token a container server receives must come from this profile."""
    valued = {flag for flag, source in rules.flag_sources.items() if source.kind != "fixed" or source.value is not None}
    switches = {flag for flag, source in rules.flag_sources.items() if source.kind == "fixed" and source.value is None}
    index = 0
    while index < len(server_args):
        token = server_args[index]
        if token in ("--model", "--mmproj"):
            index += 2
        elif token in switches:
            index += 1
        elif token in valued:
            index += 2
        else:
            raise LaunchRenderError(f"server argument {token!r} is not part of the registered profile")


def _assert_no_host_escape(argv: tuple[str, ...], mount: str) -> None:
    """Structural re-check: no entrypoint, no extra mount, never the Docker socket."""
    if "--entrypoint" in argv or "--privileged" in argv:
        raise LaunchRenderError("a launch must not override the entrypoint or run privileged")
    mounts = [argv[index + 1] for index, token in enumerate(argv) if token == "--mount"]
    if mounts != [mount]:
        raise LaunchRenderError("a launch mounts exactly the verified read-only model directory")
    if any("docker.sock" in token for token in argv):
        raise LaunchRenderError("a launch must never mount the Docker socket")
