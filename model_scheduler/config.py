"""Strict configuration loading for the single-process scheduler."""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from pathlib import Path
import re
from typing import Any, Mapping
from urllib.parse import urlsplit

import yaml

from .contracts import Capability, ModelSpec


class ConfigError(ValueError):
    """A configuration is missing, malformed, or unsafe to run."""


MODEL_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
REQUIRED_MODELS = frozenset({"embedding", "reranker", "qwen-small", "qwen-large"})


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise ConfigError("YAML mapping keys must be strings")
        if key in result:
            raise ConfigError(f"duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


@dataclass(frozen=True)
class ServerConfig:
    host: str; port: int; workers: int; max_request_body_bytes: int; body_timeout_seconds: float; shutdown_grace_seconds: float


@dataclass(frozen=True)
class LlamaSwapConfig:
    base_url: str; connect_timeout_seconds: float; control_timeout_seconds: float; load_timeout_seconds: float; unload_timeout_seconds: float


@dataclass(frozen=True)
class MemoryConfig:
    reserved_bytes: int


@dataclass(frozen=True)
class SchedulingConfig:
    priority: int; max_concurrency: int; evictable: bool; pinned: bool


@dataclass(frozen=True)
class LifecycleConfig:
    preload: bool; ttl_seconds: float


@dataclass(frozen=True)
class ModelConfig:
    model_id: str; upstream_url: str; capabilities: frozenset[str]; file: str; sha256: str; container_name: str
    memory: MemoryConfig; scheduling: SchedulingConfig; lifecycle: LifecycleConfig


@dataclass(frozen=True)
class HeatConfig:
    half_life_seconds: float; request_weight: float; token_weight: float


@dataclass(frozen=True)
class ThrashConfig:
    switch_window_seconds: float; max_switches_in_window: int; cooldown_seconds: float


@dataclass(frozen=True)
class SchedulerConfig:
    poll_interval_seconds: float; request_queue_timeout_seconds: float; queue_capacity: int; priority_aging_seconds: float
    switch_drain_timeout_seconds: float; switch_retry_seconds: float; resource_safety_margin: float; min_free_memory_bytes: int
    max_evictions_per_request: int; memory_reclaim_timeout_seconds: float; heat: HeatConfig; thrash: ThrashConfig


@dataclass(frozen=True)
class ResourcesConfig:
    provider: str; system_reserve_bytes: int; sample_interval_seconds: float; sample_max_age_seconds: float


@dataclass(frozen=True)
class StorageConfig:
    mount_path: Path; model_directory: Path; expected_uuid: str; filesystem: str


@dataclass(frozen=True)
class GatewayConfig:
    connect_timeout_seconds: float; pool_timeout_seconds: float; read_idle_timeout_seconds: float; write_idle_timeout_seconds: float
    inference_timeout_seconds: float; close_timeout_seconds: float; max_response_body_bytes: int; max_sse_event_bytes: int


@dataclass(frozen=True)
class AppConfig:
    schema_version: int; server: ServerConfig; llama_swap: LlamaSwapConfig; scheduler: SchedulerConfig
    resources: ResourcesConfig; storage: StorageConfig; gateway: GatewayConfig; models: Mapping[str, ModelConfig]


def _mapping(value: Any, path: str, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{path} must be an object")
    unknown, missing = set(value) - keys, keys - set(value)
    if unknown:
        raise ConfigError(f"{path} contains unknown keys: {', '.join(sorted(unknown))}")
    if missing:
        raise ConfigError(f"{path} is missing keys: {', '.join(sorted(missing))}")
    return value


def _bool(value: Any, path: str) -> bool:
    if type(value) is not bool:
        raise ConfigError(f"{path} must be a boolean")
    return value


def _int(value: Any, path: str, minimum: int = 0, maximum: int | None = None) -> int:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ConfigError(f"{path} is out of range")
    return value


def _number(value: Any, path: str, positive: bool = True) -> float:
    if type(value) not in (int, float) or isinstance(value, bool):
        raise ConfigError(f"{path} must be a number")
    output = float(value)
    if not isfinite(output) or (output <= 0 if positive else output < 0):
        raise ConfigError(f"{path} is out of range")
    return output


def _string(value: Any, path: str) -> str:
    if type(value) is not str or not value:
        raise ConfigError(f"{path} must be a non-empty string")
    return value


def _loopback_url(value: Any, path: str, ports: set[int]) -> str:
    raw = _string(value, path)
    parsed = urlsplit(raw)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ConfigError(f"{path} has an invalid port") from exc
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"} or parsed.username
            or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or port is None):
        raise ConfigError(f"{path} must be an http loopback URL with a fixed port")
    if port in ports:
        raise ConfigError(f"{path} reuses a port")
    ports.add(port)
    return raw.rstrip("/")


def _yaml(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read YAML configuration: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("configuration root must be an object")
    return raw


def load_config(path: str | Path) -> AppConfig:
    """Load an exact schema-v1 configuration without external I/O."""
    raw = _yaml(Path(path))
    _mapping(raw, "config", {"schema_version", "server", "llama_swap", "scheduler", "resources", "storage", "gateway", "models"})
    if _int(raw["schema_version"], "schema_version", 1) != 1:
        raise ConfigError("schema_version must equal 1")
    server = _mapping(raw["server"], "server", {"host", "port", "workers", "max_request_body_bytes", "body_timeout_seconds", "shutdown_grace_seconds"})
    host = _string(server["host"], "server.host")
    if host not in {"127.0.0.1", "::1"}:
        raise ConfigError("server.host must be a loopback address")
    server_cfg = ServerConfig(host, _int(server["port"], "server.port", 1, 65535), _int(server["workers"], "server.workers", 1, 1), _int(server["max_request_body_bytes"], "server.max_request_body_bytes", 1), _number(server["body_timeout_seconds"], "server.body_timeout_seconds"), _number(server["shutdown_grace_seconds"], "server.shutdown_grace_seconds"))
    ports = {server_cfg.port}
    swap = _mapping(raw["llama_swap"], "llama_swap", {"base_url", "connect_timeout_seconds", "control_timeout_seconds", "load_timeout_seconds", "unload_timeout_seconds"})
    swap_cfg = LlamaSwapConfig(_loopback_url(swap["base_url"], "llama_swap.base_url", ports), _number(swap["connect_timeout_seconds"], "llama_swap.connect_timeout_seconds"), _number(swap["control_timeout_seconds"], "llama_swap.control_timeout_seconds"), _number(swap["load_timeout_seconds"], "llama_swap.load_timeout_seconds"), _number(swap["unload_timeout_seconds"], "llama_swap.unload_timeout_seconds"))
    if swap_cfg.control_timeout_seconds > swap_cfg.unload_timeout_seconds:
        raise ConfigError("llama_swap.control_timeout_seconds must not exceed unload_timeout_seconds")
    scheduler = _mapping(raw["scheduler"], "scheduler", {"poll_interval_seconds", "request_queue_timeout_seconds", "queue_capacity", "priority_aging_seconds", "switch_drain_timeout_seconds", "switch_retry_seconds", "resource_safety_margin", "min_free_memory_bytes", "max_evictions_per_request", "memory_reclaim_timeout_seconds", "heat", "thrash"})
    heat = _mapping(scheduler["heat"], "scheduler.heat", {"half_life_seconds", "request_weight", "token_weight"})
    thrash = _mapping(scheduler["thrash"], "scheduler.thrash", {"switch_window_seconds", "max_switches_in_window", "cooldown_seconds"})
    margin = _number(scheduler["resource_safety_margin"], "scheduler.resource_safety_margin", False)
    if margin > 1:
        raise ConfigError("scheduler.resource_safety_margin is out of range")
    scheduler_cfg = SchedulerConfig(_number(scheduler["poll_interval_seconds"], "scheduler.poll_interval_seconds"), _number(scheduler["request_queue_timeout_seconds"], "scheduler.request_queue_timeout_seconds"), _int(scheduler["queue_capacity"], "scheduler.queue_capacity", 1), _number(scheduler["priority_aging_seconds"], "scheduler.priority_aging_seconds"), _number(scheduler["switch_drain_timeout_seconds"], "scheduler.switch_drain_timeout_seconds"), _number(scheduler["switch_retry_seconds"], "scheduler.switch_retry_seconds"), margin, _int(scheduler["min_free_memory_bytes"], "scheduler.min_free_memory_bytes"), _int(scheduler["max_evictions_per_request"], "scheduler.max_evictions_per_request", 1), _number(scheduler["memory_reclaim_timeout_seconds"], "scheduler.memory_reclaim_timeout_seconds"), HeatConfig(_number(heat["half_life_seconds"], "scheduler.heat.half_life_seconds"), _number(heat["request_weight"], "scheduler.heat.request_weight", False), _number(heat["token_weight"], "scheduler.heat.token_weight", False)), ThrashConfig(_number(thrash["switch_window_seconds"], "scheduler.thrash.switch_window_seconds"), _int(thrash["max_switches_in_window"], "scheduler.thrash.max_switches_in_window", 1), _number(thrash["cooldown_seconds"], "scheduler.thrash.cooldown_seconds")))
    resources = _mapping(raw["resources"], "resources", {"provider", "system_reserve_bytes", "sample_interval_seconds", "sample_max_age_seconds"})
    if resources["provider"] != "psutil":
        raise ConfigError("resources.provider must be psutil")
    resources_cfg = ResourcesConfig("psutil", _int(resources["system_reserve_bytes"], "resources.system_reserve_bytes"), _number(resources["sample_interval_seconds"], "resources.sample_interval_seconds"), _number(resources["sample_max_age_seconds"], "resources.sample_max_age_seconds"))
    if resources_cfg.sample_interval_seconds > resources_cfg.sample_max_age_seconds:
        raise ConfigError("resources.sample_interval_seconds must not exceed sample_max_age_seconds")
    storage = _mapping(raw["storage"], "storage", {"mount_path", "model_directory", "expected_uuid", "filesystem"})
    mount, directory = Path(_string(storage["mount_path"], "storage.mount_path")), Path(_string(storage["model_directory"], "storage.model_directory"))
    if not mount.is_absolute() or not directory.is_absolute() or directory.parent != mount or storage["filesystem"] != "ext4":
        raise ConfigError("storage must use an absolute mount_path/model_directory and ext4")
    storage_cfg = StorageConfig(mount, directory, _string(storage["expected_uuid"], "storage.expected_uuid"), "ext4")
    gateway = _mapping(raw["gateway"], "gateway", {"connect_timeout_seconds", "pool_timeout_seconds", "read_idle_timeout_seconds", "write_idle_timeout_seconds", "inference_timeout_seconds", "close_timeout_seconds", "max_response_body_bytes", "max_sse_event_bytes"})
    gateway_cfg = GatewayConfig(*(_number(gateway[key], f"gateway.{key}") for key in ("connect_timeout_seconds", "pool_timeout_seconds", "read_idle_timeout_seconds", "write_idle_timeout_seconds", "inference_timeout_seconds", "close_timeout_seconds")), _int(gateway["max_response_body_bytes"], "gateway.max_response_body_bytes", 1), _int(gateway["max_sse_event_bytes"], "gateway.max_sse_event_bytes", 1))
    if not isinstance(raw["models"], dict) or set(raw["models"]) != REQUIRED_MODELS:
        raise ConfigError("models must contain exactly embedding, reranker, qwen-small, and qwen-large")
    models: dict[str, ModelConfig] = {}
    for model_id, value in raw["models"].items():
        if not MODEL_ID.fullmatch(model_id):
            raise ConfigError(f"models.{model_id} is not a valid model id")
        model = _mapping(value, f"models.{model_id}", {"upstream_url", "capabilities", "file", "sha256", "container_name", "memory", "scheduling", "lifecycle"})
        capabilities = model["capabilities"]
        if not isinstance(capabilities, list) or not capabilities or any(type(item) is not str for item in capabilities) or set(capabilities) - {"chat", "embeddings", "rerank"}:
            raise ConfigError(f"models.{model_id}.capabilities is invalid")
        filename, digest = _string(model["file"], f"models.{model_id}.file"), _string(model["sha256"], f"models.{model_id}.sha256")
        if "/" in filename or filename in {".", ".."} or not SHA256.fullmatch(digest):
            raise ConfigError(f"models.{model_id} has an unsafe file or sha256")
        memory = _mapping(model["memory"], f"models.{model_id}.memory", {"reserved_bytes"})
        scheduling = _mapping(model["scheduling"], f"models.{model_id}.scheduling", {"priority", "max_concurrency", "evictable", "pinned"})
        lifecycle = _mapping(model["lifecycle"], f"models.{model_id}.lifecycle", {"preload", "ttl_seconds"})
        scheduling_cfg = SchedulingConfig(_int(scheduling["priority"], f"models.{model_id}.scheduling.priority", 0, 1000), _int(scheduling["max_concurrency"], f"models.{model_id}.scheduling.max_concurrency", 1, 16), _bool(scheduling["evictable"], f"models.{model_id}.scheduling.evictable"), _bool(scheduling["pinned"], f"models.{model_id}.scheduling.pinned"))
        lifecycle_cfg = LifecycleConfig(_bool(lifecycle["preload"], f"models.{model_id}.lifecycle.preload"), _number(lifecycle["ttl_seconds"], f"models.{model_id}.lifecycle.ttl_seconds", False))
        if scheduling_cfg.pinned and (scheduling_cfg.evictable or not lifecycle_cfg.preload or lifecycle_cfg.ttl_seconds != 0):
            raise ConfigError(f"models.{model_id}: pinned models must be non-evictable, preloaded, and have ttl_seconds=0")
        models[model_id] = ModelConfig(model_id, _loopback_url(model["upstream_url"], f"models.{model_id}.upstream_url", ports), frozenset(capabilities), filename, digest, _string(model["container_name"], f"models.{model_id}.container_name"), MemoryConfig(_int(memory["reserved_bytes"], f"models.{model_id}.memory.reserved_bytes", 1)), scheduling_cfg, lifecycle_cfg)
    return AppConfig(1, server_cfg, swap_cfg, scheduler_cfg, resources_cfg, storage_cfg, gateway_cfg, models)


def model_specs(config: AppConfig) -> dict[str, ModelSpec]:
    """Project the strict file configuration into the scheduler's sole model contract."""
    return {
        model_id: ModelSpec(
            model_id=model_id,
            upstream_url=model.upstream_url,
            capabilities=frozenset(Capability(capability) for capability in model.capabilities),
            reserved_bytes=model.memory.reserved_bytes,
            priority=model.scheduling.priority,
            max_concurrency=model.scheduling.max_concurrency,
            pinned=model.scheduling.pinned,
            evictable=model.scheduling.evictable,
            preload=model.lifecycle.preload,
            ttl_seconds=model.lifecycle.ttl_seconds,
        )
        for model_id, model in config.models.items()
    }
