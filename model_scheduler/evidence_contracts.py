"""Candidate, case-attempt and acceptance-report contracts (M01/P03, C09).

Pure types and strict parsers for the evidence chain:

    registration contract -> candidate -> case attempts -> acceptance report

The chain is one-directional: `candidate_digest` hashes the candidate body only
(no self digest, no run results, no host paths, no evidence output directory and
no generation time), the report references the candidate digest, and nothing in
the candidate is derived from a report. Canonical serialization sorts keys,
uses compact separators, keeps UTF-8 unescaped, forbids NaN and expands every
field including defaults, so the same body always produces the same digest.

The required-case set is derived from the candidate alone: S01--S06, O01--O06,
`B:<model>:<load|infer|envelope|cancel|stop|reload>` for every registered model
and `B:<model>:cap:<capability>` for every declared capability. A report must
map each required case to exactly one final attempt that exists in its attempt
list; unknown cases, missing cases, duplicate final conclusions and forced
`summary` fields are rejected. Structural validity is *not* a pass verdict:
nothing here decides `passed` -- the evaluator recomputes that from raw
materials (P25).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping

from .contracts_v2 import (
    CAPABILITY_MATRIX,
    ContractError,
    ModelSpec,
    RuntimeSpec,
    canonical_json_bytes,
    parse_model_spec,
    parse_runtime_spec,
)

EVIDENCE_SCHEMA_VERSION = 3

SOFTWARE_CASES = frozenset({"S01", "S02", "S03", "S04", "S05", "S06"})
OPERATIONAL_CASES = frozenset({"O01", "O02", "O03", "O04", "O05", "O06"})
BACKEND_MODEL_CASE_KINDS = ("load", "infer", "envelope", "cancel", "stop", "reload")
BACKEND_CASE_MIN_COLD_STARTS = 3
BACKEND_CASE_MIN_RELOAD_ROUNDS = 3

# plan/06-acceptance.md section 4 thresholds. A policy may only be stricter.
MIN_OPERATIONAL_SECONDS = 1800
MIN_OPERATIONAL_REQUESTS = 100
MAX_ARRIVAL_GAP_SECONDS = 15.0
MAX_SEND_DEVIATION_MILLISECONDS = 1000.0
MAX_ERROR_RATE = 0.1
MAX_QUEUE_FULL_RATE = 0.1
MAX_TIMEOUT_RATE = 0.1
REPORT_VALIDITY_SECONDS = 7 * 86_400
REPORT_FUTURE_TOLERANCE_SECONDS = 300

CAPABILITIES = frozenset(CAPABILITY_MATRIX)

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ID_RE = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,61}[a-z0-9])?")
_SOFTWARE_CASE_RE = re.compile(r"S0[1-6]")
_OPERATIONAL_CASE_RE = re.compile(r"O0[1-6]")
_BACKEND_CASE_RE = re.compile(r"B:([a-z0-9][a-z0-9._-]{0,61}[a-z0-9]):(load|infer|envelope|cancel|stop|reload)")
_BACKEND_CAP_CASE_RE = re.compile(r"B:([a-z0-9][a-z0-9._-]{0,61}[a-z0-9]):cap:([a-z]+)")


# ---------------------------------------------------------------------------
# strict helpers (same rules as contracts_v2; kept local so this module stays
# dependency-free except for the shared pure contracts)


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


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError(f"{where}: must be a non-empty string")
    if "\x00" in value:
        raise ContractError(f"{where}: must not contain NUL")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ContractError(f"{where}: must be valid UTF-8") from exc
    return value


def _text_field(data: Mapping, key: str, where: str) -> str:
    return _text(_required(data, key, where), f"{where}.{key}")


def _int_field(data: Mapping, key: str, where: str, *, minimum: int | None = None) -> int:
    value = _required(data, key, where)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{where}: {key} must be an integer")
    if minimum is not None and value < minimum:
        raise ContractError(f"{where}: {key} must be >= {minimum}")
    return value


def _number_field(data: Mapping, key: str, where: str, *, minimum: float | None = None, maximum: float | None = None) -> float:
    value = _required(data, key, where)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{where}: {key} must be a number")
    if minimum is not None and value < minimum:
        raise ContractError(f"{where}: {key} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ContractError(f"{where}: {key} must be <= {maximum}")
    return float(value)


def _sha256_field(data: Mapping, key: str, where: str) -> str:
    value = _text_field(data, key, where)
    if not _SHA256_RE.fullmatch(value):
        raise ContractError(f"{where}: {key} must be a lowercase 64-hex SHA-256")
    return value


def _id_field(data: Mapping, key: str, where: str) -> str:
    value = _text_field(data, key, where)
    if not _ID_RE.fullmatch(value):
        raise ContractError(f"{where}: {key} must be a lowercase opaque id: {value!r}")
    return value


def _sequence_field(data: Mapping, key: str, where: str) -> list:
    value = _required(data, key, where)
    if not isinstance(value, list):
        raise ContractError(f"{where}: {key} must be a list")
    return value


def _parse_utc(value: Any, where: str) -> datetime:
    text = _text(value, where)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ContractError(f"{where}: must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ContractError(f"{where}: must carry a UTC offset")
    return parsed


def _safe_relative_path(value: Any, where: str) -> str:
    """Same rule as contracts_v2: relative POSIX path, no escape or control bytes."""
    text = _text(value, where)
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in text):
        raise ContractError(f"{where}: must not contain control characters: {text!r}")
    if text.startswith("/") or "\\" in text:
        raise ContractError(f"{where}: must be a relative POSIX path: {text!r}")
    parts = text.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ContractError(f"{where}: must not contain empty, '.' or '..' segments: {text!r}")
    return text


# ---------------------------------------------------------------------------
# DTOs


@dataclass(frozen=True)
class ArtifactRef:
    """One evidence file: relative path, byte size and content hash."""

    relative_path: str
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        _safe_relative_path(self.relative_path, "artifact.relative_path")
        if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int) or self.size_bytes < 0:
            raise ContractError("artifact.size_bytes: must be a non-negative integer")
        if not isinstance(self.sha256, str) or not _SHA256_RE.fullmatch(self.sha256):
            raise ContractError("artifact.sha256: must be a lowercase 64-hex SHA-256")


@dataclass(frozen=True)
class DeviceFact:
    """C09 device facts; recorded by P21 collect and hashed into the candidate."""

    machine_id_sha256: str
    architecture: str
    device_tree_sha256: str
    mem_total_bytes: int
    os_release: str
    kernel_release: str
    gpu_identity: str
    jetpack_release: str
    container_runtime_version: str
    power_mode: str
    clock_mode: str
    model_disk_uuid: str
    scratch_disk_uuid: str


@dataclass(frozen=True)
class RuntimeStackFact:
    """CUDA/runtime stack identity recorded with the device facts."""

    cuda_version: str
    compute_capability: str
    python_version: str


@dataclass(frozen=True)
class ModelPerformancePolicy:
    """Per-model thresholds frozen before the final run (P21 produces them)."""

    model_id: str
    cold_start_seconds_max: float
    infer_milliseconds_p95_max: float
    infer_milliseconds_p99_max: float
    max_input_milliseconds_max: float


@dataclass(frozen=True)
class OperationalPolicy:
    """Mixed-load policy; every value must meet the 06-acceptance minimum."""

    duration_seconds: int
    arrival_requests: int
    arrival_gap_seconds_max: float
    send_deviation_milliseconds_max: float
    error_rate_max: float
    queue_full_rate_max: float
    timeout_rate_max: float


@dataclass(frozen=True)
class PolicyV3:
    performance: tuple[ModelPerformancePolicy, ...]
    operational: OperationalPolicy


@dataclass(frozen=True)
class CandidateV3:
    """The frozen candidate body.

    Deliberately has no self digest, no run results, no host paths, no evidence
    output directory and no generation time; `candidate_digest` returns
    sha256(canonical(body)) for the caller to write back into the config.
    """

    schema_version: int
    deployment_id: str
    source_archive_sha256: str
    config_sha256: str
    device: DeviceFact
    runtime_stack: RuntimeStackFact
    runtimes: tuple[RuntimeSpec, ...]
    models: tuple[ModelSpec, ...]
    measurement_refs: tuple[ArtifactRef, ...]
    fixture_refs: tuple[ArtifactRef, ...]
    policy: PolicyV3
    collector_sha256: str
    evaluator_sha256: str


@dataclass(frozen=True)
class CaseAttempt:
    """One attempt of one required case; failed attempts are kept forever."""

    case_id: str
    run_id: str
    attempt: int
    candidate_sha256: str
    device_digest: str
    started_at: str
    ended_at: str
    boot_id: str
    event_refs: tuple[ArtifactRef, ...]
    collector_sha256: str
    evaluator_sha256: str
    exit_code: int


@dataclass(frozen=True)
class CaseFinal:
    """The single final attempt for one case."""

    case_id: str
    run_id: str
    attempt: int


@dataclass(frozen=True)
class AcceptanceReportV3:
    """The v3 report. A `summary` field does not exist on purpose."""

    schema_version: int
    candidate_sha256: str
    device_digest: str
    run_id: str
    started_at: str
    ended_at: str
    attempts: tuple[CaseAttempt, ...]
    finals: tuple[CaseFinal, ...]
    artifacts: tuple[ArtifactRef, ...]


# ---------------------------------------------------------------------------
# parsers

ARTIFACT_KEYS = frozenset({"relative_path", "size_bytes", "sha256"})
DEVICE_KEYS = frozenset(
    {
        "machine_id_sha256",
        "architecture",
        "device_tree_sha256",
        "mem_total_bytes",
        "os_release",
        "kernel_release",
        "gpu_identity",
        "jetpack_release",
        "container_runtime_version",
        "power_mode",
        "clock_mode",
        "model_disk_uuid",
        "scratch_disk_uuid",
    }
)
RUNTIME_STACK_KEYS = frozenset({"cuda_version", "compute_capability", "python_version"})
PERFORMANCE_KEYS = frozenset(
    {"model_id", "cold_start_seconds_max", "infer_milliseconds_p95_max", "infer_milliseconds_p99_max", "max_input_milliseconds_max"}
)
OPERATIONAL_KEYS = frozenset(
    {
        "duration_seconds",
        "arrival_requests",
        "arrival_gap_seconds_max",
        "send_deviation_milliseconds_max",
        "error_rate_max",
        "queue_full_rate_max",
        "timeout_rate_max",
    }
)
POLICY_KEYS = frozenset({"performance", "operational"})
CANDIDATE_KEYS = frozenset(
    {
        "schema_version",
        "deployment_id",
        "source_archive_sha256",
        "config_sha256",
        "device",
        "runtime_stack",
        "runtimes",
        "models",
        "measurement_refs",
        "fixture_refs",
        "policy",
        "collector_sha256",
        "evaluator_sha256",
    }
)
CASE_ATTEMPT_KEYS = frozenset(
    {
        "case_id",
        "run_id",
        "attempt",
        "candidate_sha256",
        "device_digest",
        "started_at",
        "ended_at",
        "boot_id",
        "event_refs",
        "collector_sha256",
        "evaluator_sha256",
        "exit_code",
    }
)
CASE_FINAL_KEYS = frozenset({"case_id", "run_id", "attempt"})
REPORT_KEYS = frozenset(
    {
        "schema_version",
        "candidate_sha256",
        "device_digest",
        "run_id",
        "started_at",
        "ended_at",
        "case_attempt_refs",
        "final_attempts",
        "artifact_manifest",
    }
)


def parse_case_id(case_id: Any, where: str = "case_id") -> str:
    """Validate one case id against the required-set grammar and return it."""
    text = _text(case_id, where)
    if _SOFTWARE_CASE_RE.fullmatch(text) or _OPERATIONAL_CASE_RE.fullmatch(text):
        return text
    backend = _BACKEND_CASE_RE.fullmatch(text)
    if backend:
        return text
    capability = _BACKEND_CAP_CASE_RE.fullmatch(text)
    if capability:
        if capability.group(2) not in CAPABILITIES:
            raise ContractError(f"{where}: unknown capability in {text!r}")
        return text
    raise ContractError(f"{where}: not a required-case id: {text!r}")


def parse_artifact_ref(data: Mapping, where: str = "artifact") -> ArtifactRef:
    data = _mapping(data, where)
    _exact_keys(data, ARTIFACT_KEYS, where)
    return ArtifactRef(
        relative_path=_safe_relative_path(_required(data, "relative_path", where), f"{where}.relative_path"),
        size_bytes=_int_field(data, "size_bytes", where, minimum=0),
        sha256=_sha256_field(data, "sha256", where),
    )


def parse_artifact_list(items: Any, where: str) -> tuple[ArtifactRef, ...]:
    if not isinstance(items, list):
        raise ContractError(f"{where}: must be a list")
    artifacts = tuple(parse_artifact_ref(item, f"{where}[{index}]") for index, item in enumerate(items))
    paths = [artifact.relative_path for artifact in artifacts]
    duplicates = sorted({path for path in paths if paths.count(path) > 1})
    if duplicates:
        raise ContractError(f"{where}: duplicate artifact paths: {', '.join(duplicates)}")
    return artifacts


def parse_device_fact(data: Mapping, where: str = "device") -> DeviceFact:
    data = _mapping(data, where)
    _exact_keys(data, DEVICE_KEYS, where)
    return DeviceFact(
        machine_id_sha256=_sha256_field(data, "machine_id_sha256", where),
        architecture=_text_field(data, "architecture", where),
        device_tree_sha256=_sha256_field(data, "device_tree_sha256", where),
        mem_total_bytes=_int_field(data, "mem_total_bytes", where, minimum=1),
        os_release=_text_field(data, "os_release", where),
        kernel_release=_text_field(data, "kernel_release", where),
        gpu_identity=_text_field(data, "gpu_identity", where),
        jetpack_release=_text_field(data, "jetpack_release", where),
        container_runtime_version=_text_field(data, "container_runtime_version", where),
        power_mode=_text_field(data, "power_mode", where),
        clock_mode=_text_field(data, "clock_mode", where),
        model_disk_uuid=_text_field(data, "model_disk_uuid", where),
        scratch_disk_uuid=_text_field(data, "scratch_disk_uuid", where),
    )


def parse_runtime_stack(data: Mapping, where: str = "runtime_stack") -> RuntimeStackFact:
    data = _mapping(data, where)
    _exact_keys(data, RUNTIME_STACK_KEYS, where)
    return RuntimeStackFact(
        cuda_version=_text_field(data, "cuda_version", where),
        compute_capability=_text_field(data, "compute_capability", where),
        python_version=_text_field(data, "python_version", where),
    )


def parse_performance_policy(data: Mapping, where: str = "performance_policy") -> ModelPerformancePolicy:
    data = _mapping(data, where)
    _exact_keys(data, PERFORMANCE_KEYS, where)
    return ModelPerformancePolicy(
        model_id=_id_field(data, "model_id", where),
        cold_start_seconds_max=_number_field(data, "cold_start_seconds_max", where, minimum=0),
        infer_milliseconds_p95_max=_number_field(data, "infer_milliseconds_p95_max", where, minimum=0),
        infer_milliseconds_p99_max=_number_field(data, "infer_milliseconds_p99_max", where, minimum=0),
        max_input_milliseconds_max=_number_field(data, "max_input_milliseconds_max", where, minimum=0),
    )


def parse_operational_policy(data: Mapping, where: str = "operational_policy") -> OperationalPolicy:
    data = _mapping(data, where)
    _exact_keys(data, OPERATIONAL_KEYS, where)
    policy = OperationalPolicy(
        duration_seconds=_int_field(data, "duration_seconds", where, minimum=1),
        arrival_requests=_int_field(data, "arrival_requests", where, minimum=1),
        arrival_gap_seconds_max=_number_field(data, "arrival_gap_seconds_max", where, minimum=0),
        send_deviation_milliseconds_max=_number_field(data, "send_deviation_milliseconds_max", where, minimum=0),
        error_rate_max=_number_field(data, "error_rate_max", where, minimum=0, maximum=1),
        queue_full_rate_max=_number_field(data, "queue_full_rate_max", where, minimum=0, maximum=1),
        timeout_rate_max=_number_field(data, "timeout_rate_max", where, minimum=0, maximum=1),
    )
    # 06-acceptance section 4: the policy may be stricter, never looser.
    if policy.duration_seconds < MIN_OPERATIONAL_SECONDS:
        raise ContractError(f"{where}: duration_seconds must be >= {MIN_OPERATIONAL_SECONDS}")
    if policy.arrival_requests < MIN_OPERATIONAL_REQUESTS:
        raise ContractError(f"{where}: arrival_requests must be >= {MIN_OPERATIONAL_REQUESTS}")
    if policy.arrival_gap_seconds_max > MAX_ARRIVAL_GAP_SECONDS:
        raise ContractError(f"{where}: arrival_gap_seconds_max must be <= {MAX_ARRIVAL_GAP_SECONDS}")
    if policy.send_deviation_milliseconds_max > MAX_SEND_DEVIATION_MILLISECONDS:
        raise ContractError(f"{where}: send_deviation_milliseconds_max must be <= {MAX_SEND_DEVIATION_MILLISECONDS}")
    if policy.error_rate_max > MAX_ERROR_RATE:
        raise ContractError(f"{where}: error_rate_max must be <= {MAX_ERROR_RATE}")
    if policy.queue_full_rate_max > MAX_QUEUE_FULL_RATE:
        raise ContractError(f"{where}: queue_full_rate_max must be <= {MAX_QUEUE_FULL_RATE}")
    if policy.timeout_rate_max > MAX_TIMEOUT_RATE:
        raise ContractError(f"{where}: timeout_rate_max must be <= {MAX_TIMEOUT_RATE}")
    return policy


def parse_policy(data: Mapping, where: str = "policy") -> PolicyV3:
    data = _mapping(data, where)
    _exact_keys(data, POLICY_KEYS, where)
    performance = _sequence_field(data, "performance", where)
    if not performance:
        raise ContractError(f"{where}: performance must list one entry per model")
    parsed = tuple(parse_performance_policy(item, f"{where}.performance[{index}]") for index, item in enumerate(performance))
    model_ids = [entry.model_id for entry in parsed]
    duplicates = sorted({model_id for model_id in model_ids if model_ids.count(model_id) > 1})
    if duplicates:
        raise ContractError(f"{where}: duplicate performance entries: {', '.join(duplicates)}")
    return PolicyV3(performance=parsed, operational=parse_operational_policy(_required(data, "operational", where), f"{where}.operational"))


def parse_candidate(data: Mapping, where: str = "candidate") -> CandidateV3:
    data = _mapping(data, where)
    _exact_keys(data, CANDIDATE_KEYS, where)
    schema_version = _int_field(data, "schema_version", where)
    if schema_version != EVIDENCE_SCHEMA_VERSION:
        raise ContractError(f"{where}: schema_version must be {EVIDENCE_SCHEMA_VERSION}")

    runtimes = tuple(
        parse_runtime_spec(item, f"{where}.runtimes[{index}]")
        for index, item in enumerate(_sequence_field(data, "runtimes", where))
    )
    runtime_ids = [runtime.runtime_id for runtime in runtimes]
    duplicates = sorted({runtime_id for runtime_id in runtime_ids if runtime_ids.count(runtime_id) > 1})
    if duplicates:
        raise ContractError(f"{where}: duplicate runtime ids: {', '.join(duplicates)}")

    models = tuple(
        parse_model_spec(item, f"{where}.models[{index}]")
        for index, item in enumerate(_sequence_field(data, "models", where))
    )
    if not models:
        raise ContractError(f"{where}: at least one model must be registered")
    model_ids = [model.model_id for model in models]
    duplicates = sorted({model_id for model_id in model_ids if model_ids.count(model_id) > 1})
    if duplicates:
        raise ContractError(f"{where}: duplicate model ids: {', '.join(duplicates)}")
    for model in models:
        if model.runtime_id not in runtime_ids:
            raise ContractError(f"{where}: model {model.model_id!r} references unknown runtime {model.runtime_id!r}")

    policy = parse_policy(_required(data, "policy", where), f"{where}.policy")
    policy_models = {entry.model_id for entry in policy.performance}
    if policy_models != set(model_ids):
        missing = sorted(set(model_ids) - policy_models)
        unknown = sorted(policy_models - set(model_ids))
        raise ContractError(f"{where}: performance policy must cover every model (missing={missing}, unknown={unknown})")

    return CandidateV3(
        schema_version=schema_version,
        deployment_id=_id_field(data, "deployment_id", where),
        source_archive_sha256=_sha256_field(data, "source_archive_sha256", where),
        config_sha256=_sha256_field(data, "config_sha256", where),
        device=parse_device_fact(_required(data, "device", where), f"{where}.device"),
        runtime_stack=parse_runtime_stack(_required(data, "runtime_stack", where), f"{where}.runtime_stack"),
        runtimes=runtimes,
        models=models,
        measurement_refs=parse_artifact_list(_required(data, "measurement_refs", where), f"{where}.measurement_refs"),
        fixture_refs=parse_artifact_list(_required(data, "fixture_refs", where), f"{where}.fixture_refs"),
        policy=policy,
        collector_sha256=_sha256_field(data, "collector_sha256", where),
        evaluator_sha256=_sha256_field(data, "evaluator_sha256", where),
    )


def parse_case_attempt(data: Mapping, where: str = "case_attempt") -> CaseAttempt:
    data = _mapping(data, where)
    _exact_keys(data, CASE_ATTEMPT_KEYS, where)
    started_at = _text_field(data, "started_at", where)
    ended_at = _text_field(data, "ended_at", where)
    if _parse_utc(ended_at, f"{where}.ended_at") < _parse_utc(started_at, f"{where}.started_at"):
        raise ContractError(f"{where}: ended_at must not be earlier than started_at")
    return CaseAttempt(
        case_id=parse_case_id(_required(data, "case_id", where), f"{where}.case_id"),
        run_id=_id_field(data, "run_id", where),
        attempt=_int_field(data, "attempt", where, minimum=1),
        candidate_sha256=_sha256_field(data, "candidate_sha256", where),
        device_digest=_sha256_field(data, "device_digest", where),
        started_at=started_at,
        ended_at=ended_at,
        boot_id=_text_field(data, "boot_id", where),
        event_refs=parse_artifact_list(_required(data, "event_refs", where), f"{where}.event_refs"),
        collector_sha256=_sha256_field(data, "collector_sha256", where),
        evaluator_sha256=_sha256_field(data, "evaluator_sha256", where),
        exit_code=_int_field(data, "exit_code", where),
    )


def parse_case_final(data: Mapping, where: str = "case_final") -> CaseFinal:
    data = _mapping(data, where)
    _exact_keys(data, CASE_FINAL_KEYS, where)
    return CaseFinal(
        case_id=parse_case_id(_required(data, "case_id", where), f"{where}.case_id"),
        run_id=_id_field(data, "run_id", where),
        attempt=_int_field(data, "attempt", where, minimum=1),
    )


def parse_acceptance_report(data: Mapping, where: str = "report") -> AcceptanceReportV3:
    data = _mapping(data, where)
    _exact_keys(data, REPORT_KEYS, where)
    schema_version = _int_field(data, "schema_version", where)
    if schema_version != EVIDENCE_SCHEMA_VERSION:
        raise ContractError(f"{where}: schema_version must be {EVIDENCE_SCHEMA_VERSION}")
    started_at = _text_field(data, "started_at", where)
    ended_at = _text_field(data, "ended_at", where)
    if _parse_utc(ended_at, f"{where}.ended_at") < _parse_utc(started_at, f"{where}.started_at"):
        raise ContractError(f"{where}: ended_at must not be earlier than started_at")
    attempts = tuple(
        parse_case_attempt(item, f"{where}.case_attempt_refs[{index}]")
        for index, item in enumerate(_sequence_field(data, "case_attempt_refs", where))
    )
    finals = tuple(
        parse_case_final(item, f"{where}.final_attempts[{index}]")
        for index, item in enumerate(_sequence_field(data, "final_attempts", where))
    )
    return AcceptanceReportV3(
        schema_version=schema_version,
        candidate_sha256=_sha256_field(data, "candidate_sha256", where),
        device_digest=_sha256_field(data, "device_digest", where),
        run_id=_id_field(data, "run_id", where),
        started_at=started_at,
        ended_at=ended_at,
        attempts=attempts,
        finals=finals,
        artifacts=parse_artifact_list(_required(data, "artifact_manifest", where), f"{where}.artifact_manifest"),
    )


# ---------------------------------------------------------------------------
# required-case mapping and digests


def required_case_ids(candidate: CandidateV3) -> frozenset[str]:
    """The complete required set, derived from the candidate alone."""
    cases = set(SOFTWARE_CASES) | set(OPERATIONAL_CASES)
    for model in candidate.models:
        for kind in BACKEND_MODEL_CASE_KINDS:
            cases.add(f"B:{model.model_id}:{kind}")
        for capability in model.capabilities:
            cases.add(f"B:{model.model_id}:cap:{capability}")
    return frozenset(cases)


def validate_report_mapping(report: AcceptanceReportV3, candidate: CandidateV3) -> None:
    """Every required case maps to exactly one traceable final attempt.

    Failed attempts stay in `report.attempts`; this function never removes or
    deduplicates them. Structural validity is not a pass verdict.
    """
    required = required_case_ids(candidate)

    attempt_keys = {(attempt.run_id, attempt.case_id, attempt.attempt) for attempt in report.attempts}
    if len(attempt_keys) != len(report.attempts):
        raise ContractError("report: duplicate attempt (run_id, case_id, attempt)")
    for attempt in report.attempts:
        if attempt.case_id not in required:
            raise ContractError(f"report: attempt for unknown case {attempt.case_id!r}")

    final_ids = [final.case_id for final in report.finals]
    duplicates = sorted({case_id for case_id in final_ids if final_ids.count(case_id) > 1})
    if duplicates:
        raise ContractError(f"report: duplicate final conclusions: {', '.join(duplicates)}")
    final_cases = set(final_ids)
    unknown = sorted(final_cases - required)
    if unknown:
        raise ContractError(f"report: final attempts for unknown cases: {', '.join(unknown)}")
    missing = sorted(required - final_cases)
    if missing:
        raise ContractError(f"report: missing final attempts for: {', '.join(missing)}")
    for final in report.finals:
        if (final.run_id, final.case_id, final.attempt) not in attempt_keys:
            raise ContractError(f"report: final attempt for {final.case_id!r} does not exist in case_attempt_refs")

    if report.attempts:
        started = min(_parse_utc(attempt.started_at, "attempt.started_at") for attempt in report.attempts)
        ended = max(_parse_utc(attempt.ended_at, "attempt.ended_at") for attempt in report.attempts)
        if _parse_utc(report.started_at, "report.started_at") != started:
            raise ContractError("report: started_at must equal the earliest attempt start")
        if _parse_utc(report.ended_at, "report.ended_at") != ended:
            raise ContractError("report: ended_at must equal the latest attempt end")


def validate_report_identity(report: AcceptanceReportV3, candidate: CandidateV3) -> None:
    """The report must be bound to this candidate and to this device digest."""
    digest = candidate_digest(candidate)
    if report.candidate_sha256 != digest:
        raise ContractError("report: candidate_sha256 does not match the candidate")
    device = device_digest(candidate.device)
    if report.device_digest != device:
        raise ContractError("report: device_digest does not match the candidate device")


def canonical_body(value: Any) -> bytes:
    """Canonical JSON for a dataclass body: defaults expanded, sorted, compact."""
    return canonical_json_bytes(asdict(value))


def candidate_digest(candidate: CandidateV3) -> str:
    """sha256 over the candidate body only; no report, no self digest, no time."""
    return hashlib.sha256(canonical_body(candidate)).hexdigest()


def device_digest(device: DeviceFact) -> str:
    return hashlib.sha256(canonical_body(device)).hexdigest()


def artifact_manifest_digest(artifacts: Iterable[ArtifactRef]) -> str:
    """Hash a material manifest in list order (order is semantic)."""
    return hashlib.sha256(canonical_json_bytes([asdict(artifact) for artifact in artifacts])).hexdigest()
