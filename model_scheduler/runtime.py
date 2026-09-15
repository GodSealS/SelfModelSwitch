"""Explicit runtime composition for the single scheduler process."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from .backend_control import LlamaSwapBackend, ManagedModel
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
