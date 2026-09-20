"""P29: the S layer — every observation is the result of a real call.

The material contract is fixed by `evaluator.py`: each case leaves a
`case.json` whose `observations` must be exactly the interface's keys, each one
`true` only when the check really passed (`S03` also leaves the raw `numbers`
the boundary arithmetic is recomputed from). This module produces that material
by actually running the checks:

* S01 — the real registration parser over the candidate's own registration,
  strict-schema counter-examples that must all be refused, the real
  `migrate_v2` over an explicit v1 fixture and inventory (checked with
  `run.py --check-config`), and an AST scan of the service imports;
* S02 — fake ports driving the real `ModelScheduler`/`Book` for a failed load
  and a stop that returned while the instance stayed alive, plus the book's
  stale-operation rule and the C03 write-back fence rule;
* S03 — the `Book`'s own boundary arithmetic: equality admissible, one byte
  under refused, sample freshness at two seconds, and a READY instance that is
  not reserved twice;
* S04 — the real `RequestQueue` ordering, `SessionManager` soft/hard deadlines,
  the `Book`'s freeze/cancel accounting, and the `IdempotencyStore`;
* S05 — the compat request contract plus the real `BlobStore` over scratch
  directories (owner, hash, quota, expiry, restart recovery, late output);
* S06 — the real parsers, the offline verifier and the deploy preflight
  refusing tampered/forged material.

A check whose **explicit input is missing** (S01's inventory, S06's candidate
document) is `False`; nothing is ever filled in to make a case pass.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import asyncio
import hashlib
import json
from pathlib import Path
import time
from typing import Any, Callable, Mapping, Sequence
from uuid import uuid4

from .evaluator import S_CASE_OBSERVATIONS

SOFTWARE_CASES: tuple[str, ...] = tuple(sorted(S_CASE_OBSERVATIONS))
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LEGACY_CONFIG = REPO_ROOT / "config.yaml"


class SoftwareCaseError(RuntimeError):
    """A refusal that is not a case failure (a programming error, an unknown case)."""


@dataclass(frozen=True)
class SoftwareResult:
    """One S case attempt: the observations plus the material they produced."""

    case_id: str
    attempt: int
    status: str
    started_utc: str
    ended_utc: str
    duration_seconds: float
    facts: Mapping[str, Any] = field(default_factory=dict)
    problems: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in ("passed", "failed"):
            raise SoftwareCaseError(f"unknown attempt status {self.status!r}")
        if self.status == "passed" and self.problems:
            raise SoftwareCaseError("a passed case cannot carry unsolved problems")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _required(case_id: str) -> tuple[str, ...]:
    try:
        return S_CASE_OBSERVATIONS[case_id]
    except KeyError as exc:
        raise SoftwareCaseError(f"no software case is defined for {case_id!r}") from exc


def run_software_case(case_id: str, *, candidate: Any, material_dir: Path,
                      candidate_path: Path | None = None, inventory: Path | None = None,
                      legacy_config: Path | None = None,
                      check_config: Callable[[Path], int] | None = None,
                      attempt: int = 1) -> SoftwareResult:
    """Run one S case; a crash fails the case, it never invents observations."""
    required = _required(case_id)
    material = Path(material_dir)
    material.mkdir(parents=True, exist_ok=True)
    started = _utc_now()
    began = time.monotonic()
    problems: list[str] = []
    facts: dict[str, Any] = {}
    try:
        observations, facts, problems = _CHECKS[case_id](
            candidate=candidate, material_dir=material, candidate_path=candidate_path,
            inventory=inventory, legacy_config=legacy_config, check_config=check_config)
    except Exception as exc:  # noqa: BLE001 - a crashed check is a failed case, never a passed one
        observations = {}
        problems = [f"the check crashed: {type(exc).__name__}: {exc}"]
    reported = {name: observations.get(name) for name in required}
    missing = [name for name in required if name not in observations]
    if missing:
        problems = [*problems, f"the check did not report the observations: {', '.join(missing)}"]
    unsatisfied = [name for name in required if reported.get(name) is not True]
    problems = [*problems, *(f"the observation {name!r} is not evidenced" for name in unsatisfied)]
    facts = {**facts, "observations": reported}
    status = "passed" if not problems else "failed"
    return SoftwareResult(case_id=case_id, attempt=attempt, status=status, started_utc=started,
                          ended_utc=_utc_now(), duration_seconds=time.monotonic() - began,
                          facts=facts, problems=tuple(problems))


# ---------------------------------------------------------------------------
# S01 — registration, strict schema, legacy migration, no business coupling


def _check_s01(*, candidate: Any, material_dir: Path, inventory: Path | None, legacy_config: Path | None,
               check_config: Callable[[Path], int] | None, **_ignored: Any) -> tuple[dict, dict, list[str]]:
    observations: dict[str, bool] = {}
    evidence: dict[str, Any] = {}
    problems: list[str] = []

    observations["runtime_registered"], registered_evidence, registered_problems = _s01_registration(candidate)
    evidence["registration"] = registered_evidence
    problems.extend(registered_problems)

    observations["strict_schema_enforced"], strict_evidence, strict_problems = _s01_strict_schema(candidate)
    evidence["strict_schema"] = strict_evidence
    problems.extend(strict_problems)

    observations["no_business_coupling"], coupling_evidence, coupling_problems = _s01_business_coupling()
    evidence["business_coupling"] = coupling_evidence
    problems.extend(coupling_problems)

    observations["legacy_config_migrated"], migration_evidence, migration_problems = _s01_migration(
        material_dir=material_dir, inventory=inventory, legacy_config=legacy_config, check_config=check_config)
    evidence["legacy_migration"] = migration_evidence
    problems.extend(migration_problems)

    return observations, {"evidence": evidence}, problems


def _s01_registration(candidate: Any) -> tuple[bool, dict, list[str]]:
    """The candidate's own runtimes must be startable and used by its models."""
    from ..contracts_v2 import ContractError, require_startable_profile

    profiles: dict[str, str] = {}
    problems: list[str] = []
    ok = True
    for runtime in candidate.runtimes:
        try:
            profile = require_startable_profile(runtime)
        except ContractError as exc:
            ok = False
            problems.append(f"runtime {runtime.runtime_id!r} cannot start: {exc}")
            continue
        profiles[runtime.runtime_id] = profile.profile_id
    known = set(profiles)
    for model in candidate.models:
        if model.runtime_id not in known:
            ok = False
            problems.append(f"model {model.model_id!r} names an unregistered runtime {model.runtime_id!r}")
    return ok, {"runtimes": {key: profiles[key] for key in sorted(profiles)},
                "models": sorted(model.model_id for model in candidate.models)}, problems


def _registration_document(candidate: Any) -> dict:
    """The candidate's registration as a schema-v2 deployment document."""
    from dataclasses import asdict

    return {
        "schema_version": 2,
        "runtimes": [
            {"runtime_id": runtime.runtime_id, "profile_id": runtime.profile_id,
             "image_digest": runtime.image_digest, "adapter_sha256": runtime.adapter_sha256,
             "lock_sha256": runtime.lock_sha256, "startup_args": list(runtime.startup_args)}
            for runtime in candidate.runtimes
        ],
        "models": [
            {"model_id": model.model_id, "runtime_id": model.runtime_id,
             "capabilities": list(model.capabilities),
             "assets": [{"role": asset.role, "path": asset.path, "sha256": asset.sha256,
                         "size_bytes": asset.size_bytes} for asset in model.assets],
             "port": model.port, "envelope": asdict(model.envelope),
             "timeout_seconds": model.timeout_seconds, "reserved_bytes": model.reserved_bytes,
             "measured": model.measured, "measurement_ref": model.measurement_ref,
             "physical_resident_peak_bytes": model.physical_resident_peak_bytes}
            for model in candidate.models
        ],
    }


def _s01_strict_schema(candidate: Any) -> tuple[bool, dict, list[str]]:
    """The candidate's registration parses; every strict-schema counter-example is refused."""
    from ..contracts_v2 import ContractError, parse_deployment, parse_json_document

    problems: list[str] = []
    base = _registration_document(candidate)
    try:
        parse_deployment(base)
        accepted = True
    except ContractError as exc:
        accepted = False
        problems.append(f"the candidate registration does not re-parse: {exc}")

    def _mutate(change) -> dict:
        document = json.loads(json.dumps(base))
        change(document)
        return document

    def _trailing_newline(document: dict) -> None:
        document["models"][0]["model_id"] = document["models"][0]["model_id"] + "\n"

    def _unknown_top(document: dict) -> None:
        document["candidate_sha256"] = "0" * 64

    def _unknown_model(document: dict) -> None:
        document["models"][0]["secret"] = "x"

    def _duplicate_path(document: dict) -> None:
        document["models"][0]["assets"][0]["path"] = "duplicated/asset.gguf"
        document["models"][0]["assets"].append(
            {"role": "adapter", "path": "duplicated/asset.gguf", "sha256": "1" * 64, "size_bytes": 1})

    counter_examples: dict[str, Callable[[], None]] = {
        "unknown_top_level_field": lambda: parse_deployment(_mutate(_unknown_top)),
        "unknown_model_field": lambda: parse_deployment(_mutate(_unknown_model)),
        "trailing_newline_model_id": lambda: parse_deployment(_mutate(_trailing_newline)),
        "duplicate_asset_path": lambda: parse_deployment(_mutate(_duplicate_path)),
        "non_finite_number": lambda: parse_json_document('{"value": 1e999}'),
    }
    refused: dict[str, bool] = {}
    for name, exercise in counter_examples.items():
        try:
            exercise()
        except ContractError:
            refused[name] = True
        else:
            refused[name] = False
            problems.append(f"the strict schema accepted {name!r}")
    return accepted and all(refused.values()), {"registration_parses": accepted, "refused": refused}, problems


_BANNED_IMPORT_SEGMENTS = frozenset({
    "video", "ffmpeg", "transcode", "media_pipeline", "voiceprint", "face_recognition",
    "insightface", "whisper", "pyannote", "librosa", "torchaudio", "cv2",
})


def _s01_business_coupling() -> tuple[bool, dict, list[str]]:
    """No service module may import a video/media/audio business dependency."""
    import ast

    files = sorted((REPO_ROOT / "model_scheduler").rglob("*.py"))
    for name in ("app.py", "run.py"):
        path = REPO_ROOT / name
        if path.is_file():
            files.append(path)
    violations: list[str] = []
    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            violations.append(f"{path.relative_to(REPO_ROOT)} cannot be parsed: {exc}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                continue
            for module in modules:
                segments = {segment.lower() for segment in str(module).split(".")}
                banned = sorted(segments & _BANNED_IMPORT_SEGMENTS)
                if banned:
                    violations.append(f"{path.relative_to(REPO_ROOT)} imports {module!r} ({banned[0]})")
    return not violations, {"files_scanned": len(files), "violations": violations}, \
        [f"the service imports business modules: {item}" for item in violations]


def _s01_migration(*, material_dir: Path, inventory: Path | None, legacy_config: Path | None,
                   check_config: Callable[[Path], int] | None) -> tuple[bool, dict, list[str]]:
    """A v1 fixture + the explicit inventory migrate, and the result passes check-config."""
    if inventory is None:
        return False, {"inventory": None}, ["S01 needs an explicit --inventory: the adapter/lock hashes are "
                                            "build facts the candidate does not carry, and they are never guessed"]
    source = Path(legacy_config) if legacy_config is not None else DEFAULT_LEGACY_CONFIG
    if not source.is_file():
        return False, {"inventory": str(inventory), "legacy_config": str(source)}, \
            [f"the v1 configuration {source} is not present: the migration cannot be exercised"]
    from ..migration_v2 import MigrationError, migrate_v2

    workdir = Path(material_dir) / "legacy-migration"
    workdir.mkdir(parents=True, exist_ok=True)
    fixture = workdir / "legacy-v1.yaml"
    fixture.write_bytes(source.read_bytes())
    recorded_inventory = workdir / "inventory.json"  # the material keeps every input it used
    recorded_inventory.write_bytes(Path(inventory).read_bytes())
    output = workdir / "scheduler-v2.yaml"
    try:
        document = migrate_v2(fixture, recorded_inventory, output)
    except MigrationError as exc:
        return False, {"inventory": str(recorded_inventory), "legacy_config": str(fixture), "error": str(exc)}, \
            [f"the migration did not produce a schema-v2 configuration: {exc}"]
    code = (check_config or _run_check_config)(output)
    registration = document.get("registration") if isinstance(document, dict) else None
    models = [model.get("model_id") for model in (registration or {}).get("models", [])
              if isinstance(model, dict)] if isinstance(registration, dict) else []
    evidence = {"inventory": str(recorded_inventory), "inventory_sha256": _file_sha256(recorded_inventory),
                "legacy_config": str(fixture),
                "legacy_config_sha256": _file_sha256(fixture), "migrated": str(output),
                "migrated_sha256": _file_sha256(output), "check_config_exit": code,
                "models": sorted(model for model in models if isinstance(model, str))}
    if code != 0:
        return False, evidence, [f"the migrated configuration does not pass run.py --check-config (exit {code})"]
    return True, evidence, []


def _run_check_config(path: Path) -> int:
    """Run the real CLI in-process; its report stays next to the migrated file."""
    import contextlib
    import io

    from run import main as run_main

    captured = io.StringIO()
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        code = int(run_main(["--config", str(path), "--check-config"]))
    (Path(path).parent / "check-config.txt").write_text(captured.getvalue(), encoding="utf-8")
    return code


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# S02 — failures are recorded, late results and old boots are refused


def _v1_spec(model_id: str, *, port: int = 10003, reserved_bytes: int = 100) -> Any:
    from ..contracts import Capability, ModelSpec

    return ModelSpec(model_id, f"http://127.0.0.1:{port}", frozenset({Capability.CHAT}), reserved_bytes)


def _v1_book(specs: Mapping[str, Any], *, budget: int, floor: int = 20) -> Any:
    from ..model_registry import Book

    book = Book(dict(specs), model_budget=budget, free_floor=floor, margin=0)
    for model_id in specs:
        book.bootstrap_stopped(model_id)
    return book


def _check_s02(**_: Any) -> tuple[dict, dict, list[str]]:
    observations: dict[str, bool] = {}
    evidence: dict[str, Any] = {}
    problems: list[str] = []

    for name, check in (
        ("load_failure_recorded", _s02_load_failure),
        ("late_load_rejected", _s02_late_load),
        ("old_boot_rejected", _s02_old_boot),
        ("stop_returned_instance_alive_recorded", _s02_stop_alive),
        ("unknown_keeps_budget", _s02_unknown_keeps_budget),
    ):
        observations[name], evidence[name] = _attempt_check(check)
        if observations[name] is not True:
            problems.append(f"S02: {name} is not evidenced: {evidence[name]}")
    return observations, {"evidence": evidence}, problems


class _Resources:
    async def snapshot(self):  # noqa: ANN201 - the scheduler reads three attributes
        from ..contracts import MemorySample

        return MemorySample(10_000, 9_000, asyncio.get_running_loop().time())


def _s02_load_failure() -> tuple[bool, dict]:
    """A load the backend cannot verify is recorded and blocks the model."""

    async def scenario() -> tuple[bool, dict]:
        from ..contracts import Observation, Presence
        from ..model_registry import State
        from ..scheduler import ModelScheduler, ModelUnavailable

        class Backend:
            async def load(self, operation, deadline):  # noqa: ANN001
                return Observation(Presence.UNKNOWN, None, False, 0.0, "load_timeout")

            async def stop(self, operation, deadline):  # noqa: ANN001
                return Observation(Presence.STOPPED, None, False, 0.0)

        book = _v1_book({"chat": _v1_spec("chat")}, budget=1_000)
        scheduler = ModelScheduler(book, _Resources(), Backend(), poll_interval_seconds=0.01)
        try:
            await asyncio.wait_for(scheduler.acquire("chat", "req-load", time.monotonic() + 5), timeout=20)
        except ModelUnavailable as exc:
            runtime = book.runtime["chat"]
            recorded = runtime.state is State.ERROR and runtime.last_error == "load_timeout"
            return recorded, {"error": str(exc), "state": runtime.state.value, "last_error": runtime.last_error}
        except Exception as exc:  # noqa: BLE001 - any other outcome is not the recorded failure
            return False, {"error": f"{type(exc).__name__}: {exc}"}
        return False, {"error": "the load returned although the backend could not verify it"}

    return asyncio.run(scenario())


def _s02_late_load() -> tuple[bool, dict]:
    """A load that finishes after its operation was superseded cannot move the books."""
    from ..contracts import MemorySample
    from ..model_registry import StaleOperation

    book = _v1_book({"chat": _v1_spec("chat")}, budget=1_000)
    operation = book.begin_load("chat", MemorySample(10_000, 9_000, 0.0), 0.0)
    book.begin_recovery()  # a newer epoch supersedes the pending load
    try:
        book.loaded(operation, 0.0)
    except StaleOperation:
        return True, {"operation_id": operation.operation_id, "generation": operation.generation,
                      "epoch_now": book.epoch}
    return False, {"error": "a superseded load moved the books"}


def _s02_old_boot() -> tuple[bool, dict]:
    """The C03 write-back rule refuses a result from a previous boot."""
    from ..control_protocol_v1 import Fence, writeback_decision

    current = Fence(boot_id="boot-0002", model_id="chat", generation=1, operation_id="op-1",
                    execution_id="exec-1", attempt=1)
    incoming = Fence(boot_id="boot-0001", model_id="chat", generation=1, operation_id="op-1",
                     execution_id="exec-1", attempt=1)
    decision = writeback_decision(current, incoming)
    return (decision.accepted is False and decision.reason == "stale_boot"), decision.as_dict()


def _s02_stop_alive() -> tuple[bool, dict]:
    """A stop command that returned while the instance still runs keeps the reservation."""

    async def scenario() -> tuple[bool, dict]:
        from ..contracts import MemorySample, Observation, Presence
        from ..model_registry import State
        from ..scheduler import ModelScheduler, ModelUnavailable

        class Backend:
            async def load(self, operation, deadline):  # noqa: ANN001
                return Observation(Presence.RUNNING, "instance", True, 0.0)

            async def stop(self, operation, deadline):  # noqa: ANN001
                # the command was accepted, but the instance is still alive
                return Observation(Presence.RUNNING, "instance", True, 0.0, "stop_unverified")

        book = _v1_book({"chat": _v1_spec("chat")}, budget=1_000)
        operation = book.begin_load("chat", MemorySample(10_000, 9_000, 0.0), 0.0)
        book.loaded(operation, 0.0)
        before = book.committed
        scheduler = ModelScheduler(book, _Resources(), Backend(), poll_interval_seconds=0.01)
        try:
            await asyncio.wait_for(scheduler.unload("chat", time.monotonic() + 5), timeout=20)
        except ModelUnavailable as exc:
            runtime = book.runtime["chat"]
            recorded = runtime.state is State.ERROR and book.committed == before
            return recorded, {"error": str(exc), "state": runtime.state.value, "committed": book.committed,
                              "reserved_before": before}
        except Exception as exc:  # noqa: BLE001
            return False, {"error": f"{type(exc).__name__}: {exc}"}
        return False, {"error": "the stop returned without a proven stop"}

    return asyncio.run(scenario())


def _s02_unknown_keeps_budget() -> tuple[bool, dict]:
    """An unproven stop keeps both ledgers: only a proven STOPPED releases."""
    from ..contracts import MemorySample
    from ..model_registry import State

    book = _v1_book({"chat": _v1_spec("chat")}, budget=1_000)
    operation = book.begin_load("chat", MemorySample(10_000, 9_000, 0.0), 0.0)
    book.loaded(operation, 0.0)
    before = book.committed
    cleanup = book.begin_cleanup(["chat"])[0]
    book.failed(cleanup, "stop_unverified")
    kept = book.committed == before and book.runtime["chat"].state is State.ERROR
    return kept, {"committed": book.committed, "reserved_before": before,
                  "state": book.runtime["chat"].state.value}


# ---------------------------------------------------------------------------
# S03 — the memory boundary arithmetic, from the book's own answers


def _check_s03(**_: Any) -> tuple[dict, dict, list[str]]:
    from ..contracts import MemorySample
    from ..contracts_v2 import parse_model_spec, reserved_bytes_from_peak
    from ..model_registry import Book

    peak_b, peak_a = 1_000_000_000, 4_000_000_000
    reserved_b, reserved_a = reserved_bytes_from_peak(peak_b), reserved_bytes_from_peak(peak_a)
    floor = 1_000_000
    budget = reserved_a + reserved_b
    total = 64 * 1024 ** 3
    book = Book({"s03-a": _s03_spec("s03-a", reserved_a, peak_a, port=10099),
                 "s03-b": _s03_spec("s03-b", reserved_b, peak_b, port=10100)},
                model_budget=budget, free_floor=floor, margin=0)
    for model_id in ("s03-a", "s03-b"):
        book.bootstrap_stopped(model_id)

    operation = book.begin_load("s03-a", MemorySample(total, total - reserved_a, 0.0), 0.0)
    book.loaded(operation, 0.0)
    committed = book.committed

    boundary = reserved_b + floor
    equality = book.can_load("s03-b", MemorySample(total, boundary, 0.0), 0.0)
    one_under = book.can_load("s03-b", MemorySample(total, boundary - 1, 0.0), 0.0)
    fresh = book.sample_valid(MemorySample(total, boundary, 0.0), 2.0)
    stale = book.sample_valid(MemorySample(total, boundary, 0.0), 2.0001)
    ready_floor = book.can_admit_ready("s03-a", MemorySample(total, floor, 0.0), 0.0)
    ready_under = book.can_admit_ready("s03-a", MemorySample(total, floor - 1, 0.0), 0.0)

    observations = {
        "boundary_equality_holds": equality is True,
        "boundary_one_byte_rejected": one_under is False,
        "sample_freshness_enforced": bool(fresh and not stale),
        "no_double_counting": bool(ready_floor and not ready_under),
    }
    problems = [f"S03: {name} is not evidenced" for name, value in observations.items() if value is not True]
    numbers = {"measured_peak_bytes": peak_b, "model_budget_bytes": budget,
               "mem_available_bytes": boundary, "free_floor_bytes": floor,
               "committed_bytes": committed, "instance_already_reserved": False}
    evidence = {"equality_admissible": equality, "one_byte_under_admissible": one_under,
                "fresh_at_two_seconds": fresh, "fresh_beyond_two_seconds": stale,
                "ready_admissible_at_floor": ready_floor, "ready_admissible_below_floor": ready_under,
                "reserved_a": reserved_a, "reserved_b": reserved_b}
    return observations, {"numbers": numbers, "evidence": evidence}, problems


def _s03_spec(model_id: str, reserved_bytes: int, physical_peak: int, *, port: int) -> Any:
    from ..contracts_v2 import parse_model_spec

    return parse_model_spec({
        "model_id": model_id, "runtime_id": "s03-runtime", "capabilities": ["chat"],
        "assets": [{"role": "model", "path": f"{model_id}.gguf", "sha256": "0" * 64, "size_bytes": 1}],
        "port": port,
        "envelope": {"ctx_size": 4096, "max_input_tokens": 2048, "max_output_tokens": 1024,
                     "max_parallel": 1, "max_image_tokens": 0, "max_image_edge_pixels": 0, "max_images": 0},
        "timeout_seconds": 60, "reserved_bytes": reserved_bytes, "measured": True,
        "measurement_ref": "a" * 64, "physical_resident_peak_bytes": physical_peak,
    })


# ---------------------------------------------------------------------------
# S04 — sessions, queue order, cancellation accounting and idempotency


def _check_s04(**_: Any) -> tuple[dict, dict, list[str]]:
    observations: dict[str, bool] = {}
    evidence: dict[str, Any] = {}
    problems: list[str] = []
    for name, check in (
        ("interactive_priority_holds", _s04_priority),
        ("drain_keeps_lease", _s04_drain_keeps_lease),
        ("ttl_enforced", _s04_ttl),
        ("hard_deadline_enforced", _s04_hard_deadline),
        ("cancel_observed", _s04_cancel),
        ("idempotency_enforced", _s04_idempotency),
    ):
        observations[name], evidence[name] = _attempt_check(check)
        if observations[name] is not True:
            problems.append(f"S04: {name} is not evidenced: {evidence[name]}")
    return observations, {"evidence": evidence}, problems


def _attempt_check(check: Callable[[], tuple[bool, dict]]) -> tuple[bool, dict]:
    """One sub-check, isolated: a crash fails its observation, not the whole case."""
    try:
        return check()
    except Exception as exc:  # noqa: BLE001 - a crashed check is not evidence
        return False, {"error": f"{type(exc).__name__}: {exc}"}


def _s04_priority() -> tuple[bool, dict]:
    """The queue orders by priority (the aging term only helps an old waiter)."""
    from ..request_queue import RequestQueue

    queue = RequestQueue(capacity=8, aging_seconds=30)
    queue.enqueue("low", "chat", 0, 1_000.0, 0.0)
    queue.enqueue("high", "chat", 10, 1_000.0, 0.0)
    first = queue.head(0.0)
    queue.remove("high")
    second = queue.head(0.0)
    first_id = None if first is None else first.request_id
    second_id = None if second is None else second.request_id
    ok = first_id == "high" and second_id == "low"
    return ok, {"first": first_id, "second": second_id}


def _s04_drain_keeps_lease() -> tuple[bool, dict]:
    """Freezing admission for a drain keeps the existing lease and the waiter."""
    from ..contracts import MemorySample
    from ..model_registry import Conflict
    from ..request_queue import RequestQueue, WaitKind

    book = _v1_book({"chat": _v1_spec("chat"), "other": _v1_spec("other", port=10004)}, budget=1_000)
    operation = book.begin_load("chat", MemorySample(10_000, 9_000, 0.0), 0.0)
    book.loaded(operation, 0.0)
    lease = book.acquire_ready("chat", "req-drain", 0.0)
    book.freeze_for_switch(["chat"])  # a drain freezes admission, it never revokes a lease
    lease_kept = book.runtime["chat"].leases.get(lease.lease_id) == lease
    try:
        book.acquire_ready("chat", "req-second", 0.0)
        second_refused = False
    except Conflict:
        second_refused = True
    queue = RequestQueue(capacity=8)
    queue.enqueue("session-waiter", "chat", 0, 100.0, 0.0, kind=WaitKind.SESSION)
    waiter_kept = queue.contains("session-waiter")
    ok = lease_kept and second_refused and waiter_kept
    return ok, {"lease_kept": lease_kept, "new_admission_refused": second_refused, "waiter_kept": waiter_kept}


def _s04_ttl() -> tuple[bool, dict]:
    """An ACTIVE session expires at its soft TTL and cannot be renewed past it."""
    from ..request_queue import RequestQueue
    from ..session_manager import SessionConflict, SessionManager

    sessions = SessionManager()
    queue = RequestQueue(capacity=8)
    sessions.create("session-ttl", "chat", "client-a", now=0.0, queue=queue)
    sessions.mark_active("session-ttl", 0.0)
    live = sessions.is_live("session-ttl", 29.999)
    expired = not sessions.is_live("session-ttl", 30.0)
    try:
        sessions.heartbeat("session-ttl", 30.0)
        renewed = True
    except SessionConflict:
        renewed = False
    ok = bool(live and expired and not renewed)
    return ok, {"live_before_ttl": live, "live_at_ttl": not expired, "renewed_after_ttl": renewed}


def _s04_hard_deadline() -> tuple[bool, dict]:
    """A heartbeat extends the soft TTL; it never moves the hard deadline."""
    from ..request_queue import RequestQueue
    from ..session_manager import SessionManager

    sessions = SessionManager()
    queue = RequestQueue(capacity=8)
    sessions.create("session-hard", "chat", "client-a", now=0.0, queue=queue, hard_deadline_seconds=60.0)
    sessions.mark_active("session-hard", 0.0)
    for moment in (10.0, 20.0, 30.0, 40.0, 50.0):
        sessions.heartbeat("session-hard", moment)  # the client keeps the soft TTL alive
    live = sessions.is_live("session-hard", 59.9)
    stopped = not sessions.is_live("session-hard", 60.0)
    ok = bool(live and stopped)
    return ok, {"live_before_hard_deadline": live, "live_at_hard_deadline": not stopped,
                "hard_deadline_seconds": 60.0}


def _s04_cancel() -> tuple[bool, dict]:
    """A cancel is accepted; the lease, the slot and the budget stay until it ends."""
    from ..contracts import MemorySample

    book = _v1_book({"chat": _v1_spec("chat")}, budget=1_000)
    operation = book.begin_load("chat", MemorySample(10_000, 9_000, 0.0), 0.0)
    book.loaded(operation, 0.0)
    lease = book.acquire_ready("chat", "req-cancel", 0.0)
    accepted = book.begin_cancel(lease)
    kept = book.runtime["chat"].leases.get(lease.lease_id) == lease
    cancelling = book.is_cancelling(lease)
    budget_kept = book.committed == book.required("chat")
    ok = bool(accepted and kept and cancelling and budget_kept)
    return ok, {"accepted": accepted, "lease_kept": kept, "cancelling": cancelling, "budget_kept": budget_kept}


def _s04_idempotency() -> tuple[bool, dict]:
    """Same key + same payload replays; while pending it is busy; a different payload conflicts."""
    from ..idempotency import IdempotencyError, IdempotencyStore

    store = IdempotencyStore(boot_key=b"k" * 32, clock=lambda: 1_000.0)
    payload = {"model_id": "chat"}
    route, owner, key = "session.create", "uid:1000", "k-1"
    record = store.begin(route=route, owner=owner, key=key, payload=payload)
    try:
        store.begin(route=route, owner=owner, key=key, payload=payload)
        busy = False
    except IdempotencyError as exc:
        busy = getattr(exc, "code", None) == "busy"
    store.complete(record, status=201, resource_id="session-1", body={"session_id": "session-1"})
    try:
        replayed = store.replay(store.begin(route=route, owner=owner, key=key, payload=payload), payload=payload)
        replay_ok = replayed.get("status") == 201 and replayed.get("resource_id") == "session-1"
    except IdempotencyError:
        replay_ok = False
    try:
        store.begin(route=route, owner=owner, key=key, payload={"model_id": "other"})
        conflict = False
    except IdempotencyError as exc:
        conflict = getattr(exc, "code", None) == "idempotency_conflict"
    ok = bool(busy and replay_ok and conflict)
    return ok, {"pending_duplicate_busy": busy, "replay_returns_object": replay_ok,
                "different_payload_conflicts": conflict}


# ---------------------------------------------------------------------------
# S05 — the compat input contract and the blob lifecycle


def _check_s05(*, candidate: Any, **_ignored: Any) -> tuple[dict, dict, list[str]]:
    observations: dict[str, bool] = {}
    evidence: dict[str, Any] = {}
    problems: list[str] = []

    observations["compat_api_accepts"], evidence["compat"] = _attempt_check(lambda: _s05_compat(candidate))
    try:
        blob_observations, blob_evidence = _s05_blobs()
    except Exception as exc:  # noqa: BLE001 - a crashed store check is not evidence
        blob_observations = {name: False for name in _required("S05") if name != "compat_api_accepts"}
        blob_evidence = {"error": f"{type(exc).__name__}: {exc}"}
    observations.update(blob_observations)
    evidence["blobs"] = blob_evidence
    for name, value in observations.items():
        if value is not True:
            detail = evidence["compat"] if name == "compat_api_accepts" else evidence["blobs"].get(name)
            problems.append(f"S05: {name} is not evidenced: {detail}")
    return observations, {"evidence": evidence}, problems


def _s05_compat(candidate: Any) -> tuple[bool, dict]:
    """The compat request contract: a valid request is accepted, an oversized one refused.

    The registered capabilities decide which surface is exercised — chat (which
    also covers a vision model through the same route), embeddings or rerank.
    """
    for capability, check in (("chat", _s05_compat_chat), ("embeddings", _s05_compat_embeddings),
                              ("rerank", _s05_compat_rerank)):
        model = next((item for item in candidate.models if capability in item.capabilities), None)
        if model is not None:
            return check(model)
    return False, {"error": "no chat, embeddings or rerank model is registered: "
                            "the compat surface cannot be checked"}


def _s05_compat_chat(model: Any) -> tuple[bool, dict]:
    from ..api_models import ChatRequest
    from ..envelope_validator import EnvelopeError, check_chat_input

    request = ChatRequest(model=model.model_id, messages=[{"role": "user", "content": "hello"}])
    messages = [dict(message) for message in request.messages]
    try:
        accepted = asyncio.run(check_chat_input({"messages": messages, "max_tokens": 8},
                                                capabilities=model.capabilities, envelope=model.envelope))
    except EnvelopeError as exc:
        return False, {"error": f"a valid request was refused: {exc}"}
    oversized = model.envelope.max_output_tokens + 1
    try:
        asyncio.run(check_chat_input({"messages": messages, "max_tokens": oversized},
                                     capabilities=model.capabilities, envelope=model.envelope))
        refused = False
    except EnvelopeError:
        refused = True
    ok = bool(refused and isinstance(accepted, Mapping))
    return ok, {"model_id": model.model_id, "capability": "chat", "accepted": dict(accepted or {}),
                "oversized_refused": refused, "oversized_max_tokens": oversized}


def _s05_compat_embeddings(model: Any) -> tuple[bool, dict]:
    from ..api_models import EmbeddingRequest
    from ..envelope_validator import EnvelopeError, check_embeddings_input

    request = EmbeddingRequest(model=model.model_id, input=["hello"])
    inputs = [request.input] if isinstance(request.input, str) else list(request.input)
    try:
        accepted = check_embeddings_input({"input": inputs})
    except EnvelopeError as exc:
        return False, {"error": f"a valid request was refused: {exc}"}
    try:
        check_embeddings_input({"input": []})
        refused = False
    except EnvelopeError:
        refused = True
    ok = bool(refused and accepted)
    return ok, {"model_id": model.model_id, "capability": "embeddings", "accepted": accepted,
                "empty_refused": refused}


def _s05_compat_rerank(model: Any) -> tuple[bool, dict]:
    from ..api_models import RerankRequest
    from ..envelope_validator import EnvelopeError, check_rerank_input

    request = RerankRequest(model=model.model_id, query="q", documents=["a", "b"])
    try:
        accepted = check_rerank_input({"query": request.query, "documents": list(request.documents)})
    except EnvelopeError as exc:
        return False, {"error": f"a valid request was refused: {exc}"}
    try:
        check_rerank_input({"query": "q", "documents": []})
        refused = False
    except EnvelopeError:
        refused = True
    ok = bool(refused and accepted)
    return ok, {"model_id": model.model_id, "capability": "rerank", "accepted": list(accepted),
                "empty_refused": refused}


def _s05_blobs() -> tuple[dict[str, bool], dict]:
    """Every blob rule against a real store over a scratch directory."""
    import tempfile

    async def scenario(workdir: Path) -> tuple[dict[str, bool], dict]:
        from ..blob_store import BlobStore, BlobStoreError
        from ..control_protocol_v1 import Fence

        outcomes: dict[str, bool] = {}
        evidence: dict[str, Any] = {}

        class Clock:
            def __init__(self) -> None:
                self.now = 1_000.0

            def __call__(self) -> float:
                return self.now

        clock = Clock()
        payload = b'{"ok":true}'
        digest = hashlib.sha256(payload).hexdigest()
        store = BlobStore(workdir / "main", clock=clock)
        await store.upload(blob_id="b-1", owner="client-a", media_type="application/json",
                           chunks=_stream(payload), expected_sha256=digest, declared_size=len(payload))
        try:
            await store.read_all("b-1", "client-b", "exec-1")
            outcomes["blob_owner_enforced"] = False
        except BlobStoreError as exc:
            outcomes["blob_owner_enforced"] = exc.code == "not_found"
        evidence["blob_owner_enforced"] = {"owner": "client-b", "refused_as": "not_found"}

        try:
            await store.upload(blob_id="b-2", owner="client-a", media_type="application/json",
                               chunks=_stream(payload), expected_sha256="0" * 64, declared_size=len(payload))
            outcomes["blob_hash_enforced"] = False
            evidence["blob_hash_enforced"] = {"error": "the declared hash was accepted"}
        except BlobStoreError as exc:
            outcomes["blob_hash_enforced"] = exc.code == "checksum_mismatch"
            evidence["blob_hash_enforced"] = {"refused_code": exc.code}

        tight = BlobStore(workdir / "quota", clock=clock, owner_quota_bytes=2)
        try:
            await tight.upload(blob_id="b-3", owner="client-a", media_type="application/json",
                               chunks=_stream(payload), expected_sha256=digest, declared_size=len(payload))
            outcomes["quota_enforced"] = False
            evidence["quota_enforced"] = {"error": "an over-quota upload was accepted"}
        except BlobStoreError as exc:
            outcomes["quota_enforced"] = exc.code == "quota_exceeded"
            evidence["quota_enforced"] = {"refused_code": exc.code, "owner_quota_bytes": 2}

        short = BlobStore(workdir / "expiry", clock=clock, retention_seconds=10.0)
        await short.upload(blob_id="b-4", owner="client-a", media_type="application/json",
                           chunks=_stream(payload), expected_sha256=digest, declared_size=len(payload))
        clock.now += 11.0
        await short.expire()
        try:
            await short.read_all("b-4", "client-a", "exec-1")
            outcomes["expiry_enforced"] = False
            evidence["expiry_enforced"] = {"error": "an expired blob was still readable"}
        except BlobStoreError as exc:
            outcomes["expiry_enforced"] = exc.code in ("expired", "not_found")
            evidence["expiry_enforced"] = {"refused_code": exc.code}

        restart_root = workdir / "restart"
        first = BlobStore(restart_root, clock=clock)
        await first.upload(blob_id="b-5", owner="client-a", media_type="application/json",
                           chunks=_stream(payload), expected_sha256=digest, declared_size=len(payload))
        second = BlobStore(restart_root, clock=clock)
        report = await second.recover(instances_running=False, boot_id="boot-2")
        outcomes["restart_discovery_works"] = "b-5" in tuple(report.verified)
        evidence["restart_discovery_works"] = report.as_dict()

        the_fence = Fence(boot_id="boot-1", model_id="chat", generation=1, operation_id="op-1",
                          execution_id="exec-1", attempt=1)
        reservation = await store.reserve_output(blob_id="o-1", owner="client-a",
                                                 media_type="application/json", limit_bytes=64,
                                                 fence=the_fence)
        cancelled = await store.cancel_output(reservation, fence=the_fence)
        late = None
        try:
            await store.write_output(reservation, chunks=_stream(b"late"), expected_sha256=None)
            late_refused = False
        except BlobStoreError as exc:
            late_refused = exc.code == "cancelled"
            late = exc.code
        outcomes["late_output_rejected"] = bool(cancelled is True and late_refused)
        evidence["late_output_rejected"] = {"cancel_accepted": cancelled, "late_write_refused_as": late}
        return outcomes, evidence

    with tempfile.TemporaryDirectory(prefix="sms-s05-") as raw:
        return asyncio.run(scenario(Path(raw)))


async def _stream(payload: bytes):  # noqa: ANN201 - an async byte iterator
    yield payload


# ---------------------------------------------------------------------------
# S06 — tampered or forged material is refused


def _check_s06(*, candidate: Any, material_dir: Path, candidate_path: Path | None,
               **_ignored: Any) -> tuple[dict, dict, list[str]]:
    from ..evidence_contracts import (
        ContractError, parse_acceptance_report, parse_candidate, validate_report_mapping,
    )
    from ..preflight_v3 import manifest_identity, verify_environment

    observations: dict[str, bool] = {}
    evidence: dict[str, Any] = {}
    problems: list[str] = []

    if candidate_path is None or not Path(candidate_path).is_file():
        # Without the frozen candidate document the tamper checks cannot run; they are not claimed.
        for name in _required("S06"):
            observations[name] = False
        problems.append("S06 needs the candidate document: the tamper checks cannot be claimed without it")
        return observations, {"evidence": {"candidate": str(candidate_path)}}, problems
    document = json.loads(Path(candidate_path).read_text(encoding="utf-8"))

    parses = _parses_candidate(parse_candidate, document)
    forged = json.loads(json.dumps(document))
    forged["summary"] = {"passed": True}
    refused = not _parses_candidate(parse_candidate, forged)
    observations["tampered_candidate_rejected"] = bool(parses and refused)
    evidence["tampered_candidate_rejected"] = {"parses_unmodified": parses, "forged_field_refused": refused}

    summary_forged = json.loads(json.dumps(_minimal_report(candidate)))
    summary_forged["summary"] = {"passed": True}
    try:
        parse_acceptance_report(summary_forged)
        summary_refused = False
    except ContractError:
        summary_refused = True
    observations["forged_summary_rejected"] = summary_refused
    evidence["forged_summary_rejected"] = {"forged_field_refused": summary_refused}

    duplicate = _minimal_report(candidate, duplicate_final=True)
    try:
        parsed = parse_acceptance_report(duplicate)
        try:
            validate_report_mapping(parsed, candidate)
            duplicate_refused = False
        except ContractError as exc:
            duplicate_refused = "duplicate final conclusions" in str(exc)
            evidence["duplicate_final_rejected"] = {"refused": duplicate_refused, "error": str(exc)}
    except ContractError as exc:
        duplicate_refused = False
        evidence["duplicate_final_rejected"] = {"error": f"the probe report does not parse: {exc}"}
    observations["duplicate_final_rejected"] = duplicate_refused

    observations["tampered_evidence_rejected"], evidence["tampered_evidence_rejected"] = _s06_tampered_evidence(
        candidate, candidate_path, material_dir)

    site = _s06_site(candidate)
    manifest = _s06_manifest(candidate, site)
    good = verify_environment(manifest, site=site)
    asset_site = json.loads(json.dumps(site))
    for entry in asset_site["model_files"].values():
        entry["sha256"] = "c" * 64
    asset_result = verify_environment(manifest, site=asset_site)
    observations["tampered_asset_rejected"] = bool(
        good["ok"] is True and asset_result["ok"] is False
        and any("size/hash differs" in problem for problem in asset_result["problems"]))
    evidence["tampered_asset_rejected"] = {"accepted_unmodified": good["ok"],
                                           "problems": asset_result["problems"]}

    device_site = json.loads(json.dumps(site))
    device_site["mem_total_bytes"] = int(device_site["mem_total_bytes"]) + 1
    device_result = verify_environment(manifest, site=device_site)
    observations["tampered_device_rejected"] = bool(
        good["ok"] is True and device_result["ok"] is False
        and any("mem_total_bytes" in problem for problem in device_result["problems"]))
    evidence["tampered_device_rejected"] = {"accepted_unmodified": good["ok"],
                                            "problems": device_result["problems"]}
    evidence["manifest_identity"] = manifest_identity(manifest)
    return observations, {"evidence": evidence}, problems


def _parses_candidate(parse: Callable[[Mapping], Any], document: Mapping) -> bool:
    from ..evidence_contracts import ContractError

    try:
        parse(document)
    except ContractError:
        return False
    return True


def _minimal_report(candidate: Any, *, duplicate_final: bool = False) -> dict:
    """A structurally valid report for the S01 case; used to probe the mapping rules."""
    reference = {"relative_path": "cases/S01/attempt-1/case.json", "size_bytes": 1, "sha256": "0" * 64}
    report = _report_with(candidate, reference, run_id="s06-probe")
    if duplicate_final:
        second = dict(report["case_attempt_refs"][0])
        second["attempt"] = 2
        report["case_attempt_refs"] = [report["case_attempt_refs"][0], second]
        report["final_attempts"] = [{"case_id": "S01", "run_id": "s06-probe", "attempt": 1},
                                    {"case_id": "S01", "run_id": "s06-probe", "attempt": 2}]
    return report


def _s06_tampered_evidence(candidate: Any, candidate_path: Path, material_dir: Path) -> tuple[bool, dict]:
    """The offline verifier refuses material whose bytes no longer match the manifest."""
    from . import EXIT_INPUT
    from .verify import verify_evidence

    workdir = Path(material_dir) / "tampered-evidence"
    case_file = workdir / "cases" / "S01" / "case.json"
    case_file.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"case_id": "S01",
                       "observations": {name: True for name in S_CASE_OBSERVATIONS["S01"]}},
                      sort_keys=True).encode("utf-8")
    case_file.write_bytes(body)
    reference = {"relative_path": "cases/S01/case.json", "size_bytes": len(body),
                 "sha256": hashlib.sha256(body).hexdigest()}
    report = _report_with(candidate, reference, run_id="s06-tamper")
    (workdir / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    case_file.write_bytes(body + b" ")  # the bytes change; the manifest does not
    code, document = verify_evidence(candidate_path=Path(candidate_path), evidence_dir=workdir)
    refused = code == EXIT_INPUT and "differs from the manifest" in str(document.get("error"))
    return refused, {"exit": code, "error": str(document.get("error"))}


def _report_with(candidate: Any, reference: Mapping[str, Any], *, run_id: str) -> dict:
    from ..evidence_contracts import candidate_digest, device_digest

    digest, device = candidate_digest(candidate), device_digest(candidate.device)
    started = ended = _utc_now()
    attempt = {"case_id": "S01", "run_id": run_id, "attempt": 1, "candidate_sha256": digest,
               "device_digest": device, "started_at": started, "ended_at": ended, "boot_id": "s06",
               "event_refs": [dict(reference)], "collector_sha256": candidate.collector_sha256,
               "evaluator_sha256": candidate.evaluator_sha256, "exit_code": 0}
    return {"schema_version": 3, "candidate_sha256": digest, "device_digest": device, "run_id": run_id,
            "started_at": started, "ended_at": ended, "case_attempt_refs": [attempt],
            "final_attempts": [{"case_id": "S01", "run_id": run_id, "attempt": 1}],
            "artifact_manifest": [dict(reference)]}


def _s06_site(candidate: Any) -> dict:
    from ..evidence_contracts import candidate_digest, device_digest

    model = candidate.models[0]
    asset = model.assets[0]
    return {
        "machine_id_sha256": "1" * 64, "architecture": "aarch64", "device_tree_sha256": "2" * 64,
        "mem_total_bytes": 64 * 1024 ** 3, "kernel_release": "5.15.148-tegra",
        "model_disk_uuid": "3" * 64, "scratch_disk_uuid": "4" * 64,
        "config_sha256": candidate.config_sha256, "source_archive_sha256": candidate.source_archive_sha256,
        "filesystem": "ext4", "images": {model_image_digest(candidate): True},
        "model_files": {asset.path: {"size_bytes": asset.size_bytes, "sha256": asset.sha256}},
        "candidate_digest": candidate_digest(candidate), "device_digest": device_digest(candidate.device),
    }


def model_image_digest(candidate: Any) -> str:
    runtime = candidate.runtimes[0]
    return runtime.image_digest


def _s06_manifest(candidate: Any, site: Mapping[str, Any]) -> dict:
    from ..preflight_v3 import manifest_identity

    model = candidate.models[0]
    asset = model.assets[0]
    manifest = {
        "schema_version": 3, "mode": "production", "deployment_id": candidate.deployment_id,
        "candidate_sha256": site["candidate_digest"], "source_archive_sha256": candidate.source_archive_sha256,
        "config_sha256": candidate.config_sha256, "device_digest": site["device_digest"],
        "model_filesystem": "ext4",
        "device": {field: site[field] for field in ("machine_id_sha256", "architecture", "device_tree_sha256",
                                                    "mem_total_bytes", "kernel_release", "model_disk_uuid",
                                                    "scratch_disk_uuid")},
        "models": {model.model_id: {"image_digest": model_image_digest(candidate),
                                    "container_name": f"sms-{candidate.deployment_id}-{model.model_id}",
                                    "assets": [{"path": asset.path, "size_bytes": asset.size_bytes,
                                                "sha256": asset.sha256}]}},
        "evidence": {"started_at": _utc_now()},
    }
    manifest["identity_sha256"] = manifest_identity(manifest)
    return manifest


_CHECKS: dict[str, Callable[..., tuple[dict, dict, list[str]]]] = {
    "S01": _check_s01,
    "S02": _check_s02,
    "S03": _check_s03,
    "S04": _check_s04,
    "S05": _check_s05,
    "S06": _check_s06,
}
