"""CT09: the frozen lab candidate, and the checks that refuse a cosmetic one.

Everything CT10 runs on the device must be pinned here: the tested commit, the
lab input it uses, the runtime chat policy source, the fixture set, the template
hashes, the loopback address, the timeouts, the request budget, the budget caps
and the rollback input. This module is the *record and its checks*, never a
measurement: a value nobody measured has to stay visible as an exception, and a
placeholder, a floating image tag or a budget too small for the required suite
is refused instead of being discovered while the device is busy.

The record is plain JSON data, so it can be re-read, re-hashed and compared by
the evaluator; nothing here derives a verdict from a stored status.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from typing import Any, Mapping, Sequence

from .chat_compat import COMPAT_CAPABILITIES, ROUNDS, VARIANTS

SCHEMA_VERSION = 1

FREEZE_KEYS = frozenset({
    "schema_version", "code_sha", "lab_input", "policy_source_sha256", "fixture_set_sha256",
    "template_hashes", "service_base_url", "timeouts", "request_limit", "budget_caps",
    "models", "rollback", "exceptions", "frozen_at_utc",
})
LAB_INPUT_KEYS = frozenset({"path", "sha256"})
TIMEOUT_KEYS = frozenset({"connect_seconds", "read_idle_seconds", "total_seconds"})
ROLLBACK_KEYS = frozenset({"input_path", "input_sha256", "code_sha"})
MODEL_KEYS = frozenset({"capabilities", "image_digest", "model_sha256", "template_sha256",
                        "max_parallel", "effort_values"})

LEGACY_VARIANTS = 4  # chat-json, chat-sse, vision-json, cold-count
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
# Values an authoring hand leaves behind; a frozen record may not contain them.
PLACEHOLDERS = frozenset({"REQUIRED_64_HEX", "REQUIRED", "TBD", "TODO", "unknown", "UNKNOWN",
                          "placeholder", "PLACEHOLDER", "<fill>", "null"})
_LOOPBACK = ("http://127.0.0.1:", "http://localhost:", "http://[::1]:")


class FreezeError(RuntimeError):
    """The record is not a freezable candidate input."""


def _mapping(value: Any, where: str) -> Mapping:
    if not isinstance(value, dict):
        raise FreezeError(f"{where}: expected an object, got {type(value).__name__}")
    return value


def _exact_keys(data: Mapping, allowed: frozenset, where: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise FreezeError(f"{where}: unknown fields: {', '.join(unknown)}")
    missing = sorted(allowed - set(data))
    if missing:
        raise FreezeError(f"{where}: missing fields: {', '.join(missing)}")


def _text(value: Any, where: str, *, pattern: re.Pattern | None = None) -> str:
    if not isinstance(value, str) or not value:
        raise FreezeError(f"{where}: must be a non-empty string")
    if pattern is not None and not pattern.fullmatch(value):
        raise FreezeError(f"{where}: {value!r} is not a lowercase hex digest" if pattern is _SHA256_RE
                          else f"{where}: {value!r} is not a full commit sha")
    return value


def _positive(value: Any, where: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise FreezeError(f"{where}: must be a finite positive number")
    return value


def parse_freeze(document: Mapping[str, Any]) -> dict:
    """Validate the freeze record; a record that cannot be trusted is refused."""
    data = _mapping(document, "freeze")
    _exact_keys(data, FREEZE_KEYS, "freeze")
    if data["schema_version"] != SCHEMA_VERSION:
        raise FreezeError(f"freeze: unsupported schema_version {data['schema_version']!r}")
    code_sha = _text(data["code_sha"], "code_sha", pattern=_COMMIT_RE)

    lab = _mapping(data["lab_input"], "lab_input")
    _exact_keys(lab, LAB_INPUT_KEYS, "lab_input")
    _text(lab["path"], "lab_input.path")
    _text(lab["sha256"], "lab_input.sha256", pattern=_SHA256_RE)

    policy = _text(data["policy_source_sha256"], "policy_source_sha256", pattern=_SHA256_RE)
    fixture_set = _text(data["fixture_set_sha256"], "fixture_set_sha256", pattern=_SHA256_RE)

    templates = _mapping(data["template_hashes"], "template_hashes")
    for model_id, digest in templates.items():
        _text(digest, f"template_hashes[{model_id}]")  # content, checked below

    url = _text(data["service_base_url"], "service_base_url")
    if "@" in url.split("://", 1)[-1]:
        raise FreezeError("service_base_url: credentials are never part of a frozen base url")
    if url.rstrip("/").endswith("/v1"):
        raise FreezeError("service_base_url: the base url carries no /v1 suffix")
    if not url.startswith(_LOOPBACK):
        raise FreezeError(f"service_base_url {url!r} is not the loopback service under test")

    timeouts = _mapping(data["timeouts"], "timeouts")
    _exact_keys(timeouts, TIMEOUT_KEYS, "timeouts")
    for key in sorted(TIMEOUT_KEYS):
        _positive(timeouts[key], f"timeouts.{key}")

    limit = data["request_limit"]
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise FreezeError("request_limit: must be a positive integer")

    caps = _mapping(data["budget_caps"], "budget_caps")
    for model_id, cap in caps.items():
        if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
            raise FreezeError(f"budget_caps[{model_id}]: must be a positive integer")

    models = _mapping(data["models"], "models")
    if not models:
        raise FreezeError("models: at least one model must be frozen")
    for model_id, entry in models.items():
        parsed_entry = _mapping(entry, f"models[{model_id}]")
        _exact_keys(parsed_entry, MODEL_KEYS, f"models[{model_id}]")
        capabilities = parsed_entry["capabilities"]
        if not isinstance(capabilities, list) or not capabilities \
                or not all(isinstance(item, str) for item in capabilities):
            raise FreezeError(f"models[{model_id}].capabilities: must be a non-empty list of strings")
        if not isinstance(parsed_entry["image_digest"], str) or not parsed_entry["image_digest"]:
            raise FreezeError(f"models[{model_id}].image_digest: must be a non-empty string")
        # Structure only: a placeholder or a zero digest is a *content* problem
        # `check_freeze` reports, so it stays visible instead of an opaque refusal.
        _text(parsed_entry["model_sha256"], f"models[{model_id}].model_sha256")
        _text(parsed_entry["template_sha256"], f"models[{model_id}].template_sha256")
        if isinstance(parsed_entry["max_parallel"], bool) or not isinstance(parsed_entry["max_parallel"], int) \
                or parsed_entry["max_parallel"] < 1:
            raise FreezeError(f"models[{model_id}].max_parallel: must be a positive integer")

    rollback = _mapping(data["rollback"], "rollback")
    _exact_keys(rollback, ROLLBACK_KEYS, "rollback")
    _text(rollback["input_path"], "rollback.input_path")
    _text(rollback["input_sha256"], "rollback.input_sha256", pattern=_SHA256_RE)
    if rollback["code_sha"] is not None:
        _text(rollback["code_sha"], "rollback.code_sha", pattern=_COMMIT_RE)

    exceptions = data["exceptions"]
    if not isinstance(exceptions, list) or not all(isinstance(item, str) for item in exceptions):
        raise FreezeError("exceptions: must be a list of strings")

    stamp = _text(data["frozen_at_utc"], "frozen_at_utc")
    try:
        parsed_stamp = datetime.fromisoformat(stamp)
    except ValueError as exc:
        raise FreezeError(f"frozen_at_utc: {stamp!r} is not an ISO-8601 timestamp") from exc
    if parsed_stamp.tzinfo is None:
        raise FreezeError("frozen_at_utc: the timestamp must carry its timezone")

    return {
        "schema_version": SCHEMA_VERSION,
        "code_sha": code_sha,
        "lab_input": dict(lab),
        "policy_source_sha256": policy,
        "fixture_set_sha256": fixture_set,
        "template_hashes": dict(templates),
        "service_base_url": url,
        "timeouts": dict(timeouts),
        "request_limit": limit,
        "budget_caps": dict(caps),
        "models": dict(models),
        "rollback": dict(rollback),
        "exceptions": list(exceptions),
        "frozen_at_utc": stamp,
    }


def minimum_requests(models: Mapping[str, Any], *, capabilities_of: Sequence[str] = COMPAT_CAPABILITIES) -> int:
    """How many generation requests the frozen suite needs, at least.

    Each compat capability costs one two-round case per variant, the tools and
    thinking combination costs another set, and a legacy model still owes its
    four plain variants plus one budget-boundary batch of `max_parallel`.
    """
    total = 0
    for model_id, entry in models.items():
        entry = _mapping(entry, f"models[{model_id}]")
        declared = set(entry.get("capabilities", ()))
        parallel = entry.get("max_parallel", 1)
        compat = [capability for capability in capabilities_of if capability in declared]
        total += len(compat) * len(VARIANTS) * ROUNDS
        if len(compat) == len(capabilities_of) and len(compat) > 1:
            total += len(VARIANTS) * ROUNDS  # the tools+thinking combination case
        if declared & {"chat", "vision"}:
            total += LEGACY_VARIANTS + int(parallel)
    return total


def check_freeze(freeze: Mapping[str, Any]) -> list[str]:
    """The reasons this record cannot be handed to the device; empty means freezable."""
    problems: list[str] = []
    models = freeze.get("models", {})
    for model_id, entry in models.items():
        entry = entry if isinstance(entry, Mapping) else {}
        digest = entry.get("image_digest")
        if not isinstance(digest, str) or "@sha256:" not in digest:
            problems.append(f"models[{model_id}].image_digest: a frozen image must be a digest, not a floating tag")
        for key in ("model_sha256", "template_sha256"):
            value = entry.get(key)
            if not isinstance(value, str) or not value:
                problems.append(f"models[{model_id}].{key}: must be a non-empty string")
            elif value in PLACEHOLDERS:
                problems.append(f"models[{model_id}].{key}: {value!r} is a placeholder")
            elif not _SHA256_RE.fullmatch(value):
                problems.append(f"models[{model_id}].{key}: must be a lowercase 64-hex digest")
            elif set(value) == {"0"}:
                problems.append(f"models[{model_id}].{key}: an all-zero digest is a placeholder")
        if "effort_values" in entry and entry["effort_values"] is None:
            problems.append(f"models[{model_id}].effort_values: an undefined effort set is not a frozen value")
        effort = entry.get("effort_values")
        if isinstance(effort, list) and any(not isinstance(item, str) or not item for item in effort):
            problems.append(f"models[{model_id}].effort_values: must hold non-empty strings")

    templates = freeze.get("template_hashes")
    templates = templates if isinstance(templates, Mapping) else {}
    caps = freeze.get("budget_caps")
    caps = caps if isinstance(caps, Mapping) else {}
    for model_id, entry in models.items():
        entry = entry if isinstance(entry, Mapping) else {}
        frozen_template = templates.get(model_id)
        if not isinstance(frozen_template, str) or not frozen_template:
            problems.append(f"template_hashes[{model_id}]: the frozen templates do not name this model")
        elif frozen_template in PLACEHOLDERS or not _SHA256_RE.fullmatch(frozen_template):
            problems.append(f"template_hashes[{model_id}]: {frozen_template!r} is not a frozen 64-hex template")
        elif frozen_template != entry.get("template_sha256"):
            problems.append(f"template_hashes[{model_id}]: does not match the model entry's template_sha256")
        if model_id not in caps:
            problems.append(f"budget_caps[{model_id}]: a frozen model without a budget cap")
    for label, mapping in (("template_hashes", templates), ("budget_caps", caps)):
        for model_id in sorted(set(mapping) - set(models)):
            problems.append(f"{label}[{model_id}]: names a model that is not frozen")

    for holder in (freeze, freeze.get("lab_input") or {}, freeze.get("rollback") or {}):
        for key, value in holder.items():
            if isinstance(value, str) and value in PLACEHOLDERS:
                problems.append(f"{key}: {value!r} is an undefined value")

    required = minimum_requests(models)
    limit = freeze.get("request_limit")
    if isinstance(limit, int) and not isinstance(limit, bool) and limit < required:
        problems.append(f"request_limit: {limit} is below the {required} requests the frozen suite needs")
    if not freeze.get("exceptions"):
        problems.append("exceptions: a freeze without recorded exceptions claims nothing was left unmeasured")
    return problems


def freeze_digest(freeze: Mapping[str, Any]) -> str:
    """One digest for the whole record: the identity CT10 binds its run to."""
    payload = json.dumps(freeze, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                         allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
