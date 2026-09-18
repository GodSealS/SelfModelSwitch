"""Controlled calibration: temporary budget, exclusive site, sampled memory (P21).

`calibrate` (plan/08-execution-plan.md §5) runs in a maintenance window:
production admission closed, no other managed instance, the single-instance
lock held — each of those is re-verified live, because the maintenance file is
a record, never a permit. It owns the M00 §5 accounting and derives every
number from RAW samples (MemTotal/MemFree/MemAvailable are kept per row):
a summary is never copied.

Two material sources:

* a fresh run executes the registered cases; its round material carries the
  monotonic window boundaries, so the §5 criteria (interval, gaps, baseline
  median, delta, baseline shift, swap) are evaluated here;
* `--from-evidence DIR` recomputes from preserved raw material. When the
  preserved round has no boundary facts, only the C02 physical upper bound can
  be proven — the round is reported `unverified`, the calibration `blocked`,
  production stays closed and the software result is kept (C02).

Stop accounting: every round must carry a proven stop or an explicit UNKNOWN;
a round that claims neither is refused. An over-envelope image fact is refused
with the exact registered bound — the probe's 1.05 probe-only tolerance is not
an acceptance relaxation (P21 AC4).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import statistics
import threading
import time
from typing import Any, Callable, Mapping, Sequence

from ..contracts_v2 import ContractError
from ..evidence_contracts import parse_device_fact, parse_runtime_stack
from ..resource_monitor import system_nonfree_upper_bound_v1
from ..ports_v3 import MemorySample

SAMPLING_INTERVAL_SECONDS = 0.1
MEASUREMENT_GAP_SECONDS = 0.5
WINDOW_SECONDS = 10.0
BASELINE_SHIFT_LIMIT_BYTES = 256 * 1024**2
DEFAULT_RUNS = 3
# The calibration sampler keeps BOTH raw figures C02 needs: MemTotal and MemFree.
MEMINFO_HEADER = "t_mono,utc,mem_available_bytes,mem_total_bytes,mem_free_bytes,swap_free_bytes,cached_bytes"
# The M00 probe kept MemAvailable but not MemFree; preserved material in this
# shape cannot prove the `system_nonfree_upper_bound_v1` bound and is reported
# unverified instead of being estimated.
PROBE_MEMINFO_HEADER = "t_mono,utc,mem_available_bytes,mem_total_bytes,swap_free_bytes,cached_bytes"

MAINTENANCE_KEYS = frozenset({"production_admission_closed", "instances_stopped", "checked_utc", "checked_by", "notes"})


class CalibrationError(RuntimeError):
    """A calibration refusal; `semantic` picks exit 3 over exit 2."""

    def __init__(self, message: str, *, semantic: bool) -> None:
        super().__init__(message)
        self.semantic = semantic


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# raw material


@dataclass(frozen=True)
class RoundMaterial:
    """One preserved round: raw samples plus whatever facts it carries."""

    label: str
    source_dir: Path
    samples: list[dict]
    launch_ts: float | None
    end_ts: float | None
    stop: dict
    cases: dict
    raw_files: tuple[tuple[Path, str], ...]  # (path, sha256)

    @property
    def has_window(self) -> bool:
        return self.launch_ts is not None and self.end_ts is not None


def load_meminfo_csv(path: Path) -> list[dict]:
    """The sampler's raw rows: monotonic + UTC + MemAvailable/MemTotal/MemFree/swap.

    The probe-era shape (MemFree absent) is accepted and marked, because the
    C02 physical bound needs that raw field and must not be estimated around it.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CalibrationError(f"cannot read raw samples {path}: {exc}", semantic=False) from exc
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise CalibrationError(f"{path}: no raw samples", semantic=False)
    header = lines[0].strip()
    if header == MEMINFO_HEADER:
        has_free = True
    elif header == PROBE_MEMINFO_HEADER:
        has_free = False
    else:
        raise CalibrationError(f"{path}: unexpected sampling header {header!r}", semantic=False)
    rows: list[dict] = []
    for index, line in enumerate(lines[1:], start=2):
        parts = line.split(",")
        if len(parts) != (7 if has_free else 6):
            raise CalibrationError(f"{path}:{index}: expected {7 if has_free else 6} columns", semantic=False)
        try:
            rows.append({
                "t": float(parts[0]),
                "utc": parts[1],
                "available_bytes": int(parts[2]),
                "total_bytes": int(parts[3]),
                "mem_free_bytes": int(parts[4]) if has_free else None,
                "swap_free_bytes": int(parts[5]) if has_free else int(parts[4]),
                "cached_bytes": int(parts[6]) if has_free else int(parts[5]),
            })
        except ValueError as exc:
            raise CalibrationError(f"{path}:{index}: {exc}", semantic=False) from exc
    if not rows:
        raise CalibrationError(f"{path}: no raw samples", semantic=False)
    return rows


def _json_file(path: Path, *, label: str) -> dict:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CalibrationError(f"cannot read {label} {path}: {exc}", semantic=False) from exc
    if not isinstance(document, dict):
        raise CalibrationError(f"{label} {path} must be a JSON object", semantic=False)
    return document


def load_round_material(directory: Path) -> RoundMaterial:
    """One round directory: calibrate material or a preserved probe run."""
    sampling = directory / "sampling" / "meminfo.csv"
    if not sampling.is_file():
        raise CalibrationError(f"{directory}: no sampling/meminfo.csv", semantic=False)
    samples = load_meminfo_csv(sampling)
    raw_files: list[tuple[Path, str]] = [(sampling, _sha256_file(sampling))]

    round_json = directory / "round.json"
    run_json = directory / "run.json"
    if round_json.is_file():
        document = _json_file(round_json, label="round material")
        raw_files.append((round_json, _sha256_file(round_json)))
        launch_ts = document.get("launch_ts")
        end_ts = document.get("end_ts")
        if not isinstance(launch_ts, (int, float)) or not isinstance(end_ts, (int, float)) or end_ts < launch_ts:
            raise CalibrationError(f"{round_json}: the round window is missing or invalid", semantic=False)
        stop = document.get("stop")
        if not isinstance(stop, dict) or not stop:
            raise CalibrationError(f"{round_json}: a round must record its stop or UNKNOWN", semantic=True)
        cases = document.get("cases") if isinstance(document.get("cases"), dict) else {}
        return RoundMaterial(directory.name, directory, samples, float(launch_ts), float(end_ts), stop, cases,
                             tuple(raw_files))

    if run_json.is_file():
        document = _json_file(run_json, label="preserved run")
        raw_files.append((run_json, _sha256_file(run_json)))
        stop = document.get("stop")
        if not isinstance(stop, dict) or not stop:
            raise CalibrationError(f"{run_json}: a round must record its stop or UNKNOWN", semantic=True)
        cases = document.get("cases") if isinstance(document.get("cases"), dict) else {}
        # preserved probe material has no monotonic window: the bound is still
        # recomputed from the raw figures (a superset window, never a summary copy)
        return RoundMaterial(directory.name, directory, samples, None, None, stop, cases, tuple(raw_files))

    raise CalibrationError(f"{directory}: neither round.json nor run.json is present", semantic=False)


# ---------------------------------------------------------------------------
# the §5 accounting (raw-derived)


class MemorySampler:
    """The calibration sampler: ≤100ms cadence, keeping MemTotal/MemFree/MemAvailable.

    C02 needs MemTotal and MemFree raw; the M00 probe kept MemAvailable only, so
    its preserved material cannot prove the physical bound (see `evaluate_round`).
    """

    def __init__(self, path: Path, *, interval: float = SAMPLING_INTERVAL_SECONDS,
                 reader: Callable[[], dict | None] | None = None) -> None:
        if interval <= 0:
            raise CalibrationError("the sampling interval must be positive", semantic=False)
        self.path = path
        self.interval = interval
        self._reader = reader if reader is not None else read_meminfo_row
        self.rows: list[dict] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        with self.path.open("w", encoding="utf-8") as handle:
            handle.write(MEMINFO_HEADER + "\n")
            while not self._stop.is_set():
                sample = self._reader()
                if sample is not None:
                    self.rows.append(sample)
                    handle.write(f"{sample['t']:.3f},{sample['utc']},{sample['available_bytes']},"
                                 f"{sample['total_bytes']},{sample['mem_free_bytes']},{sample['swap_free_bytes']},"
                                 f"{sample['cached_bytes']}\n")
                    handle.flush()
                self._stop.wait(self.interval)

    def stop(self, post_seconds: float = WINDOW_SECONDS) -> None:
        if post_seconds > 0:
            time.sleep(post_seconds)
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)


def read_meminfo_row(path: str = "/proc/meminfo") -> dict | None:
    """One raw /proc/meminfo reading: MemTotal, MemFree (C02) and MemAvailable."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    figures: dict[str, int] = {}
    for key in ("MemTotal", "MemFree", "MemAvailable", "Cached", "SwapFree"):
        match = re.search(rf"^{key}:\s+(\d+)\s*kB", text, re.MULTILINE)
        if match is not None:
            figures[key] = int(match.group(1)) * 1024
    if not {"MemTotal", "MemFree", "MemAvailable"} <= set(figures):
        return None
    moment = time.monotonic()
    return {"t": moment, "utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "available_bytes": figures["MemAvailable"], "total_bytes": figures["MemTotal"],
            "mem_free_bytes": figures["MemFree"], "swap_free_bytes": figures.get("SwapFree", 0),
            "cached_bytes": figures.get("Cached", 0)}


def evaluate_round(material: RoundMaterial) -> dict:
    """The M00 §5 criteria and the C02 physical bound, derived from raw rows."""
    samples = material.samples
    metrics: dict[str, Any] = {
        "round": material.label,
        "raw_samples": len(samples),
        "raw_files": [{"path": str(path), "sha256": digest} for path, digest in material.raw_files],
        "window": None,
        "sampling_interval_seconds": None,
        "max_gap_seconds": None,
        "gaps_over_500ms": None,
        "baseline_bytes": None,
        "min_bytes": None,
        "post_baseline_bytes": None,
        "delta_bytes": None,
        "baseline_shift_bytes": None,
        "swap_used": None,
        "measurement_valid": False,
        "criteria_note": None,
        "physical_upper_bound_bytes": None,
        "physical_upper_bound_window": None,
        "bound_note": None,
        "stop": material.stop,
        "stop_quiescent": material.stop.get("quiescent") is True,
        "image_tokens_measured": None,
    }
    gaps = [second["t"] - first["t"] for first, second in zip(samples, samples[1:])]
    intervals = [gap for gap in gaps]
    metrics["sampling_interval_seconds"] = round(statistics.median(intervals), 4) if intervals else None
    metrics["max_gap_seconds"] = round(max(intervals), 4) if intervals else None
    metrics["gaps_over_500ms"] = sum(1 for gap in intervals if gap > MEASUREMENT_GAP_SECONDS)
    swap_free_min = min(row["swap_free_bytes"] for row in samples)
    metrics["swap_used"] = swap_free_min < samples[0]["swap_free_bytes"]

    if material.has_window:
        launch_ts, end_ts = material.launch_ts, material.end_ts
        baseline_window = [row for row in samples if launch_ts - WINDOW_SECONDS <= row["t"] < launch_ts]
        run_window = [row for row in samples if launch_ts <= row["t"] <= end_ts]
        post_window = [row for row in samples if end_ts < row["t"] <= end_ts + WINDOW_SECONDS]
        metrics["window"] = {"launch_monotonic": launch_ts, "end_monotonic": end_ts,
                             "baseline_rows": len(baseline_window), "run_rows": len(run_window),
                             "post_rows": len(post_window)}
        if baseline_window:
            metrics["baseline_bytes"] = int(statistics.median(row["available_bytes"] for row in baseline_window))
        if run_window:
            metrics["min_bytes"] = min(row["available_bytes"] for row in run_window)
        if post_window:
            metrics["post_baseline_bytes"] = int(statistics.median(row["available_bytes"] for row in post_window))
        if metrics["baseline_bytes"] is not None and metrics["min_bytes"] is not None:
            metrics["delta_bytes"] = max(0, metrics["baseline_bytes"] - metrics["min_bytes"])
        if metrics["baseline_bytes"] is not None and metrics["post_baseline_bytes"] is not None:
            metrics["baseline_shift_bytes"] = abs(metrics["post_baseline_bytes"] - metrics["baseline_bytes"])
        # M00 §5 validity: no sampling hole over 500ms, delta>0, a stable baseline,
        # no swap. The cadence itself (≤100ms) is the sampler's protocol, reported
        # as `sampling_interval_seconds` — real hardware reads ~0.101s, which is
        # not a hole and must not be treated as one.
        metrics["measurement_valid"] = bool(
            metrics["baseline_bytes"] is not None
            and metrics["min_bytes"] is not None
            and metrics["post_baseline_bytes"] is not None
            and metrics["delta_bytes"] > 0
            and metrics["gaps_over_500ms"] == 0
            and metrics["sampling_interval_seconds"] is not None
            and metrics["baseline_shift_bytes"] <= BASELINE_SHIFT_LIMIT_BYTES
            and not metrics["swap_used"]  # M00 §5: any swap use fails the round
        )
        window_rows = run_window
        metrics["physical_upper_bound_window"] = "run_window"
    else:
        metrics["criteria_note"] = ("preserved material carries no monotonic window: the §5 criteria cannot be "
                                    "recomputed from it, so this round stays unverified")
        window_rows = samples
        metrics["physical_upper_bound_window"] = "all_raw_rows"

    if window_rows and all(row["mem_free_bytes"] is not None for row in window_rows):
        metrics["physical_upper_bound_bytes"] = system_nonfree_upper_bound_v1(
            MemorySample(mem_total_bytes=row["total_bytes"], mem_free_bytes=row["mem_free_bytes"],
                         mem_available_bytes=row["available_bytes"], sampled_at_monotonic=row["t"])
            for row in window_rows)
    else:
        metrics["bound_note"] = ("the raw samples do not carry MemFree: system_nonfree_upper_bound_v1 cannot be "
                                 "proven from this material and stays null (production stays blocked)")
    metrics["image_tokens_measured"] = _image_tokens_of(material.cases)
    return metrics


def _image_tokens_of(cases: Mapping) -> int | None:
    image = cases.get("image_max") if isinstance(cases.get("image_max"), dict) else {}
    summary = image.get("summary") if isinstance(image.get("summary"), dict) else {}
    for candidate in (image.get("image_tokens_measured"), summary.get("image_tokens_measured")):
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            return candidate
    return None


def verify_image_fact(metrics: Mapping[str, Any], *, envelope) -> None:
    """P21 AC4: the formal check is exact — the probe's 1.05 tolerance is probe-only."""
    tokens = metrics.get("image_tokens_measured")
    if tokens is None:
        raise CalibrationError(f"round {metrics['round']}: the image token fact is missing", semantic=True)
    if envelope is None:
        raise CalibrationError(f"round {metrics['round']}: no registered envelope to check the image fact against",
                               semantic=True)
    if tokens > envelope.max_image_tokens:
        raise CalibrationError(
            f"round {metrics['round']}: measured image tokens {tokens} exceed the registered envelope "
            f"{envelope.max_image_tokens} (no probe tolerance is carried into acceptance)", semantic=True)


def summarize_measurements(rounds: list[dict], *, budget_bytes: int) -> dict:
    """The traceable measurement: peaks, R, and the C02 physical upper bound."""
    deltas = [round_["delta_bytes"] or 0 for round_ in rounds]
    bounds = [round_["physical_upper_bound_bytes"] or 0 for round_ in rounds]
    measured_peak = max(deltas) if deltas else 0
    physical_peak = max(bounds) if bounds else 0
    verified = [round_ for round_ in rounds if round_["measurement_valid"]]
    unverified = [round_ for round_ in rounds if not round_["measurement_valid"]]
    stops_proven = all(round_["stop_quiescent"] for round_ in rounds)
    # An unproven figure stays null: zero would read like a measurement (P21 AC3).
    proven_peak = measured_peak if verified else None
    return {
        "runs": len(rounds),
        "verified_runs": len(verified),
        "unverified_runs": [round_["round"] for round_ in unverified],
        "measured_peak_bytes": proven_peak,
        "reserved_bytes": (measured_peak * 115 + 99) // 100 if verified else None,  # C02 exact integers
        "physical_resident_peak_bytes": physical_peak or None,
        "physical_upper_bound_method": "system_nonfree_upper_bound_v1",
        "temporary_budget_bytes": budget_bytes,
        "budget_exceeds_physical_bound": budget_bytes > physical_peak if physical_peak else None,
        "physical_bound_proven": physical_peak > 0,
        "stops_proven": stops_proven,
        # C02: without a proven physical bound production stays closed, the software
        # result is kept (P21 AC3).
        "verdict": "passed" if rounds and not unverified and stops_proven and physical_peak > 0 else "blocked",
    }


# ---------------------------------------------------------------------------
# maintenance and the CLI entry


def verify_maintenance(maintenance_path: Path, *, lock_path: Path, socket_path: Path | None,
                       containers: Callable[[], Sequence[str]],
                       port_busy: Callable[[], bool]) -> dict:
    """Re-verify live what the maintenance record merely claims (plan/08 §5)."""
    document = _json_file(maintenance_path, label="maintenance record")
    if set(document) != MAINTENANCE_KEYS:
        raise CalibrationError(f"{maintenance_path}: maintenance keys must be exactly {sorted(MAINTENANCE_KEYS)}",
                               semantic=False)
    for key in ("production_admission_closed", "instances_stopped"):
        if document.get(key) is not True:
            raise CalibrationError(f"{maintenance_path}: {key} is not declared true", semantic=True)
    if not isinstance(document.get("checked_by"), str) or not document["checked_by"].strip():
        raise CalibrationError(f"{maintenance_path}: checked_by is required", semantic=False)

    from ..instance_lock import InstanceLocked, acquire

    try:
        with acquire(lock_path):
            pass
    except InstanceLocked as exc:
        raise CalibrationError(f"a scheduler instance holds {lock_path}: {exc}", semantic=True) from exc
    except OSError as exc:
        raise CalibrationError(f"cannot take the instance lock {lock_path}: {exc}", semantic=True) from exc

    names = [str(name) for name in containers()]
    if names:
        raise CalibrationError(f"managed instances are still running during calibration: {names}", semantic=True)
    if socket_path is not None and socket_path.exists():
        raise CalibrationError(f"a control socket is present at {socket_path}: calibration requires a closed site",
                               semantic=True)
    if port_busy():
        raise CalibrationError("a registered model port is busy: calibration requires a closed site", semantic=True)
    return {"live_verified_utc": document.get("checked_utc"), "declared_by": document["checked_by"],
            "containers": names, "lock_path": str(lock_path)}


def run_calibration(*, config_path: Path, facts_path: Path, maintenance_path: Path, budget_bytes: int,
                    runs: int, output: Path, from_evidence: Path | None = None,
                    containers: Callable[[], Sequence[str]] | None = None,
                    port_busy: Callable[[], bool] | None = None) -> dict:
    """The CLI entry: validate inputs, verify the site, evaluate material, persist everything."""
    if isinstance(budget_bytes, bool) or not isinstance(budget_bytes, int) or budget_bytes <= 0:
        raise CalibrationError("--budget-bytes must be a positive integer (the temporary calibration budget)",
                               semantic=False)
    if runs < 1:
        raise CalibrationError("--runs must be a positive integer", semantic=False)

    facts_document = _json_file(facts_path, label="facts")
    try:
        device = parse_device_fact(facts_document.get("device"))
        parse_runtime_stack(facts_document.get("runtime_stack"))
    except ContractError as exc:
        raise CalibrationError(f"{facts_path}: {exc}", semantic=False) from exc
    if budget_bytes > device.mem_total_bytes:
        raise CalibrationError("--budget-bytes exceeds the recorded MemTotal: that budget cannot be honoured",
                               semantic=False)

    from ..config import ConfigError, load_config

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        raise CalibrationError(f"cannot load {config_path}: {exc}", semantic=False) from exc
    if getattr(config, "schema_version", None) != 2:
        raise CalibrationError("calibration requires a schema v2 configuration", semantic=False)

    if from_evidence is None:
        raise CalibrationError(
            "a fresh calibration executes the registered cases and needs the controlled runner; "
            "this build has no runner wired yet (P22/P29), provide --from-evidence with preserved raw material",
            semantic=True)

    directories = sorted(path for path in from_evidence.iterdir() if path.is_dir()) if from_evidence.is_dir() else []
    if not directories:
        raise CalibrationError(f"{from_evidence}: no round directories found", semantic=False)
    if len(directories) != runs:
        raise CalibrationError(
            f"{from_evidence}: {len(directories)} round(s) present but --runs says {runs}", semantic=False)

    materials = [load_round_material(directory) for directory in directories]
    # The image fact is checked against the registered VISION envelope (M00's model);
    # without one, any image token count is unproven and refused.
    vision_envelopes = [model.envelope for model in config.models.values() if "vision" in model.capabilities]
    envelope = vision_envelopes[0] if vision_envelopes else None
    metrics = []
    for material in materials:
        round_metrics = evaluate_round(material)
        if round_metrics["image_tokens_measured"] is not None:
            verify_image_fact(round_metrics, envelope=envelope)
        metrics.append(round_metrics)

    maintenance = verify_maintenance(
        maintenance_path,
        lock_path=Path(config.control.socket_path).parent / "scheduler.lock",
        socket_path=Path(config.control.socket_path) if Path(config.control.socket_path).exists() else None,
        containers=containers if containers is not None else _docker_instances(config),
        port_busy=port_busy if port_busy is not None else _registered_ports_busy(config),
    )

    output.mkdir(parents=True, exist_ok=True)
    raw_dir = output / "raw"
    raw_dir.mkdir(exist_ok=True)
    for material in materials:
        for path, _digest in material.raw_files:
            target = raw_dir / material.label / path.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())

    summary = summarize_measurements(metrics, budget_bytes=budget_bytes)

    measurement = {
        "schema_version": 1,
        "tool": "model_scheduler.acceptance calibrate",
        "config_sha256": _sha256_file(config_path),
        "facts_sha256": _sha256_file(facts_path),
        "maintenance_sha256": _sha256_file(maintenance_path),
        "maintenance": maintenance,
        "no_probe_tolerance": True,
        "rounds": metrics,
        "summary": summary,
    }
    (output / "measurements.json").write_text(json.dumps(measurement, indent=2, sort_keys=True) + "\n",
                                              encoding="utf-8")
    return summary


def run_live_calibration(*, config_path: Path, facts_path: Path, maintenance_path: Path, budget_bytes: int,
                         runs: int, output: Path, model_id: str | None = None, deployment_id: str | None = None,
                         container_runtime: str = "nvidia", driver=None,
                         clock: Callable[[], float] = time.monotonic,
                         sleep: Callable[[float], None] = time.sleep) -> dict:
    """The fresh calibration: launch the registered model, sample, stop with proof.

    The launch is the official lab path (`render_container_launch` with an explicit
    temporary budget), so a later candidate runs exactly what was measured here.
    The rounds land under `output/raw/runN/` and are evaluated by the same
    raw-derived §5/C02 code as preserved material — one accounting, no shortcut.
    """
    from ..contracts_v2 import DeploymentSpec
    from ..runtime_profiles import LaunchRenderError, render_container_launch
    from .live import LiveError, run_live_round

    if isinstance(budget_bytes, bool) or not isinstance(budget_bytes, int) or budget_bytes <= 0:
        raise CalibrationError("--budget-bytes must be a positive integer (the temporary calibration budget)",
                               semantic=False)
    if runs < 1:
        raise CalibrationError("--runs must be a positive integer", semantic=False)
    if not isinstance(deployment_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", deployment_id):
        raise CalibrationError("a fresh calibration requires --deployment-id ([a-z0-9][a-z0-9-]{0,63}): "
                               "the launch identity must be explicit", semantic=False)

    facts_document = _json_file(facts_path, label="facts")
    try:
        device = parse_device_fact(facts_document.get("device"))
        parse_runtime_stack(facts_document.get("runtime_stack"))
    except ContractError as exc:
        raise CalibrationError(f"{facts_path}: {exc}", semantic=False) from exc
    if budget_bytes > device.mem_total_bytes:
        raise CalibrationError("--budget-bytes exceeds the recorded MemTotal: that budget cannot be honoured",
                               semantic=False)

    from ..config import AppConfigV2, ConfigError, config_digest, load_config

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        raise CalibrationError(f"cannot load {config_path}: {exc}", semantic=False) from exc
    if not isinstance(config, AppConfigV2):
        raise CalibrationError("calibration requires a schema v2 configuration", semantic=False)
    import model_scheduler.config as config_module

    try:
        config_sha = config_digest(config_module._yaml(Path(config_path)))
    except (ConfigError, OSError, ValueError) as exc:
        raise CalibrationError(f"cannot digest {config_path}: {exc}", semantic=False) from exc

    # Identity first: the facts must describe this machine before any model moves.
    from .collect import FactsReader

    machine_id = hashlib.sha256(FactsReader().read_text("/etc/machine-id").strip().encode("utf-8")).hexdigest()
    if device.machine_id_sha256 != machine_id:
        raise CalibrationError("the facts were taken on a different machine: calibration refuses to measure here",
                               semantic=True)

    models = config.models
    if model_id is None:
        vision = [model for model in models.values() if "vision" in model.capabilities]
        if len(vision) != 1:
            raise CalibrationError("--model is required: the registration does not name exactly one vision model",
                                   semantic=False)
        model = vision[0]
    else:
        model = models.get(model_id)
        if model is None:
            raise CalibrationError(f"--model {model_id!r} is not registered", semantic=False)
    if model.envelope.max_image_tokens <= 0 or model.envelope.max_image_edge_pixels <= 0:
        raise CalibrationError(f"model {model.model_id!r} registers no image limits: its image fact cannot be proven",
                               semantic=False)

    deployment = DeploymentSpec(runtimes=tuple(config.runtimes.values()), models=tuple(models.values()))
    try:
        launch = render_container_launch(
            deployment, model.model_id, deployment_id=deployment_id,
            model_directory=config.storage.model_directory, config_sha256=config_sha, mode="lab",
            container_runtime=container_runtime, temporary_budget_bytes=budget_bytes)
    except LaunchRenderError as exc:
        raise CalibrationError(f"cannot render the lab launch for {model.model_id!r}: {exc}", semantic=False) from exc

    maintenance = verify_maintenance(
        maintenance_path,
        lock_path=Path(config.control.socket_path).parent / "scheduler.lock",
        socket_path=Path(config.control.socket_path) if Path(config.control.socket_path).exists() else None,
        containers=_docker_instances(config),
        port_busy=_registered_ports_busy(config),
    )

    output.mkdir(parents=True, exist_ok=True)
    raw_dir = output / "raw"
    raw_dir.mkdir(exist_ok=True)
    materials = []
    for index in range(1, runs + 1):
        try:
            directory = run_live_round(launch, output=raw_dir / f"run{index}", driver=driver,
                                       edge=model.envelope.max_image_edge_pixels, clock=clock, sleep=sleep)
        except LiveError as exc:
            raise CalibrationError(f"round {index}: {exc}", semantic=exc.semantic) from exc
        materials.append(load_round_material(directory))

    metrics = []
    for material in materials:
        round_metrics = evaluate_round(material)
        if round_metrics["image_tokens_measured"] is not None:
            verify_image_fact(round_metrics, envelope=model.envelope)
        metrics.append(round_metrics)

    summary = summarize_measurements(metrics, budget_bytes=budget_bytes)
    summary["model_id"] = model.model_id
    measurement = {
        "schema_version": 1,
        "tool": "model_scheduler.acceptance calibrate",
        "config_sha256": _sha256_file(config_path),
        "facts_sha256": _sha256_file(facts_path),
        "maintenance_sha256": _sha256_file(maintenance_path),
        "maintenance": maintenance,
        "no_probe_tolerance": True,
        "launch": {
            "deployment_id": launch.deployment_id,
            "model_id": launch.model_id,
            "runtime_id": launch.runtime_id,
            "profile_id": launch.profile_id,
            "image_digest": launch.image_digest,
            "mode": launch.mode,
            "temporary_budget_bytes": budget_bytes,
            "config_sha256": launch.config_sha256,
            "container_name": launch.container_name,
            "argv": list(launch.argv),
            "labels": dict(launch.labels),
        },
        "rounds": metrics,
        "summary": summary,
    }
    (output / "measurements.json").write_text(json.dumps(measurement, indent=2, sort_keys=True) + "\n",
                                              encoding="utf-8")
    return summary


def _docker_instances(config) -> Callable[[], Sequence[str]]:
    """Live container names matching any registered model id of this deployment."""
    import subprocess

    needles = set(config.models)

    def probe() -> Sequence[str]:
        try:
            result = subprocess.run(["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True,
                                    timeout=30)
        except (OSError, subprocess.SubprocessError):
            return []
        if result.returncode != 0:
            return []
        names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        return [name for name in names if any(needle in name for needle in needles)]

    return probe


def _registered_ports_busy(config) -> Callable[[], bool]:
    """A listening port of any registered model means the site is not closed."""
    import socket

    ports = sorted({model.port for model in config.models.values()})

    def probe() -> bool:
        for port in ports:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.2)
                if sock.connect_ex(("127.0.0.1", port)) == 0:
                    return True
        return False

    return probe
