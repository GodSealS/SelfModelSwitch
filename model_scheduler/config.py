from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Any
import yaml


@dataclass(frozen=True)
class MemoryConfig:
    reserved_bytes: int


@dataclass(frozen=True)
class SchedulingConfig:
    priority: int = 0
    evictable: bool = True
    pinned: bool = False


@dataclass(frozen=True)
class LifecycleConfig:
    ttl_seconds: int = 0


@dataclass(frozen=True)
class ModelConfig:
    model_id: str
    memory: MemoryConfig
    scheduling: SchedulingConfig
    lifecycle: LifecycleConfig


@dataclass(frozen=True)
class HeatConfig:
    half_life_seconds: float = 1800.0
    request_weight: float = 1.0
    token_weight: float = 0.0001
    active_bonus: float = 2.0


@dataclass(frozen=True)
class ThrashConfig:
    switch_window_seconds: float = 10.0
    max_switches_in_window: int = 3
    cooldown_seconds: float = 15.0


@dataclass(frozen=True)
class SchedulerConfig:
    poll_interval_seconds: float
    resource_safety_margin: float
    min_free_memory_bytes: int
    max_evictions_per_request: int
    request_queue_timeout_seconds: float
    heat: HeatConfig
    thrash: ThrashConfig


@dataclass(frozen=True)
class AppConfig:
    server: Dict[str, Any]
    llama_swap: Dict[str, Any]
    scheduler: SchedulerConfig
    resources: Dict[str, Any]
    models: Dict[str, ModelConfig]


def _bytes(value: Any) -> int:
    if isinstance(value, int):
        return value
    s = str(value).strip().upper()
    units = [("TIB", 1024**4), ("GIB", 1024**3), ("MIB", 1024**2), ("KIB", 1024),
             ("TB", 1000**4), ("GB", 1000**3), ("MB", 1000**2), ("KB", 1000)]
    for unit, factor in units:
        if s.endswith(unit):
            return int(float(s[:-len(unit)].strip()) * factor)
    return int(float(s))


def load_config(path: str | Path) -> AppConfig:
    raw = yaml.safe_load(Path(path).read_text()) or {}

    h = raw["scheduler"].get("heat", {})
    t = raw["scheduler"].get("thrash", {})

    heat = HeatConfig(
        half_life_seconds=float(h.get("half_life_seconds", 1800)),
        request_weight=float(h.get("request_weight", 1.0)),
        token_weight=float(h.get("token_weight", 0.0001)),
        active_bonus=float(h.get("active_bonus", 2.0)),
    )
    thrash = ThrashConfig(
        switch_window_seconds=float(t.get("switch_window_seconds", 10)),
        max_switches_in_window=int(t.get("max_switches_in_window", 3)),
        cooldown_seconds=float(t.get("cooldown_seconds", 15)),
    )
    sc = raw["scheduler"]
    scheduler = SchedulerConfig(
        poll_interval_seconds=float(sc.get("poll_interval_seconds", 2)),
        resource_safety_margin=float(sc.get("resource_safety_margin", 0.15)),
        min_free_memory_bytes=_bytes(sc.get("min_free_memory_bytes", 2 * 1024**3)),
        max_evictions_per_request=int(sc.get("max_evictions_per_request", 8)),
        request_queue_timeout_seconds=float(sc.get("request_queue_timeout_seconds", 1800)),
        heat=heat,
        thrash=thrash,
    )

    models = {}
    for model_id, m in raw.get("models", {}).items():
        models[model_id] = ModelConfig(
            model_id=model_id,
            memory=MemoryConfig(_bytes(m["memory"]["reserved_bytes"])),
            scheduling=SchedulingConfig(
                priority=int(m.get("scheduling", {}).get("priority", 0)),
                evictable=bool(m.get("scheduling", {}).get("evictable", True)),
                pinned=bool(m.get("scheduling", {}).get("pinned", False)),
            ),
            lifecycle=LifecycleConfig(
                ttl_seconds=int(m.get("lifecycle", {}).get("ttl_seconds", 0))
            ),
        )

    return AppConfig(
        server=raw.get("server", {}),
        llama_swap=raw["llama_swap"],
        scheduler=scheduler,
        resources=raw.get("resources", {}),
        models=models,
    )
