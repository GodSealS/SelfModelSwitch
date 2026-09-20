"""P30: the real ports behind the O cases.

O01 is the mixed workload against the deployment's compat surface plus an
end-state probe the zero tolerances are read from; O05 is the P26 preflight
primitive against the live site. Both are honest material: every number comes
from the live service, the container listing, `/proc/vmstat` or a file on this
machine.

The remaining ports (O02 storage faults, O03 Docker faults, O04 restart, O06
lab release) need the deployment's private mount namespace and its service
manager. Until they are wired their cases report `not_run` with the missing
condition — never a placeholder pass.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import re
import subprocess
import time
from typing import Any, Callable, Mapping, Sequence

import httpx

from ..evidence_contracts import ContractError, candidate_digest, device_digest, parse_candidate
from ..preflight_v3 import PreflightError, manifest_identity, verify_environment
from ..process_observer import DEPLOYMENT_LABEL
from .workload import PlannedRequest

SITE_DEVICE_FIELDS = ("machine_id_sha256", "architecture", "device_tree_sha256", "mem_total_bytes",
                      "kernel_release", "model_disk_uuid", "scratch_disk_uuid")
DOCKER_TIMEOUT_SECONDS = 60


class PortError(RuntimeError):
    """A port refusal; the orchestrator turns it into a failed step, never a pass."""


# ---------------------------------------------------------------------------
# O01: the compat surface as the workload client, and the end-state probe


@dataclass
class HttpWorkloadDriver:
    """One request per arrival, measured end to end on the client side.

    The control view does not expose a phase split, so the whole latency is
    recorded as `execute_seconds` with `queue_seconds`/`load_seconds` at zero:
    the total a p95 is computed from still contains queueing, loading and
    execution — it is simply not split into parts the service never reports.
    """

    base_url: str
    timeout_seconds: float = 1800.0
    clock: Callable[[], float] = field(default=time.monotonic)
    post: Callable[..., Any] = field(default=httpx.post)

    def send(self, request: PlannedRequest) -> Mapping[str, Any]:
        if request.capability != "chat":
            raise PortError(f"the workload driver speaks chat; got {request.capability!r}")
        payload = {"model": request.model_id,
                   "messages": [{"role": "user", "content": "Reply with one word."}],
                   "max_tokens": 8}
        started = self.clock()
        try:
            response = self.post(self.base_url.rstrip("/") + "/v1/chat/completions", json=payload,
                                 timeout=self.timeout_seconds)
        except Exception as exc:  # noqa: BLE001 - a refused send is material, not a crash
            return {"error": f"{type(exc).__name__}: {exc}"}
        elapsed = self.clock() - started
        document: dict[str, Any] = {"status_code": response.status_code,
                                    "queue_seconds": 0.0, "load_seconds": 0.0,
                                    "execute_seconds": round(elapsed, 3),
                                    "latency_seconds": round(elapsed, 3),
                                    "latency_source": "client end-to-end"}
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, Mapping):
            document["usage"] = body.get("usage")
            document["model"] = body.get("model") or request.model_id
        return document


@dataclass
class FinalStateProbe:
    """The zero-tolerance end state, read from the live service and this machine.

    Every figure has a named source: `queue_depth`/`leases`/`sessions` come from
    `/api/status`, `instances_running`/`residual` from the managed container
    listing, `oom` from the `/proc/vmstat` delta and `unexpected_500` from the
    service log when one is supplied. A figure that cannot be read stays `None`,
    which fails the case instead of asserting a zero nobody observed.
    """

    base_url: str
    deployment_id: str
    log_path: Path | None = None
    timeout_seconds: float = 60.0
    _oom_baseline: int | None = field(default=None, init=False)

    def baseline(self) -> None:
        self._oom_baseline = _oom_kills()

    def read(self) -> dict:
        status = _get_json(self.base_url, "/api/status", timeout=self.timeout_seconds)
        models = status.get("models") if isinstance(status.get("models"), Mapping) else {}
        leases = None
        if isinstance(models, Mapping) and models:
            leases = sum(int(model.get("in_flight") or 0) + int(model.get("cancelling") or 0)
                         for model in models.values() if isinstance(model, Mapping))
        sessions = status.get("sessions") if isinstance(status.get("sessions"), Mapping) else None
        running = _managed_container_count(self.deployment_id)
        kills = _oom_kills()
        return {
            "oom": None if self._oom_baseline is None or kills is None else max(0, kills - self._oom_baseline),
            "unexpected_500": _count_500(self.log_path),
            "unsafe_evictions": _unsafe_evictions(models),
            "residual": running,
            "queue_depth": status.get("queue_size") if isinstance(status.get("queue_size"), int) else None,
            "leases": leases,
            "sessions": None if sessions is None else len(sessions.get("pending") or []),
            "instances_running": running,
        }

    # The orchestrator reads the end state through a mapping so the probe runs
    # *after* the workload, not when the call is assembled.
    def mapping(self) -> Mapping[str, Any]:
        return _LazyMapping(self.read)


class _LazyMapping(Mapping):
    """A read-only mapping that evaluates its reader on first access."""

    def __init__(self, reader: Callable[[], Mapping[str, Any]]) -> None:
        self._reader = reader
        self._value: Mapping[str, Any] | None = None

    def _resolve(self) -> Mapping[str, Any]:
        if self._value is None:
            self._value = dict(self._reader())
        return self._value

    def __getitem__(self, key: str) -> Any:
        return self._resolve()[key]

    def __iter__(self):
        return iter(self._resolve())

    def __len__(self) -> int:
        return len(self._resolve())


def _get_json(base_url: str, path: str, *, timeout: float) -> dict:
    try:
        response = httpx.get(base_url.rstrip("/") + path, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        raise PortError(f"cannot read {path}: {type(exc).__name__}: {exc}") from exc
    try:
        document = response.json()
    except ValueError as exc:
        raise PortError(f"{path} did not answer JSON") from exc
    if not isinstance(document, dict):
        raise PortError(f"{path} did not answer an object")
    return document


def _run(argv: Sequence[str], *, timeout: float = DOCKER_TIMEOUT_SECONDS) -> tuple[int, str]:
    try:
        result = subprocess.run(list(argv), capture_output=True, text=True, check=False, timeout=timeout)  # noqa: S603
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PortError(f"{argv[0]} is not usable: {exc}") from exc
    return result.returncode, result.stdout


def _managed_container_count(deployment_id: str) -> int | None:
    """How many containers of this deployment exist at all (running or not)."""
    try:
        code, stdout = _run(["docker", "ps", "--all", "--quiet", "--no-trunc",
                             "--filter", f"label={DEPLOYMENT_LABEL}={deployment_id}"])
    except PortError:
        return None
    if code != 0:
        return None
    return len([line for line in stdout.splitlines() if line.strip()])


def _oom_kills() -> int | None:
    try:
        text = Path("/proc/vmstat").read_text(encoding="utf-8")
    except OSError:
        return None
    found = re.search(r"^oom_kill\s+(\d+)$", text, re.MULTILINE)
    return int(found.group(1)) if found else None


def _count_500(log_path: Path | None) -> int | None:
    """Unexpected 500s in the service's own access log; no log, no claim."""
    if log_path is None:
        return None
    try:
        text = Path(log_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return sum(1 for line in text.splitlines() if re.search(r'"\s*500\s', line))


def _unsafe_evictions(models: Any) -> int | None:
    """Models left in an error state by a stop/eviction: an unsafe eviction would show here."""
    if not isinstance(models, Mapping):
        return None
    markers = ("stop_unverified", "evict", "cleanup")
    return sum(1 for model in models.values() if isinstance(model, Mapping)
               and isinstance(model.get("last_error"), str)
               and any(marker in model["last_error"] for marker in markers))


# ---------------------------------------------------------------------------
# O05: the P26 preflight primitive plus the tamper scenarios


@dataclass
class PreflightPrimitivePort:
    """`verify_environment` over the live site; nothing is loaded, no gate is invoked."""

    site: Mapping[str, Any]
    tamper: Sequence[Mapping[str, Any]] = ()

    def preflight(self, candidate: Any, evidence: Any) -> Mapping[str, Any]:
        try:
            manifest = self._manifest(candidate, evidence)
        except (ContractError, PreflightError, KeyError, TypeError, ValueError) as exc:
            return {"accepted": False, "reason": f"{type(exc).__name__}: {exc}", "loaded": 0,
                    "production_gate": False}
        try:
            result = verify_environment(manifest, site=self.site)
        except PreflightError as exc:
            return {"accepted": False, "reason": str(exc), "loaded": 0, "production_gate": False}
        problems = list(result["problems"])
        return {"accepted": bool(result["ok"]), "reason": problems[0] if problems else "",
                "loaded": result["loaded_models"], "production_gate": False}

    def tamper_scenarios(self) -> Sequence[Mapping[str, Any]]:
        return list(self.tamper)

    def _manifest(self, candidate: Any, evidence: Any) -> dict:
        parsed = parse_candidate(candidate)  # a tampered candidate cannot even be parsed
        started = None
        if isinstance(evidence, Mapping):
            started = evidence.get("started_at")
        moment = started if isinstance(started, str) and started else _utc_now()
        device = asdict(parsed.device)
        manifest = {
            "schema_version": 3, "mode": "production", "deployment_id": parsed.deployment_id,
            "candidate_sha256": candidate_digest(parsed),
            "source_archive_sha256": parsed.source_archive_sha256,
            "config_sha256": parsed.config_sha256,
            "device_digest": device_digest(parsed.device),
            # The candidate's own device facts: layer 1 compares them with the live
            # site, so editing the registration must be refused, not papered over.
            "device": {field: device[field] for field in SITE_DEVICE_FIELDS},
            "model_filesystem": self.site["filesystem"],
            "models": {model.model_id: {
                "image_digest": _runtime_image(parsed, model.runtime_id),
                "container_name": f"sms-{parsed.deployment_id}-{model.model_id}",
                "assets": [{"path": asset.path, "size_bytes": asset.size_bytes, "sha256": asset.sha256}
                           for asset in model.assets]} for model in parsed.models},
            "evidence": {"started_at": moment},
        }
        manifest["identity_sha256"] = manifest_identity(manifest)
        return manifest


def _runtime_image(candidate: Any, runtime_id: str) -> str:
    for runtime in candidate.runtimes:
        if runtime.runtime_id == runtime_id:
            return runtime.image_digest
    raise PortError(f"model names an unregistered runtime {runtime_id!r}")


def read_site_facts(*, candidate: Any, config_path: Path, model_directory: Path,
                    source_archive_sha256: str, scratch_path: Path | None = None) -> dict:
    """The live site facts layer 1 compares against; every value is read on site."""
    device = read_device_facts(config_path=config_path, model_directory=model_directory, scratch_path=scratch_path)
    files: dict[str, dict] = {}
    for model in candidate.models:
        for asset in model.assets:
            path = Path(model_directory) / asset.path
            try:
                payload = path.read_bytes()
            except OSError as exc:
                raise PortError(f"the registered asset {asset.path} is not readable: {exc}") from exc
            files[asset.path] = {"size_bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
    images = {}
    for runtime in candidate.runtimes:
        images[runtime.image_digest] = image_is_present(runtime.image_digest)
    from ..config import load_config

    config = load_config(Path(config_path))
    digest = config.config_digest() if hasattr(config, "config_digest") else None
    return {**device, "filesystem": _filesystem_of(model_directory),
            "images": images, "model_files": files,
            "config_sha256": digest, "source_archive_sha256": source_archive_sha256}


def read_device_facts(*, config_path: Path, model_directory: Path, scratch_path: Path | None = None) -> dict:
    """The C09 device fields, read from this machine (no value is guessed)."""
    machine_id = Path("/etc/machine-id")
    meminfo = Path("/proc/meminfo")
    if not machine_id.is_file() or not meminfo.is_file():
        raise PortError("this machine does not expose /etc/machine-id or /proc/meminfo")
    found = re.search(r"^MemTotal:\s+(\d+) kB", meminfo.read_text(encoding="utf-8"), re.MULTILINE)
    if not found:
        raise PortError("MemTotal is not readable")
    scratch = Path(scratch_path) if scratch_path is not None else Path(config_path).parent
    return {
        "machine_id_sha256": hashlib.sha256(machine_id.read_bytes().strip()).hexdigest(),
        "architecture": platform.machine(),
        "device_tree_sha256": _device_tree_sha256(),
        "mem_total_bytes": int(found.group(1)) * 1024,
        "kernel_release": platform.release(),
        "model_disk_uuid": _mount_uuid(model_directory),
        "scratch_disk_uuid": _mount_uuid(scratch),
    }


def _device_tree_sha256() -> str:
    root = Path("/proc/device-tree")
    if not root.is_dir():
        raise PortError("/proc/device-tree is not present")
    digest = hashlib.sha256()
    for path in sorted(entry for entry in root.rglob("*") if entry.is_file()):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _mount_uuid(path: Path) -> str:
    code, stdout = _run(["findmnt", "-no", "UUID", "--target", str(path)])
    uuid = stdout.strip().splitlines()[0].strip() if stdout.strip() else ""
    if code != 0 or not uuid:
        raise PortError(f"no filesystem UUID is readable for {path}")
    return uuid


def _filesystem_of(path: Path) -> str:
    code, stdout = _run(["findmnt", "-no", "FSTYPE", "--target", str(path)])
    value = stdout.strip().splitlines()[0].strip() if stdout.strip() else ""
    if code != 0 or not value:
        raise PortError(f"no filesystem type is readable for {path}")
    return value


def image_is_present(image_digest: str) -> bool:
    """Whether the pinned digest really exists locally (`docker image inspect`)."""
    reference = image_digest.split("@", 1)[0] if "@" in image_digest else image_digest
    code, stdout = _run(["docker", "image", "inspect", "--format", "{{.Id}}", reference])
    if code != 0 or not stdout.strip():
        return False
    recorded = image_digest.split("sha256:", 1)[1] if "sha256:" in image_digest else ""
    identifier = stdout.strip().splitlines()[0].strip()
    return identifier.startswith(f"sha256:{recorded}")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_boot_id(base_url: str, *, timeout: float = 30.0) -> str | None:
    """The live deployment's boot id, when the compat surface answers."""
    try:
        document = _get_json(base_url, "/api/status", timeout=timeout)
    except PortError:
        return None
    boot = document.get("boot_id")
    return boot if isinstance(boot, str) and boot else None


def tamper_scenarios(candidate_document: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The O05 scenarios: each edit must be refused before any load."""
    scenarios: list[dict[str, Any]] = []

    def edited(mutate) -> dict:
        document = json.loads(json.dumps(candidate_document))
        mutate(document)
        return document

    def _asset(document: dict) -> None:
        document["models"][0]["assets"][0]["sha256"] = "0" * 64

    def _device(document: dict) -> None:
        document["device"]["mem_total_bytes"] = int(document["device"]["mem_total_bytes"]) + 1

    def _image(document: dict) -> None:
        document["runtimes"][0]["image_digest"] = "sms-llama-cpp@sha256:" + "1" * 64

    scenarios.append({"name": "asset hash", "candidate": edited(_asset), "evidence": {}})
    scenarios.append({"name": "device change", "candidate": edited(_device), "evidence": {}})
    scenarios.append({"name": "image digest", "candidate": edited(_image), "evidence": {}})
    scenarios.append({"name": "expired evidence", "candidate": json.loads(json.dumps(candidate_document)),
                      "evidence": {"started_at": "2020-01-01T00:00:00+00:00"}})
    return scenarios


# ---------------------------------------------------------------------------
# ports that are not wired yet


class NotWiredPort:
    """A port that is not wired: every action reports itself unavailable.

    The orchestrator maps `{"available": False}` to `not_run`, so an unwired
    port can never turn into a passed case.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def __getattr__(self, name: str) -> Callable[..., dict]:
        def unavailable(*_args: Any, **_kwargs: Any) -> dict:
            return {"available": False, "reason": self.reason, "action": name}

        return unavailable
