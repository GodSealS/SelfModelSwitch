"""Explicit runtime composition for the single scheduler process."""
from __future__ import annotations

from typing import Any

from .config import AppConfig, model_specs
from .model_registry import Book
from .resource_monitor import ResourceMonitor
from .scheduler import ModelScheduler


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
        recovery=recovery,
        admission_guard=storage_guard,
    )
