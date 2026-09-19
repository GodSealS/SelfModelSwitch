"""The P22 raw collector: structured events, raw samples and failure material on disk.

The collector is deliberately decoupled from the executor: it is an `EventSink`
plus a raw-sample recorder, so a case runner only emits and never owns files.

Rules it enforces (plan/08-execution-plan.md C03, P22):

* every persisted event carries **both clocks** and the run / case / attempt /
  fence / instance / device attribution of the moment it was emitted;
* `sequence` is strictly increasing per boot — the sink owns ordering, it never
  repairs or reorders;
* device activity is derived from **raw samples** (tegrastats lines, process
  maps). A boolean "gpu_verified" is never the evidence, and without raw
  samples the attribution is refused instead of asserted;
* nothing is ever deleted: failed material stays on disk for the report, and
  `close()` writes a per-file size/sha256 manifest (excluding itself, which
  cannot hash its own digest).
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any, Iterable, Mapping

from ..control_protocol_v1 import InstanceIdentity
from ..ports_v3 import Clock, EventRecord

COLLECTOR_VERSION = 1
_GR3D_PERCENT = re.compile(r"GR3D_FREQ\s+(\d+)%")
CUDA_LIBRARY_MARKERS = ("libcuda.so", "libcudart.so", "libcublas")


class CollectorError(RuntimeError):
    """The collector refuses to write material it cannot attribute."""


def collector_sha256() -> str:
    """The collector's own source hash, pinned into every candidate."""
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class _SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def utc_now(self) -> datetime:
        return datetime.now(timezone.utc)


def derive_attribution(rows: Iterable[Mapping[str, Any]]) -> dict:
    """Device activity recomputed from raw rows; both the collector and the driver use it.

    The window that matters belongs to the caller: a driver that samples one
    execution derives that execution's activity, while the collector derives the
    activity of every sample it persisted. Neither ever accepts a boolean.
    """
    counts: dict[str, int] = {}
    gr3d_peak: int | None = None
    cuda_mapped = False
    for row in rows:
        kind = str(row.get("kind") or "")
        raw = str(row.get("raw", ""))
        counts[kind] = counts.get(kind, 0) + 1
        if kind == "tegrastats":
            found = _GR3D_PERCENT.search(raw)
            if found is not None:
                gr3d_peak = max(gr3d_peak or 0, int(found.group(1)))
        elif kind == "proc_maps" and any(marker in raw for marker in CUDA_LIBRARY_MARKERS):
            cuda_mapped = True
    if not counts:
        raise CollectorError("device attribution needs at least one raw sample: a boolean claim is not evidence")
    return {"gr3d_peak_pct": gr3d_peak, "cuda_library_mapped": cuda_mapped,
            "raw_samples": dict(sorted(counts.items()))}


def _instance_facts(instance: InstanceIdentity | Mapping[str, Any] | None) -> dict | None:
    if instance is None:
        return None
    if is_dataclass(instance):
        return asdict(instance)
    if isinstance(instance, Mapping):
        return dict(instance)
    raise CollectorError("instance attribution must be an InstanceIdentity or a mapping")


def _sha256(value: str, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise CollectorError(f"{label} must be a lowercase 64-hex SHA-256")
    return value


class FileCollector:
    """Persist one run's events, samples and failures under `directory`."""

    def __init__(self, directory: Path, *, run_id: str, candidate_sha256: str, device_digest: str,
                 boot_id: str, clock: Clock | None = None) -> None:
        if not isinstance(run_id, str) or not run_id:
            raise CollectorError("run_id is required")
        if not isinstance(boot_id, str) or not boot_id:
            raise CollectorError("boot_id is required")
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / "samples").mkdir(exist_ok=True)
        self.run_id = run_id
        self.boot_id = boot_id
        self.candidate_sha256 = _sha256(candidate_sha256, "candidate_sha256")
        self.device_digest = _sha256(device_digest, "device_digest")
        self._clock: Clock = clock if clock is not None else _SystemClock()
        self._context: dict | None = None
        self._last_sequence: int | None = None
        self._sample_counts: dict[str, int] = {}

    # -- case context ------------------------------------------------------

    def begin_case(self, case_id: str, *, attempt: int, instance: InstanceIdentity | Mapping[str, Any] | None = None) -> None:
        if not isinstance(case_id, str) or not case_id:
            raise CollectorError("case_id is required")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise CollectorError("attempt must be a positive integer")
        self._context = {"case_id": case_id, "attempt": attempt, "instance": _instance_facts(instance)}

    def end_case(self, *, status: str) -> None:
        if self._context is None:
            raise CollectorError("no case context to end")
        row = {**self._run_stamp(), **self._context, "status": status}
        self._append("cases.jsonl", row)
        self._context = None

    # -- the EventSink surface --------------------------------------------

    def emit(self, event: EventRecord) -> None:
        if not isinstance(event, EventRecord):
            raise CollectorError("only a validated EventRecord can be persisted")
        if self._context is None:
            raise CollectorError("no case context: an event without case attribution is refused")
        if self._last_sequence is not None and event.sequence <= self._last_sequence:
            raise CollectorError(f"event sequence must be strictly increasing: {event.sequence} after {self._last_sequence}")
        self._last_sequence = event.sequence
        row = {**self._run_stamp(), **self._context, "event": self._event_body(event)}
        self._append("events.jsonl", row)

    def _event_body(self, event: EventRecord) -> dict:
        return {
            "schema_version": event.schema_version,
            "event_id": event.event_id,
            "sequence": event.sequence,
            "utc_time": event.utc_time.isoformat(),
            "monotonic_time": event.monotonic_time,
            "fence": asdict(event.fence),
            "type": event.type,
            "payload": dict(event.payload),
        }

    # -- raw material ------------------------------------------------------

    def record_sample(self, kind: str, raw: Any, *, note: str | None = None) -> None:
        """One raw sample, kept as read (text or JSON-shaped figures)."""
        if not isinstance(kind, str) or not re.fullmatch(r"[a-z0-9_]+", kind):
            raise CollectorError(f"sample kind must be a lowercase identifier, got {kind!r}")
        row = {**self._stamp(), "run_id": self.run_id, "kind": kind, "note": note, "raw": raw}
        self._append(Path("samples") / f"{kind}.jsonl", row)
        self._sample_counts[kind] = self._sample_counts.get(kind, 0) + 1

    def record_failure(self, *, stage: str, error: str, detail: Mapping[str, Any] | None = None) -> None:
        if not isinstance(stage, str) or not stage:
            raise CollectorError("a failure stage is required")
        row = {**self._stamp(), "run_id": self.run_id, "stage": stage, "error": str(error),
               "detail": dict(detail) if detail is not None else None,
               "case_id": (self._context or {}).get("case_id"), "attempt": (self._context or {}).get("attempt"),
               "instance": (self._context or {}).get("instance")}
        self._append("failures.jsonl", row)

    def attribution(self) -> dict:
        """Device activity derived from raw samples; a boolean is never the evidence."""
        if not self._sample_counts:
            raise CollectorError("device attribution needs at least one raw sample: a boolean claim is not evidence")
        return derive_attribution(self._all_samples())

    def _all_samples(self) -> list[dict]:
        rows: list[dict] = []
        for path in sorted((self.directory / "samples").glob("*.jsonl")):
            rows.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
        return rows

    def _read_samples(self, kind: str) -> list[dict]:
        path = self.directory / "samples" / f"{kind}.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    # -- material accounting ----------------------------------------------

    def manifest(self) -> dict:
        files = []
        for path in sorted(candidate for candidate in self.directory.rglob("*") if candidate.is_file()):
            if path.name == "manifest.json":
                continue  # a manifest cannot hash itself
            files.append({"relative_path": str(path.relative_to(self.directory)),
                          "size_bytes": path.stat().st_size, "sha256": _sha256_file(path)})
        return {"collector_version": COLLECTOR_VERSION, "collector_sha256": collector_sha256(),
                "run_id": self.run_id, "boot_id": self.boot_id, "candidate_sha256": self.candidate_sha256,
                "device_digest": self.device_digest, "files": files}

    def close(self) -> dict:
        manifest = self.manifest()
        (self.directory / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                                                      encoding="utf-8")
        return manifest

    # -- internals ---------------------------------------------------------

    def _stamp(self) -> dict:
        return {"persisted_monotonic": self._clock.monotonic(),
                "persisted_utc": self._clock.utc_now().isoformat()}

    def _run_stamp(self) -> dict:
        return {**self._stamp(), "run_id": self.run_id, "boot_id": self.boot_id,
                "candidate_sha256": self.candidate_sha256, "device_digest": self.device_digest}

    def _append(self, relative: Path | str, row: Mapping[str, Any]) -> None:
        path = self.directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
            handle.flush()
