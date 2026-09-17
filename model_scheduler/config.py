"""Strict configuration loading for the single-process scheduler.

Schema v1 stays the running-service contract; schema v2 adds the dynamic
registration, the C04 session policy and the C07/C08 transport and control
sections on top of the strict v2 contracts. Nothing here guesses a runtime, a
digest, an asset size or a budget: values a v1 file cannot already state are
required from the explicit migration inventory.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from math import isfinite
from pathlib import Path
import re
from typing import Any, Mapping
from urllib.parse import urlsplit

import yaml

from .contracts import Capability, ModelSpec
from .contracts_v2 import (
    SESSION_LIMITS,
    SCHEMA_VERSION as V2_SCHEMA_VERSION,
    CONTROL_SOCKET_PATH,
    ContractError,
    RuntimeSpec,
    canonical_json_bytes,
    parse_deployment,
)
from .contracts_v2 import ModelSpec as RegisteredModel


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


# ---------------------------------------------------------------------------
# schema v2
#
# The v2 top level is fixed; the only additional key is the derived
# `candidate_sha256`, which is filled in after a candidate is bound and is never
# part of the configuration's own digest (plan/08-execution-plan.md C09).


@dataclass(frozen=True)
class SessionPolicy:
    """Explicit C04 session limits; the defaults are the fixed contract values."""

    queue_capacity: int; queue_timeout_seconds: int; prepare_limit_seconds: int; heartbeat_seconds: int
    ttl_seconds: int; stop_grace_seconds: int; cleanup_limit_seconds: int; hard_timeout_seconds: int


@dataclass(frozen=True)
class SchedulerConfigV2:
    poll_interval_seconds: float; request_queue_timeout_seconds: float; queue_capacity: int; priority_aging_seconds: float
    switch_drain_timeout_seconds: float; switch_retry_seconds: float; resource_safety_margin: float; min_free_memory_bytes: int
    max_evictions_per_request: int; memory_reclaim_timeout_seconds: float; heat: HeatConfig; thrash: ThrashConfig
    pinned_models: tuple[str, ...]; preload_models: tuple[str, ...]; sessions: SessionPolicy


@dataclass(frozen=True)
class ResourcesConfigV2:
    provider: str; system_reserve_bytes: int; sample_interval_seconds: float; sample_max_age_seconds: float
    model_budget_bytes: int


@dataclass(frozen=True)
class StorageConfigV2:
    mount_path: Path; model_directory: Path; expected_uuid: str; filesystem: str; verify_timeout_seconds: int


@dataclass(frozen=True)
class ControlConfig:
    socket_path: Path; allowed_uids: tuple[int, ...]; peer_group: str | None


@dataclass(frozen=True)
class BlobsConfig:
    root: Path; owner_quota_bytes: int; total_quota_bytes: int; chunk_reserve_bytes: int; min_free_disk_bytes: int
    input_ttl_seconds: int; output_ttl_seconds: int; max_blob_bytes: int


@dataclass(frozen=True)
class AppConfigV2:
    schema_version: int; runtimes: Mapping[str, RuntimeSpec]; models: Mapping[str, RegisteredModel]
    server: ServerConfig; scheduler: SchedulerConfigV2; resources: ResourcesConfigV2; storage: StorageConfigV2
    gateway: GatewayConfig; control: ControlConfig; blobs: BlobsConfig; candidate_sha256: str | None


V2_REQUIRED_KEYS = frozenset({"schema_version", "registration", "server", "scheduler", "resources", "storage", "gateway", "control", "blobs"})
V2_DERIVED_KEYS = frozenset({"candidate_sha256"})
V2_REGISTRATION_KEYS = frozenset({"runtimes", "models"})
V2_SCHEDULER_REQUIRED = frozenset({"poll_interval_seconds", "request_queue_timeout_seconds", "queue_capacity", "priority_aging_seconds", "switch_drain_timeout_seconds", "switch_retry_seconds", "resource_safety_margin", "min_free_memory_bytes", "max_evictions_per_request", "memory_reclaim_timeout_seconds", "heat", "thrash"})
V2_SCHEDULER_OPTIONAL = frozenset({"pinned_models", "preload_models", "sessions"})
V2_RESOURCES_KEYS = frozenset({"provider", "system_reserve_bytes", "sample_interval_seconds", "sample_max_age_seconds", "model_budget_bytes"})
V2_STORAGE_REQUIRED = frozenset({"mount_path", "model_directory", "expected_uuid", "filesystem"})
V2_STORAGE_OPTIONAL = frozenset({"verify_timeout_seconds"})
V2_CONTROL_REQUIRED = frozenset({"allowed_uids"})
V2_CONTROL_OPTIONAL = frozenset({"socket_path", "peer_group"})
V2_BLOBS_REQUIRED = frozenset({"root"})
V2_BLOBS_OPTIONAL = frozenset({"owner_quota_bytes", "total_quota_bytes", "chunk_reserve_bytes", "min_free_disk_bytes", "input_ttl_seconds", "output_ttl_seconds", "max_blob_bytes"})

# Type/range bounds for every new field, so none of them is ad hoc (C04/C07/C08).
_SESSION_BOUNDS = {
    "queue_capacity": (1, SESSION_LIMITS["queue_capacity"]),
    "queue_timeout_seconds": (1, SESSION_LIMITS["queue_timeout_seconds"]),
    "prepare_limit_seconds": (1, SESSION_LIMITS["prepare_limit_seconds"]),
    "heartbeat_seconds": (1, SESSION_LIMITS["heartbeat_seconds"]),
    "ttl_seconds": (1, SESSION_LIMITS["session_ttl_seconds"]),
    "stop_grace_seconds": (1, SESSION_LIMITS["stop_grace_seconds"]),
    "cleanup_limit_seconds": (1, SESSION_LIMITS["cleanup_limit_seconds"]),
    "hard_timeout_seconds": (1, 86400),
}
SESSION_DEFAULTS = {
    "queue_capacity": SESSION_LIMITS["queue_capacity"],
    "queue_timeout_seconds": SESSION_LIMITS["queue_timeout_seconds"],
    "prepare_limit_seconds": SESSION_LIMITS["prepare_limit_seconds"],
    "heartbeat_seconds": SESSION_LIMITS["heartbeat_seconds"],
    "ttl_seconds": SESSION_LIMITS["session_ttl_seconds"],
    "stop_grace_seconds": SESSION_LIMITS["stop_grace_seconds"],
    "cleanup_limit_seconds": SESSION_LIMITS["cleanup_limit_seconds"],
    "hard_timeout_seconds": 3600,
}
BLOB_DEFAULTS = {
    "owner_quota_bytes": 4294967296,
    "total_quota_bytes": 17179869184,
    "chunk_reserve_bytes": 1073741824,
    "min_free_disk_bytes": 2147483648,
    "input_ttl_seconds": 86400,
    "output_ttl_seconds": 86400,
    "max_blob_bytes": 1073741824,
}
_MAX_BLOB_BYTES = 1073741824
_MAX_UID = 4294967295


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


def _strict(value: Any, path: str, required: frozenset[str], optional: frozenset[str] = frozenset()) -> dict[str, Any]:
    """Like `_mapping` but tolerates documented optional keys."""
    if not isinstance(value, dict):
        raise ConfigError(f"{path} must be an object")
    unknown = set(value) - required - optional
    if unknown:
        raise ConfigError(f"{path} contains unknown keys: {', '.join(sorted(unknown))}")
    missing = required - set(value)
    if missing:
        raise ConfigError(f"{path} is missing keys: {', '.join(sorted(missing))}")
    return value


def _absolute_path(value: Any, path: str) -> Path:
    candidate = Path(_string(value, path))
    if not candidate.is_absolute():
        raise ConfigError(f"{path} must be an absolute path")
    return candidate


def _model_id_list(value: Any, path: str, registered: Mapping[str, Any]) -> tuple[str, ...]:
    """Model references must name models the registration actually declares."""
    if not isinstance(value, list):
        raise ConfigError(f"{path} must be a list of model ids")
    result: list[str] = []
    for item in value:
        model_id = _string(item, f"{path} entry")
        if model_id not in registered:
            raise ConfigError(f"{path} references unregistered model {model_id!r}")
        if model_id in result:
            raise ConfigError(f"{path} contains duplicate model id {model_id!r}")
        result.append(model_id)
    return tuple(result)


def _session_policy(raw: dict[str, Any]) -> SessionPolicy:
    data = _strict(raw, "scheduler.sessions", frozenset(), frozenset(_SESSION_BOUNDS))
    values: dict[str, int] = {}
    for key, default in SESSION_DEFAULTS.items():
        minimum, maximum = _SESSION_BOUNDS[key]
        values[key] = _int(data.get(key, default), f"scheduler.sessions.{key}", minimum, maximum)
    if values["prepare_limit_seconds"] > values["queue_timeout_seconds"]:
        raise ConfigError("scheduler.sessions.prepare_limit_seconds must not exceed queue_timeout_seconds")
    if values["cleanup_limit_seconds"] > values["hard_timeout_seconds"]:
        raise ConfigError("scheduler.sessions.cleanup_limit_seconds exceeds hard_timeout_seconds")
    if values["stop_grace_seconds"] > values["cleanup_limit_seconds"]:
        raise ConfigError("scheduler.sessions.stop_grace_seconds exceeds cleanup_limit_seconds")
    return SessionPolicy(
        values["queue_capacity"], values["queue_timeout_seconds"], values["prepare_limit_seconds"],
        values["heartbeat_seconds"], values["ttl_seconds"], values["stop_grace_seconds"],
        values["cleanup_limit_seconds"], values["hard_timeout_seconds"],
    )


def _control_config(raw: dict[str, Any]) -> ControlConfig:
    data = _strict(raw, "control", V2_CONTROL_REQUIRED, V2_CONTROL_OPTIONAL)
    uids = data["allowed_uids"]
    if not isinstance(uids, list) or not uids or any(type(item) is not int or not 0 <= item <= _MAX_UID for item in uids):
        raise ConfigError("control.allowed_uids must be a non-empty list of numeric UIDs")
    if len(set(uids)) != len(uids):
        raise ConfigError("control.allowed_uids must not repeat a UID")
    group = data.get("peer_group")
    if group is not None and type(group) is not str:
        raise ConfigError("control.peer_group must be null or a string")
    socket_path = _absolute_path(data.get("socket_path", CONTROL_SOCKET_PATH), "control.socket_path")
    return ControlConfig(socket_path, tuple(uids), group or None)


def _blobs_config(raw: dict[str, Any], model_directory: Path, mount_path: Path) -> BlobsConfig:
    data = _strict(raw, "blobs", V2_BLOBS_REQUIRED, V2_BLOBS_OPTIONAL)
    root = _absolute_path(data["root"], "blobs.root")
    if root in {mount_path, model_directory}:
        raise ConfigError("blobs.root must not share the read-only model media")
    values = {key: _int(data.get(key, default), f"blobs.{key}", 1) for key, default in BLOB_DEFAULTS.items()}
    if values["max_blob_bytes"] > _MAX_BLOB_BYTES:
        raise ConfigError("blobs.max_blob_bytes exceeds the 1 GiB transport limit")
    if values["owner_quota_bytes"] > values["total_quota_bytes"]:
        raise ConfigError("blobs.owner_quota_bytes must not exceed total_quota_bytes")
    if values["chunk_reserve_bytes"] > values["max_blob_bytes"]:
        raise ConfigError("blobs.chunk_reserve_bytes must not exceed max_blob_bytes")
    return BlobsConfig(
        root, values["owner_quota_bytes"], values["total_quota_bytes"], values["chunk_reserve_bytes"],
        values["min_free_disk_bytes"], values["input_ttl_seconds"], values["output_ttl_seconds"],
        values["max_blob_bytes"],
    )


def parse_v2_config(raw: Mapping[str, Any]) -> AppConfigV2:
    """Parse an already loaded schema-v2 configuration mapping."""
    data = _strict(raw, "config", V2_REQUIRED_KEYS, V2_DERIVED_KEYS)
    candidate_sha256 = data.get("candidate_sha256")
    if candidate_sha256 is not None and not SHA256.fullmatch(_string(candidate_sha256, "candidate_sha256")):
        raise ConfigError("candidate_sha256 must be null or a lowercase 64-hex SHA-256")
    registration = _strict(data["registration"], "registration", V2_REGISTRATION_KEYS)
    try:
        deployment = parse_deployment({"schema_version": V2_SCHEMA_VERSION, **registration}, "registration")
    except ContractError as exc:
        raise ConfigError(str(exc)) from exc
    models = {spec.model_id: spec for spec in deployment.models}
    runtimes = {spec.runtime_id: spec for spec in deployment.runtimes}

    server = _strict(data["server"], "server", frozenset({"host", "port", "workers", "max_request_body_bytes", "body_timeout_seconds", "shutdown_grace_seconds"}))
    host = _string(server["host"], "server.host")
    if host not in {"127.0.0.1", "::1"}:
        raise ConfigError("server.host must be a loopback address")
    server_cfg = ServerConfig(host, _int(server["port"], "server.port", 1, 65535), _int(server["workers"], "server.workers", 1, 1), _int(server["max_request_body_bytes"], "server.max_request_body_bytes", 1), _number(server["body_timeout_seconds"], "server.body_timeout_seconds"), _number(server["shutdown_grace_seconds"], "server.shutdown_grace_seconds"))
    ports = {server_cfg.port}
    for spec in deployment.models:
        if spec.port in ports:
            raise ConfigError(f"registration.models reuses port {spec.port}")
        ports.add(spec.port)

    scheduler = _strict(data["scheduler"], "scheduler", V2_SCHEDULER_REQUIRED, V2_SCHEDULER_OPTIONAL)
    heat = _mapping(scheduler["heat"], "scheduler.heat", {"half_life_seconds", "request_weight", "token_weight"})
    thrash = _mapping(scheduler["thrash"], "scheduler.thrash", {"switch_window_seconds", "max_switches_in_window", "cooldown_seconds"})
    margin = _number(scheduler["resource_safety_margin"], "scheduler.resource_safety_margin", False)
    if margin > 1:
        raise ConfigError("scheduler.resource_safety_margin is out of range")
    pinned = _model_id_list(scheduler.get("pinned_models", []), "scheduler.pinned_models", models)
    preload = _model_id_list(scheduler.get("preload_models", []), "scheduler.preload_models", models)
    if not set(pinned) <= set(preload):
        raise ConfigError("scheduler: pinned models must also be preloaded")
    scheduler_cfg = SchedulerConfigV2(
        _number(scheduler["poll_interval_seconds"], "scheduler.poll_interval_seconds"),
        _number(scheduler["request_queue_timeout_seconds"], "scheduler.request_queue_timeout_seconds"),
        _int(scheduler["queue_capacity"], "scheduler.queue_capacity", 1),
        _number(scheduler["priority_aging_seconds"], "scheduler.priority_aging_seconds"),
        _number(scheduler["switch_drain_timeout_seconds"], "scheduler.switch_drain_timeout_seconds"),
        _number(scheduler["switch_retry_seconds"], "scheduler.switch_retry_seconds"),
        margin,
        _int(scheduler["min_free_memory_bytes"], "scheduler.min_free_memory_bytes"),
        _int(scheduler["max_evictions_per_request"], "scheduler.max_evictions_per_request", 1),
        _number(scheduler["memory_reclaim_timeout_seconds"], "scheduler.memory_reclaim_timeout_seconds"),
        HeatConfig(_number(heat["half_life_seconds"], "scheduler.heat.half_life_seconds"), _number(heat["request_weight"], "scheduler.heat.request_weight", False), _number(heat["token_weight"], "scheduler.heat.token_weight", False)),
        ThrashConfig(_number(thrash["switch_window_seconds"], "scheduler.thrash.switch_window_seconds"), _int(thrash["max_switches_in_window"], "scheduler.thrash.max_switches_in_window", 1), _number(thrash["cooldown_seconds"], "scheduler.thrash.cooldown_seconds")),
        pinned,
        preload,
        _session_policy(scheduler.get("sessions", {})),
    )

    resources = _strict(data["resources"], "resources", V2_RESOURCES_KEYS)
    if resources["provider"] != "psutil":
        raise ConfigError("resources.provider must be psutil")
    interval = _number(resources["sample_interval_seconds"], "resources.sample_interval_seconds")
    max_age = _number(resources["sample_max_age_seconds"], "resources.sample_max_age_seconds")
    if interval > max_age:
        raise ConfigError("resources.sample_interval_seconds must not exceed sample_max_age_seconds")
    resources_cfg = ResourcesConfigV2("psutil", _int(resources["system_reserve_bytes"], "resources.system_reserve_bytes"), interval, max_age, _int(resources["model_budget_bytes"], "resources.model_budget_bytes", 1))

    storage = _strict(data["storage"], "storage", V2_STORAGE_REQUIRED, V2_STORAGE_OPTIONAL)
    mount, directory = Path(_string(storage["mount_path"], "storage.mount_path")), Path(_string(storage["model_directory"], "storage.model_directory"))
    if not mount.is_absolute() or not directory.is_absolute() or directory.parent != mount or storage["filesystem"] != "ext4":
        raise ConfigError("storage must use an absolute mount_path/model_directory and ext4")
    storage_cfg = StorageConfigV2(mount, directory, _string(storage["expected_uuid"], "storage.expected_uuid"), "ext4", _int(storage.get("verify_timeout_seconds", 900), "storage.verify_timeout_seconds", 1, 86400))

    gateway = _mapping(data["gateway"], "gateway", {"connect_timeout_seconds", "pool_timeout_seconds", "read_idle_timeout_seconds", "write_idle_timeout_seconds", "inference_timeout_seconds", "close_timeout_seconds", "max_response_body_bytes", "max_sse_event_bytes"})
    gateway_cfg = GatewayConfig(*(_number(gateway[key], f"gateway.{key}") for key in ("connect_timeout_seconds", "pool_timeout_seconds", "read_idle_timeout_seconds", "write_idle_timeout_seconds", "inference_timeout_seconds", "close_timeout_seconds")), _int(gateway["max_response_body_bytes"], "gateway.max_response_body_bytes", 1), _int(gateway["max_sse_event_bytes"], "gateway.max_sse_event_bytes", 1))

    return AppConfigV2(
        V2_SCHEMA_VERSION, runtimes, models, server_cfg, scheduler_cfg, resources_cfg, storage_cfg, gateway_cfg,
        _control_config(data["control"]), _blobs_config(data["blobs"], directory, mount), candidate_sha256,
    )


def _check_json_native(value: Any, path: str) -> None:
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int) or isinstance(value, float):
        if isinstance(value, float) and not isfinite(value):
            raise ConfigError(f"{path}: non-finite numbers are not allowed")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _check_json_native(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _check_json_native(item, f"{path}.{key}")
        return
    raise ConfigError(f"{path}: {type(value).__name__} is not representable in configuration JSON")


def config_digest(raw: Mapping[str, Any]) -> str:
    """Canonical digest of a configuration, excluding the derived candidate_sha256.

    The candidate backfill is written back after the candidate digest exists, so
    the two cannot reference each other (plan/08-execution-plan.md C09).
    """
    body = {key: value for key, value in raw.items() if key not in V2_DERIVED_KEYS}
    for key, value in body.items():
        _check_json_native(value, f"config.{key}")
    return hashlib.sha256(canonical_json_bytes(body)).hexdigest()


def load_config(path: str | Path) -> AppConfig | AppConfigV2:
    """Load a schema-v1 or schema-v2 configuration without external I/O."""
    raw = _yaml(Path(path))
    version = raw.get("schema_version")
    if version == 1:
        return _load_v1(raw)
    if version == V2_SCHEMA_VERSION:
        return parse_v2_config(raw)
    raise ConfigError("schema_version must be 1 or 2")


def _load_v1(raw: dict[str, Any]) -> AppConfig:
    """Load an exact schema-v1 configuration mapping without external I/O."""
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
