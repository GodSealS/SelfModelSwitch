"""Strict control-protocol-v1 DTOs, limits, error map and schema export (M01/P02).

This module is the executable contract for the local control API described in
`plan/03-api.md` and fixed by C03--C06 in `plan/08-execution-plan.md`. It is a
pure contracts module: nothing here talks to HTTP, Docker, storage or the
scheduler.

Strictness rules shared with `contracts_v2`:

* unknown fields, duplicate JSON keys, booleans posing as integers, non-finite
  numbers, NUL bytes, non-UTF-8 text and non-JSON value types are rejected;
* every identifier and digest is matched against the whole string;
* the JSON parser and canonical serializer are imported from `contracts_v2` so
  the whole service shares one `ContractError` type and one canonical form.

Terminal evidence cannot be forged:

* `succeeded` requires a result and no error, `failed`/`cancelled` require an
  error and no result;
* every execution view carries a complete fence whose `execution_id` matches the
  view and whose `attempt` starts at 1;
* a terminal view must report `compute_quiescent=true`; a non-terminal view must
  report `compute_quiescent`, `result` and `error` as null;
* a `dispatched` view requires a full instance identity, while a `not_started`
  view must not invent one (a queued cancellation is legal without a container).

`python -m model_scheduler.control_protocol_v1 export-schema --output PATH`
renders the JSON Schema document for independent clients. The document is
derived from these constants, enumerations and DTO parsers only; it is not a
hand-maintained copy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from .contracts_v2 import CAPABILITY_MATRIX, ContractError, canonical_json_bytes, parse_json_document

PROTOCOL_VERSION = 1
PROTOCOL_VERSION_HEADER = "X-SMS-Protocol-Version"

# C05: control v1 limits. Byte limits are measured on canonical JSON.
MAX_INLINE_INPUT_BYTES = 4 * 1024 * 1024
MAX_PARAMETERS_BYTES = 8_192
MAX_BLOB_BYTES = 1024**3
BLOB_RESULT_RETENTION_SECONDS = 86_400
IDEMPOTENCY_KEY_MAX_LENGTH = 128
CORRELATION_ID_MAX_LENGTH = 128
REQUEST_ID_MAX_LENGTH = 128

# C06: the executable capability set is closed; audio, Torch and ORT are not
# part of it and may only be added together with their own M00 and fixtures.
CAPABILITIES = frozenset(CAPABILITY_MATRIX)

# C04 session states and preparing phases. "ready" is not a session state.
SESSION_STATES = frozenset({"preparing", "active", "draining", "blocked", "closed"})
SESSION_PHASES = frozenset({"queued", "loading", "draining_existing"})

# C04 execution states. A cancelling execution stays cancelling until a trusted
# stop or terminal proof exists; no state is invented on timeout.
EXECUTION_STATES = frozenset({"queued", "running", "succeeded", "failed", "cancelling", "cancelled"})
EXECUTION_TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})

# C03 dispatch bookkeeping: locally terminated requests use `not_started`.
DISPATCH_STATES = frozenset({"not_started", "dispatched"})

# C05 error table: code -> (HTTP status, retryable). `retryable` only means the
# state may recover; the service never re-sends an uncertain inference.
_ERROR_TABLE: Mapping[str, tuple[int, bool]] = {
    "malformed_json": (400, False),
    "unsupported_protocol": (400, False),
    "peer_forbidden": (403, False),
    "not_found": (404, False),
    "stale_token": (409, False),
    "session_expired": (409, False),
    "idempotency_conflict": (409, False),
    "busy": (409, True),
    "blob_in_use": (409, True),
    "reference_expired": (410, False),
    "payload_too_large": (413, False),
    "unsupported_media_type": (415, False),
    "contract_violation": (422, False),
    "envelope_exceeded": (422, False),
    "capability_mismatch": (422, False),
    "queue_full": (429, True),
    "quota_exceeded": (429, True),
    "backend_failed": (502, False),
    "instance_unknown": (503, True),
    "storage_unavailable": (503, True),
    "resource_unavailable": (503, True),
    "temporarily_unavailable": (503, True),
    "queue_timeout": (504, True),
    "execution_timeout": (504, False),
}
ERROR_STATUS: Mapping[str, int] = {code: status for code, (status, _) in _ERROR_TABLE.items()}
RETRYABLE_ERROR_CODES = frozenset(code for code, (_, retryable) in _ERROR_TABLE.items() if retryable)

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ID_RE = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,61}[a-z0-9])?")
_ID_PATTERN = r"^[a-z0-9](?:[a-z0-9._-]{0,61}[a-z0-9])?$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_MEDIA_TYPE_RE = re.compile(r"[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*")
_MEDIA_TYPE_PATTERN = r"^[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*$"
_IMAGE_DIGEST_RE = re.compile(r"[^@\s]+@sha256:[0-9a-f]{64}")
_IMAGE_DIGEST_PATTERN = r"^[^@\s]+@sha256:[0-9a-f]{64}$"

_MAX_PROTOCOL_INTEGER = 2**31 - 1


# ---------------------------------------------------------------------------
# capability parameter rules (C06)
#
# `parameters` is a closed set per capability: a key outside the operation's
# table is rejected, and so is a value outside its type or range. Derived
# defaults (max_tokens 4096 capped by the model envelope, rerank top_n as the
# document count) and the vision blob `text` requirement are applied by the
# execution layer, not here; this module only locks the wire format.


@dataclass(frozen=True)
class ParameterRule:
    """One allowed parameter: its JSON kind and machine-checkable bounds."""

    kind: str
    description: str
    minimum: int | float | None = None
    maximum: int | float | None = None
    exclusive_minimum: bool = False
    const: str | None = None


_CHAT_PARAMETERS: Mapping[str, ParameterRule] = {
    "max_tokens": ParameterRule(
        "integer",
        "requested output tokens; the effective value is min(4096, model max_output_tokens)",
        minimum=1,
        maximum=_MAX_PROTOCOL_INTEGER,
    ),
    "temperature": ParameterRule("number", "sampling temperature", minimum=0, maximum=2),
    "top_p": ParameterRule("number", "nucleus sampling probability", minimum=0, maximum=1, exclusive_minimum=True),
    "seed": ParameterRule("integer", "deterministic sampling seed", minimum=0, maximum=_MAX_PROTOCOL_INTEGER),
    # Acceptance needs a round that really consumes its declared output budget: a
    # model that stops early would leave the boundary unproven rather than unmet.
    "ignore_eos": ParameterRule("boolean", "keep generating until max_tokens, ignoring the end-of-sequence token"),
}

PARAMETER_RULES: Mapping[str, Mapping[str, ParameterRule]] = {
    "chat": _CHAT_PARAMETERS,
    "vision": {
        **_CHAT_PARAMETERS,
        # Only used with an image Blob input, where it is required; a plain
        # inline data URL carries its text inside the chat payload.
        "text": ParameterRule("string", "user text paired with an image Blob input"),
    },
    "embeddings": {
        "encoding_format": ParameterRule("const", "first release accepts only float", const="float"),
    },
    "rerank": {
        "top_n": ParameterRule("integer", "number of ranked documents to return", minimum=1, maximum=_MAX_PROTOCOL_INTEGER),
        "return_documents": ParameterRule("boolean", "include document text in the response"),
    },
}

# The capability set and the parameter tables must not drift apart: a new
# capability without its own closed parameter table would silently accept
# arbitrary parameters.
if set(PARAMETER_RULES) != set(CAPABILITIES):
    raise RuntimeError(
        "capability/parameter mismatch: "
        f"rules={sorted(PARAMETER_RULES)} capabilities={sorted(CAPABILITIES)}"
    )


# ---------------------------------------------------------------------------
# strict value helpers


def _mapping(value: Any, where: str) -> Mapping:
    if not isinstance(value, dict):
        raise ContractError(f"{where}: expected an object, got {type(value).__name__}")
    return value


def _exact_keys(data: Mapping, allowed: frozenset[str], where: str) -> None:
    unknown = sorted(set(data) - set(allowed))
    if unknown:
        raise ContractError(f"{where}: unknown fields: {', '.join(unknown)}")


def _required(data: Mapping, key: str, where: str) -> Any:
    if key not in data:
        raise ContractError(f"{where}: {key} is required")
    return data[key]


def _check_utf8_text(value: str, where: str) -> str:
    """Every nested string must be NUL-free and encodable as UTF-8."""
    if "\x00" in value:
        raise ContractError(f"{where}: must not contain NUL")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ContractError(f"{where}: must be valid UTF-8") from exc
    return value


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError(f"{where}: must be a non-empty string")
    return _check_utf8_text(value, where)


def _text_field(data: Mapping, key: str, where: str) -> str:
    return _text(_required(data, key, where), f"{where}.{key}")


def _bounded_text(value: Any, where: str, *, max_bytes: int) -> str:
    text = _text(value, where)
    if len(text.encode("utf-8")) > max_bytes:
        raise ContractError(f"{where}: must be at most {max_bytes} UTF-8 bytes")
    return text


def _bounded_text_field(data: Mapping, key: str, where: str, *, max_bytes: int) -> str:
    return _bounded_text(_required(data, key, where), f"{where}.{key}", max_bytes=max_bytes)


def _id_field(data: Mapping, key: str, where: str) -> str:
    value = _text_field(data, key, where)
    if not _ID_RE.fullmatch(value):
        raise ContractError(f"{where}: {key} must be a lowercase opaque id: {value!r}")
    return value


def _int_field(
    data: Mapping,
    key: str,
    where: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    value = _required(data, key, where)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{where}: {key} must be an integer")
    if minimum is not None and value < minimum:
        raise ContractError(f"{where}: {key} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ContractError(f"{where}: {key} must be <= {maximum}")
    return value


def _enum_field(data: Mapping, key: str, where: str, allowed: frozenset[str]) -> str:
    value = _text_field(data, key, where)
    if value not in allowed:
        raise ContractError(f"{where}: {key} must be one of {', '.join(sorted(allowed))}")
    return value


def _nullable_enum_field(data: Mapping, key: str, where: str, allowed: frozenset[str]) -> str | None:
    value = _required(data, key, where)
    if value is None:
        return None
    if not isinstance(value, str) or value not in allowed:
        raise ContractError(f"{where}: {key} must be null or one of {', '.join(sorted(allowed))}")
    return value


def _sha256_field(value: Any, where: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ContractError(f"{where}: must be a lowercase 64-hex SHA-256")
    return value


def _utc_timestamp_field(data: Mapping, key: str, where: str) -> str:
    value = _text_field(data, key, where)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ContractError(f"{where}: {key} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ContractError(f"{where}: {key} must carry a UTC offset")
    return value


def _validate_json_value(value: Any, where: str) -> None:
    """Reject anything that is not plain finite JSON with UTF-8 text.

    Used for nested payloads (`inline` input and idempotency payloads) where the
    shape is owned by the capability adapter, not by this module.
    """
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractError(f"{where}: non-finite numbers are not allowed")
        return
    if isinstance(value, str):
        _check_utf8_text(value, where)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ContractError(f"{where}: object keys must be strings")
            _check_utf8_text(key, f"{where} (key)")
            _validate_json_value(item, f"{where}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{where}[{index}]")
        return
    raise ContractError(f"{where}: unsupported value type {type(value).__name__}")


# ---------------------------------------------------------------------------
# DTOs


@dataclass(frozen=True)
class WritebackDecision:
    """The verdict on one late write-back, plus the raw fence it carried (C03).

    A rejected write-back is never dropped silently: the caller records the
    decision with its reason and the original fence as raw material, so the
    evidence shows what arrived too late instead of losing it.
    """

    accepted: bool
    reason: str | None
    fence: "Fence"

    def as_dict(self) -> dict[str, object]:
        return {"accepted": self.accepted, "reason": self.reason, "fence": fence_document(self.fence)}


WRITEBACK_REJECTIONS = frozenset(
    {
        "stale_boot",
        "foreign_model",
        "stale_generation",
        "stale_operation",
        "foreign_execution",
        "attempt_mismatch",
        "stale_attempt",
    }
)


def fence_document(fence: "Fence") -> dict[str, object]:
    """The raw fence as a plain JSON object, for events and evidence."""
    if not isinstance(fence, Fence):
        raise ContractError("fence_document: a Fence is required")
    return {
        "boot_id": fence.boot_id,
        "model_id": fence.model_id,
        "generation": fence.generation,
        "operation_id": fence.operation_id,
        "execution_id": fence.execution_id,
        "attempt": fence.attempt,
    }


def writeback_decision(current: "Fence", incoming: "Fence") -> WritebackDecision:
    """C03: only the same boot, generation, operation and a non-older attempt may apply.

    A restart gets a new `boot_id`, a new load a new `generation`/`operation_id`,
    and every execution attempt is numbered, so a late result or terminal event
    from a superseded instance can never change the current books.
    """
    if not isinstance(current, Fence) or not isinstance(incoming, Fence):
        raise ContractError("writeback_decision: both fences must be Fence values")
    checks = (
        ("stale_boot", incoming.boot_id != current.boot_id),
        ("foreign_model", incoming.model_id != current.model_id),
        ("stale_generation", incoming.generation != current.generation),
        ("stale_operation", incoming.operation_id != current.operation_id),
        ("foreign_execution", current.execution_id is not None and incoming.execution_id != current.execution_id),
        ("attempt_mismatch", (current.attempt is None) != (incoming.attempt is None)),
    )
    for reason, failed in checks:
        if failed:
            return WritebackDecision(False, reason, incoming)
    if current.attempt is not None and incoming.attempt < current.attempt:
        return WritebackDecision(False, "stale_attempt", incoming)
    return WritebackDecision(True, None, incoming)


@dataclass(frozen=True)
class ErrorDetail:
    code: str
    message: str
    retryable: bool


@dataclass(frozen=True)
class Fence:
    """C03 fence: non-execution operations carry null execution_id and attempt."""

    boot_id: str
    model_id: str
    generation: int
    operation_id: str
    execution_id: str | None
    attempt: int | None


@dataclass(frozen=True)
class InstanceIdentity:
    container_id: str
    started_at: str
    deployment_id: str
    model_id: str
    runtime_id: str
    candidate_digest: str
    image_digest: str


@dataclass(frozen=True)
class BlobRef:
    blob_id: str
    owner: str
    sha256: str
    size_bytes: int
    media_type: str


@dataclass(frozen=True)
class ExecutionInput:
    inline: dict | None
    blob: BlobRef | None


@dataclass(frozen=True)
class SessionCreateRequest:
    model_id: str
    idempotency_key: str
    correlation_id: str | None


@dataclass(frozen=True)
class SessionView:
    session_id: str
    state: str
    phase: str | None
    boot_id: str
    model_id: str
    expires_in_ms: int
    hard_remaining_ms: int
    owner_token: str | None
    error: ErrorDetail | None


@dataclass(frozen=True)
class ExecutionCreateRequest:
    session_token: str
    operation: str
    input: ExecutionInput
    parameters: dict
    idempotency_key: str


@dataclass(frozen=True)
class ExecutionView:
    execution_id: str
    state: str
    dispatch_state: str
    compute_quiescent: bool | None
    result: BlobRef | None
    error: ErrorDetail | None
    instance: InstanceIdentity | None
    fence: Fence


# ---------------------------------------------------------------------------
# parsers

ERROR_DOCUMENT_KEYS = frozenset({"error", "request_id"})
ERROR_DETAIL_KEYS = frozenset({"code", "message", "retryable"})
SESSION_CREATE_KEYS = frozenset({"model_id", "idempotency_key", "correlation_id"})
SESSION_VIEW_KEYS = frozenset(
    {"session_id", "state", "phase", "boot_id", "model_id", "expires_in_ms", "hard_remaining_ms", "owner_token", "error"}
)
EXECUTION_CREATE_KEYS = frozenset({"session_token", "operation", "input", "parameters", "idempotency_key"})
EXECUTION_INPUT_KEYS = frozenset({"inline", "blob"})
BLOB_REF_KEYS = frozenset({"blob_id", "owner", "sha256", "size_bytes", "media_type"})
EXECUTION_VIEW_KEYS = frozenset(
    {"execution_id", "state", "dispatch_state", "compute_quiescent", "result", "error", "instance", "fence"}
)
FENCE_KEYS = frozenset({"boot_id", "model_id", "generation", "operation_id", "execution_id", "attempt"})
INSTANCE_KEYS = frozenset(
    {"container_id", "started_at", "deployment_id", "model_id", "runtime_id", "candidate_digest", "image_digest"}
)


def parse_error_detail(data: Mapping, where: str = "error") -> ErrorDetail:
    data = _mapping(data, where)
    _exact_keys(data, ERROR_DETAIL_KEYS, where)
    code = _text_field(data, "code", where)
    if code not in ERROR_STATUS:
        raise ContractError(f"{where}: unknown error code: {code!r}")
    message = _text_field(data, "message", where)
    retryable = _required(data, "retryable", where)
    if not isinstance(retryable, bool):
        raise ContractError(f"{where}: retryable must be a boolean")
    if retryable is not (code in RETRYABLE_ERROR_CODES):
        raise ContractError(f"{where}: retryable does not match the error table for {code!r}")
    return ErrorDetail(code=code, message=message, retryable=retryable)


def parse_error_document(data: Mapping, where: str = "error_document") -> tuple[ErrorDetail, str]:
    data = _mapping(data, where)
    _exact_keys(data, ERROR_DOCUMENT_KEYS, where)
    detail = parse_error_detail(_required(data, "error", where), f"{where}.error")
    request_id = _bounded_text_field(data, "request_id", where, max_bytes=REQUEST_ID_MAX_LENGTH)
    return detail, request_id


def error_document(code: str, message: str, request_id: str) -> dict:
    if code not in ERROR_STATUS:
        raise ContractError(f"error_document: unknown error code: {code!r}")
    detail = ErrorDetail(code=code, message=_text(message, "message"), retryable=code in RETRYABLE_ERROR_CODES)
    return {
        "error": {"code": detail.code, "message": detail.message, "retryable": detail.retryable},
        "request_id": _bounded_text(request_id, "request_id", max_bytes=REQUEST_ID_MAX_LENGTH),
    }


def is_retryable_error(code: str) -> bool:
    if code not in ERROR_STATUS:
        raise ContractError(f"unknown error code: {code!r}")
    return code in RETRYABLE_ERROR_CODES


def parse_session_create_request(data: Mapping, where: str = "session_create") -> SessionCreateRequest:
    data = _mapping(data, where)
    _exact_keys(data, SESSION_CREATE_KEYS, where)
    model_id = _id_field(data, "model_id", where)
    idempotency_key = _bounded_text_field(data, "idempotency_key", where, max_bytes=IDEMPOTENCY_KEY_MAX_LENGTH)
    correlation_id: str | None = None
    if "correlation_id" in data and data["correlation_id"] is not None:
        correlation_id = _bounded_text_field(data, "correlation_id", where, max_bytes=CORRELATION_ID_MAX_LENGTH)
    return SessionCreateRequest(model_id=model_id, idempotency_key=idempotency_key, correlation_id=correlation_id)


def parse_session_view(data: Mapping, where: str = "session_view") -> SessionView:
    data = _mapping(data, where)
    _exact_keys(data, SESSION_VIEW_KEYS, where)
    session_id = _id_field(data, "session_id", where)
    state = _enum_field(data, "state", where, SESSION_STATES)
    phase = _nullable_enum_field(data, "phase", where, SESSION_PHASES)
    boot_id = _text_field(data, "boot_id", where)
    model_id = _id_field(data, "model_id", where)
    expires_in_ms = _int_field(data, "expires_in_ms", where, minimum=0)
    hard_remaining_ms = _int_field(data, "hard_remaining_ms", where, minimum=0)
    token_raw = _required(data, "owner_token", where)
    if state == "closed":
        if token_raw is not None:
            raise ContractError(f"{where}: a closed session must report owner_token=null")
        owner_token: str | None = None
    else:
        owner_token = _text(token_raw, f"{where}.owner_token")
    error_raw = _required(data, "error", where)
    error = None if error_raw is None else parse_error_detail(error_raw, f"{where}.error")
    return SessionView(
        session_id=session_id,
        state=state,
        phase=phase,
        boot_id=boot_id,
        model_id=model_id,
        expires_in_ms=expires_in_ms,
        hard_remaining_ms=hard_remaining_ms,
        owner_token=owner_token,
        error=error,
    )


def parse_blob_ref(data: Mapping, where: str = "blob_ref") -> BlobRef:
    data = _mapping(data, where)
    _exact_keys(data, BLOB_REF_KEYS, where)
    blob_id = _id_field(data, "blob_id", where)
    owner = _text_field(data, "owner", where)
    sha256 = _sha256_field(_required(data, "sha256", where), f"{where}.sha256")
    size_bytes = _int_field(data, "size_bytes", where, minimum=1, maximum=MAX_BLOB_BYTES)
    media_type = _text_field(data, "media_type", where)
    if not _MEDIA_TYPE_RE.fullmatch(media_type):
        raise ContractError(f"{where}: media_type must be a lowercase type/subtype without parameters")
    return BlobRef(blob_id=blob_id, owner=owner, sha256=sha256, size_bytes=size_bytes, media_type=media_type)


def parse_execution_input(data: Mapping, where: str = "input") -> ExecutionInput:
    data = _mapping(data, where)
    _exact_keys(data, EXECUTION_INPUT_KEYS, where)
    has_inline = "inline" in data
    has_blob = "blob" in data
    if has_inline == has_blob:
        raise ContractError(f"{where}: exactly one of inline or blob is required")
    if has_inline:
        inline = _mapping(data["inline"], f"{where}.inline")
        if not inline:
            raise ContractError(f"{where}.inline: must be a non-empty object")
        _validate_json_value(inline, f"{where}.inline")
        if len(canonical_json_bytes(inline)) > MAX_INLINE_INPUT_BYTES:
            raise ContractError(f"{where}.inline: must be at most {MAX_INLINE_INPUT_BYTES} canonical JSON bytes")
        return ExecutionInput(inline=dict(inline), blob=None)
    blob = parse_blob_ref(data["blob"], f"{where}.blob")
    return ExecutionInput(inline=None, blob=blob)


def _parse_parameters(operation: str, value: Any, where: str) -> dict:
    data = _mapping(value, where)
    rules = PARAMETER_RULES[operation]
    unknown = sorted(set(data) - set(rules))
    if unknown:
        raise ContractError(f"{where}: unsupported parameters for {operation}: {', '.join(unknown)}")
    parsed: dict[str, Any] = {}
    for key, rule in rules.items():
        if key not in data:
            continue
        parsed[key] = _parse_parameter_value(rule, data[key], f"{where}.{key}")
    if len(canonical_json_bytes(parsed)) > MAX_PARAMETERS_BYTES:
        raise ContractError(f"{where}: must be at most {MAX_PARAMETERS_BYTES} canonical JSON bytes")
    return parsed


def _parse_parameter_value(rule: ParameterRule, value: Any, where: str) -> Any:
    if rule.kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ContractError(f"{where}: must be an integer")
        _check_parameter_bounds(rule, value, where)
        return value
    if rule.kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ContractError(f"{where}: must be a finite number")
        if not math.isfinite(value):
            raise ContractError(f"{where}: must be a finite number")
        _check_parameter_bounds(rule, value, where)
        return value
    if rule.kind == "boolean":
        if not isinstance(value, bool):
            raise ContractError(f"{where}: must be a boolean")
        return value
    if rule.kind == "string":
        return _text(value, where)
    if rule.kind == "const":
        if value != rule.const:
            raise ContractError(f"{where}: must be {rule.const!r}")
        return value
    raise ContractError(f"{where}: unregistered parameter rule kind {rule.kind!r}")


def _check_parameter_bounds(rule: ParameterRule, value: int | float, where: str) -> None:
    if rule.exclusive_minimum:
        if rule.minimum is not None and value <= rule.minimum:
            raise ContractError(f"{where}: must be > {rule.minimum}")
    elif rule.minimum is not None and value < rule.minimum:
        raise ContractError(f"{where}: must be >= {rule.minimum}")
    if rule.maximum is not None and value > rule.maximum:
        raise ContractError(f"{where}: must be <= {rule.maximum}")


def parse_execution_create_request(data: Mapping, where: str = "execution_create") -> ExecutionCreateRequest:
    data = _mapping(data, where)
    _exact_keys(data, EXECUTION_CREATE_KEYS, where)
    session_token = _text_field(data, "session_token", where)
    operation = _enum_field(data, "operation", where, CAPABILITIES)
    input_ = parse_execution_input(_required(data, "input", where), f"{where}.input")
    parameters = _parse_parameters(operation, _required(data, "parameters", where), f"{where}.parameters")
    idempotency_key = _bounded_text_field(data, "idempotency_key", where, max_bytes=IDEMPOTENCY_KEY_MAX_LENGTH)
    return ExecutionCreateRequest(
        session_token=session_token,
        operation=operation,
        input=input_,
        parameters=parameters,
        idempotency_key=idempotency_key,
    )


def parse_fence(data: Mapping, where: str = "fence") -> Fence:
    data = _mapping(data, where)
    _exact_keys(data, FENCE_KEYS, where)
    boot_id = _text_field(data, "boot_id", where)
    model_id = _id_field(data, "model_id", where)
    generation = _int_field(data, "generation", where, minimum=1)
    operation_id = _id_field(data, "operation_id", where)
    execution_id_raw = _required(data, "execution_id", where)
    attempt_raw = _required(data, "attempt", where)
    if (execution_id_raw is None) != (attempt_raw is None):
        raise ContractError(f"{where}: execution_id and attempt must be null together")
    execution_id: str | None = None
    attempt: int | None = None
    if execution_id_raw is not None:
        if not isinstance(execution_id_raw, str) or not _ID_RE.fullmatch(execution_id_raw):
            raise ContractError(f"{where}: execution_id must be a lowercase opaque id")
        execution_id = execution_id_raw
        if isinstance(attempt_raw, bool) or not isinstance(attempt_raw, int) or attempt_raw < 1:
            raise ContractError(f"{where}: an execution fence requires attempt >= 1")
        attempt = attempt_raw
    return Fence(
        boot_id=boot_id,
        model_id=model_id,
        generation=generation,
        operation_id=operation_id,
        execution_id=execution_id,
        attempt=attempt,
    )


def parse_instance_identity(data: Mapping, where: str = "instance") -> InstanceIdentity:
    data = _mapping(data, where)
    _exact_keys(data, INSTANCE_KEYS, where)
    container_id = _text_field(data, "container_id", where)
    started_at = _utc_timestamp_field(data, "started_at", where)
    deployment_id = _id_field(data, "deployment_id", where)
    model_id = _id_field(data, "model_id", where)
    runtime_id = _id_field(data, "runtime_id", where)
    candidate_digest = _sha256_field(_required(data, "candidate_digest", where), f"{where}.candidate_digest")
    image_digest = _text_field(data, "image_digest", where)
    if not _IMAGE_DIGEST_RE.fullmatch(image_digest):
        raise ContractError(f"{where}: image_digest must be name@sha256:<64-hex>")
    return InstanceIdentity(
        container_id=container_id,
        started_at=started_at,
        deployment_id=deployment_id,
        model_id=model_id,
        runtime_id=runtime_id,
        candidate_digest=candidate_digest,
        image_digest=image_digest,
    )


def parse_execution_view(data: Mapping, where: str = "execution_view") -> ExecutionView:
    data = _mapping(data, where)
    _exact_keys(data, EXECUTION_VIEW_KEYS, where)
    execution_id = _id_field(data, "execution_id", where)
    state = _enum_field(data, "state", where, EXECUTION_STATES)
    dispatch_state = _enum_field(data, "dispatch_state", where, DISPATCH_STATES)
    fence = parse_fence(_required(data, "fence", where), f"{where}.fence")
    if fence.execution_id != execution_id or fence.attempt is None:
        raise ContractError(f"{where}: fence must identify this execution with attempt >= 1")

    instance_raw = _required(data, "instance", where)
    instance = None if instance_raw is None else parse_instance_identity(instance_raw, f"{where}.instance")
    if dispatch_state == "dispatched":
        if instance is None:
            raise ContractError(f"{where}: a dispatched execution requires a full instance identity")
    elif instance is not None:
        raise ContractError(f"{where}: a not_started execution must not carry a container identity")

    result_raw = _required(data, "result", where)
    result = None if result_raw is None else parse_blob_ref(result_raw, f"{where}.result")
    error_raw = _required(data, "error", where)
    error = None if error_raw is None else parse_error_detail(error_raw, f"{where}.error")
    quiescent_raw = _required(data, "compute_quiescent", where)

    if state in EXECUTION_TERMINAL_STATES:
        if quiescent_raw is not True:
            raise ContractError(f"{where}: a terminal execution must report compute_quiescent=true")
        if state == "succeeded":
            if result is None:
                raise ContractError(f"{where}: a succeeded execution requires a result")
            if error is not None:
                raise ContractError(f"{where}: a succeeded execution must not carry an error")
        else:
            if error is None:
                raise ContractError(f"{where}: a {state} execution requires an error")
            if result is not None:
                raise ContractError(f"{where}: a {state} execution must not carry a result")
        return ExecutionView(
            execution_id=execution_id,
            state=state,
            dispatch_state=dispatch_state,
            compute_quiescent=True,
            result=result,
            error=error,
            instance=instance,
            fence=fence,
        )

    if quiescent_raw is not None:
        raise ContractError(f"{where}: a non-terminal execution must report compute_quiescent=null")
    if result is not None:
        raise ContractError(f"{where}: a non-terminal execution must not carry a result")
    if error is not None:
        raise ContractError(f"{where}: a non-terminal execution must not carry an error")
    return ExecutionView(
        execution_id=execution_id,
        state=state,
        dispatch_state=dispatch_state,
        compute_quiescent=None,
        result=None,
        error=None,
        instance=instance,
        fence=fence,
    )


# ---------------------------------------------------------------------------
# idempotency and retention helpers


def idempotency_fingerprint(owner: str, key: str, payload: Any) -> str:
    """C05 idempotency fingerprint: owner + key + canonical payload hash.

    The fingerprint never contains the idempotency key value itself beyond this
    hash and must not include a session token; callers pass the payload built
    from the parsed request (for executions the meaning of the key is compared
    under the same boot, owner and route namespace).
    """
    owner_text = _text(owner, "owner")
    key_text = _bounded_text(key, "idempotency_key", max_bytes=IDEMPOTENCY_KEY_MAX_LENGTH)
    _validate_json_value(payload, "payload")
    body = {"owner": owner_text, "key": key_text, "payload": payload}
    return hashlib.sha256(canonical_json_bytes(body)).hexdigest()


def reference_expired(
    created_at: float,
    now: float,
    retention_seconds: int = BLOB_RESULT_RETENTION_SECONDS,
) -> bool:
    """C07 retention check: a reference expires at created_at + retention."""
    if isinstance(retention_seconds, bool) or not isinstance(retention_seconds, int) or retention_seconds < 1:
        raise ContractError("retention_seconds: must be a positive integer")
    for name, value in (("created_at", created_at), ("now", now)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ContractError(f"{name}: must be a finite number")
    return now - created_at >= retention_seconds


# ---------------------------------------------------------------------------
# JSON Schema export


def _parameter_schema(rule: ParameterRule) -> dict:
    if rule.kind == "integer":
        schema: dict[str, Any] = {"type": "integer", "description": rule.description}
        if rule.minimum is not None:
            schema["minimum"] = rule.minimum
        if rule.maximum is not None:
            schema["maximum"] = rule.maximum
        return schema
    if rule.kind == "number":
        schema = {"type": "number", "description": rule.description}
        if rule.minimum is not None:
            schema["exclusiveMinimum" if rule.exclusive_minimum else "minimum"] = rule.minimum
        if rule.maximum is not None:
            schema["maximum"] = rule.maximum
        return schema
    if rule.kind == "boolean":
        return {"type": "boolean", "description": rule.description}
    if rule.kind == "string":
        return {"type": "string", "minLength": 1, "description": rule.description}
    if rule.kind == "const":
        return {"const": rule.const, "description": rule.description}
    raise ContractError(f"unregistered parameter rule kind {rule.kind!r}")


def _parameters_schema(operation: str) -> dict:
    properties = {name: _parameter_schema(rule) for name, rule in sorted(PARAMETER_RULES[operation].items())}
    return {"type": "object", "additionalProperties": False, "properties": properties}


def schema_document() -> dict:
    """The control-v1 JSON Schema, derived from this module's constants only."""
    defs: dict[str, Any] = {
        "ErrorDetail": {
            "type": "object",
            "additionalProperties": False,
            "required": ["code", "message", "retryable"],
            "properties": {
                "code": {"enum": sorted(ERROR_STATUS)},
                "message": {"type": "string", "minLength": 1},
                "retryable": {
                    "type": "boolean",
                    "description": "must match the documented retryable flag for the code",
                },
            },
        },
        "ErrorDocument": {
            "type": "object",
            "additionalProperties": False,
            "required": ["error", "request_id"],
            "properties": {
                "error": {"$ref": "#/$defs/ErrorDetail"},
                "request_id": {"type": "string", "minLength": 1, "maxLength": REQUEST_ID_MAX_LENGTH},
            },
        },
        "BlobRef": {
            "type": "object",
            "additionalProperties": False,
            "required": ["blob_id", "owner", "sha256", "size_bytes", "media_type"],
            "properties": {
                "blob_id": {"type": "string", "pattern": _ID_PATTERN},
                "owner": {"type": "string", "minLength": 1},
                "sha256": {"type": "string", "pattern": _SHA256_PATTERN},
                "size_bytes": {"type": "integer", "minimum": 1, "maximum": MAX_BLOB_BYTES},
                "media_type": {"type": "string", "pattern": _MEDIA_TYPE_PATTERN},
            },
        },
        "InstanceIdentity": {
            "type": "object",
            "additionalProperties": False,
            "required": ["container_id", "started_at", "deployment_id", "model_id", "runtime_id", "candidate_digest", "image_digest"],
            "properties": {
                "container_id": {"type": "string", "minLength": 1},
                "started_at": {"type": "string", "format": "date-time", "description": "ISO-8601 with a UTC offset"},
                "deployment_id": {"type": "string", "pattern": _ID_PATTERN},
                "model_id": {"type": "string", "pattern": _ID_PATTERN},
                "runtime_id": {"type": "string", "pattern": _ID_PATTERN},
                "candidate_digest": {"type": "string", "pattern": _SHA256_PATTERN},
                "image_digest": {"type": "string", "pattern": _IMAGE_DIGEST_PATTERN},
            },
        },
        "Fence": {
            "type": "object",
            "additionalProperties": False,
            "required": ["boot_id", "model_id", "generation", "operation_id", "execution_id", "attempt"],
            "properties": {
                "boot_id": {"type": "string", "minLength": 1},
                "model_id": {"type": "string", "pattern": _ID_PATTERN},
                "generation": {"type": "integer", "minimum": 1},
                "operation_id": {"type": "string", "pattern": _ID_PATTERN},
                "execution_id": {"type": ["string", "null"], "pattern": _ID_PATTERN},
                "attempt": {
                    "type": ["integer", "null"],
                    "minimum": 1,
                    "description": "null for non-execution operations; executions start at 1",
                },
            },
        },
        "SessionCreateRequest": {
            "type": "object",
            "additionalProperties": False,
            "required": ["model_id", "idempotency_key"],
            "properties": {
                "model_id": {"type": "string", "pattern": _ID_PATTERN},
                "idempotency_key": {"type": "string", "minLength": 1, "maxLength": IDEMPOTENCY_KEY_MAX_LENGTH},
                "correlation_id": {"type": ["string", "null"], "minLength": 1, "maxLength": CORRELATION_ID_MAX_LENGTH},
            },
        },
        "SessionView": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "session_id",
                "state",
                "phase",
                "boot_id",
                "model_id",
                "expires_in_ms",
                "hard_remaining_ms",
                "owner_token",
                "error",
            ],
            "properties": {
                "session_id": {"type": "string", "pattern": _ID_PATTERN},
                "state": {"enum": sorted(SESSION_STATES)},
                "phase": {"enum": sorted(SESSION_PHASES) + [None], "description": "preparing phases from C04"},
                "boot_id": {"type": "string", "minLength": 1},
                "model_id": {"type": "string", "pattern": _ID_PATTERN},
                "expires_in_ms": {"type": "integer", "minimum": 0},
                "hard_remaining_ms": {"type": "integer", "minimum": 0},
                "owner_token": {
                    "type": ["string", "null"],
                    "minLength": 1,
                    "description": "null exactly when the session is closed; only returned to the owner",
                },
                "error": {"anyOf": [{"$ref": "#/$defs/ErrorDetail"}, {"type": "null"}]},
            },
        },
        "ExecutionInput": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "inline": {"type": "object", "minProperties": 1},
                "blob": {"$ref": "#/$defs/BlobRef"},
            },
            "oneOf": [{"required": ["inline"]}, {"required": ["blob"]}],
        },
    }
    for operation in sorted(CAPABILITIES):
        defs[f"Parameters_{operation}"] = _parameters_schema(operation)
    defs["ExecutionCreateRequest"] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["session_token", "operation", "input", "parameters", "idempotency_key"],
        "properties": {
            "session_token": {"type": "string", "minLength": 1},
            "operation": {"enum": sorted(CAPABILITIES)},
            "input": {"$ref": "#/$defs/ExecutionInput"},
            "parameters": {"type": "object"},
            "idempotency_key": {"type": "string", "minLength": 1, "maxLength": IDEMPOTENCY_KEY_MAX_LENGTH},
        },
        "allOf": [
            {
                "if": {"required": ["operation"], "properties": {"operation": {"const": operation}}},
                "then": {"properties": {"parameters": {"$ref": f"#/$defs/Parameters_{operation}"}}},
            }
            for operation in sorted(CAPABILITIES)
        ],
    }
    defs["ExecutionView"] = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "execution_id",
            "state",
            "dispatch_state",
            "compute_quiescent",
            "result",
            "error",
            "instance",
            "fence",
        ],
        "properties": {
            "execution_id": {"type": "string", "pattern": _ID_PATTERN},
            "state": {"enum": sorted(EXECUTION_STATES)},
            "dispatch_state": {"enum": sorted(DISPATCH_STATES)},
            "compute_quiescent": {
                "type": ["boolean", "null"],
                "description": "true exactly for terminal states",
            },
            "result": {"anyOf": [{"$ref": "#/$defs/BlobRef"}, {"type": "null"}]},
            "error": {"anyOf": [{"$ref": "#/$defs/ErrorDetail"}, {"type": "null"}]},
            "instance": {"anyOf": [{"$ref": "#/$defs/InstanceIdentity"}, {"type": "null"}]},
            "fence": {"$ref": "#/$defs/Fence"},
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "SelfModelSwitch control protocol v1",
        "description": (
            "Strict DTOs for the local control API. Cross-field rules (terminal evidence, "
            "dispatch/instance pairing, token/null pairing, per-operation parameters) are "
            "enforced by model_scheduler.control_protocol_v1 and only documented here."
        ),
        "protocol_version": PROTOCOL_VERSION,
        "protocol_version_header": PROTOCOL_VERSION_HEADER,
        "limits": {
            "max_inline_input_bytes": MAX_INLINE_INPUT_BYTES,
            "max_parameters_bytes": MAX_PARAMETERS_BYTES,
            "max_blob_bytes": MAX_BLOB_BYTES,
            "blob_result_retention_seconds": BLOB_RESULT_RETENTION_SECONDS,
            "idempotency_key_max_length": IDEMPOTENCY_KEY_MAX_LENGTH,
            "correlation_id_max_length": CORRELATION_ID_MAX_LENGTH,
            "request_id_max_length": REQUEST_ID_MAX_LENGTH,
        },
        "capabilities": {operation: sorted(PARAMETER_RULES[operation]) for operation in sorted(CAPABILITIES)},
        "errors": {
            code: {"status": status, "retryable": retryable}
            for code, (status, retryable) in sorted(_ERROR_TABLE.items())
        },
        "$defs": defs,
    }


def render_schema_text() -> str:
    return json.dumps(schema_document(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_schema(output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_schema_text(), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m model_scheduler.control_protocol_v1",
        description="Control protocol v1 contract utilities.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    export = subparsers.add_parser("export-schema", help="render the control-v1 JSON Schema document")
    export.add_argument("--output", required=True, type=Path, help="output path for schemas/control-v1.json")
    args = parser.parse_args(argv)
    if args.command == "export-schema":
        write_schema(args.output)
        return 0
    parser.error(f"unknown command {args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
