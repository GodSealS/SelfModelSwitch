"""The per-case material shape the evaluator and merge re-read.

`evaluator.load_case_material` reads `<case>/case.json`, its raw samples come
from `<case>/samples/<kind>.jsonl`, and `verify.merge_runs` takes the parent of
the first event reference as the material root. A run therefore has to leave
exactly one directory per attempt, and this module is the only writer of that
shape:

    <root>/cases/<case_id>/attempt-<n>/case.json
    <root>/cases/<case_id>/attempt-<n>/samples/<kind>.jsonl

`case.json` carries the case's own facts at the top level (that is where the
evaluator reads them: `provider`, `instance`, `observed`, `output`, `fixture`,
`observations`, `numbers`, `failure`) plus the bookkeeping a reader needs —
status, attempt, problems. The stored status is never an input to a verdict;
it is written so the material can be read by a human.

The same writer serves the B layer (`backend_cases.CaseExecutor` calls
`begin_case` / `record_sample` / `record_failure` / `end_case`) and the S layer
(each software case writes its own observations and numbers).
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any, Mapping, Protocol, Sequence

from ..control_protocol_v1 import InstanceIdentity
from ..evidence_contracts import ArtifactRef

CASE_ROOT = "cases"
CASE_FILE = "case.json"
SAMPLES_DIRECTORY = "samples"
FAILURES_FILE = "failures.jsonl"
MANIFEST_FILE = "manifest.json"
_SLUG = re.compile(r"[a-z0-9_-]+")


class MaterialError(RuntimeError):
    """The store refuses material it cannot place or re-read."""


class CaseMaterialSink(Protocol):
    """What `CaseExecutor` uses: the store, not a file format, is the interface."""

    def begin_case(self, case_id: str, *, attempt: int,
                   instance: InstanceIdentity | Mapping[str, Any] | None = None) -> Any: ...

    def end_case(self, *, status: str, facts: Mapping[str, Any] | None = None,
                 failure: str | None = None, problems: Sequence[str] = ()) -> Any: ...

    def record_sample(self, kind: str, raw: Any, *, note: str | None = None) -> None: ...

    def record_failure(self, *, stage: str, error: str,
                       detail: Mapping[str, Any] | None = None) -> None: ...


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_sha256(value: str, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise MaterialError(f"{label} must be a lowercase 64-hex SHA-256")
    return value


def _slug(case_id: str) -> str:
    """`B:qwen-small:cap:vision` becomes a directory name, exactly one way."""
    slug = str(case_id).replace(":", "_").lower()
    if not _SLUG.fullmatch(slug):
        raise MaterialError(f"case id {case_id!r} cannot name a material directory")
    return slug


def _jsonable(value: Any) -> Any:
    """Facts are persisted as JSON; anything JSON cannot hold is refused loudly."""
    try:
        return json.loads(json.dumps(value, default=str, sort_keys=True))
    except (TypeError, ValueError) as exc:
        raise MaterialError(f"case facts must be JSON-shaped: {exc}") from exc


class _SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def utc_now(self) -> datetime:
        return datetime.now(timezone.utc)


class CaseMaterialStore:
    """Write one directory per case attempt, in the shape recomputation expects."""

    def __init__(self, root: Path, *, run_id: str, candidate_sha256: str, device_digest: str,
                 clock: Any = None) -> None:
        if not isinstance(run_id, str) or not run_id:
            raise MaterialError("run_id is required")
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.candidate_sha256 = _require_sha256(candidate_sha256, "candidate_sha256")
        self.device_digest = _require_sha256(device_digest, "device_digest")
        self._clock = clock if clock is not None else _SystemClock()
        self._context: tuple[str, int, Path] | None = None

    # -- placement ---------------------------------------------------------

    def case_directory(self, case_id: str, attempt: int) -> Path:
        """Where one attempt's material lives (created by `begin_case`)."""
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise MaterialError("attempt must be a positive integer")
        return self.root / CASE_ROOT / _slug(case_id) / f"attempt-{attempt}"

    def begin_case(self, case_id: str, *, attempt: int,
                   instance: InstanceIdentity | Mapping[str, Any] | None = None) -> Path:
        directory = self.case_directory(case_id, attempt)
        (directory / SAMPLES_DIRECTORY).mkdir(parents=True, exist_ok=True)
        self._context = (str(case_id), attempt, directory)
        return directory

    def end_case(self, *, status: str, facts: Mapping[str, Any] | None = None,
                 failure: str | None = None, problems: Sequence[str] = ()) -> Path:
        """Close the open case; the facts land at the top level of `case.json`."""
        case_id, attempt, directory = self._open_case()
        document: dict[str, Any] = dict(_jsonable(dict(facts or {})))
        document["case_id"] = case_id
        document["attempt"] = attempt
        document["status"] = str(status)
        document["failure"] = None if failure is None else str(failure)
        document["problems"] = [str(item) for item in problems]
        self._write_json(directory / CASE_FILE, document)
        self._context = None
        return directory

    # -- raw material ------------------------------------------------------

    def record_sample(self, kind: str, raw: Any, *, note: str | None = None) -> None:
        """One raw row, kept as read (the evaluator recomputes from it)."""
        if not isinstance(kind, str) or not re.fullmatch(r"[a-z0-9_]+", kind):
            raise MaterialError(f"sample kind must be a lowercase identifier, got {kind!r}")
        _, _, directory = self._open_case()
        row = {**self._stamp(), "kind": kind, "note": note, "raw": raw}
        self._append(directory / SAMPLES_DIRECTORY / f"{kind}.jsonl", row)

    def record_failure(self, *, stage: str, error: str, detail: Mapping[str, Any] | None = None) -> None:
        """A failure is itself material; it is kept, never replaced."""
        if not isinstance(stage, str) or not stage:
            raise MaterialError("a failure stage is required")
        case_id, attempt, directory = self._open_case()
        row = {**self._stamp(), "case_id": case_id, "attempt": attempt, "stage": stage, "error": str(error),
               "detail": _jsonable(dict(detail)) if detail is not None else None}
        self._append(directory / FAILURES_FILE, row)

    # -- reading -----------------------------------------------------------

    def refs(self, case_id: str, attempt: int) -> tuple[ArtifactRef, ...]:
        """This attempt's material, `case.json` first.

        `merge_runs` takes the parent of the first reference as the material
        root, so the order is part of the contract and not an accident of
        sorting.
        """
        directory = self.case_directory(case_id, attempt)
        if not directory.is_dir():
            raise MaterialError(f"case {case_id!r} attempt {attempt} has no material directory")
        files = sorted(path for path in directory.rglob("*") if path.is_file())
        if not files:
            raise MaterialError(f"case {case_id!r} attempt {attempt} left no material")
        references = [ArtifactRef(relative_path=str(path.relative_to(self.root)),
                                  size_bytes=path.stat().st_size, sha256=_sha256_file(path)) for path in files]
        references.sort(key=lambda ref: (not ref.relative_path.endswith(f"/{CASE_FILE}"), ref.relative_path))
        return tuple(references)

    def manifest(self) -> dict:
        files = []
        for path in sorted(candidate for candidate in self.root.rglob("*") if candidate.is_file()):
            if path.name == MANIFEST_FILE:
                continue  # a manifest cannot hash itself
            files.append({"relative_path": str(path.relative_to(self.root)),
                          "size_bytes": path.stat().st_size, "sha256": _sha256_file(path)})
        return {"run_id": self.run_id, "candidate_sha256": self.candidate_sha256,
                "device_digest": self.device_digest, "files": files}

    def close(self) -> dict:
        manifest = self.manifest()
        (self.root / MANIFEST_FILE).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                                               encoding="utf-8")
        return manifest

    # -- internals ---------------------------------------------------------

    def _open_case(self) -> tuple[str, int, Path]:
        if self._context is None:
            raise MaterialError("no case is open: begin_case first")
        return self._context

    def _stamp(self) -> dict:
        return {"persisted_monotonic": self._clock.monotonic(),
                "persisted_utc": self._clock.utc_now().isoformat()}

    def _write_json(self, path: Path, document: Mapping[str, Any]) -> None:
        path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def _append(self, path: Path, row: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
            handle.flush()


def instance_facts(instance: InstanceIdentity | Mapping[str, Any] | None) -> dict | None:
    """The instance attribution a case records; a dataclass or a mapping, never a guess."""
    if instance is None:
        return None
    if is_dataclass(instance):
        return asdict(instance)
    if isinstance(instance, Mapping):
        return dict(instance)
    raise MaterialError("instance attribution must be an InstanceIdentity or a mapping")
