"""Explicit schema-v1 to schema-v2 configuration migration (M02/P04).

The legacy eight-section v1 file states model ids, capabilities, single-file
names, hashes, ports and activity policy. It cannot state which runtime, image,
adapter, lock, envelope, asset size or measurement material backs a model, nor
the memory budget, the control identities or the transport root. Those come from
an inventory document supplied by the administrator and checked against the
measurements this plan requires. Anything missing is reported, never guessed:
the migration writes either a complete schema-v2 configuration or nothing.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

import yaml

from .config import (
    BLOB_DEFAULTS,
    SESSION_DEFAULTS,
    AppConfig,
    AppConfigV2,
    ConfigError,
    load_config,
    parse_v2_config,
)
from .contracts_v2 import effective_reserved_bytes, parse_json_document

_RUNTIME_KEYS = ("profile_id", "image_digest", "adapter_sha256", "lock_sha256", "startup_args")
_ENVELOPE_KEYS = (
    "ctx_size",
    "max_input_tokens",
    "max_output_tokens",
    "max_parallel",
    "max_image_tokens",
    "max_image_edge_pixels",
    "max_images",
)
_ASSET_KEYS = ("role", "path", "sha256", "size_bytes")


class MigrationError(ValueError):
    """The migration cannot produce a startable configuration."""


class MissingInventory(MigrationError):
    """The inventory is incomplete, or it contradicts the v1 configuration."""

    def __init__(self, missing: list[str], conflicts: list[str]) -> None:
        super().__init__("migration inventory is incomplete or conflicts with the v1 configuration")
        self.report = {"missing": sorted(set(missing)), "conflicts": sorted(set(conflicts))}


class _Report:
    def __init__(self) -> None:
        self.missing: list[str] = []
        self.conflicts: list[str] = []

    def need(self, data: Mapping[str, Any] | None, key: str, path: str) -> Any:
        """Return an explicitly supplied value, or record it as missing."""
        if not isinstance(data, Mapping) or data.get(key) is None:
            self.missing.append(f"{path}.{key}")
            return None
        return data[key]


def _mapping_or_none(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _migrate_runtimes(referenced: set[str], inventory_runtimes: Mapping[str, Any], report: _Report) -> list[dict[str, Any]]:
    runtimes: list[dict[str, Any]] = []
    for runtime_id in sorted(referenced):
        path = f"runtimes.{runtime_id}"
        spec = inventory_runtimes.get(runtime_id) if isinstance(inventory_runtimes, Mapping) else None
        if spec is None:
            report.missing.append(path)
            continue
        values = {key: report.need(spec, key, path) for key in _RUNTIME_KEYS}
        args = values["startup_args"]
        if not isinstance(args, list):
            report.missing.append(f"{path}.startup_args")
            args = []
        runtimes.append(
            {
                "runtime_id": runtime_id,
                "profile_id": values["profile_id"],
                "image_digest": values["image_digest"],
                "adapter_sha256": values["adapter_sha256"],
                "lock_sha256": values["lock_sha256"],
                "startup_args": list(args),
            }
        )
    return runtimes


def _migrate_envelope(entry: Mapping[str, Any], path: str, report: _Report) -> dict[str, Any]:
    raw = report.need(entry, "envelope", path)
    if not isinstance(raw, Mapping):
        report.missing.append(f"{path}.envelope")
        return {key: None for key in _ENVELOPE_KEYS}
    return {key: report.need(raw, key, f"{path}.envelope") for key in _ENVELOPE_KEYS}


def _migrate_assets(entry: Mapping[str, Any], path: str, old: Any, report: _Report) -> list[dict[str, Any]]:
    raw = report.need(entry, "assets", path)
    if not isinstance(raw, list) or not raw:
        report.missing.append(f"{path}.assets")
        return []
    assets: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        item_path = f"{path}.assets[{index}]"
        if not isinstance(item, Mapping):
            report.missing.append(item_path)
            continue
        asset = {key: report.need(item, key, item_path) for key in _ASSET_KEYS}
        # The v1 file already names and hashes the single model asset; the inventory
        # must agree with it rather than silently repointing the registration.
        if asset["role"] == "model":
            if asset["path"] != old.file:
                report.conflicts.append(f"{item_path}.path")
            if asset["sha256"] != old.sha256:
                report.conflicts.append(f"{item_path}.sha256")
        assets.append(asset)
    return assets


def _migrate_measurement(entry: Mapping[str, Any], path: str, measured: bool, report: _Report) -> tuple[Any, Any]:
    if not measured:
        return None, None
    ref = entry.get("measurement_ref")
    peak = entry.get("physical_resident_peak_bytes")
    if ref is None:
        report.missing.append(f"{path}.measurement_ref")
    if peak is None:
        report.missing.append(f"{path}.physical_resident_peak_bytes")
    return ref, peak


def _migrate_models(
    legacy: AppConfig, inventory_models: Mapping[str, Any], report: _Report
) -> tuple[list[dict[str, Any]], tuple[str, ...], tuple[str, ...]]:
    """Carry every v1 model over under its own id, keeping pinned/preload intent."""
    models: list[dict[str, Any]] = []
    pinned: list[str] = []
    preload: list[str] = []
    for model_id in sorted(legacy.models):
        old = legacy.models[model_id]
        path = f"models.{model_id}"
        entry = inventory_models.get(model_id) if isinstance(inventory_models, Mapping) else None
        if entry is None:
            report.missing.append(path)
            continue
        envelope = _migrate_envelope(entry, path, report)
        if envelope.get("max_parallel") is not None and envelope["max_parallel"] != old.scheduling.max_concurrency:
            report.conflicts.append(f"{path}.envelope.max_parallel")
        assets = _migrate_assets(entry, path, old, report)
        measured = entry.get("measured")
        if not isinstance(measured, bool):
            report.missing.append(f"{path}.measured")
            measured, measurement_ref, physical_peak = False, None, None
        else:
            measurement_ref, physical_peak = _migrate_measurement(entry, path, measured, report)
        port = urlsplit(old.upstream_url).port
        models.append(
            {
                "model_id": model_id,
                "runtime_id": report.need(entry, "runtime_id", path),
                "capabilities": list(sorted(old.capabilities)),
                "assets": assets,
                "port": port if port is not None else 0,
                "envelope": envelope,
                "timeout_seconds": report.need(entry, "timeout_seconds", path),
                # v1 reserves are raw peaks; the single margin is applied here, once.
                "reserved_bytes": effective_reserved_bytes(
                    old.memory.reserved_bytes, legacy_v1_margin=legacy.scheduler.resource_safety_margin
                ),
                "measured": measured,
                "measurement_ref": measurement_ref,
                "physical_resident_peak_bytes": physical_peak,
            }
        )
        if old.scheduling.pinned:
            pinned.append(model_id)
        if old.lifecycle.preload:
            preload.append(model_id)
    return models, tuple(pinned), tuple(preload)


def _migrate_document(legacy: AppConfig, inventory: Mapping[str, Any], report: _Report) -> dict[str, Any]:
    inventory_models = _mapping_or_none(inventory.get("models"))
    for model_id in sorted(inventory_models):
        if model_id not in legacy.models:
            report.conflicts.append(f"models.{model_id}")

    models, pinned, preload = _migrate_models(legacy, inventory_models, report)
    referenced = {model["runtime_id"] for model in models if model["runtime_id"] is not None}
    runtimes = _migrate_runtimes(referenced, _mapping_or_none(inventory.get("runtimes")), report)

    old_scheduler = legacy.scheduler
    old_storage = legacy.storage
    return {
        "schema_version": 2,
        "registration": {"runtimes": runtimes, "models": models},
        "server": {
            "host": legacy.server.host,
            "port": legacy.server.port,
            "workers": legacy.server.workers,
            "max_request_body_bytes": legacy.server.max_request_body_bytes,
            "body_timeout_seconds": legacy.server.body_timeout_seconds,
            "shutdown_grace_seconds": legacy.server.shutdown_grace_seconds,
        },
        "scheduler": {
            "poll_interval_seconds": old_scheduler.poll_interval_seconds,
            "request_queue_timeout_seconds": old_scheduler.request_queue_timeout_seconds,
            "queue_capacity": old_scheduler.queue_capacity,
            "priority_aging_seconds": old_scheduler.priority_aging_seconds,
            "switch_drain_timeout_seconds": old_scheduler.switch_drain_timeout_seconds,
            "switch_retry_seconds": old_scheduler.switch_retry_seconds,
            "resource_safety_margin": old_scheduler.resource_safety_margin,
            "min_free_memory_bytes": old_scheduler.min_free_memory_bytes,
            "max_evictions_per_request": old_scheduler.max_evictions_per_request,
            "memory_reclaim_timeout_seconds": old_scheduler.memory_reclaim_timeout_seconds,
            "pinned_models": list(pinned),
            "preload_models": list(preload),
            "heat": {
                "half_life_seconds": old_scheduler.heat.half_life_seconds,
                "request_weight": old_scheduler.heat.request_weight,
                "token_weight": old_scheduler.heat.token_weight,
            },
            "thrash": {
                "switch_window_seconds": old_scheduler.thrash.switch_window_seconds,
                "max_switches_in_window": old_scheduler.thrash.max_switches_in_window,
                "cooldown_seconds": old_scheduler.thrash.cooldown_seconds,
            },
            "sessions": dict(SESSION_DEFAULTS),
        },
        "resources": {
            "provider": legacy.resources.provider,
            "system_reserve_bytes": legacy.resources.system_reserve_bytes,
            "sample_interval_seconds": legacy.resources.sample_interval_seconds,
            "sample_max_age_seconds": legacy.resources.sample_max_age_seconds,
            "model_budget_bytes": _mapping_or_none(inventory.get("resources")).get("model_budget_bytes"),
        },
        "storage": {
            "mount_path": str(old_storage.mount_path),
            "model_directory": str(old_storage.model_directory),
            "expected_uuid": old_storage.expected_uuid,
            "filesystem": old_storage.filesystem,
            "verify_timeout_seconds": 900,
        },
        "gateway": {
            "connect_timeout_seconds": legacy.gateway.connect_timeout_seconds,
            "pool_timeout_seconds": legacy.gateway.pool_timeout_seconds,
            "read_idle_timeout_seconds": legacy.gateway.read_idle_timeout_seconds,
            "write_idle_timeout_seconds": legacy.gateway.write_idle_timeout_seconds,
            "inference_timeout_seconds": legacy.gateway.inference_timeout_seconds,
            "close_timeout_seconds": legacy.gateway.close_timeout_seconds,
            "max_response_body_bytes": legacy.gateway.max_response_body_bytes,
            "max_sse_event_bytes": legacy.gateway.max_sse_event_bytes,
        },
        "control": {
            "socket_path": _mapping_or_none(inventory.get("control")).get("socket_path"),
            "allowed_uids": list(report.need(_mapping_or_none(inventory.get("control")), "allowed_uids", "control") or []),
        },
        "blobs": {
            "root": report.need(_mapping_or_none(inventory.get("blobs")), "root", "blobs"),
            "owner_quota_bytes": BLOB_DEFAULTS["owner_quota_bytes"],
            "total_quota_bytes": BLOB_DEFAULTS["total_quota_bytes"],
            "chunk_reserve_bytes": BLOB_DEFAULTS["chunk_reserve_bytes"],
            "min_free_disk_bytes": BLOB_DEFAULTS["min_free_disk_bytes"],
            "input_ttl_seconds": BLOB_DEFAULTS["input_ttl_seconds"],
            "output_ttl_seconds": BLOB_DEFAULTS["output_ttl_seconds"],
            "max_blob_bytes": BLOB_DEFAULTS["max_blob_bytes"],
        },
        "candidate_sha256": None,
    }


def migrate_v2(source: str | Path, inventory: str | Path, output: str | Path) -> dict[str, Any]:
    """Convert a strict v1 configuration into a strict v2 one, or write nothing."""
    destination = Path(output)
    if destination.exists():
        raise MigrationError("migration output already exists")
    source_path = Path(source)
    try:
        inventory_data = parse_json_document(Path(inventory).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MigrationError(f"cannot read migration inventory: {exc}") from exc
    if not isinstance(inventory_data, Mapping):
        raise MigrationError("migration inventory must be a JSON object")
    original = source_path.read_bytes()
    try:
        legacy = load_config(source_path)
    except (ConfigError, OSError) as exc:
        raise MigrationError(f"cannot read v1 configuration: {exc}") from exc
    if isinstance(legacy, AppConfigV2):
        raise MigrationError("input configuration is already schema_version 2")

    report = _Report()
    document = _migrate_document(legacy, inventory_data, report)
    if document["resources"]["model_budget_bytes"] is None:
        report.missing.append("resources.model_budget_bytes")
    if report.conflicts or report.missing:
        raise MissingInventory(report.missing, report.conflicts)
    if source_path.read_bytes() != original:
        raise MigrationError("input configuration changed while migrating")
    if document["control"]["socket_path"] is None:
        del document["control"]["socket_path"]
    try:
        parse_v2_config(document)
    except ConfigError as exc:
        raise MigrationError(f"migrated configuration would not load: {exc}") from exc
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return document
