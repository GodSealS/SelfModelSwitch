"""Explicit runtime composition for the single scheduler process."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from .backend_control import LlamaSwapBackend, ManagedModel
from .deploy import DeployError
from .config import AppConfig, model_specs
from .control_recovery import ControlRecoveryClient
from .llama_swap_client import LlamaSwapClient, LlamaSwapControlContract
from .model_registry import Book
from .process_observer import ProcessObserver
from .resource_monitor import ResourceMonitor
from .scheduler import ModelScheduler
from .storage_monitor import StorageAdmissionGuard, StorageMonitor


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE = re.compile(r"[^@]+@sha256:[0-9a-f]{64}\Z")
_DEPLOYMENT = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")


class RuntimeCompositionError(ValueError):
    """The rendered manifest cannot safely be joined to this scheduler config."""


def build_backend(config: AppConfig, manifest: Any, config_sha256: str, contract: LlamaSwapControlContract) -> LlamaSwapBackend:
    """Join one fixture-bound control contract to a matching rendered manifest."""
    if not isinstance(manifest, dict) or not _SHA256.fullmatch(config_sha256):
        raise RuntimeCompositionError("invalid runtime manifest identity")
    deployment_id, image, manifest_digest, models = (
        manifest.get("deployment_id"),
        manifest.get("image"),
        manifest.get("config_sha256"),
        manifest.get("models"),
    )
    if (
        not isinstance(deployment_id, str)
        or not _DEPLOYMENT.fullmatch(deployment_id)
        or not isinstance(image, str)
        or not _IMAGE.fullmatch(image)
        or manifest_digest != config_sha256
        or not isinstance(models, dict)
        or set(models) != set(config.models)
    ):
        raise RuntimeCompositionError("invalid runtime manifest identity")
    managed: dict[str, ManagedModel] = {}
    for model_id, model in config.models.items():
        rendered = models[model_id]
        if (
            not isinstance(rendered, dict)
            or rendered.get("container_name") != model.container_name
            or rendered.get("file") != model.file
            or rendered.get("sha256") != model.sha256
            or model.container_name != f"sms-{deployment_id}-{model_id}"
        ):
            raise RuntimeCompositionError("manifest/config model identity mismatch")
        port = urlsplit(model.upstream_url).port
        if port is None:
            raise RuntimeCompositionError("invalid model loopback port")
        managed[model_id] = ManagedModel(model.container_name, port)
    control = LlamaSwapClient(
        config.llama_swap.base_url,
        timeout=config.llama_swap.control_timeout_seconds,
        load_timeout=config.llama_swap.load_timeout_seconds,
        contract=contract,
    )
    return LlamaSwapBackend(control, ProcessObserver(deployment_id, image, config_sha256), managed)


def build_scheduler(config: AppConfig, backend: Any, *, resources: Any | None = None, storage_guard: Any | None = None, recovery: Any | None = None) -> ModelScheduler:
    """Build the one authoritative book from strict config and injected ports."""
    resource_port = resources or ResourceMonitor()
    snapshot = resource_port.snapshot_now()
    budget = snapshot.total_bytes - config.resources.system_reserve_bytes - config.scheduler.min_free_memory_bytes
    if budget <= 0:
        raise ValueError("system memory cannot satisfy configured reserves")
    book = Book(
        model_specs(config),
        model_budget=budget,
        free_floor=config.scheduler.min_free_memory_bytes,
        margin=config.scheduler.resource_safety_margin,
        max_sample_age=config.resources.sample_max_age_seconds,
        half_life=config.scheduler.heat.half_life_seconds,
        request_weight=config.scheduler.heat.request_weight,
        token_weight=config.scheduler.heat.token_weight,
    )
    for model_id in book.specs:
        book.bootstrap_stopped(model_id)
    guard = storage_guard if storage_guard is not None else StorageAdmissionGuard(
        StorageMonitor(
            config.storage.mount_path,
            config.storage.model_directory,
            config.storage.expected_uuid,
            config.storage.filesystem,
        ),
        config.models,
        sample_interval_seconds=config.resources.sample_interval_seconds,
    )
    return ModelScheduler(
        book,
        resource_port,
        backend,
        queue_capacity=config.scheduler.queue_capacity,
        priority_aging_seconds=config.scheduler.priority_aging_seconds,
        poll_interval_seconds=config.scheduler.poll_interval_seconds,
        max_evictions=config.scheduler.max_evictions_per_request,
        switch_drain_timeout_seconds=config.scheduler.switch_drain_timeout_seconds,
        switch_retry_seconds=config.scheduler.switch_retry_seconds,
        switch_window_seconds=config.scheduler.thrash.switch_window_seconds,
        max_switches_in_window=config.scheduler.thrash.max_switches_in_window,
        cooldown_seconds=config.scheduler.thrash.cooldown_seconds,
        recovery=recovery if recovery is not None else ControlRecoveryClient(),
        admission_guard=guard,
    )

def load_lab_manifest(path: str | Path, *, config_path: str | Path | None = None) -> dict[str, Any]:
    """Load a lab manifest rendered by `deploy render --mode lab` (P06b).

    A lab manifest is a test instance identity: it is marked lab-only and binds
    the exact configuration digest plus every model's rendered argv, image,
    runtime and profile. Given `config_path` the digest is re-checked, so a
    manifest can never be joined to a different configuration.
    """
    manifest_path = Path(path)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeployError(f"cannot read the lab manifest: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("mode") != "lab" or manifest.get("lab_only") is not True:
        raise DeployError("the lab manifest must be an explicit lab-only render")
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("models"), dict) or not manifest["models"]:
        raise DeployError("the lab manifest declares no model")
    for model_id, entry in manifest["models"].items():
        if not isinstance(entry, dict) or not isinstance(entry.get("argv"), list) or not entry["argv"]:
            raise DeployError(f"the lab manifest entry for {model_id!r} has no rendered argv")
        digest = hashlib.sha256(b"\x00".join(token.encode("utf-8") for token in entry["argv"])).hexdigest()
        if entry.get("argv_sha256") != digest:
            raise DeployError(f"the lab manifest entry for {model_id!r} does not match its argv digest")
    if config_path is not None:
        try:
            config_sha256 = hashlib.sha256(Path(config_path).read_bytes()).hexdigest()
        except OSError as exc:
            raise DeployError(f"cannot read the lab configuration: {exc}") from exc
        if manifest.get("config_sha256") != config_sha256:
            raise DeployError("the lab manifest config digest does not match this configuration")
    return manifest


def lab_launch_argv(manifest: Mapping[str, Any], model_id: str) -> list[str]:
    """The exact rendered argv of one lab model; unknown ids are refused."""
    models = manifest.get("models") if isinstance(manifest, Mapping) else None
    entry = models.get(model_id) if isinstance(models, Mapping) else None
    if not isinstance(entry, Mapping) or not isinstance(entry.get("argv"), list):
        raise DeployError(f"the lab manifest has no rendered entry for model {model_id!r}")
    return list(entry["argv"])
