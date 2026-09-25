"""CT10: the `lab-chat-report-v1` writer and its structural checks (acceptance §7).

The report is material, not a verdict: it records which attempt produced which files,
and `final` may only cite an attempt that exists. A stored `summary`, a
`production_ready`/`device_backend_ready` claim, an unknown field, a final that points
at a missing attempt, a symlinked or rewritten artifact and a second write into the same
output directory are all refused. Semantic verdicts are recomputed by `verify` from the
raw material; nothing here trusts a status.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

REPORT_FILE = "lab-chat-report.json"
SCHEMA = "lab-chat-report-v1"
SUITES = ("candidate", "gateway", "rollback")
STATUSES = ("passed", "failed", "not_run")
#: Fields the plan forbids: a report never carries a stored verdict or a release claim.
FORBIDDEN_FIELDS = ("summary", "production_ready", "device_backend_ready")
FILE_LIST_FIELDS = ("request_files", "response_files", "observation_files", "cleanup_files")

TOP_KEYS = frozenset({"schema", "suite", "site_sha256", "code_sha", "policy_source_sha256",
                      "fixture_set_sha256", "candidate_report_sha256", "started_at_utc", "ended_at_utc",
                      "attempts", "final", "artifacts"})
ATTEMPT_KEYS = frozenset({"case_id", "variant", "attempt", "status", "reason", "request_files", "response_files",
                          "observation_files", "cleanup_files", "start", "end"})
FINAL_KEYS = frozenset({"case_id", "variant", "attempt"})
ARTIFACT_KEYS = frozenset({"path", "bytes", "sha256"})

_HEX64 = re.compile(r"[0-9a-f]{64}")
_HEX40 = re.compile(r"[0-9a-f]{40}")


class ReportError(RuntimeError):
    """The report or its material cannot be trusted; it is refused, never summarised."""


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def collect_artifacts(root: str | Path) -> list[dict]:
    """Every material file of the output directory, sorted, hashed, and never a symlink."""
    base = Path(root)
    entries: list[dict] = []
    for path in sorted(candidate for candidate in base.rglob("*") if candidate.is_symlink() or candidate.is_file()):
        if path.name == REPORT_FILE:
            continue
        if path.is_symlink():
            raise ReportError(f"{path}: a material symlink is refused")
        entries.append({"path": path.relative_to(base).as_posix(), "bytes": path.stat().st_size,
                        "sha256": sha256_file(path)})
    return entries


def write_report(directory: str | Path, *, suite: str, site_sha256: str, code_sha: str,
                 policy_source_sha256: str, fixture_set_sha256: str, candidate_report_sha256: str | None,
                 started_at_utc: str, ended_at_utc: str, attempts: Sequence[Mapping[str, Any]],
                 final: Sequence[Mapping[str, Any]]) -> Path:
    """Write the one report of this output directory; existing material is never overwritten."""
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    path = root / REPORT_FILE
    if path.exists():
        raise ReportError(f"the report already exists: {path}")
    document = {
        "schema": SCHEMA,
        "suite": suite,
        "site_sha256": site_sha256,
        "code_sha": code_sha,
        "policy_source_sha256": policy_source_sha256,
        "fixture_set_sha256": fixture_set_sha256,
        "candidate_report_sha256": candidate_report_sha256,
        "started_at_utc": started_at_utc,
        "ended_at_utc": ended_at_utc,
        "attempts": [dict(row) for row in attempts],
        "final": [dict(row) for row in final],
        "artifacts": collect_artifacts(root),
    }
    path.write_bytes((json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"))
    return path


def load_report(root: str | Path) -> dict:
    path = Path(root) / REPORT_FILE
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReportError(f"cannot read {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ReportError(f"{path}: the report must be a JSON object")
    return document


def report_problems(document: Mapping[str, Any], *, required: Iterable[tuple[str, str]],
                    root: str | Path | None = None) -> list[str]:
    """The reasons this report is not the frozen schema; empty means structurally sound."""
    problems: list[str] = []
    if not isinstance(document, Mapping):
        return ["the report is not an object"]
    for forbidden in FORBIDDEN_FIELDS:
        if forbidden in document:
            problems.append(f"{forbidden}: a lab chat report carries no stored verdict or release claim")
    unknown = sorted(set(document) - TOP_KEYS)
    if unknown:
        problems.append(f"unknown fields: {', '.join(unknown)}")
    missing = sorted(TOP_KEYS - set(document))
    if missing:
        problems.append(f"missing fields: {', '.join(missing)}")
        return problems
    base = Path(root) if root is not None else None

    if document["schema"] != SCHEMA:
        problems.append(f"schema: {document['schema']!r} is not {SCHEMA!r}")
    if document["suite"] not in SUITES:
        problems.append(f"suite: {document['suite']!r} is not one of {', '.join(SUITES)}")
    for label in ("site_sha256", "policy_source_sha256", "fixture_set_sha256"):
        if not _hex(document[label], bits=64):
            problems.append(f"{label}: must be a lowercase 64-hex digest")
    if not _hex(document["code_sha"], bits=40):
        problems.append("code_sha: must be a lowercase 40-hex commit")
    candidate_report = document["candidate_report_sha256"]
    if candidate_report is not None and not _hex(candidate_report, bits=64):
        problems.append("candidate_report_sha256: must be null or a lowercase 64-hex digest")
    if document["suite"] == "candidate" and candidate_report is not None:
        problems.append("candidate_report_sha256: the candidate suite is the one that is bound, not binding")
    for label in ("started_at_utc", "ended_at_utc"):
        if not _instant(document[label]):
            problems.append(f"{label}: must be an ISO-8601 UTC timestamp with its offset")

    attempts = document["attempts"]
    if not isinstance(attempts, list) or not attempts:
        problems.append("attempts: a run records the attempts it made")
        attempts = []
    known: set[tuple[str, str, int]] = set()
    for index, row in enumerate(attempts, start=1):
        where = f"attempts[{index}]"
        if not isinstance(row, Mapping):
            problems.append(f"{where}: must be an object")
            continue
        unknown = sorted(set(row) - ATTEMPT_KEYS)
        if unknown:
            problems.append(f"{where}: unknown fields: {', '.join(unknown)}")
        absent = sorted(ATTEMPT_KEYS - set(row))
        if absent:
            problems.append(f"{where}: missing fields: {', '.join(absent)}")
            continue
        key = (row["case_id"], row["variant"], row["attempt"])
        if not all(isinstance(part, str) and part for part in key[:2]):
            problems.append(f"{where}: case_id and variant must be non-empty strings")
        if isinstance(row["attempt"], bool) or not isinstance(row["attempt"], int) or row["attempt"] < 1:
            problems.append(f"{where}: attempt must be a positive integer")
        elif key in known:
            problems.append(f"{where}: duplicate attempt {key[2]} for {key[0]}/{key[1]}")
        else:
            known.add(key)
        if row["status"] not in STATUSES:
            problems.append(f"{where}: status {row['status']!r} is not one of {', '.join(STATUSES)}")
        if row["reason"] is not None and not isinstance(row["reason"], str):
            problems.append(f"{where}: reason must be a string or null")
        if row["status"] == "failed" and not row["reason"]:
            problems.append(f"{where}: a failed attempt carries its reason")
        for label in ("start", "end"):
            if not _instant(row[label]):
                problems.append(f"{where}: {label} must be an ISO-8601 UTC timestamp with its offset")
        for field in FILE_LIST_FIELDS:
            for raw in _strings(row[field]):
                problems.extend(_relative_problems(raw, f"{where}.{field}", base))

    required_set = {(case_id, variant) for case_id, variant in required}
    final = document["final"]
    if not isinstance(final, list):
        problems.append("final: must be an array of case/variant/attempt rows")
        final = []
    cited: dict[tuple[str, str], int] = {}
    for index, row in enumerate(final, start=1):
        where = f"final[{index}]"
        if not isinstance(row, Mapping) or set(row) != FINAL_KEYS:
            problems.append(f"{where}: rows carry exactly case_id, variant and attempt")
            continue
        pair = (row["case_id"], row["variant"])
        if pair not in required_set:
            problems.append(f"{where}: {pair[0]}/{pair[1]} is not a required case of this suite")
            continue
        if pair in cited:
            problems.append(f"{where}: {pair[0]}/{pair[1]} is cited more than once")
            continue
        if (pair[0], pair[1], row["attempt"]) not in known:
            problems.append(f"{where}: {pair[0]}/{pair[1]} cites attempt {row['attempt']}, which does not exist")
        cited[pair] = row["attempt"]
    for pair in sorted(required_set - set(cited)):
        problems.append(f"final: the required case {pair[0]}/{pair[1]} has no citing attempt")

    artifacts = document["artifacts"]
    if not isinstance(artifacts, list):
        problems.append("artifacts: must be an array of path/bytes/sha256 rows")
        artifacts = []
    for index, row in enumerate(artifacts, start=1):
        where = f"artifacts[{index}]"
        if not isinstance(row, Mapping) or set(row) != ARTIFACT_KEYS:
            problems.append(f"{where}: rows carry exactly path, bytes and sha256")
            continue
        if isinstance(row["bytes"], bool) or not isinstance(row["bytes"], int) or row["bytes"] < 0:
            problems.append(f"{where}: bytes must be a non-negative integer")
        if not _hex(row["sha256"], bits=64):
            problems.append(f"{where}: sha256 must be a lowercase 64-hex digest")
        problems.extend(_relative_problems(row["path"], where, base))
        if base is not None:
            problems.extend(_bytes_problems(base / row["path"], row["bytes"], row["sha256"], where))
    return problems


def _hex(value: Any, *, bits: int) -> bool:
    pattern = _HEX64 if bits == 64 else _HEX40
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _instant(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _relative_problems(raw: Any, where: str, root: Path | None) -> list[str]:
    if not isinstance(raw, str) or not raw:
        return [f"{where}: a relative path is required"]
    parts = Path(raw).parts
    if Path(raw).is_absolute() or ".." in parts:
        return [f"{where}: {raw!r} escapes the evidence root"]
    if root is None:
        return []
    target = root / raw
    if target.is_symlink():
        return [f"{where}: {raw!r} is a symlink"]
    if not target.is_file():
        return [f"{where}: {raw!r} is missing"]
    return []


def _bytes_problems(target: Path, size: int, digest: Any, where: str) -> list[str]:
    if target.is_symlink() or not target.is_file():
        return []  # reported by _relative_problems
    problems: list[str] = []
    if target.stat().st_size != size:
        problems.append(f"{where}: {target.name} is {target.stat().st_size} bytes, not {size}")
    if isinstance(digest, str) and _HEX64.fullmatch(digest) and sha256_file(target) != digest:
        problems.append(f"{where}: {target.name} no longer matches the recorded digest")
    return problems
