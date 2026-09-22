"""P22: the pure source archive and the frozen candidate.

Two commands live here (plan/08-execution-plan.md §5):

* `source` — a deterministic archive of the allowlisted *tracked* source files
  (a clean commit's `app.py`, `run.py`, `model_scheduler/`, `scripts/`,
  `deploy/`, `tests/`, `pyproject.toml` and the requirement locks). Generated
  evidence, weights and credentials never travel; plan documents are not source
  identity, so editing them cannot change the archive hash. Metadata is fixed
  (prefix `source/`, sorted members, uid/gid/mtime 0, gzip mtime 0) and the
  archive never embeds its own digest or a commit time.
* `candidate` — freezes device facts, measurements, policy, fixtures and the
  source archive into `CandidateV3`. Every value is re-derived from the
  supplied material: facts are re-parsed, model assets are re-hashed on disk,
  fixture files and the evaluator are re-hashed, the measurement manifest must
  equal the registration's `measurement_ref`, and a mixed model set, an
  unmeasured model, an uncovered capability or a blocked measurement all stop
  the build. The body never contains its own digest (`candidate_sha256` is
  written back into the config, and `config_digest` excludes it), so the hash
  chain cannot cycle.
"""
from __future__ import annotations

from dataclasses import asdict
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import tarfile
from typing import Any, Mapping

from .. import config as config_module
from .. import evidence_contracts as ec
from ..config import AppConfigV2, load_config, ConfigError
from ..contracts_v2 import ContractError, require_production_openable
from ..evidence_contracts import ArtifactRef, artifact_manifest_digest, candidate_digest, parse_candidate, parse_policy
from .collector import COLLECTOR_VERSION

SOURCE_PREFIX = "source"
SOURCE_ALLOWLIST = ("app.py", "run.py", "model_scheduler", "scripts", "deploy", "tests",
                    "pyproject.toml", "requirements.in", "requirements-dev.in",
                    "requirements.lock", "requirements-dev.lock")
SOURCE_EXCLUDED_SUFFIXES = (".gguf", ".safetensors", ".bin", ".onnx", ".pt", ".pem", ".key", ".crt", ".pyc")
SOURCE_EXCLUDED_NAMES = (".env", ".netrc", "credentials.json", "id_rsa")
COLLECTOR_MEMBER = f"{SOURCE_PREFIX}/model_scheduler/acceptance/collector.py"
FIXTURE_KEYS = frozenset({"schema_version", "evaluator", "fixtures"})
FIXTURE_ENTRY_KEYS = frozenset({"fixture_id", "capabilities", "artifact"})
_DEPLOYMENT_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


class CandidateError(RuntimeError):
    """A candidate refusal; `input_error` picks CLI exit 2 over exit 3."""

    def __init__(self, message: str, *, input_error: bool = True) -> None:
        super().__init__(message)
        self.input_error = input_error


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git(root: Path, args: list[str]) -> str:
    try:
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise CandidateError(f"git is required for the source archive: {exc}") from exc
    if result.returncode != 0:
        raise CandidateError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _dirty_paths(porcelain_z: str) -> list[str]:
    entries = [entry for entry in porcelain_z.split("\0") if entry]
    paths: list[str] = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        status, path = entry[:2], entry[3:]
        paths.append(path)
        index += 2 if ("R" in status or "C" in status) else 1
    return paths


def _excluded(relative: str) -> bool:
    path = Path(relative)
    return path.name in SOURCE_EXCLUDED_NAMES or path.suffix in SOURCE_EXCLUDED_SUFFIXES or "__pycache__" in path.parts


def build_source_archive(*, root: Path, output: Path) -> dict:
    """Deterministic archive of the allowlisted tracked source; refuses a dirty tree."""
    root = Path(root)
    dirty = _dirty_paths(_git(root, ["status", "--porcelain", "-z", "--", *SOURCE_ALLOWLIST]))
    if dirty:
        raise CandidateError(f"a dirty allowlisted file cannot be archived: {', '.join(sorted(dirty)[:5])}")

    tracked = [entry for entry in _git(root, ["ls-files", "-z", "--", *SOURCE_ALLOWLIST]).split("\0") if entry]
    members: list[tuple[str, Path]] = []
    excluded: list[str] = []
    for relative in sorted(tracked):
        path = root / relative
        if not path.is_file():
            continue
        if _excluded(relative):
            excluded.append(relative)
            continue
        members.append((f"{SOURCE_PREFIX}/{relative}", path))
    if not members:
        raise CandidateError("the allowlist matched no tracked source file")

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for name, path in sorted(members):
                    payload = path.read_bytes()
                    info = tarfile.TarInfo(name)
                    info.size = len(payload)
                    info.mode = 0o644
                    info.mtime = 0
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    archive.addfile(info, io.BytesIO(payload))
    return {"output": str(output), "sha256": _sha256_file(output), "members": [name for name, _ in sorted(members)],
            "excluded": excluded}


def member_sha256(archive: Path, member: str) -> str:
    """Hash one archive member; a missing member is an input error, never ignored."""
    try:
        with tarfile.open(archive, "r:gz") as handle:
            try:
                found = handle.extractfile(member)
            except KeyError as exc:
                raise CandidateError(
                    f"{archive}: member {member!r} is absent (the collector must be archived)") from exc
            if found is None:
                raise CandidateError(f"{archive}: member {member!r} is absent (the collector must be archived)")
            return hashlib.sha256(found.read()).hexdigest()
    except (OSError, tarfile.TarError) as exc:
        raise CandidateError(f"cannot read {archive}: {exc}") from exc


def _read_json(path: Path, label: str) -> Mapping[str, Any]:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CandidateError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise CandidateError(f"{label} {path} must be a JSON object")
    return document


def _artifact(document: Any, *, where: str, base: Path) -> tuple[ArtifactRef, Path]:
    try:
        reference = ec.parse_artifact_ref(document, where)
    except ContractError as exc:
        raise CandidateError(f"{where}: {exc}") from exc
    path = base / reference.relative_path
    if not path.is_file():
        raise CandidateError(f"{where}: {reference.relative_path} is not present under {base}")
    if path.stat().st_size != reference.size_bytes:
        raise CandidateError(f"{where}: {reference.relative_path} size differs from its declared size_bytes")
    if _sha256_file(path) != reference.sha256:
        raise CandidateError(f"{where}: {reference.relative_path} hash differs from its declared sha256")
    return reference, path


def _material(directory: Path) -> list[ArtifactRef]:
    return [ArtifactRef(relative_path=str(path.relative_to(directory)), size_bytes=path.stat().st_size,
                        sha256=_sha256_file(path))
            for path in sorted(candidate for candidate in directory.rglob("*") if candidate.is_file())]


def _measurement_summary(directory: Path) -> Mapping[str, Any]:
    document = _read_json(directory / "measurements.json", "measurement material")
    summary = document.get("summary")
    if not isinstance(summary, dict):
        raise CandidateError(f"{directory}/measurements.json: a summary is required")
    if summary.get("verdict") != "passed" or summary.get("physical_bound_proven") is not True:
        raise CandidateError("the measurement does not prove a physical bound: production candidates need a passed "
                             "calibration with a proven bound (C02)")
    bound = summary.get("physical_resident_peak_bytes")
    if isinstance(bound, bool) or not isinstance(bound, int) or bound <= 0:
        raise CandidateError("the measurement does not prove a physical bound: it is null or non-positive")
    return summary


def build_candidate(*, config_path: Path, facts_path: Path, measurements_dir: Path, policy_path: Path,
                    fixtures_path: Path, source_path: Path, deployment_id: str, output: Path) -> dict:
    """Assemble the frozen candidate body and return its recomputable digest.

    The single-directory `measurements_dir` form serves exactly one registered
    model (K6/RP10). A mixed or incomplete set is refused right after the
    configuration parses — before any expensive material is read and before
    anything is written — so it can neither consume inputs nor leave an output.
    """
    if not isinstance(deployment_id, str) or not _DEPLOYMENT_ID.fullmatch(deployment_id):
        raise CandidateError("--deployment-id must match [a-z0-9][a-z0-9-]{0,63}")

    try:
        raw_config = config_module._yaml(Path(config_path))  # the same loader the deployment uses
        config = load_config(config_path)
    except ConfigError as exc:
        raise CandidateError(f"cannot load {config_path}: {exc}") from exc
    if not isinstance(config, AppConfigV2):
        raise CandidateError("the candidate requires a schema v2 configuration")
    config_sha = config_module.config_digest(raw_config)

    if len(config.models) != 1:
        raise CandidateError(f"--measurements DIR supports exactly one registered model, but this configuration "
                             f"registers {len(config.models)}: a mixed set needs per-model measurement material")
    runtimes_by_id = {runtime.runtime_id: runtime for runtime in config.runtimes.values()}
    for registered in config.models.values():
        try:
            require_production_openable(registered, runtimes_by_id[registered.runtime_id])
        except ContractError as exc:
            raise CandidateError(str(exc)) from exc

    facts = _read_json(facts_path, "facts")
    try:
        device = ec.parse_device_fact(facts.get("device"))
        runtime_stack = ec.parse_runtime_stack(facts.get("runtime_stack"))
    except ContractError as exc:
        raise CandidateError(f"{facts_path}: {exc}") from exc

    source_sha = _sha256_file(Path(source_path))
    try:
        collector_sha = member_sha256(Path(source_path), COLLECTOR_MEMBER)
    except CandidateError as exc:
        raise CandidateError(f"the source archive does not pin the collector: {exc}") from exc

    measured = [model for model in config.models.values() if model.measured]
    if not measured:
        raise CandidateError("a model that is not measured cannot enter a production candidate: "
                             "no model declares measured=true")
    summary = _measurement_summary(Path(measurements_dir))
    named = summary.get("model_id")
    model = measured[0]
    if named is not None and named != model.model_id:
        raise CandidateError(f"the measurement measured {named!r} but {model.model_id!r} claims measured=true")

    measurement_refs = _material(Path(measurements_dir))
    manifest_digest = artifact_manifest_digest(measurement_refs)
    if model.measurement_ref != manifest_digest:
        raise CandidateError(f"model {model.model_id!r}: measurement_ref does not match the supplied measurement "
                              f"material ({manifest_digest})")
    if model.physical_resident_peak_bytes != summary["physical_resident_peak_bytes"]:
        raise CandidateError(f"model {model.model_id!r}: physical_resident_peak_bytes does not match the measured "
                             "physical upper bound")

    model_directory = Path(config.storage.model_directory)
    for registered in config.models.values():
        for asset in registered.assets:
            path = model_directory / asset.path
            if not path.is_file():
                raise CandidateError(f"model {registered.model_id!r}: asset {asset.path!r} is not present under "
                                     f"{model_directory}")
            if path.stat().st_size != asset.size_bytes or _sha256_file(path) != asset.sha256:
                raise CandidateError(f"model {registered.model_id!r}: asset {asset.path!r} size/hash differs from the "
                                     "registration")

    fixtures = _read_json(fixtures_path, "fixtures")
    if set(fixtures) != FIXTURE_KEYS:
        raise CandidateError(f"{fixtures_path}: keys must be exactly {sorted(FIXTURE_KEYS)}")
    fixtures_base = Path(fixtures_path).parent
    evaluator = fixtures.get("evaluator")
    if not isinstance(evaluator, dict):
        raise CandidateError(f"{fixtures_path}: the evaluator material is required (its hash is frozen into the "
                             "candidate)")
    evaluator_ref, _path = _artifact(evaluator, where="fixtures.evaluator", base=fixtures_base)

    fixture_refs: list[ArtifactRef] = []
    covered: set[str] = set()
    for index, entry in enumerate(fixtures.get("fixtures") or []):
        where = f"fixtures.fixtures[{index}]"
        if not isinstance(entry, dict) or set(entry) != FIXTURE_ENTRY_KEYS:
            raise CandidateError(f"{where}: keys must be exactly {sorted(FIXTURE_ENTRY_KEYS)}")
        capabilities = entry.get("capabilities")
        if not isinstance(capabilities, list) or not capabilities or not all(isinstance(item, str) for item in capabilities):
            raise CandidateError(f"{where}: capabilities must be a non-empty list of strings")
        reference, _file = _artifact(entry.get("artifact"), where=f"{where}.artifact", base=fixtures_base)
        fixture_refs.append(reference)
        covered.update(capabilities)
    if not fixture_refs:
        raise CandidateError(f"{fixtures_path}: at least one fixture is required")
    for registered in config.models.values():
        missing = sorted(set(registered.capabilities) - covered)
        if missing:
            raise CandidateError(f"model {registered.model_id!r}: no fixture covers {', '.join(missing)}")

    try:
        policy = parse_policy(_read_json(policy_path, "policy"), "policy")
    except ContractError as exc:
        raise CandidateError(f"{policy_path}: {exc}") from exc

    body = {
        "schema_version": ec.EVIDENCE_SCHEMA_VERSION,
        "deployment_id": deployment_id,
        "source_archive_sha256": source_sha,
        "config_sha256": config_sha,
        "device": asdict(device),
        "runtime_stack": asdict(runtime_stack),
        "runtimes": [asdict(runtime) for runtime in config.runtimes.values()],
        "models": [asdict(registered) for registered in config.models.values()],
        "measurement_refs": [asdict(reference) for reference in measurement_refs],
        "fixture_refs": [asdict(reference) for reference in sorted(fixture_refs, key=lambda item: item.relative_path)],
        "policy": asdict(policy),
        "collector_sha256": collector_sha,
        "evaluator_sha256": evaluator_ref.sha256,
    }
    body = json.loads(json.dumps(body))  # the written artifact is the parsed artifact: lists stay lists
    try:
        candidate = parse_candidate(body)
    except ContractError as exc:
        raise CandidateError(f"the assembled candidate is not valid: {exc}") from exc

    digest = candidate_digest(candidate)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"candidate_sha256": digest, "config_sha256": config_sha, "source_archive_sha256": source_sha,
            "collector_sha256": collector_sha, "collector_version": COLLECTOR_VERSION,
            "measurement_refs": len(measurement_refs), "fixture_refs": len(fixture_refs), "output": str(output)}
