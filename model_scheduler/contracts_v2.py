"""Strict schema-v2 contracts for dynamic model registration (M01 slice 1).

Additive module: the legacy schema-v1 contracts in `contracts.py` stay the
source of truth for the running service until M02 implements the explicit
migration. Everything here rejects unknown fields, booleans posing as
integers, non-finite numbers, duplicate keys, unsafe asset paths and any
job/stage/pipeline vocabulary.

Scope of this slice (plan/05-tasks-and-acceptance.md M01):
dynamic model IDs, per-file asset registration, pinned runtimes, envelopes,
a capability matrix, and the protocol version / idempotency / permission /
limit constants for the generic control protocol.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Mapping

SCHEMA_VERSION = 2
CONTROL_PROTOCOL_VERSION = 1

PORT_RANGE = (10001, 19999)
IDEMPOTENCY_RETENTION_SECONDS = 86_400
CONTROL_SOCKET_PATH = "/run/self-model-switch/control.sock"
CONTROL_SOCKET_MODE = 0o660
SESSION_LIMITS = {
    "queue_capacity": 128,
    "queue_timeout_seconds": 1800,
    "prepare_limit_seconds": 900,
    "heartbeat_seconds": 10,
    "session_ttl_seconds": 30,
    "stop_grace_seconds": 30,
    "cleanup_limit_seconds": 60,
}

ASSET_ROLES = frozenset({"model", "projector", "tokenizer", "config", "auxiliary"})

# Startup arguments a runtime is allowed to declare. Values are derived by the
# deployment renderer from the envelope and assets; arbitrary entrypoints,
# extra args, model or projector paths are not part of the contract.
ALLOWED_STARTUP_FLAGS = frozenset(
    {
        "--parallel",
        "--kv-unified-per-slot",
        "--image-max-tokens",
        "--n-gpu-layers",
        "--flash-attn",
        "--load-mode",
        "--no-warmup",
        "--no-webui",
        "--host",
        "--port",
        "--ctx-size",
        "--batch-size",
        "--ubatch-size",
        "--threads",
    }
)

_MAX_PARALLEL = 64
_MAX_TIMEOUT_SECONDS = 86_400

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,61}[a-z0-9])?$")
_FLAG_RE = re.compile(r"^--[a-z][a-z0-9-]*$")
_IMAGE_DIGEST_RE = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")


class ContractError(ValueError):
    pass


@dataclass(frozen=True)
class CapabilitySpec:
    capability: str
    required_asset_roles: tuple[str, ...]
    protocol: str


# The capability matrix is a closed set: adding a capability requires a matrix
# change reviewed with M01, never an implicit adapter fallback.
CAPABILITY_MATRIX: Mapping[str, CapabilitySpec] = {
    "chat": CapabilitySpec("chat", ("model",), "openai-chat"),
    "vision": CapabilitySpec("vision", ("model", "projector"), "openai-chat-media"),
    "embeddings": CapabilitySpec("embeddings", ("model",), "openai-embeddings"),
    "rerank": CapabilitySpec("rerank", ("model",), "llama-rerank"),
}


@dataclass(frozen=True)
class AssetRef:
    role: str
    path: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class RuntimeSpec:
    runtime_id: str
    image_digest: str
    adapter_sha256: str
    lock_sha256: str
    startup_args: tuple[str, ...]


@dataclass(frozen=True)
class Envelope:
    ctx_size: int
    max_input_tokens: int
    max_output_tokens: int
    max_parallel: int
    max_image_tokens: int
    max_image_edge_pixels: int


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    runtime_id: str
    capabilities: tuple[str, ...]
    assets: tuple[AssetRef, ...]
    port: int
    envelope: Envelope
    timeout_seconds: int
    reserved_bytes: int
    measured: bool


@dataclass(frozen=True)
class DeploymentSpec:
    runtimes: tuple[RuntimeSpec, ...]
    models: tuple[ModelSpec, ...]


# ---------------------------------------------------------------------------
# strict value helpers


def _mapping(value: Any, where: str) -> Mapping:
    if not isinstance(value, dict):
        raise ContractError(f"{where}: expected an object, got {type(value).__name__}")
    return value


def _exact_keys(data: Mapping, allowed: frozenset, where: str) -> None:
    unknown = sorted(set(data) - set(allowed))
    if unknown:
        raise ContractError(f"{where}: unknown fields: {', '.join(unknown)}")


def _str(data: Mapping, key: str, where: str, *, pattern: re.Pattern | None = None) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ContractError(f"{where}: {key} must be a non-empty string")
    if pattern is not None and not pattern.match(value):
        raise ContractError(f"{where}: {key} has invalid format: {value!r}")
    return value


def _int(data: Mapping, key: str, where: str, *, minimum: int | None = None, maximum: int | None = None) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{where}: {key} must be an integer")
    if minimum is not None and value < minimum:
        raise ContractError(f"{where}: {key} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ContractError(f"{where}: {key} must be <= {maximum}")
    return value


def _bool(data: Mapping, key: str, where: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise ContractError(f"{where}: {key} must be a boolean")
    return value


def _sequence(data: Mapping, key: str, where: str) -> list:
    value = data.get(key)
    if not isinstance(value, list):
        raise ContractError(f"{where}: {key} must be a list")
    return value


def _sha256_value(value: Any, where: str, key: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.match(value):
        raise ContractError(f"{where}: {key} must be a lowercase 64-hex SHA-256")
    return value


def _safe_relative_path(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError(f"{where}: path must be a non-empty string")
    if value.startswith("/") or "\\" in value:
        raise ContractError(f"{where}: path must be a relative POSIX path: {value!r}")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ContractError(f"{where}: path must not contain empty, '.' or '..' segments: {value!r}")
    return value


# ---------------------------------------------------------------------------
# parsers


ASSET_KEYS = frozenset({"role", "path", "sha256", "size_bytes"})
RUNTIME_KEYS = frozenset({"runtime_id", "image_digest", "adapter_sha256", "lock_sha256", "startup_args"})
ENVELOPE_KEYS = frozenset(
    {"ctx_size", "max_input_tokens", "max_output_tokens", "max_parallel", "max_image_tokens", "max_image_edge_pixels"}
)
MODEL_KEYS = frozenset(
    {
        "model_id",
        "runtime_id",
        "capabilities",
        "assets",
        "port",
        "envelope",
        "timeout_seconds",
        "reserved_bytes",
        "measured",
    }
)
DEPLOYMENT_KEYS = frozenset({"schema_version", "runtimes", "models"})


def parse_asset_ref(data: Mapping, where: str = "asset") -> AssetRef:
    data = _mapping(data, where)
    _exact_keys(data, ASSET_KEYS, where)
    role = _str(data, "role", where)
    if role not in ASSET_ROLES:
        raise ContractError(f"{where}: unknown asset role {role!r}")
    path = _safe_relative_path(data.get("path"), where)
    sha256 = _sha256_value(data.get("sha256"), where, "sha256")
    size_bytes = _int(data, "size_bytes", where, minimum=1)
    return AssetRef(role=role, path=path, sha256=sha256, size_bytes=size_bytes)


def parse_runtime_spec(data: Mapping, where: str = "runtime") -> RuntimeSpec:
    data = _mapping(data, where)
    _exact_keys(data, RUNTIME_KEYS, where)
    runtime_id = _str(data, "runtime_id", where, pattern=_ID_RE)
    image_digest = _str(data, "image_digest", where)
    if not _IMAGE_DIGEST_RE.match(image_digest):
        raise ContractError(f"{where}: image_digest must be a digest reference, not a floating tag: {image_digest!r}")
    adapter_sha256 = _sha256_value(data.get("adapter_sha256"), where, "adapter_sha256")
    lock_sha256 = _sha256_value(data.get("lock_sha256"), where, "lock_sha256")
    startup_args = _parse_startup_args(data, where)
    return RuntimeSpec(
        runtime_id=runtime_id,
        image_digest=image_digest,
        adapter_sha256=adapter_sha256,
        lock_sha256=lock_sha256,
        startup_args=startup_args,
    )


def _parse_startup_args(data: Mapping, where: str) -> tuple[str, ...]:
    raw = _sequence(data, "startup_args", where)
    seen: set[str] = set()
    result: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not _FLAG_RE.match(item):
            raise ContractError(f"{where}: invalid startup arg {item!r}")
        if item not in ALLOWED_STARTUP_FLAGS:
            raise ContractError(f"{where}: startup arg {item!r} is not allowed")
        if item in seen:
            raise ContractError(f"{where}: duplicate startup arg {item!r}")
        seen.add(item)
        result.append(item)
    return tuple(result)


def parse_envelope(data: Mapping, where: str = "envelope") -> Envelope:
    data = _mapping(data, where)
    _exact_keys(data, ENVELOPE_KEYS, where)
    ctx_size = _int(data, "ctx_size", where, minimum=512)
    max_input = _int(data, "max_input_tokens", where, minimum=1)
    max_output = _int(data, "max_output_tokens", where, minimum=1)
    max_parallel = _int(data, "max_parallel", where, minimum=1, maximum=_MAX_PARALLEL)
    image_tokens = _int(data, "max_image_tokens", where, minimum=0)
    image_edge = _int(data, "max_image_edge_pixels", where, minimum=0)
    if max_input + max_output > ctx_size:
        raise ContractError(f"{where}: max_input_tokens + max_output_tokens exceeds ctx_size")
    if image_tokens >= max_input:
        raise ContractError(f"{where}: max_image_tokens must be smaller than max_input_tokens")
    if (image_tokens == 0) != (image_edge == 0):
        raise ContractError(f"{where}: max_image_tokens and max_image_edge_pixels must both be zero or both be set")
    if image_edge != 0 and image_edge < 28:
        raise ContractError(f"{where}: max_image_edge_pixels is too small")
    return Envelope(
        ctx_size=ctx_size,
        max_input_tokens=max_input,
        max_output_tokens=max_output,
        max_parallel=max_parallel,
        max_image_tokens=image_tokens,
        max_image_edge_pixels=image_edge,
    )


def parse_model_spec(data: Mapping, where: str = "model") -> ModelSpec:
    data = _mapping(data, where)
    _exact_keys(data, MODEL_KEYS, where)
    model_id = _str(data, "model_id", where, pattern=_ID_RE)
    where = f"model {model_id!r}"
    runtime_id = _str(data, "runtime_id", where, pattern=_ID_RE)
    capabilities = _parse_capabilities(data, where)
    assets = _parse_assets(data, where)
    port_min, port_max = PORT_RANGE
    port = _int(data, "port", where, minimum=port_min, maximum=port_max)
    envelope = parse_envelope(_mapping(data.get("envelope"), f"{where}.envelope"), f"{where}.envelope")
    timeout_seconds = _int(data, "timeout_seconds", where, minimum=1, maximum=_MAX_TIMEOUT_SECONDS)
    reserved_bytes = _int(data, "reserved_bytes", where, minimum=1)
    measured = _bool(data, "measured", where)
    if "vision" in capabilities and envelope.max_image_tokens <= 0:
        raise ContractError(f"{where}: vision capability requires a positive max_image_tokens")
    return ModelSpec(
        model_id=model_id,
        runtime_id=runtime_id,
        capabilities=capabilities,
        assets=assets,
        port=port,
        envelope=envelope,
        timeout_seconds=timeout_seconds,
        reserved_bytes=reserved_bytes,
        measured=measured,
    )


def _parse_capabilities(data: Mapping, where: str) -> tuple[str, ...]:
    raw = _sequence(data, "capabilities", where)
    if not raw:
        raise ContractError(f"{where}: capabilities must not be empty")
    seen: set[str] = set()
    result: list[str] = []
    for item in raw:
        if not isinstance(item, str) or item not in CAPABILITY_MATRIX:
            raise ContractError(f"{where}: unknown capability {item!r}")
        if item in seen:
            raise ContractError(f"{where}: duplicate capability {item!r}")
        seen.add(item)
        result.append(item)
    return tuple(result)


def _parse_assets(data: Mapping, where: str) -> tuple[AssetRef, ...]:
    raw = _sequence(data, "assets", where)
    if not raw:
        raise ContractError(f"{where}: assets must not be empty")
    assets: list[AssetRef] = []
    roles: set[str] = set()
    for item in raw:
        asset = parse_asset_ref(item, f"{where}.assets[{len(assets)}]")
        if asset.role in roles:
            raise ContractError(f"{where}: duplicate asset role {asset.role!r}")
        roles.add(asset.role)
        assets.append(asset)
    required: set[str] = set()
    for capability in _capability_names(data):
        required.update(CAPABILITY_MATRIX[capability].required_asset_roles)
    missing = sorted(required - roles)
    if missing:
        raise ContractError(f"{where}: capabilities require missing asset roles: {', '.join(missing)}")
    return tuple(assets)


def _capability_names(data: Mapping) -> tuple[str, ...]:
    raw = data.get("capabilities")
    if not isinstance(raw, list):
        return ()
    return tuple(item for item in raw if isinstance(item, str))


def parse_deployment(data: Mapping, where: str = "deployment") -> DeploymentSpec:
    data = _mapping(data, where)
    _exact_keys(data, DEPLOYMENT_KEYS, where)
    version = _int(data, "schema_version", where)
    if version != SCHEMA_VERSION:
        raise ContractError(f"{where}: unsupported schema_version {version}, expected {SCHEMA_VERSION}")
    runtimes_raw = _sequence(data, "runtimes", where)
    if not runtimes_raw:
        raise ContractError(f"{where}: runtimes must not be empty")
    runtimes: list[RuntimeSpec] = []
    runtime_ids: set[str] = set()
    for item in runtimes_raw:
        runtime = parse_runtime_spec(item, f"{where}.runtimes[{len(runtimes)}]")
        if runtime.runtime_id in runtime_ids:
            raise ContractError(f"{where}: duplicate runtime_id {runtime.runtime_id!r}")
        runtime_ids.add(runtime.runtime_id)
        runtimes.append(runtime)
    models_raw = _sequence(data, "models", where)
    if not models_raw:
        raise ContractError(f"{where}: models must not be empty")
    models: list[ModelSpec] = []
    model_ids: set[str] = set()
    ports: set[int] = set()
    for item in models_raw:
        model = parse_model_spec(item, f"{where}.models[{len(models)}]")
        if model.model_id in model_ids:
            raise ContractError(f"{where}: duplicate model_id {model.model_id!r}")
        if model.port in ports:
            raise ContractError(f"{where}: duplicate port {model.port}")
        if model.runtime_id not in runtime_ids:
            raise ContractError(f"{where}: model {model.model_id!r} references unknown runtime {model.runtime_id!r}")
        model_ids.add(model.model_id)
        ports.add(model.port)
        models.append(model)
    return DeploymentSpec(runtimes=tuple(runtimes), models=tuple(models))


def canonical_json_bytes(value: Any) -> bytes:
    """Canonical JSON per plan/04-deployment.md section 3: sorted keys, compact separators, UTF-8, no NaN."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def deployment_digest(deployment: DeploymentSpec) -> str:
    return hashlib.sha256(canonical_json_bytes(asdict(deployment))).hexdigest()


def _reject_constant(text: str) -> Any:
    raise ContractError(f"non-finite JSON number: {text}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_json_document(text: str) -> Any:
    try:
        return json.loads(text, parse_constant=_reject_constant, object_pairs_hook=_reject_duplicate_keys)
    except ContractError:
        raise
    except json.JSONDecodeError as exc:
        raise ContractError(f"invalid JSON: {exc}") from exc


def parse_deployment_json(text: str) -> DeploymentSpec:
    return parse_deployment(parse_json_document(text))
