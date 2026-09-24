"""The CT01 chat probe: fixed-image inspection and bounded compatibility probing.

    python -m model_scheduler.acceptance.chat_probe inspect --site <site-input.json> --output <dir>
    python -m model_scheduler.acceptance.chat_probe probe --site <site-input.json> --phase baseline --output <dir>

`inspect` never starts a model: it records the hardware, the checkout, the fixed
image's version/help, the per-asset hashes, the rendered argv and the chat
template the runtime would use. `probe` sends a bounded number of generation
requests through the compat surface — never more than `site.request_limit` —
and keeps every raw request and response.

Case statuses are computed from what was recorded (`passed`, `unsupported`,
`needs_candidate`, `failed`, `not_run`). A 200 response to an unrecognised
field is never evidence that the field had an effect: such a case keeps
`failed` (reason `not_proven`) or `needs_candidate`.

Exit codes: 0 the command's own checks passed (for a baseline probe: the
collection is complete, which is **not** a capability verdict), 2 an input,
structure or missing-file error, 3 the complete material shows a semantic
failure (a candidate probe with any required case that is not `passed`).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import struct
import subprocess
import sys
import time
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

import httpx

from . import EXIT_FAILED, EXIT_INPUT, EXIT_OK

PROBE_SCHEMA_VERSION = 1
INSPECT_FILE = "inspect.json"
REPORT_FILE = "probe.json"
POLICY_FILE = "policy-candidates.json"
ARTIFACTS_KEY = "artifacts"

CASE_IDS: tuple[str, ...] = ("D01", "D02", "D03", "D04", "D05", "D06", "D07", "D08", "D09")
STATUS_PASSED = "passed"
STATUS_UNSUPPORTED = "unsupported"
STATUS_NEEDS_CANDIDATE = "needs_candidate"
STATUS_FAILED = "failed"
STATUS_NOT_RUN = "not_run"
STATUS_CLOSURE: tuple[str, ...] = (STATUS_PASSED, STATUS_UNSUPPORTED, STATUS_NEEDS_CANDIDATE, STATUS_FAILED,
                                   STATUS_NOT_RUN)
_SEVERITY = {STATUS_PASSED: 0, STATUS_UNSUPPORTED: 1, STATUS_NEEDS_CANDIDATE: 2, STATUS_FAILED: 3,
             STATUS_NOT_RUN: 4}

BUDGET_FIELDS: tuple[str, ...] = ("max_tokens", "max_completion_tokens", "n_predict")
BUDGET_PROMPT = ("Print positive integers starting from 1, separated by spaces. "
                 "Continue until the generation limit.")
BUDGET_SMALL = 32
BUDGET_LARGE = 64
TOOL_NAME = "get_weather"
TOOL_ARGUMENTS: dict[str, str] = {"city": "Beijing"}
TOOL_RESULT_TEXT = '{"city":"Beijing","marker":"SMS_WEATHER_OK_27","temperature_c":23}'
THINKING_PROMPT = "Compute 19 * 23. Finish with RESULT=437."
DENIED_TEMPLATE_FIELDS: tuple[str, ...] = ("chat_template", "chat_template_kwargs", "reasoning_format",
                                           "parse_tool_calls", "generation_prompt")
TEMPLATE_REQUEST_FIELDS: tuple[str, ...] = ("messages", "tools", "tool_choice", "parallel_tool_calls",
                                            "reasoning_effort")
UNKNOWN_FIELD = "sms_probe_unknown_field"
MAX_ARRAY_ELEMENTS = 20_000_000
MAX_STRING_BYTES = 64 * 1024 * 1024
EFFORT_LEVELS: tuple[str, ...] = ("minimal", "low", "medium", "high", "xhigh", "max")

_HEX40 = re.compile(r"[0-9a-f]{40}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_VERSION_COMMIT = re.compile(r"commit\s+([0-9a-f]{6,40})")
_BUILD_VERSION = re.compile(r"version:\s*([0-9][^\s(]*)")

SITE_KEYS = frozenset({"schema_version", "checkout", "remote_url", "branch", "expected_sha", "deployment_id",
                       "mode", "phase", "service_base_url", "gateway_base_url", "auth_env", "hardware_file",
                       "models", "timeouts", "request_limit", "policy_source_sha256", "fixture_set_sha256",
                       "rollback_input", "rollback_sha", "client_evidence"})
MODEL_KEYS = frozenset({"capabilities", "runtime_id", "profile_id", "image_digest", "model_path", "model_sha256",
                        "projector_path", "projector_sha256", "template_sha256", "upstream_base_url", "envelope",
                        "launch_argv"})
ENVELOPE_KEYS = frozenset({"ctx_size", "max_input_tokens", "max_output_tokens", "max_parallel",
                           "max_image_tokens", "max_image_edge_pixels", "max_images"})
TIMEOUT_KEYS = frozenset({"connect_seconds", "read_idle_seconds", "total_seconds"})
CLIENT_KEYS = frozenset({"name", "version", "evidence_root"})


class ProbeInputError(RuntimeError):
    """A missing or ill-formed input: exit 2, with the reason on stderr."""


# --------------------------------------------------------------------------- site input


@dataclass(frozen=True)
class EnvelopeInput:
    ctx_size: int
    max_input_tokens: int
    max_output_tokens: int
    max_parallel: int
    max_image_tokens: int
    max_image_edge_pixels: int
    max_images: int


@dataclass(frozen=True)
class ModelInput:
    model_id: str
    capabilities: tuple[str, ...]
    runtime_id: str
    profile_id: str
    image_digest: str
    model_path: str
    model_sha256: str
    projector_path: str | None
    projector_sha256: str | None
    template_sha256: str | None
    upstream_base_url: str
    envelope: EnvelopeInput
    launch_argv: tuple[str, ...]


@dataclass(frozen=True)
class Timeouts:
    connect_seconds: float
    read_idle_seconds: float
    total_seconds: float


@dataclass(frozen=True)
class SiteInput:
    path: Path
    sha256: str
    checkout: str
    remote_url: str
    branch: str
    expected_sha: str
    deployment_id: str
    mode: str
    phase: str
    service_base_url: str
    gateway_base_url: str | None
    auth_env: str | None
    hardware_file: str
    models: dict[str, ModelInput]
    timeouts: Timeouts
    request_limit: int
    policy_source_sha256: str | None
    fixture_set_sha256: str | None
    rollback_input: str
    rollback_sha: str | None
    client_evidence: tuple[dict[str, str], ...]


def _reject_keys(document: Mapping[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = sorted(set(document) - allowed)
    if unknown:
        raise ProbeInputError(f"{label} carries unknown fields: {', '.join(unknown)}")
    missing = sorted(allowed - set(document))
    if missing:
        raise ProbeInputError(f"{label} is missing fields: {', '.join(missing)}")


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ProbeInputError(f"{label} must be a non-empty string without NUL")
    return value


def _int(value: Any, label: str, *, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        raise ProbeInputError(f"{label} must be an integer >= {minimum}")
    return value


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or type(value) not in (int, float):
        raise ProbeInputError(f"{label} must be a number")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ProbeInputError(f"{label} must be a finite positive number")
    return number


def _hex(value: Any, label: str, *, bits: int) -> str:
    pattern = _HEX40 if bits == 40 else _HEX64
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ProbeInputError(f"{label} must be a lowercase {bits}-hex digest")
    return value


def _nullable_hex(value: Any, label: str) -> str | None:
    return None if value is None else _hex(value, label, bits=64)


def _url(value: Any, label: str, *, allow_null: bool = False) -> str | None:
    if value is None and allow_null:
        return None
    text = _text(value, label)
    if "@" in text.split("//", 1)[-1]:
        raise ProbeInputError(f"{label} must not embed credentials")
    if text.rstrip("/").endswith("/v1"):
        raise ProbeInputError(f"{label} must not carry a /v1 suffix")
    return text


def _regular_file(path: str, label: str) -> Path:
    candidate = Path(path)
    if not candidate.is_file() or candidate.is_symlink():
        raise ProbeInputError(f"{label} is not a regular file: {path}")
    return candidate


def load_site_input(path: str | Path) -> SiteInput:
    """Parse the site input exactly as acceptance §2 defines it; nothing is guessed."""
    source = Path(path)
    if not source.is_file() or source.is_symlink():
        raise ProbeInputError(f"the site input is not a regular file: {source}")
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProbeInputError(f"cannot read {source}: {exc}") from exc
    if not isinstance(document, dict):
        raise ProbeInputError("the site input must be a JSON object")
    _reject_keys(document, SITE_KEYS, "the site input")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ProbeInputError("schema_version must be the integer 1")
    checkout = _text(document["checkout"], "checkout")
    if not Path(checkout).is_dir():
        raise ProbeInputError(f"checkout is not an existing directory: {checkout}")
    mode = _text(document["mode"], "mode")
    if mode != "lab":
        raise ProbeInputError("mode must be lab")
    phase = _text(document["phase"], "phase")
    if phase not in ("baseline", "candidate"):
        raise ProbeInputError("phase must be baseline or candidate")
    models_document = document["models"]
    if not isinstance(models_document, dict) or not models_document:
        raise ProbeInputError("models must be a non-empty object keyed by model id")
    models: dict[str, ModelInput] = {}
    for model_id, model_document in models_document.items():
        if not isinstance(model_id, str) or not model_id:
            raise ProbeInputError("model ids must be non-empty strings")
        if not isinstance(model_document, dict):
            raise ProbeInputError(f"model {model_id} must be an object")
        _reject_keys(model_document, MODEL_KEYS, f"model {model_id}")
        capabilities = model_document["capabilities"]
        if (not isinstance(capabilities, list) or not capabilities
                or not all(isinstance(item, str) and item for item in capabilities)
                or len(set(capabilities)) != len(capabilities)):
            raise ProbeInputError(f"model {model_id} capabilities must be a non-empty list of unique strings")
        projector_path = model_document["projector_path"]
        projector_sha = model_document["projector_sha256"]
        if (projector_path is None) != (projector_sha is None):
            raise ProbeInputError(f"model {model_id} projector path and hash must both be present or both null")
        if projector_path is not None:
            _text(projector_path, f"model {model_id} projector_path")
            _hex(projector_sha, f"model {model_id} projector_sha256", bits=64)
        _regular_file(model_document["model_path"], f"model {model_id} model_path")
        if projector_path is not None:
            _regular_file(projector_path, f"model {model_id} projector_path")
        envelope_document = model_document["envelope"]
        if not isinstance(envelope_document, dict):
            raise ProbeInputError(f"model {model_id} envelope must be an object")
        _reject_keys(envelope_document, ENVELOPE_KEYS, f"model {model_id} envelope")
        envelope = EnvelopeInput(**{key: _int(value, f"model {model_id} envelope.{key}")
                                    for key, value in envelope_document.items()})
        launch_argv = model_document["launch_argv"]
        if not isinstance(launch_argv, list) or not launch_argv or not all(isinstance(a, str) for a in launch_argv):
            raise ProbeInputError(f"model {model_id} launch_argv must be a non-empty list of strings")
        models[model_id] = ModelInput(
            model_id=model_id,
            capabilities=tuple(capabilities),
            runtime_id=_text(model_document["runtime_id"], f"model {model_id} runtime_id"),
            profile_id=_text(model_document["profile_id"], f"model {model_id} profile_id"),
            image_digest=_text(model_document["image_digest"], f"model {model_id} image_digest"),
            model_path=_text(model_document["model_path"], f"model {model_id} model_path"),
            model_sha256=_hex(model_document["model_sha256"], f"model {model_id} model_sha256", bits=64),
            projector_path=projector_path,
            projector_sha256=projector_sha,
            template_sha256=_nullable_hex(model_document["template_sha256"], f"model {model_id} template_sha256"),
            upstream_base_url=_url(model_document["upstream_base_url"], f"model {model_id} upstream_base_url"),
            envelope=envelope,
            launch_argv=tuple(launch_argv),
        )
        if "vision" in models[model_id].capabilities and projector_path is None:
            raise ProbeInputError(f"model {model_id} declares vision without a projector")
    timeouts_document = document["timeouts"]
    if not isinstance(timeouts_document, dict):
        raise ProbeInputError("timeouts must be an object")
    _reject_keys(timeouts_document, TIMEOUT_KEYS, "timeouts")
    timeouts = Timeouts(**{key: _number(value, f"timeouts.{key}") for key, value in timeouts_document.items()})
    hardware_file = _text(document["hardware_file"], "hardware_file")
    if os.path.isabs(hardware_file):
        raise ProbeInputError("hardware_file must be relative to the site input")
    hardware_path = source.parent / hardware_file
    if hardware_path.is_symlink() or not hardware_path.is_file():
        raise ProbeInputError(f"hardware_file is missing or not a regular file: {hardware_path}")
    if hardware_path.resolve() != (source.parent.resolve() / hardware_file):
        raise ProbeInputError(f"hardware_file escapes the site input directory: {hardware_file}")
    clients = document["client_evidence"]
    if not isinstance(clients, list):
        raise ProbeInputError("client_evidence must be an array")
    client_entries: list[dict[str, str]] = []
    names: set[str] = set()
    for entry in clients:
        if not isinstance(entry, dict):
            raise ProbeInputError("each client_evidence entry must be an object")
        _reject_keys(entry, CLIENT_KEYS, "a client_evidence entry")
        name = _text(entry["name"], "client_evidence.name")
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
            raise ProbeInputError(f"client name {name!r} must be lowercase ASCII letters, digits and hyphens")
        if name in names:
            raise ProbeInputError(f"client_evidence repeats the name {name!r}")
        names.add(name)
        client_entries.append({"name": name, "version": _text(entry["version"], "client_evidence.version"),
                               "evidence_root": _text(entry["evidence_root"], "client_evidence.evidence_root")})
    rollback_input = _text(document["rollback_input"], "rollback_input")
    if not os.path.isabs(rollback_input) or not Path(rollback_input).is_file():
        raise ProbeInputError("rollback_input must be an existing absolute file path")
    return SiteInput(
        path=source,
        sha256=sha256_file(source),
        checkout=checkout,
        remote_url=_url(document["remote_url"], "remote_url"),
        branch=_text(document["branch"], "branch"),
        expected_sha=_hex(document["expected_sha"], "expected_sha", bits=40),
        deployment_id=_text(document["deployment_id"], "deployment_id"),
        mode=mode,
        phase=phase,
        service_base_url=_url(document["service_base_url"], "service_base_url"),
        gateway_base_url=_url(document["gateway_base_url"], "gateway_base_url", allow_null=True),
        auth_env=None if document["auth_env"] is None else _text(document["auth_env"], "auth_env"),
        hardware_file=hardware_file,
        models=models,
        timeouts=timeouts,
        request_limit=_int(document["request_limit"], "request_limit"),
        policy_source_sha256=_nullable_hex(document["policy_source_sha256"], "policy_source_sha256"),
        fixture_set_sha256=_nullable_hex(document["fixture_set_sha256"], "fixture_set_sha256"),
        rollback_input=rollback_input,
        rollback_sha=None if document["rollback_sha"] is None else _hex(document["rollback_sha"], "rollback_sha",
                                                                       bits=40),
        client_evidence=tuple(client_entries),
    )


def require_probe_ready(site: SiteInput, *, phase: str) -> None:
    """The candidate phase needs the identities a baseline is allowed to leave open."""
    if phase != site.phase:
        raise ProbeInputError(f"--phase {phase} does not match the site input phase {site.phase}")
    if phase == "candidate":
        if site.policy_source_sha256 is None:
            raise ProbeInputError("a candidate probe requires policy_source_sha256")
        if site.fixture_set_sha256 is None:
            raise ProbeInputError("a candidate probe requires fixture_set_sha256")
        for model_id, model in site.models.items():
            if model.template_sha256 is None:
                raise ProbeInputError(f"a candidate probe requires template_sha256 for {model_id}")


# --------------------------------------------------------------------------- output


def prepare_output(path: Path) -> Path:
    """Refuse anything but a fresh or empty directory: material is never overwritten."""
    candidate = Path(path)
    if candidate.exists():
        if not candidate.is_dir():
            raise ProbeInputError(f"the output exists and is not a directory: {candidate}")
        if any(candidate.iterdir()):
            raise ProbeInputError(f"the output directory is not empty, refusing to overwrite material: {candidate}")
    else:
        candidate.mkdir(parents=True)
    return candidate


class OutputDirectory:
    """Every byte this probe keeps, plus the manifest that names and hashes it."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def write_bytes(self, relative: str, payload: bytes) -> str:
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        return relative

    def write_json(self, relative: str, document: Any) -> str:
        return self.write_bytes(relative, (json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
                                .encode("utf-8"))

    def write_text(self, relative: str, text: str) -> str:
        return self.write_bytes(relative, text.encode("utf-8"))

    def artifacts(self) -> list[dict[str, Any]]:
        entries = []
        for path in sorted(candidate for candidate in self.root.rglob("*") if candidate.is_file()):
            entries.append({"path": str(path.relative_to(self.root)), "size": path.stat().st_size,
                            "sha256": sha256_file(path)})
        return entries


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


# --------------------------------------------------------------------------- ports


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class ShellPort(Protocol):
    def run(self, argv: Sequence[str], *, timeout: float) -> CommandResult: ...


class SubprocessShell:
    def run(self, argv: Sequence[str], *, timeout: float) -> CommandResult:
        try:
            done = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout, check=False)  # noqa: S603
        except (OSError, subprocess.SubprocessError) as exc:
            return CommandResult(tuple(argv), -1, "", f"{type(exc).__name__}: {exc}")
        return CommandResult(tuple(argv), done.returncode, done.stdout, done.stderr)


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


class HttpPort(Protocol):
    def request(self, method: str, url: str, *, headers: Mapping[str, str] | None = None,
                body: bytes | None = None, timeout: float) -> HttpResponse: ...


class HttpxPort:
    def __init__(self, *, connect_seconds: float) -> None:
        self._connect_seconds = connect_seconds

    def request(self, method: str, url: str, *, headers: Mapping[str, str] | None = None,
                body: bytes | None = None, timeout: float) -> HttpResponse:
        try:
            with httpx.Client(timeout=httpx.Timeout(self._connect_seconds, read=timeout),
                              follow_redirects=False) as client:
                answer = client.request(method, url, headers=dict(headers or {}), content=body)
        except Exception as exc:  # noqa: BLE001 - a transport failure is material, not a crash
            return HttpResponse(0, {}, f"{type(exc).__name__}: {exc}".encode("utf-8"))
        return HttpResponse(answer.status_code, dict(answer.headers), answer.content)


class Clock(Protocol):
    def utc_now(self) -> datetime: ...

    def monotonic(self) -> float: ...


class SystemClock:
    def utc_now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        return time.monotonic()


# --------------------------------------------------------------------------- helpers


def read_gguf_chat_template(path: str | Path) -> bytes | None:
    """The template the runtime would use, read from the immutable GGUF metadata.

    Only the metadata region is touched: the tensor payload of a multi-gigabyte
    file is never read, and the file is never written.
    """
    sizes = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}
    limit = 256 * 1024 * 1024
    try:
        with Path(path).open("rb") as handle:
            header = handle.read(4 + 4 + 8 + 8)
            if len(header) < 24 or header[:4] != b"GGUF":
                return None
            kv_count = struct.unpack("<Q", header[16:24])[0]
            for _ in range(min(kv_count, 100000)):
                if handle.tell() > limit:
                    return None
                raw_length = handle.read(8)
                if len(raw_length) < 8:
                    return None
                key = handle.read(struct.unpack("<Q", raw_length)[0])
                raw_type = handle.read(4)
                if len(raw_type) < 4:
                    return None
                value_type = struct.unpack("<I", raw_type)[0]
                if value_type == 8:  # GGUF string
                    raw_value = handle.read(8)
                    if len(raw_value) < 8:
                        return None
                    length = struct.unpack("<Q", raw_value)[0]
                    if length > MAX_STRING_BYTES:
                        return None
                    value = handle.read(length)
                    if key == b"tokenizer.chat_template":
                        return value
                    continue
                if value_type == 9:  # array: type, count, then every element
                    raw_array = handle.read(12)
                    if len(raw_array) < 12:
                        return None
                    element_type, count = struct.unpack("<IQ", raw_array)
                    if count > MAX_ARRAY_ELEMENTS:
                        return None
                    if element_type == 8:
                        # Skipping fewer elements than the array holds would leave the
                        # reader misaligned, so the whole array is walked.
                        for _ in range(count):
                            raw_value = handle.read(8)
                            if len(raw_value) < 8:
                                return None
                            length = struct.unpack("<Q", raw_value)[0]
                            if length > MAX_STRING_BYTES:
                                return None
                            handle.read(length)
                    elif element_type in sizes:
                        span = sizes[element_type] * count
                        if span > limit:
                            return None
                        handle.read(span)
                    else:
                        return None
                    continue
                if value_type not in sizes:
                    return None
                handle.read(sizes[value_type])
    except OSError:
        return None
    return None


def render_probe_png(size: int = 8) -> bytes:
    """A deterministic PNG the probe owns; nothing is downloaded."""
    rows = []
    for y in range(size):
        row = bytearray(b"\x00")
        for x in range(size):
            row += bytes(((x * 32) % 256, (y * 32) % 256, 96))
        rows.append(bytes(row))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (len(payload).to_bytes(4, "big") + kind + payload
                + zlib.crc32(kind + payload).to_bytes(4, "big"))

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


def image_data_url() -> str:
    return "data:image/png;base64," + base64.b64encode(render_probe_png()).decode("ascii")


def _worst(statuses: Sequence[str]) -> str:
    return max(statuses, key=lambda status: _SEVERITY.get(status, 4))


def _tool_definition(*, description: str) -> dict[str, Any]:
    return {"type": "function",
            "function": {"name": TOOL_NAME, "description": description,
                         "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                                        "required": ["city"], "additionalProperties": False}}}


def _redact(headers: Mapping[str, str]) -> dict[str, str]:
    safe = {}
    for key, value in headers.items():
        lowered = key.lower()
        safe[key] = "redacted" if lowered in ("authorization", "x-api-key", "api-key") else str(value)
    return safe


# --------------------------------------------------------------------------- the probe


@dataclass
class CaseContext:
    """What one case may do: bounded requests, observations, and nothing else."""

    case_id: str
    out: OutputDirectory
    probe: "ChatProbe"

    def chat(self, label: str, model_id: str, payload: Mapping[str, Any]) -> HttpResponse | None:
        return self.probe.send_chat(self.case_id, label, model_id, payload)

    def diagnostic(self, label: str, model_id: str, path: str, *, method: str = "POST",
                   payload: Mapping[str, Any] | None = None) -> HttpResponse:
        return self.probe.send_diagnostic(self.case_id, label, model_id, path, method=method, payload=payload)

    def observe(self, label: str, document: Any) -> str:
        return self.probe.observe(self.case_id, label, document)


@dataclass
class CaseRow:
    case_id: str
    status: str
    source_ref: dict[str, Any]
    request_files: list[str] = field(default_factory=list)
    response_files: list[str] = field(default_factory=list)
    observation_files: list[str] = field(default_factory=list)
    expected: str = ""
    actual: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None
    started_at_utc: str = ""
    ended_at_utc: str = ""

    def document(self, phase: str) -> dict[str, Any]:
        return {"id": self.case_id, "phase": phase, "status": self.status, "source_ref": self.source_ref,
                "request_files": sorted(self.request_files), "response_files": sorted(self.response_files),
                "observation_files": sorted(self.observation_files), "expected": self.expected,
                "actual": self.actual, "reason": self.reason, "started_at_utc": self.started_at_utc,
                "ended_at_utc": self.ended_at_utc}


class ChatProbe:
    """Collects identity material and runs the bounded D01—D09 cases."""

    def __init__(self, site: SiteInput, out: OutputDirectory, *, shell: ShellPort, http: HttpPort,
                 clock: Clock, phase: str) -> None:
        self.site = site
        self.out = out
        self.shell = shell
        self.http = http
        self.clock = clock
        self.phase = phase
        self.requests_used = 0
        self.request_files: list[str] = []
        self.response_files: list[str] = []
        self.observation_files: list[str] = []
        self.cases: list[CaseRow] = []
        self.helpers: dict[str, Any] = {}
        self._auth: str | None = (os.environ.get(site.auth_env) if site.auth_env else None)

    # -- request budget ----------------------------------------------------

    def planned_requests(self) -> dict[str, int]:
        """How many generation requests each case needs, prechecked before anything is sent."""
        tool_targets = self.capability_targets("tools")
        thinking_targets = self.capability_targets("thinking")
        vision_models = [model_id for model_id, model in self.site.models.items() if "vision" in model.capabilities]
        plan = {"D01": 0,
                "D02": len(self.site.models) * (len(BUDGET_FIELDS) + 2),
                "D03": len(tool_targets) * 5,
                "D04": len(tool_targets) * 3,
                "D05": len(tool_targets) * 3,
                "D06": len(thinking_targets) * (1 + len(EFFORT_LEVELS)),
                "D07": len(vision_models) + (1 if tool_targets else 0),
                "D08": len(tool_targets) * 3,
                "D09": len(tool_targets) * (len(DENIED_TEMPLATE_FIELDS) + 1)}
        return plan

    def capability_targets(self, capability: str) -> tuple[str, ...]:
        """The models to probe for a new capability.

        Registered first; when the deployment does not register it yet (this is
        the baseline's whole point) the last registered model is probed, which
        is where the extension plans to enable it.
        """
        with_capability = tuple(model_id for model_id, model in self.site.models.items()
                                if capability in model.capabilities)
        if with_capability:
            return with_capability
        if not self.site.models:
            return ()
        return (list(self.site.models)[-1],)

    # -- transport ---------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self._auth:
            headers["authorization"] = f"Bearer {self._auth}"
        return headers

    def send_chat(self, case_id: str, label: str, model_id: str, payload: Mapping[str, Any]) -> HttpResponse | None:
        if self.requests_used >= self.site.request_limit:
            return None
        self.requests_used += 1
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        url = self.site.service_base_url.rstrip("/") + "/v1/chat/completions"
        request_file = self.out.write_json(f"cases/{case_id}/requests/{label}.json",
                                           {"model_id": model_id, "url": url, "method": "POST",
                                            "headers": _redact(self._headers()), "body": payload})
        started = self.clock.utc_now()
        # A cold load happens inside this call, so the frozen *total* bounds the wait;
        # a per-chunk idle bound would cut a load that legitimately takes minutes.
        answer = self.http.request("POST", url, headers=self._headers(), body=body,
                                   timeout=self.site.timeouts.total_seconds)
        elapsed = (self.clock.utc_now() - started).total_seconds()
        parsed: Any = None
        try:
            parsed = answer.json()
        except (ValueError, UnicodeDecodeError):
            parsed = None
        response_file = self.out.write_json(f"cases/{case_id}/responses/{label}.json",
                                            {"status": answer.status, "headers": _redact(answer.headers),
                                             "elapsed_seconds": round(elapsed, 3),
                                             "body": parsed if parsed is not None else
                                             answer.body.decode("utf-8", "replace")})
        self.request_files.append(request_file)
        self.response_files.append(response_file)
        return answer

    def send_diagnostic(self, case_id: str, label: str, model_id: str, path: str, *, method: str = "POST",
                        payload: Mapping[str, Any] | None = None) -> HttpResponse:
        model = self.site.models[model_id]
        url = model.upstream_base_url.rstrip("/") + path
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        answer = self.http.request(method, url, headers=self._headers(), body=body,
                                   timeout=self.site.timeouts.total_seconds)
        parsed: Any = None
        try:
            parsed = answer.json()
        except (ValueError, UnicodeDecodeError):
            parsed = None
        self.observation_files.append(self.out.write_json(
            f"cases/{case_id}/observations/{label}.json",
            {"kind": "upstream_diagnostic", "url": url, "method": method, "status": answer.status,
             "body": parsed if parsed is not None else answer.body.decode("utf-8", "replace")}))
        return answer

    def observe(self, case_id: str, label: str, document: Any) -> str:
        relative = f"cases/{case_id}/observations/{label}.json"
        self.observation_files.append(self.out.write_json(relative, document))
        return relative

    # -- shared collection -------------------------------------------------

    def _shell_text(self, relative: str, argv: Sequence[str], *, timeout: float) -> CommandResult:
        result = self.shell.run(argv, timeout=timeout)
        self.out.write_text(relative,
                            f"$ {' '.join(argv)}\n--- exit: {result.returncode}\n"
                            f"--- stdout\n{result.stdout}\n--- stderr\n{result.stderr}\n")
        return result

    def collect_hardware(self) -> dict[str, Any]:
        commands = {
            "hostname": ["hostname"],
            "uname": ["uname", "-a"],
            "tegra_release": ["sh", "-c", "cat /etc/nv_tegra_release 2>/dev/null || echo unavailable"],
            "df": ["df", "-h"],
            "mount": ["mount"],
            "nvidia_smi": ["sh", "-c", "nvidia-smi 2>&1 | head -20 || true"],
            "tegrastats": ["sh", "-c", "timeout 3 tegrastats 2>/dev/null | tail -3 || echo unavailable"],
            "python": [sys.executable or "python3", "-VV"],
        }
        results = {}
        for name, argv in commands.items():
            done = self._shell_text(f"raw/{name}.txt", argv, timeout=30.0)
            results[name] = {"exit": done.returncode, "stdout": done.stdout.strip(), "stderr": done.stderr.strip()}
        return results

    def collect_git(self) -> dict[str, Any]:
        checkout = self.site.checkout
        results = {}
        for name, argv in (("head", ["git", "-C", checkout, "rev-parse", "HEAD"]),
                           ("status", ["git", "-C", checkout, "status", "--porcelain", "--untracked-files=all"]),
                           ("remotes", ["git", "-C", checkout, "remote", "-v"]),
                           ("branch", ["git", "-C", checkout, "branch", "--show-current"])):
            done = self._shell_text(f"raw/git-{name}.txt", argv, timeout=30.0)
            results[name] = {"exit": done.returncode, "stdout": done.stdout.strip()}
        results["expected_sha"] = self.site.expected_sha
        results["matches_expected"] = results["head"]["stdout"].strip() == self.site.expected_sha
        results["clean"] = results["status"]["stdout"] == ""
        return results

    def collect_assets(self) -> dict[str, Any]:
        report: dict[str, Any] = {}
        for model_id, model in self.site.models.items():
            entry: dict[str, Any] = {}
            for role, path, expected in (("model", model.model_path, model.model_sha256),
                                         ("projector", model.projector_path, model.projector_sha256)):
                if path is None:
                    entry[role] = None
                    continue
                candidate = Path(path)
                actual = sha256_file(candidate) if candidate.is_file() else None
                entry[role] = {"path": path, "expected_sha256": expected, "actual_sha256": actual,
                               "size_bytes": candidate.stat().st_size if candidate.is_file() else None,
                               "matches": actual == expected}
            report[model_id] = entry
        self.out.write_json("raw/assets.json", report)
        return report

    def collect_image(self) -> dict[str, Any]:
        """The fixed image's own identity: inspect, `--version`, `--help`; never a pull."""
        digests = sorted({model.image_digest for model in self.site.models.values()})
        report: dict[str, Any] = {"image_digests": digests}
        for index, digest in enumerate(digests):
            done = self.shell.run(["docker", "image", "inspect", digest], timeout=60.0)
            self.out.write_text(f"raw/image-inspect-{index}.txt",
                                f"$ docker image inspect {digest}\n--- exit: {done.returncode}\n"
                                f"{done.stdout}\n{done.stderr}\n")
            identity: Any = None
            try:
                identity = json.loads(done.stdout) if done.stdout.strip() else None
            except ValueError:
                identity = None
            report.setdefault("inspect", []).append({"digest": digest, "exit": done.returncode,
                                                     "resolved_id": _first_id(identity),
                                                     "matches_site": _matches_digest(identity, digest)})
            for label, flag in (("version", "--version"), ("help", "--help")):
                # A short process with no model and no GPU: the CUDA libraries are
                # bind-mounted read-only, so no compute context is created.
                result = self.shell.run(["docker", "run", "--rm",
                                         "-v", "/usr/lib/aarch64-linux-gnu/nvidia:/host-nvidia:ro",
                                         "-e", "LD_LIBRARY_PATH=/opt/llama-cpp:/host-nvidia:"
                                               "/usr/local/cuda/targets/aarch64-linux/lib",
                                         digest, flag], timeout=120.0)
                self.out.write_text(f"raw/image-{label}-{index}.txt",
                                    f"$ docker run --rm {digest} {flag}\n--- exit: {result.returncode}\n"
                                    f"--- stdout\n{result.stdout}\n--- stderr\n{result.stderr}\n")
                report.setdefault(label, []).append({"digest": digest, "exit": result.returncode,
                                                     "stdout": result.stdout, "stderr": result.stderr})
        version_text = "\n".join(item["stdout"] + item["stderr"] for item in report.get("version", []))
        commit = _VERSION_COMMIT.search(version_text)
        version = _BUILD_VERSION.search(version_text)
        report["version_string"] = version.group(1) if version else None
        report["source_commit"] = commit.group(1) if commit else None
        help_text = "\n".join(item["stdout"] for item in report.get("help", []))
        report["flags"] = {flag: flag in help_text for flag in ("--jinja", "--reasoning-format", "--reasoning-effort",
                                                                "--reasoning-budget", "--ignore-eos",
                                                                "--chat-template-kwargs")}
        return report

    def collect_instances(self) -> dict[str, Any]:
        """The argv a running instance really has: a flag in `--help` is not an enabled flag."""
        report: dict[str, Any] = {}
        for model_id in self.site.models:
            listing = self.shell.run(["docker", "ps", "--filter",
                                      f"label=io.self-model-switch.model={model_id}",
                                      "--format", "{{.Names}}"], timeout=30.0)
            names = [name for name in listing.stdout.split() if name]
            entry: dict[str, Any] = {"running": bool(names), "containers": names, "argv": None, "flags": {}}
            if names:
                done = self.shell.run(["docker", "inspect", names[0], "--format", "{{json .Args}}"], timeout=30.0)
                raw = done.stdout.strip()
                if raw:
                    try:
                        entry["argv"] = json.loads(raw)
                    except ValueError:
                        entry["argv"] = raw
            if isinstance(entry["argv"], list):
                entry["flags"] = {flag: flag in entry["argv"]
                                  for flag in ("--jinja", "--reasoning-format", "--reasoning-effort")}
            report[model_id] = entry
        self.out.write_json("raw/instances.json", report)
        return report

    def collect_templates(self) -> dict[str, Any]:
        report: dict[str, Any] = {}
        for model_id, model in self.site.models.items():
            entry: dict[str, Any] = {"gguf_template_bytes": None, "gguf_template_sha256": None,
                                     "props_template_sha256": None, "props_status": None, "source": None}
            raw = read_gguf_chat_template(model.model_path)
            if raw is not None:
                entry["gguf_template_bytes"] = len(raw)
                entry["gguf_template_sha256"] = sha256_bytes(raw)
                entry["source"] = "gguf:tokenizer.chat_template"
                self.out.write_bytes(f"raw/template-{model_id}.jinja", raw)
            props = self.http.request("GET", model.upstream_base_url.rstrip("/") + "/props", headers=self._headers(),
                                      timeout=self.site.timeouts.connect_seconds)
            entry["props_status"] = props.status
            if props.status == 200:
                try:
                    document = props.json()
                except (ValueError, UnicodeDecodeError):
                    document = None
                self.out.write_json(f"raw/props-{model_id}.json", document)
                template = document.get("chat_template") if isinstance(document, dict) else None
                if isinstance(template, str):
                    entry["props_template_sha256"] = sha256_bytes(template.encode("utf-8"))
            sha = entry["gguf_template_sha256"] or entry["props_template_sha256"]
            entry["template_sha256"] = sha
            entry["matches_site"] = None if sha is None else sha == model.template_sha256
            report[model_id] = entry
        return report

    # -- commands ----------------------------------------------------------

    def inspect(self) -> dict[str, Any]:
        hardware = self.collect_hardware()
        git_state = self.collect_git()
        assets = self.collect_assets()
        image = self.collect_image()
        instances = self.collect_instances()
        templates = self.collect_templates()
        missing = []
        if not image.get("source_commit"):
            missing.append("image source commit")
        if not image.get("version_string"):
            missing.append("image version string")
        for model_id, entry in templates.items():
            if entry["template_sha256"] is None:
                missing.append(f"template bytes for {model_id}")
        for model_id, entry in assets.items():
            for role in ("model", "projector"):
                if entry.get(role) is not None and not entry[role]["matches"]:
                    missing.append(f"{role} hash mismatch for {model_id}")
        if not git_state["matches_expected"]:
            missing.append("checkout HEAD differs from expected_sha")
        if not git_state["clean"]:
            missing.append("checkout is not clean")
        document = {"schema_version": PROBE_SCHEMA_VERSION, "phase": self.phase, "site_sha256": self.site.sha256,
                    "deployment_id": self.site.deployment_id, "checkout": self.site.checkout,
                    "expected_sha": self.site.expected_sha, "code_sha": git_state["head"]["stdout"].strip(),
                    "hardware": hardware, "git": git_state, "assets": assets, "image": image,
                    "instances": instances, "templates": templates, "missing": sorted(missing)}
        self.out.write_json(INSPECT_FILE, document)
        return document

    def probe(self) -> dict[str, Any]:
        plan = self.planned_requests()
        total = sum(plan.values())
        if total > self.site.request_limit:
            raise ProbeInputError(f"the expanded cases need {total} generation requests but request_limit is "
                                  f"{self.site.request_limit}")
        git_state = self.collect_git()
        assets = self.collect_assets()
        image = self.collect_image()
        instances = self.collect_instances()
        templates = self.collect_templates()
        self.helpers = {"git": git_state, "assets": assets, "image": image, "templates": templates,
                        "instances": instances}
        handlers: dict[str, Callable[[CaseContext], tuple[str, dict[str, Any], str | None]]] = {
            "D01": self._case_identity, "D02": self._case_budget, "D03": self._case_tool_choice,
            "D04": self._case_tool_template, "D05": self._case_history_count, "D06": self._case_thinking,
            "D07": self._case_vision, "D08": self._case_service_history, "D09": self._case_parameter_coverage,
        }
        for case_id in CASE_IDS:
            started = self.clock.utc_now()
            row = CaseRow(case_id=case_id, status=STATUS_NOT_RUN, source_ref={}, started_at_utc=_stamp(started))
            context = CaseContext(case_id=case_id, out=self.out, probe=self)
            try:
                status, actual, reason = handlers[case_id](context)
            except Exception as exc:  # noqa: BLE001 - a crashed case is material, not a silent skip
                status, actual, reason = STATUS_FAILED, {"error": f"{type(exc).__name__}: {exc}"}, "case_error"
            row.status = status if status in STATUS_CLOSURE else STATUS_FAILED
            row.actual = actual
            row.reason = reason
            row.source_ref = self._source_ref(case_id)
            row.request_files = [path for path in self.request_files if f"/{case_id}/" in path]
            row.response_files = [path for path in self.response_files if f"/{case_id}/" in path]
            row.observation_files = [path for path in self.observation_files if f"/{case_id}/" in path]
            row.ended_at_utc = _stamp(self.clock.utc_now())
            self.cases.append(row)
        report = {"schema_version": PROBE_SCHEMA_VERSION, "site_sha256": self.site.sha256, "phase": self.phase,
                  "code_sha": git_state["head"]["stdout"].strip(),
                  "requests_used": self.requests_used, "request_limit": self.site.request_limit,
                  "cases": [row.document(self.phase) for row in self.cases], "artifacts": self.out.artifacts()}
        self.out.write_json(REPORT_FILE, report)
        self.out.write_json(POLICY_FILE, self.policy_candidates())
        return report

    def policy_candidates(self) -> dict[str, Any]:
        """The RuntimeChatPolicy candidate values this run can defend (TC01)."""
        image = self.helpers.get("image", {})
        templates = self.helpers.get("templates", {})
        supported = set(self.helpers.get("supported_output_fields", set()))
        effort = set(self.helpers.get("effort_values", set()))
        return {"image_digest": sorted({model.image_digest for model in self.site.models.values()}),
                "source_revision": image.get("source_commit"), "version_string": image.get("version_string"),
                "flags": image.get("flags", {}),
                "recognized_output_fields": sorted(BUDGET_FIELDS),
                "supported_output_fields": sorted(supported),
                "effort_values": sorted(effort),
                "denied_template_fields": sorted(DENIED_TEMPLATE_FIELDS),
                "template_request_fields": sorted(TEMPLATE_REQUEST_FIELDS),
                "templates": {model_id: entry.get("template_sha256") for model_id, entry in templates.items()}}

    def _source_ref(self, case_id: str) -> dict[str, Any]:
        image = self.helpers.get("image", {})
        return {"image_version": image.get("version_string"), "image_source_commit": image.get("source_commit"),
                "image_digest": sorted({model.image_digest for model in self.site.models.values()}),
                "case": case_id, "phase": self.phase}

    # -- D cases -----------------------------------------------------------

    def _case_identity(self, ctx: CaseContext) -> tuple[str, dict[str, Any], str | None]:
        image = self.helpers["image"]
        templates = self.helpers["templates"]
        assets = self.helpers["assets"]
        git_state = self.helpers["git"]
        ctx.observe("identity", {"image": image, "templates": templates, "assets": assets, "git": git_state})
        mismatches = [item["digest"] for item in image.get("inspect", []) if not item.get("matches_site")]
        template_missing = sorted(model_id for model_id, entry in templates.items()
                                  if entry["template_sha256"] is None)
        hash_mismatch = sorted(f"{model_id}:{role}" for model_id, entry in assets.items()
                               for role in ("model", "projector")
                               if entry.get(role) is not None and not entry[role]["matches"])
        actual = {"image_digest_mismatch": mismatches, "missing_templates": template_missing,
                  "asset_hash_mismatch": hash_mismatch,
                  "template_sha256": {model_id: entry["template_sha256"] for model_id, entry in templates.items()},
                  "jinja_flag_source": image.get("flags", {}).get("--jinja"),
                  "reasoning_format_flag_source": image.get("flags", {}).get("--reasoning-format"),
                  "instance_flags": {model_id: entry["flags"]
                                     for model_id, entry in self.helpers.get("instances", {}).items()},
                  "checkout_matches_expected": git_state["matches_expected"], "checkout_clean": git_state["clean"]}
        if mismatches or hash_mismatch:
            return STATUS_FAILED, actual, "identity mismatch"
        if template_missing:
            return STATUS_FAILED, actual, f"the chat template could not be read for {', '.join(template_missing)}"
        if not git_state["matches_expected"] or not git_state["clean"]:
            return STATUS_FAILED, actual, "the checkout is not the expected commit or is dirty"
        if self.phase == "candidate":
            stale = sorted(model_id for model_id, entry in templates.items() if entry["matches_site"] is False)
            if stale:
                return STATUS_FAILED, actual, f"template hash differs from the site input for {', '.join(stale)}"
        return STATUS_PASSED, actual, None

    def _case_budget(self, ctx: CaseContext) -> tuple[str, dict[str, Any], str | None]:
        ignore_eos = bool(self.helpers.get("image", {}).get("flags", {}).get("--ignore-eos"))
        per_field: dict[str, Any] = {}
        statuses: list[str] = []
        supported: set[str] = set()
        for model_id in self.site.models:
            model_report: dict[str, Any] = {}
            for field in BUDGET_FIELDS:
                values: tuple[int | None, ...] = ((BUDGET_SMALL, BUDGET_LARGE) if field == "max_tokens"
                                                  else (BUDGET_SMALL,))
                for value in values:
                    payload: dict[str, Any] = {"model": model_id,
                                               "messages": [{"role": "user", "content": BUDGET_PROMPT}],
                                               field: value}
                    if ignore_eos:
                        payload["ignore_eos"] = True
                    answer = ctx.chat(f"{model_id}-{field}-{value}", model_id, payload)
                    key = f"{field}-{value}"
                    if answer is None:
                        model_report[key] = {"status": None, "reason": "request_limit"}
                        statuses.append(STATUS_NOT_RUN)
                        continue
                    observation = _summarise_completion(answer)
                    exhausted = (observation["finish_reason"] == "length"
                                 and isinstance(observation["completion_tokens"], int)
                                 and 0 < observation["completion_tokens"] <= value)
                    observation["exhausted"] = exhausted
                    model_report[key] = observation
                    if observation["http_status"] != 200:
                        statuses.append(STATUS_FAILED)
                    elif exhausted:
                        statuses.append(STATUS_PASSED)
                        supported.add(field)
                    else:
                        statuses.append(STATUS_FAILED)  # a short answer does not prove the budget
                        observation["not_proven"] = True
            per_field[model_id] = model_report
        # The unbudgeted default runs last: nothing is left to collect that a runtime
        # generating without a bound could disturb.
        for model_id in self.site.models:
            payload: dict[str, Any] = {"model": model_id,
                                       "messages": [{"role": "user", "content": BUDGET_PROMPT}]}
            if ignore_eos:
                payload["ignore_eos"] = True
            answer = ctx.chat(f"{model_id}-none-default", model_id, payload)
            observation = ({"status": None, "reason": "request_limit"} if answer is None
                           else _summarise_completion(answer))
            per_field[model_id]["none-default"] = observation
            if answer is None:
                statuses.append(STATUS_NOT_RUN)
            else:
                statuses.append(STATUS_PASSED if observation["http_status"] == 200 else STATUS_FAILED)
        ctx.observe("budget", per_field)
        self.helpers["supported_output_fields"] = supported
        actual = {"per_model": per_field, "supported_output_fields": sorted(supported),
                  "recognized_output_fields": list(BUDGET_FIELDS)}
        status = _worst(statuses)
        reason = None
        if status == STATUS_FAILED and any(item.get("not_proven") for model in per_field.values()
                                           for item in model.values() if isinstance(item, dict)):
            reason = "not_proven"
        if "max_tokens" not in supported:
            status = STATUS_FAILED
            reason = reason or "max_tokens did not provably bound the output"
        return status, actual, reason

    def _case_tool_choice(self, ctx: CaseContext) -> tuple[str, dict[str, Any], str | None]:
        targets = self.capability_targets("tools")
        if not targets:
            return STATUS_NOT_RUN, {"targets": []}, "no model to probe"
        statuses: list[str] = []
        detail: dict[str, Any] = {}
        for model_id in targets:
            registered = "tools" in self.site.models[model_id].capabilities
            variants = {
                "forced": {"tool_choice": {"type": "function", "function": {"name": TOOL_NAME}}},
                "required": {"tool_choice": "required"},
                "auto": {"tool_choice": "auto"},
                "none": {"tool_choice": "none"},
                "parallel_false": {"tool_choice": {"type": "function", "function": {"name": TOOL_NAME}},
                                   "parallel_tool_calls": False},
            }
            model_detail: dict[str, Any] = {}
            for label, extra in variants.items():
                payload = {"model": model_id,
                           "messages": [{"role": "user", "content": f"Call {TOOL_NAME} with city Beijing."}],
                           "tools": [_tool_definition(description="Return a fixed weather fixture for a city.")],
                           "max_tokens": min(1024, self.site.models[model_id].envelope.max_output_tokens)}
                payload.update(extra)
                answer = ctx.chat(label, model_id, payload)
                if answer is None:
                    model_detail[label] = {"status": None, "reason": "request_limit"}
                    statuses.append(STATUS_NOT_RUN)
                    continue
                observation = _summarise_tool_call(answer)
                model_detail[label] = observation
                if not registered:
                    # The deployment does not offer the capability yet: that is a
                    # candidate gap, not a defect of what is deployed today.
                    statuses.append(STATUS_NEEDS_CANDIDATE)
                    continue
                if observation["http_status"] != 200:
                    statuses.append(STATUS_FAILED)
                    continue
                calls = observation["tool_calls"]
                if label in ("forced", "required", "parallel_false"):
                    ok = (len(calls) == 1 and calls[0]["name"] == TOOL_NAME
                          and calls[0]["arguments"] == TOOL_ARGUMENTS)
                    statuses.append(STATUS_PASSED if ok else STATUS_FAILED)
                elif label == "none":
                    statuses.append(STATUS_PASSED if not calls else STATUS_FAILED)
                else:
                    statuses.append(STATUS_PASSED)  # auto: calling or not calling are both legal
            detail[model_id] = {"registered": registered, "variants": model_detail}
        ctx.observe("tool_choice", detail)
        return _worst(statuses), {"per_model": detail}, None

    def _case_tool_template(self, ctx: CaseContext) -> tuple[str, dict[str, Any], str | None]:
        targets = self.capability_targets("tools")
        if not targets:
            return STATUS_NOT_RUN, {"targets": []}, "no model to probe"
        messages = [{"role": "user", "content": "Say OK."}]
        variants = {
            "no_tools": [],
            "short_tools": [_tool_definition(description="Return a fixed weather fixture for a city.")],
            "long_tools": [_tool_definition(description="Return a fixed weather fixture for a city. " * 40)],
        }
        statuses: list[str] = []
        detail: dict[str, Any] = {}
        for model_id in targets:
            registered = "tools" in self.site.models[model_id].capabilities
            model_detail: dict[str, Any] = {}
            for label, tools in variants.items():
                payload: dict[str, Any] = {"model": model_id, "messages": messages,
                                           "max_tokens": min(1024, self.site.models[model_id]
                                                            .envelope.max_output_tokens)}
                if tools:
                    payload["tools"] = tools
                answer = ctx.chat(label, model_id, payload)
                if answer is None:
                    model_detail[label] = {"status": None, "reason": "request_limit"}
                    statuses.append(STATUS_NOT_RUN)
                    continue
                usage = _summarise_completion(answer)
                projected = _projected_tokens(ctx, model_id, payload)
                usage["projected_input_tokens"] = projected
                model_detail[label] = usage
                if projected is None:
                    statuses.append(STATUS_NEEDS_CANDIDATE if not registered else STATUS_FAILED)
                elif usage["http_status"] == 200 and projected != usage["prompt_tokens"]:
                    statuses.append(STATUS_FAILED)
                else:
                    statuses.append(STATUS_PASSED)
            counts = {label: entry.get("projected_input_tokens") for label, entry in model_detail.items()}
            if counts.get("no_tools") is not None and counts.get("no_tools") == counts.get("short_tools"):
                statuses.append(STATUS_FAILED)  # the tool definitions are not reaching the template
            detail[model_id] = {"registered": registered, "variants": model_detail}
        ctx.observe("tool_template", detail)
        return _worst(statuses), {"per_model": detail}, None

    def _case_history_count(self, ctx: CaseContext) -> tuple[str, dict[str, Any], str | None]:
        targets = self.capability_targets("tools")
        if not targets:
            return STATUS_NOT_RUN, {"targets": []}, "no model to probe"
        statuses: list[str] = []
        detail: dict[str, Any] = {}
        for model_id in targets:
            registered = "tools" in self.site.models[model_id].capabilities
            model_detail: dict[str, Any] = {}
            for label, arguments, reasoning in (
                    ("short", json.dumps(TOOL_ARGUMENTS), None),
                    ("large", json.dumps({**TOOL_ARGUMENTS, "note": "x" * 4096}), None),
                    ("reasoning", json.dumps(TOOL_ARGUMENTS), "The city is Beijing.")):
                assistant: dict[str, Any] = {"role": "assistant",
                                             "tool_calls": [{"id": "call_probe_history_1", "type": "function",
                                                             "function": {"name": TOOL_NAME, "arguments": arguments}}]}
                if reasoning is not None:
                    assistant["reasoning_content"] = reasoning
                payload = {"model": model_id,
                           "messages": [{"role": "user", "content": "What is the weather in Beijing?"}, assistant,
                                        {"role": "tool", "tool_call_id": "call_probe_history_1",
                                         "content": TOOL_RESULT_TEXT},
                                        {"role": "user", "content": "Reply with the marker only."}],
                           "max_tokens": min(1024, self.site.models[model_id].envelope.max_output_tokens)}
                answer = ctx.chat(label, model_id, payload)
                if answer is None:
                    model_detail[label] = {"status": None, "reason": "request_limit"}
                    statuses.append(STATUS_NOT_RUN)
                    continue
                usage = _summarise_completion(answer)
                projected = _projected_tokens(ctx, model_id, payload)
                usage["projected_input_tokens"] = projected
                model_detail[label] = usage
                if projected is None:
                    statuses.append(STATUS_NEEDS_CANDIDATE if not registered else STATUS_FAILED)
                elif projected != usage["prompt_tokens"]:
                    statuses.append(STATUS_FAILED)
                else:
                    statuses.append(STATUS_PASSED)
            small = model_detail.get("short", {}).get("projected_input_tokens")
            large = model_detail.get("large", {}).get("projected_input_tokens")
            if small is not None and large is not None and large <= small:
                statuses.append(STATUS_FAILED)  # the longer arguments are not being charged
            detail[model_id] = {"registered": registered, "variants": model_detail}
        ctx.observe("history_count", detail)
        return _worst(statuses), {"per_model": detail}, None

    def _case_thinking(self, ctx: CaseContext) -> tuple[str, dict[str, Any], str | None]:
        targets = self.capability_targets("thinking")
        if not targets:
            return STATUS_NOT_RUN, {"targets": []}, "no model to probe"
        statuses: list[str] = []
        accepted: set[str] = set()
        detail: dict[str, Any] = {}
        for model_id in targets:
            registered = "thinking" in self.site.models[model_id].capabilities
            model_detail: dict[str, Any] = {}
            payload: dict[str, Any] = {"model": model_id,
                                       "messages": [{"role": "user", "content": THINKING_PROMPT}],
                                       "max_tokens": min(1024, self.site.models[model_id]
                                                        .envelope.max_output_tokens)}
            answer = ctx.chat("default", model_id, payload)
            if answer is None:
                model_detail["default"] = {"status": None, "reason": "request_limit"}
                statuses.append(STATUS_NOT_RUN)
            else:
                summary = _summarise_completion(answer)
                summary["reasoning_present"] = bool(summary.get("reasoning"))
                model_detail["default"] = summary
                if summary["http_status"] != 200:
                    statuses.append(STATUS_FAILED)
                elif summary["reasoning_present"] and summary["content"]:
                    statuses.append(STATUS_PASSED)
                elif not registered:
                    statuses.append(STATUS_NEEDS_CANDIDATE)
                else:
                    statuses.append(STATUS_FAILED)
            for level in EFFORT_LEVELS:
                effort_payload = dict(payload, reasoning_effort=level)
                answer = ctx.chat(f"effort-{level}", model_id, effort_payload)
                if answer is None:
                    model_detail[f"effort-{level}"] = {"status": None, "reason": "request_limit"}
                    statuses.append(STATUS_NOT_RUN)
                    continue
                summary = _summarise_completion(answer)
                model_detail[f"effort-{level}"] = summary
                if summary["http_status"] == 200:
                    accepted.add(level)
                    statuses.append(STATUS_PASSED)
                elif not registered:
                    statuses.append(STATUS_NEEDS_CANDIDATE)
                else:
                    statuses.append(STATUS_FAILED)
            detail[model_id] = {"registered": registered, "variants": model_detail}
        ctx.observe("thinking", detail)
        self.helpers["effort_values"] = accepted
        return _worst(statuses), {"per_model": detail, "accepted_effort_values": sorted(accepted)}, None

    def _case_vision(self, ctx: CaseContext) -> tuple[str, dict[str, Any], str | None]:
        statuses: list[str] = []
        detail: dict[str, Any] = {}
        url = image_data_url()
        for model_id, model in self.site.models.items():
            if "vision" not in model.capabilities:
                detail[model_id] = {"skipped": "the model does not register vision"}
                continue
            payload = {"model": model_id,
                       "messages": [{"role": "user", "content": [{"type": "text", "text": "Describe the image."},
                                                                 {"type": "image_url", "image_url": {"url": url}}]}],
                       "max_tokens": min(1024, model.envelope.max_output_tokens)}
            answer = ctx.chat("vision", model_id, payload)
            if answer is None:
                detail[model_id] = {"status": None, "reason": "request_limit"}
                statuses.append(STATUS_NOT_RUN)
                continue
            summary = _summarise_completion(answer)
            text_only = dict(payload, messages=[{"role": "user", "content": "Describe the image."}])
            projected_image = _projected_tokens(ctx, model_id, payload)
            projected_text = _projected_tokens(ctx, model_id, text_only)
            summary["projected_image_tokens"] = projected_image
            summary["projected_text_tokens"] = projected_text
            detail[model_id] = summary
            if summary["http_status"] != 200:
                statuses.append(STATUS_FAILED)
            elif projected_image is None or projected_text is None:
                statuses.append(STATUS_NEEDS_CANDIDATE)
            elif projected_image <= projected_text:
                statuses.append(STATUS_FAILED)  # the image is not charged: an undercount
            else:
                statuses.append(STATUS_PASSED)
        for model_id in self.capability_targets("tools"):
            assistant = {"role": "assistant", "reasoning_content": "Looking at the image.",
                         "tool_calls": [{"id": "call_probe_vision_1", "type": "function",
                                         "function": {"name": TOOL_NAME, "arguments": json.dumps(TOOL_ARGUMENTS)}}]}
            payload = {"model": model_id,
                       "messages": [{"role": "user", "content": [{"type": "text", "text": "Describe the image."},
                                                                 {"type": "image_url", "image_url": {"url": url}}]},
                                    assistant, {"role": "tool", "tool_call_id": "call_probe_vision_1",
                                                "content": TOOL_RESULT_TEXT},
                                    {"role": "user", "content": "Reply with the marker only."}],
                       "max_tokens": min(1024, self.site.models[model_id].envelope.max_output_tokens)}
            answer = ctx.chat("combo", model_id, payload)
            if answer is None:
                detail[f"{model_id}-combo"] = {"status": None, "reason": "request_limit"}
                statuses.append(STATUS_NOT_RUN)
                continue
            summary = _summarise_completion(answer)
            detail[f"{model_id}-combo"] = summary
            statuses.append(STATUS_PASSED if summary["http_status"] == 200 and summary["content"]
                            else STATUS_NEEDS_CANDIDATE
                            if "tools" not in self.site.models[model_id].capabilities else STATUS_FAILED)
        ctx.observe("vision", detail)
        return _worst(statuses), {"per_model": detail}, None

    def _case_service_history(self, ctx: CaseContext) -> tuple[str, dict[str, Any], str | None]:
        """Synthetic histories through the compat surface; the baseline only records them."""
        targets = self.capability_targets("tools")
        if not targets:
            return STATUS_NOT_RUN, {"targets": []}, "no model to probe"
        statuses: list[str] = []
        detail: dict[str, Any] = {}
        for model_id in targets:
            registered = "tools" in self.site.models[model_id].capabilities
            model_detail: dict[str, Any] = {}
            for label, assistant in (
                    ("null_content", {"role": "assistant", "content": None}),
                    ("missing_content", {"role": "assistant"}),
                    ("empty_content", {"role": "assistant", "content": ""})):
                assistant = dict(assistant, tool_calls=[{"id": "call_probe_service_1", "type": "function",
                                                         "function": {"name": TOOL_NAME,
                                                                      "arguments": json.dumps(TOOL_ARGUMENTS)}}])
                payload = {"model": model_id,
                           "messages": [{"role": "user", "content": "What is the weather in Beijing?"}, assistant,
                                        {"role": "tool", "tool_call_id": "call_probe_service_1",
                                         "content": TOOL_RESULT_TEXT}],
                           "max_tokens": min(1024, self.site.models[model_id].envelope.max_output_tokens)}
                answer = ctx.chat(label, model_id, payload)
                if answer is None:
                    model_detail[label] = {"status": None, "reason": "request_limit"}
                    statuses.append(STATUS_NOT_RUN)
                    continue
                summary = _summarise_completion(answer)
                model_detail[label] = summary
                if self.phase == "baseline":
                    statuses.append(STATUS_NEEDS_CANDIDATE)
                elif label == "missing_content":
                    statuses.append(STATUS_PASSED if summary["http_status"] == 200 else STATUS_FAILED)
                elif registered:
                    statuses.append(STATUS_PASSED if summary["http_status"] == 200 else STATUS_FAILED)
                else:
                    statuses.append(STATUS_FAILED)
            detail[model_id] = {"registered": registered, "variants": model_detail}
        ctx.observe("service_history", detail)
        status = _worst(statuses)
        reason = "baseline: only the observed status codes and failure stages are recorded" \
            if self.phase == "baseline" else None
        return status, {"per_model": detail}, reason

    def _case_parameter_coverage(self, ctx: CaseContext) -> tuple[str, dict[str, Any], str | None]:
        targets = self.capability_targets("tools")
        if not targets:
            return STATUS_NOT_RUN, {"targets": []}, "no model to probe"
        statuses: list[str] = []
        detail: dict[str, Any] = {}
        for model_id in targets:
            registered = ("tools" in self.site.models[model_id].capabilities
                          or "thinking" in self.site.models[model_id].capabilities)
            model_detail: dict[str, Any] = {}
            fields = list(DENIED_TEMPLATE_FIELDS) + [UNKNOWN_FIELD]
            for field in fields:
                payload: dict[str, Any] = {"model": model_id,
                                           "messages": [{"role": "user", "content": "Say OK."}],
                                           "max_tokens": min(1024, self.site.models[model_id]
                                                            .envelope.max_output_tokens),
                                           field: "deepseek" if field == "reasoning_format" else None}
                answer = ctx.chat(field, model_id, payload)
                if answer is None:
                    model_detail[field] = {"status": None, "reason": "request_limit"}
                    statuses.append(STATUS_NOT_RUN)
                    continue
                summary = _summarise_completion(answer)
                forwarded = summary["http_status"] == 200
                summary["forwarded"] = forwarded
                model_detail[field] = summary
                if not registered:
                    statuses.append(STATUS_NEEDS_CANDIDATE)
                elif forwarded:
                    statuses.append(STATUS_FAILED)  # a registered model must refuse template overrides
                else:
                    statuses.append(STATUS_PASSED)
            detail[model_id] = {"registered": registered, "fields": model_detail}
        ctx.observe("parameter_coverage", detail)
        status = _worst(statuses)
        reason = None
        if status == STATUS_NEEDS_CANDIDATE:
            reason = "the deployment does not enforce the template/budget override policy yet"
        elif status == STATUS_FAILED:
            reason = "a registered model forwarded a template override"
        return status, {"per_model": detail}, reason


# --------------------------------------------------------------------------- response helpers


def _summarise_completion(answer: HttpResponse) -> dict[str, Any]:
    summary: dict[str, Any] = {"http_status": answer.status, "finish_reason": None, "content": "",
                               "reasoning": None, "prompt_tokens": None, "completion_tokens": None,
                               "tool_calls": [], "error": None}
    if answer.status != 200:
        summary["error"] = answer.body.decode("utf-8", "replace")[:2000]
        return summary
    try:
        document = answer.json()
    except (ValueError, UnicodeDecodeError):
        summary["error"] = "the response body is not JSON"
        return summary
    if not isinstance(document, dict):
        summary["error"] = "the response body is not an object"
        return summary
    usage = document.get("usage") if isinstance(document.get("usage"), dict) else {}
    summary["prompt_tokens"] = usage.get("prompt_tokens")
    summary["completion_tokens"] = usage.get("completion_tokens")
    choices = document.get("choices")
    if not isinstance(choices, list) or not choices:
        summary["error"] = "the response carries no choices"
        return summary
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    summary["finish_reason"] = choices[0].get("finish_reason") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return summary
    content = message.get("content")
    summary["content"] = content.strip() if isinstance(content, str) else ""
    reasoning = message.get("reasoning_content")
    summary["reasoning"] = reasoning if isinstance(reasoning, str) else None
    summary["tool_calls"] = _tool_calls(message)
    return summary


def _tool_calls(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    calls = message.get("tool_calls")
    if not isinstance(calls, list):
        return []
    extracted: list[dict[str, Any]] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        raw = function.get("arguments")
        arguments: Any = None
        if isinstance(raw, str):
            try:
                arguments = json.loads(raw)
            except ValueError:
                arguments = raw
        extracted.append({"id": call.get("id"), "name": function.get("name"), "arguments": arguments})
    return extracted


def _summarise_tool_call(answer: HttpResponse) -> dict[str, Any]:
    summary = _summarise_completion(answer)
    summary["call_count"] = len(summary["tool_calls"])
    return summary


def _projected_tokens(ctx: CaseContext, model_id: str, payload: Mapping[str, Any]) -> int | None:
    """apply-template + tokenize, the runtime's own count of the template it renders."""
    template_payload = {key: payload[key] for key in TEMPLATE_REQUEST_FIELDS if key in payload}
    answer = ctx.diagnostic("apply-template", model_id, "/apply-template", payload=template_payload)
    if answer.status != 200:
        return None
    try:
        document = answer.json()
    except (ValueError, UnicodeDecodeError):
        return None
    prompt = document.get("prompt") if isinstance(document, dict) else None
    if not isinstance(prompt, str):
        return None
    tokenized = ctx.diagnostic("tokenize", model_id, "/tokenize", payload={"content": prompt, "add_special": True})
    if tokenized.status != 200:
        return None
    try:
        tokens = tokenized.json().get("tokens")
    except (ValueError, UnicodeDecodeError):
        return None
    return len(tokens) if isinstance(tokens, list) else None


def _first_id(document: Any) -> str | None:
    if isinstance(document, list) and document and isinstance(document[0], dict):
        identifier = document[0].get("Id")
        return identifier if isinstance(identifier, str) else None
    return None


def _matches_digest(document: Any, digest: str) -> bool:
    """`sms-llama-cpp@sha256:<hex>` must match the resolved image id's own hex."""
    expected = digest.rsplit("@", 1)[-1]
    if not expected or not isinstance(document, list) or not document or not isinstance(document[0], dict):
        return False
    entry = document[0]
    found: list[str] = []
    identifier = entry.get("Id")
    if isinstance(identifier, str):
        found.append(identifier.rsplit("@", 1)[-1])
    listed = entry.get("RepoDigests")
    if isinstance(listed, list):
        found.extend(item.rsplit("@", 1)[-1] for item in listed if isinstance(item, str))
    return expected in found


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------- CLI


def _load(args: argparse.Namespace, phase: str | None) -> tuple[SiteInput, OutputDirectory]:
    site = load_site_input(args.site)
    effective = phase or site.phase
    require_probe_ready(site, phase=effective)
    return site, OutputDirectory(prepare_output(args.output))


def _inspect(args: argparse.Namespace, *, shell: ShellPort | None = None, http: HttpPort | None = None,
             clock: Clock | None = None) -> int:
    site, out = _load(args, None)
    probe = ChatProbe(site, out, shell=shell if shell is not None else SubprocessShell(),
                      http=http if http is not None else HttpxPort(connect_seconds=site.timeouts.connect_seconds),
                      clock=clock if clock is not None else SystemClock(), phase=site.phase)
    document = probe.inspect()
    print(f"inspect written to {out.root}: missing={len(document['missing'])} "
          f"template_sha256={ {k: v['template_sha256'] for k, v in document['templates'].items()} }")
    return EXIT_FAILED if document["missing"] else EXIT_OK


def _probe(args: argparse.Namespace, *, shell: ShellPort | None = None, http: HttpPort | None = None,
           clock: Clock | None = None) -> int:
    site, out = _load(args, args.phase)
    probe = ChatProbe(site, out, shell=shell if shell is not None else SubprocessShell(),
                      http=http if http is not None else HttpxPort(connect_seconds=site.timeouts.connect_seconds),
                      clock=clock if clock is not None else SystemClock(), phase=args.phase)
    plan_total = sum(probe.planned_requests().values())
    if plan_total > site.request_limit:
        print(f"probe: the expanded cases need {plan_total} generation requests but request_limit is "
              f"{site.request_limit}", file=sys.stderr)
        return EXIT_INPUT
    report = probe.probe()
    statuses = {row["id"]: row["status"] for row in report["cases"]}
    print(f"probe written to {out.root}: requests={report['requests_used']}/{report['request_limit']}")
    print("cases: " + ", ".join(f"{case}={statuses[case]}" for case in CASE_IDS))
    if args.phase == "candidate":
        return EXIT_FAILED if any(status != STATUS_PASSED for status in statuses.values()) else EXIT_OK
    # A baseline probe only promises complete material; a case that never ran is not material.
    return EXIT_FAILED if any(status == STATUS_NOT_RUN for status in statuses.values()) else EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m model_scheduler.acceptance.chat_probe",
                                     description="fixed-image chat capability probe (CT01)")
    sub = parser.add_subparsers(dest="command", required=True)

    inspect_parser = sub.add_parser("inspect", help="read-only identity collection; never starts a model")
    inspect_parser.add_argument("--site", type=Path, required=True)
    inspect_parser.add_argument("--output", type=Path, required=True)

    probe_parser = sub.add_parser("probe", help="bounded compatibility probing against the running deployment")
    probe_parser.add_argument("--site", type=Path, required=True)
    probe_parser.add_argument("--phase", choices=("baseline", "candidate"), required=True)
    probe_parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None, *, shell: ShellPort | None = None, http: HttpPort | None = None,
         clock: Clock | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "inspect":
            return _inspect(args, shell=shell, http=http, clock=clock)
        return _probe(args, shell=shell, http=http, clock=clock)
    except ProbeInputError as exc:
        print(f"{args.command}: {exc}", file=sys.stderr)
        return EXIT_INPUT


if __name__ == "__main__":
    raise SystemExit(main())
