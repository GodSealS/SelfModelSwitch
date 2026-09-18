"""P21: site facts collection and the calibration accounting (M06).

The tests drive the real code paths with injected readers/runners: a fact that
cannot be read is an input error (exit 2), never a guess, and every calibration
exit records either a proven stop or an explicit UNKNOWN.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import pytest

from model_scheduler.acceptance import EXIT_FAILED, EXIT_INPUT, EXIT_OK
from model_scheduler.acceptance.collect import FactsError, collect_facts
from model_scheduler import evidence_contracts as ec

MEMINFO = "MemTotal:       65536000 kB\nMemFree:        41943040 kB\nMemAvailable:   50331648 kB\n"

OS_RELEASE = 'NAME="Ubuntu"\nPRETTY_NAME="Ubuntu 22.04.4 LTS"\nVERSION_ID="22.04"\n'

NVIDIA_SMI = (
    "Orin (nvgpu), GPU-e6c84ee6-0000-0000-0000-000000000000, 8.7\n"
)

SMI_FULL = "NVIDIA-SMI 540.4.0    Driver Version: 540.4.0    CUDA Version: 12.6\n"


class FakeReader:
    """A scripted site: files, commands, and the platform strings the tool may use."""

    def __init__(self, *, files: dict[str, str], commands: dict[tuple, str]) -> None:
        self.files = files
        self.commands = commands
        self.commands_seen: list[tuple] = []

    def read_text(self, path: str) -> str:
        if path not in self.files:
            raise FactsError(f"cannot read {path}")
        return self.files[path]

    def run(self, argv) -> str:
        key = tuple(argv)
        self.commands_seen.append(key)
        if key not in self.commands:
            raise FactsError(f"command failed: {' '.join(argv)}")
        return self.commands[key]

    def machine(self) -> str:
        return "aarch64"

    def kernel(self) -> str:
        return "5.15.148-tegra"

    def python(self) -> str:
        return "3.12.14"


def _reader(**overrides) -> FakeReader:
    files = {
        "/etc/machine-id": "0123456789abcdef0123456789abcdef\n",
        "/proc/device-tree/model": "NVIDIA Jetson AGX Orin Developer Kit\x00",
        "/proc/device-tree/compatible": "nvidia,p3737-0000+p3701-0005\x00nvidia,tegra234\x00",
        "/proc/meminfo": MEMINFO,
        "/etc/os-release": OS_RELEASE,
        "/etc/nv_tegra_release": "# R36 (release), REVISION: 4.7, GCID: 40895960\n",
        "/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor": "schedutil\n",
    }
    commands = {
        ("nvidia-smi", "--query-gpu=name,uuid", "--format=csv,noheader"): NVIDIA_SMI,
        ("nvidia-smi",): SMI_FULL,
        ("nvidia-smi", "--query-gpu=compute_cap", "--format=csv,noheader"): "8.7\n",
        ("docker", "--version"): "Docker version 27.3.1, build ce12230\n",
        ("nvpmodel", "-q"): "MAXN\n",
        ("findmnt", "-no", "UUID", "--target", "/models"): "16d53274-d9f5-4282-9b44-7fdd43cba9ca\n",
        ("findmnt", "-no", "UUID", "--target", "/var/lib/self-model-switch"): "0f9b2c31-1111-2222-3333-444455556666\n",
    }
    files.update(overrides.pop("files", {}))
    commands.update(overrides.pop("commands", {}))
    return FakeReader(files=files, commands=commands)


def _collect(reader=None, **kwargs):
    return collect_facts(model_disk=kwargs.pop("model_disk", "/models"),
                         scratch_disk=kwargs.pop("scratch_disk", "/var/lib/self-model-switch"),
                         reader=reader or _reader())


def test_collect_records_every_c09_fact_from_a_named_source() -> None:
    reader = _reader()
    facts = _collect(reader)

    device = facts.device
    assert device.machine_id_sha256 == hashlib.sha256(b"0123456789abcdef0123456789abcdef").hexdigest()
    assert device.architecture == "aarch64"
    assert device.mem_total_bytes == 65536000 * 1024
    assert device.os_release == "Ubuntu 22.04.4 LTS"
    assert device.kernel_release == "5.15.148-tegra"
    assert "Orin" in device.gpu_identity and "GPU-e6c84ee6" in device.gpu_identity
    assert "R36" in device.jetpack_release
    assert device.container_runtime_version.startswith("Docker version 27.3.1")
    assert device.power_mode == "MAXN"
    assert device.clock_mode == "schedutil"
    assert device.model_disk_uuid == "16d53274-d9f5-4282-9b44-7fdd43cba9ca"
    assert device.scratch_disk_uuid == "0f9b2c31-1111-2222-3333-444455556666"

    stack = facts.runtime_stack
    assert stack.cuda_version == "12.6"
    assert stack.compute_capability == "8.7"
    assert stack.python_version == "3.12.14"

    # every field carries the source it was read from; nothing is recorded anonymously
    sources = {key: entry.source for key, entry in facts.provenance.items()}
    assert sources["device.machine_id_sha256"] == "file:/etc/machine-id"
    assert sources["device.power_mode"] == "command:nvpmodel -q"
    assert sources["device.model_disk_uuid"] == "command:findmnt -no UUID --target /models"
    assert sources["runtime_stack.cuda_version"] == "command:nvidia-smi"
    assert sources["device.architecture"] == "platform:machine"
    assert sources["device.kernel_release"] == "platform:kernel"
    assert len(facts.provenance) == 16  # 13 device fields + 3 stack fields: every field is traceable


def test_the_facts_document_round_trips_through_the_strict_c09_parsers() -> None:
    document = _collect().document()

    parsed_device = ec.parse_device_fact(document["device"])
    parsed_stack = ec.parse_runtime_stack(document["runtime_stack"])

    assert parsed_device.mem_total_bytes == 65536000 * 1024
    assert parsed_stack.compute_capability == "8.7"
    assert json.loads(json.dumps(document)) == document  # JSON-safe, no Path/bytes leakage


def test_collect_refuses_a_missing_reading_instead_of_guessing() -> None:
    reader = _reader(files={"/etc/nv_tegra_release": ""})

    with pytest.raises(FactsError):
        _collect(reader)


def test_collect_refuses_an_unresolvable_disk_uuid() -> None:
    reader = _reader(commands={("findmnt", "-no", "UUID", "--target", "/models"): ""})

    with pytest.raises(FactsError, match="UUID"):
        _collect(reader)


def test_collect_refuses_a_memory_total_that_is_not_a_number() -> None:
    reader = _reader(files={"/proc/meminfo": "MemTotal:       unknown kB\nMemFree: 1 kB\nMemAvailable: 1 kB\n"})

    with pytest.raises(FactsError):
        _collect(reader)


def test_device_tree_digest_covers_both_identity_blobs() -> None:
    reader = _reader()
    facts = _collect(reader)
    expected = hashlib.sha256(
        b"NVIDIA Jetson AGX Orin Developer Kit\x00" + b"\x00" + b"nvidia,p3737-0000+p3701-0005\x00nvidia,tegra234\x00"
    ).hexdigest()

    assert facts.device.device_tree_sha256 == expected


def test_the_cli_refuses_a_non_empty_output(tmp_path) -> None:
    from model_scheduler.acceptance.__main__ import main

    output = tmp_path / "facts.json"
    output.write_text("{}\n", encoding="utf-8")

    code = main(["collect", "--output", str(output), "--model-disk", "/models", "--scratch-disk", "/scratch"])

    assert code == EXIT_INPUT


def test_the_cli_requires_disk_inputs_instead_of_guessing(tmp_path, capsys) -> None:
    from model_scheduler.acceptance.__main__ import main

    code = main(["collect", "--output", str(tmp_path / "facts.json")])

    assert code == EXIT_INPUT
    assert "never guessed" in capsys.readouterr().err


def test_the_cli_writes_facts_that_parse_again(tmp_path, monkeypatch) -> None:
    from model_scheduler.acceptance import __main__ as cli

    monkeypatch.setattr(cli, "collect_facts", lambda **kwargs: _collect())
    output = tmp_path / "facts.json"

    code = cli.main(["collect", "--output", str(output), "--model-disk", "/models", "--scratch-disk", "/scratch"])

    assert code == EXIT_OK
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["schema_version"] == 1
    assert ec.parse_device_fact(document["device"]).power_mode == "MAXN"


def test_the_cli_derives_disk_locations_from_a_v2_config(tmp_path, monkeypatch) -> None:
    import importlib.util

    from model_scheduler.acceptance import __main__ as cli

    spec = importlib.util.spec_from_file_location("sms_v2_cal_config", Path(__file__).resolve().parent / "test_config.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(module.V2, encoding="utf-8")

    seen: dict = {}

    def fake_collect(*, model_disk, scratch_disk, reader=None):
        seen["paths"] = (model_disk, scratch_disk)
        return _collect()

    monkeypatch.setattr(cli, "collect_facts", fake_collect)
    code = cli.main(["collect", "--config", str(config_path), "--output", str(tmp_path / "facts.json")])

    assert code == EXIT_OK
    assert seen["paths"] == ("/mnt/model-ssd/models", "/var/lib/self-model-switch/blobs")


# ---------------------------------------------------------------------------
# calibrate: the §5 accounting, C02 physical bound, maintenance and CLI


from model_scheduler.acceptance import calibrate as cal  # noqa: E402

TOTAL = 65_536_000_000
BASELINE_AVAILABLE = 50_000_000_000
RUN_MIN_AVAILABLE = 44_700_000_000
BASELINE_FREE = 40_000_000_000
RUN_MIN_FREE = 34_000_000_000
SWAP_TOTAL = 4_000_000_000
LAUNCH, END = 1_000.0, 1_020.0


def _rows(*, interval: float = 0.1, gap: bool = False, shift: bool = False, swap: bool = False) -> list[tuple]:
    rows: list[tuple] = []

    def add(t: float, available: int, free: int, swap_free: int) -> None:
        rows.append((round(t, 3), available, free, swap_free))

    t = LAUNCH - cal.WINDOW_SECONDS
    while t < LAUNCH - 1e-9:
        add(t, BASELINE_AVAILABLE, BASELINE_FREE, SWAP_TOTAL)
        t = round(t + interval, 3)
    run_t = LAUNCH
    while run_t <= END + 1e-9:
        add(run_t, RUN_MIN_AVAILABLE, RUN_MIN_FREE, SWAP_TOTAL - 1 if swap else SWAP_TOTAL)
        run_t = round(run_t + interval, 3)
    post_t = round(END + interval, 3)
    while post_t <= END + cal.WINDOW_SECONDS + 1e-9:
        add(post_t, BASELINE_AVAILABLE - (300 * 1024**2 if shift else 0), BASELINE_FREE, SWAP_TOTAL)
        post_t = round(post_t + interval, 3)
    if gap:
        # drop six consecutive 100ms rows: one >500ms sampling hole
        rows = [row for row in rows if not (round(LAUNCH + 0.4, 3) - 1e-6 <= row[0] <= round(LAUNCH + 0.9, 3) + 1e-6)]
    return rows


def _csv(rows: list[tuple], *, with_free: bool = True) -> str:
    lines = [cal.MEMINFO_HEADER if with_free else cal.PROBE_MEMINFO_HEADER]
    for index, (t, available, free, swap_free) in enumerate(rows):
        columns = [f"{t:.3f}", f"2026-09-18T00:00:{index:02d}Z", str(available), str(TOTAL)]
        if with_free:
            columns.append(str(free))
        columns += [str(swap_free), "1000000"]
        lines.append(",".join(columns))
    return "\n".join(lines) + "\n"


def _write_round(root: Path, label: str, *, rows=None, with_free: bool = True, window: bool = True,
                 quiescent: bool = True, image_tokens: int = 1227, run_json: bool = False,
                 stop: dict | None = None) -> Path:
    directory = root / label
    (directory / "sampling").mkdir(parents=True, exist_ok=True)
    (directory / "sampling" / "meminfo.csv").write_text(_csv(rows if rows is not None else _rows(),
                                                             with_free=with_free), encoding="utf-8")
    document = {
        "stop": stop if stop is not None else {"quiescent": quiescent, "exit_code": 0, "port_free": True,
                                               "reclaimed": True, "kill_used": False},
        "cases": {"image_max": {"image_tokens_measured": image_tokens}},
    }
    if window:
        document |= {"launch_ts": LAUNCH, "end_ts": END}
    (directory / ("run.json" if run_json else "round.json")).write_text(json.dumps(document), encoding="utf-8")
    return directory


def test_a_verified_round_derives_the_c02_measurement_from_raw_rows(tmp_path) -> None:
    material = cal.load_round_material(_write_round(tmp_path, "round-1"))

    metrics = cal.evaluate_round(material)

    assert metrics["measurement_valid"] is True
    assert metrics["baseline_bytes"] == BASELINE_AVAILABLE
    assert metrics["delta_bytes"] == BASELINE_AVAILABLE - RUN_MIN_AVAILABLE
    assert metrics["swap_used"] is False and metrics["gaps_over_500ms"] == 0
    assert metrics["sampling_interval_seconds"] == 0.1  # the sampler cadence is reported, not a validity gate
    assert metrics["physical_upper_bound_bytes"] == TOTAL - RUN_MIN_FREE  # raw MemTotal - raw MemFree
    assert metrics["physical_upper_bound_window"] == "run_window"

    summary = cal.summarize_measurements([metrics], budget_bytes=16_000_000_000)
    assert summary["verdict"] == "passed"
    assert summary["measured_peak_bytes"] == BASELINE_AVAILABLE - RUN_MIN_AVAILABLE
    assert summary["reserved_bytes"] == (summary["measured_peak_bytes"] * 115 + 99) // 100
    assert summary["physical_resident_peak_bytes"] == TOTAL - RUN_MIN_FREE
    assert summary["physical_bound_proven"] is True


def test_a_sampling_gap_a_shift_and_swap_each_fail_the_round(tmp_path) -> None:
    gap = cal.evaluate_round(cal.load_round_material((_write_round(tmp_path, "gap", rows=_rows(gap=True)))))
    shift = cal.evaluate_round(cal.load_round_material((_write_round(tmp_path, "shift", rows=_rows(shift=True)))))
    swap = cal.evaluate_round(cal.load_round_material((_write_round(tmp_path, "swap", rows=_rows(swap=True)))))
    sparse = cal.evaluate_round(cal.load_round_material((_write_round(tmp_path, "sparse", rows=_rows(interval=0.6)))))

    assert gap["gaps_over_500ms"] == 1 and gap["measurement_valid"] is False
    assert shift["baseline_shift_bytes"] > cal.BASELINE_SHIFT_LIMIT_BYTES and shift["measurement_valid"] is False
    assert swap["swap_used"] is True and swap["measurement_valid"] is False
    assert sparse["gaps_over_500ms"] > 0 and sparse["measurement_valid"] is False
    assert cal.summarize_measurements([gap], budget_bytes=1)["verdict"] == "blocked"


def test_the_calibration_sampler_records_memfree_raw_rows(tmp_path) -> None:
    import time

    def fake_reader():
        return {"t": time.monotonic(), "utc": "2026-09-18T00:00:00Z", "available_bytes": 1000,
                "total_bytes": 2048, "mem_free_bytes": 500, "swap_free_bytes": 100, "cached_bytes": 10}

    sampler = cal.MemorySampler(tmp_path / "sampling.csv", interval=0.05, reader=fake_reader)
    sampler.start()
    time.sleep(0.3)
    sampler.stop(post_seconds=0)

    text = (tmp_path / "sampling.csv").read_text(encoding="utf-8")
    assert text.splitlines()[0] == cal.MEMINFO_HEADER
    assert len(sampler.rows) >= 2
    assert all(row["mem_free_bytes"] is not None for row in sampler.rows)  # C02 needs that raw field
    assert len(cal.load_meminfo_csv(tmp_path / "sampling.csv")) == len(sampler.rows)


def test_a_round_without_a_stop_or_unknown_record_is_refused(tmp_path) -> None:
    directory = _write_round(tmp_path, "round-1")
    document = json.loads((directory / "round.json").read_text())
    del document["stop"]
    (directory / "round.json").write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(cal.CalibrationError, match="stop or UNKNOWN") as refused:
        cal.load_round_material(directory)
    assert refused.value.semantic is True


def test_preserved_probe_material_without_memfree_blocks_the_physical_bound(tmp_path) -> None:
    directory = _write_round(tmp_path, "run-1", with_free=False, window=False, run_json=True)

    metrics = cal.evaluate_round(cal.load_round_material(directory))

    assert metrics["physical_upper_bound_bytes"] is None
    assert "MemFree" in metrics["bound_note"]
    assert metrics["measurement_valid"] is False and "no monotonic window" in metrics["criteria_note"]
    summary = cal.summarize_measurements([metrics], budget_bytes=16_000_000_000)
    assert summary["verdict"] == "blocked" and summary["physical_bound_proven"] is False


def test_an_over_envelope_image_fact_is_refused_without_the_probe_tolerance() -> None:
    class Envelope:
        max_image_tokens = 1280

    cal.verify_image_fact({"round": "round-1", "image_tokens_measured": 1280}, envelope=Envelope)  # exact boundary

    with pytest.raises(cal.CalibrationError, match="exceed the registered envelope") as refused:
        cal.verify_image_fact({"round": "round-1", "image_tokens_measured": 1281}, envelope=Envelope)
    assert refused.value.semantic is True  # 1344 (the probe's 1.05) would have passed: it must not


def test_maintenance_must_be_re_verified_live_not_only_declared(tmp_path) -> None:
    record = tmp_path / "maintenance.json"
    record.write_text(json.dumps({"production_admission_closed": True, "instances_stopped": True,
                                  "checked_utc": "2026-09-18T00:00:00Z", "checked_by": "jtzn", "notes": []}),
                      encoding="utf-8")
    lock = tmp_path / "scheduler.lock"

    report = cal.verify_maintenance(record, lock_path=lock, socket_path=None, containers=lambda: [],
                                    port_busy=lambda: False)
    assert report["declared_by"] == "jtzn"

    with pytest.raises(cal.CalibrationError, match="still running") as busy:
        cal.verify_maintenance(record, lock_path=lock, socket_path=None, containers=lambda: ["sms-qwen-small"],
                               port_busy=lambda: False)
    assert busy.value.semantic is True

    with pytest.raises(cal.CalibrationError, match="port is busy"):
        cal.verify_maintenance(record, lock_path=lock, socket_path=None, containers=lambda: [],
                               port_busy=lambda: True)

    closed = tmp_path / "not_closed.json"
    closed.write_text(json.dumps({"production_admission_closed": False, "instances_stopped": True,
                                  "checked_utc": "x", "checked_by": "jtzn", "notes": []}), encoding="utf-8")
    with pytest.raises(cal.CalibrationError, match="not declared true") as refused:
        cal.verify_maintenance(closed, lock_path=lock, socket_path=None, containers=lambda: [], port_busy=lambda: False)
    assert refused.value.semantic is True


def _site(tmp_path: Path, rounds: int = 3, *, vision: bool = True,
          with_free: bool = True) -> tuple[Path, Path, Path]:
    """A complete calibrate input set: v2 config (optionally vision), facts, maintenance."""
    import importlib.util

    import yaml

    spec = importlib.util.spec_from_file_location("sms_v2_cal_fixture", Path(__file__).resolve().parent / "test_config.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    document = yaml.safe_load(module.V2)
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    document["control"]["socket_path"] = str(run_dir / "control.sock")
    if vision:
        model = document["registration"]["models"][1]  # qwen-small
        model["capabilities"] = ["vision"]
        model["assets"].append({"role": "projector", "path": "mmproj.gguf", "sha256": "f" * 64, "size_bytes": 100})
        model["envelope"].update({"max_image_tokens": 1280, "max_image_edge_pixels": 1024, "max_images": 1})
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(document), encoding="utf-8")

    facts = _collect()
    facts_path = tmp_path / "facts.json"
    facts_path.write_text(json.dumps(facts.document()), encoding="utf-8")
    maintenance = tmp_path / "maintenance.json"
    maintenance.write_text(json.dumps({"production_admission_closed": True, "instances_stopped": True,
                                       "checked_utc": "2026-09-18T00:00:00Z", "checked_by": "jtzn", "notes": []}),
                           encoding="utf-8")
    evidence = tmp_path / "evidence"
    for index in range(1, rounds + 1):
        if with_free:
            _write_round(evidence, f"round-{index}")
        else:
            _write_round(evidence, f"run{index}", with_free=False, window=False, run_json=True)
    return config_path, facts_path, maintenance


def test_the_cli_keeps_preserved_material_but_exits_3_when_the_bound_is_unproven(tmp_path) -> None:
    config_path, facts_path, maintenance = _site(tmp_path, rounds=1, with_free=False)
    output = tmp_path / "calibration"

    from model_scheduler.acceptance.__main__ import main

    code = main(["calibrate", "--config", str(config_path), "--facts", str(facts_path),
                 "--maintenance", str(maintenance), "--budget-bytes", "16000000000", "--runs", "1",
                 "--output", str(output), "--from-evidence", str(tmp_path / "evidence")])

    assert code == EXIT_FAILED  # C02: an unprovable physical bound blocks, it never passes
    measurement = json.loads((output / "measurements.json").read_text(encoding="utf-8"))
    assert measurement["summary"]["verdict"] == "blocked"
    assert "MemFree" in measurement["rounds"][0]["bound_note"]  # the software result is kept and explains why


# ---------------------------------------------------------------------------
# calibrate, fresh path: the official lab launch, one accounting for both paths


from model_scheduler.acceptance import live as live_module  # noqa: E402


class _FakeLaunch:
    """Only the identity a round reads; the argv itself is rendered by the profiles."""

    argv = ("docker", "run", "--name", "sms-lab-qwen-small", "x")
    labels = {"io.self-model-switch.model": "qwen-small"}
    container_name = "sms-lab-qwen-small"
    deployment_id = "lab"
    model_id = "qwen-small"
    runtime_id = "llama-cpp-1"
    profile_id = "llama-cpp-gguf-v1"
    image_digest = "registry.example/sms-runtime@sha256:" + "a" * 64
    port = 18001
    mode = "lab"
    config_sha256 = "b" * 64
    model_directory = "/models"
    container_runtime = "nvidia"


class _FakeSampler:
    """Writes the whole series up front: the round logic never invents a sample."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(_csv(_rows()), encoding="utf-8")

    def stop(self, post_seconds: float = 0.0) -> None:
        return None


class _Clock:
    def __init__(self, value: float = LAUNCH - cal.WINDOW_SECONDS) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value = round(self.value + seconds, 3)


class _FakeDriver:
    def __init__(self, *, present: bool = True, tokens: int | None = 1227, residual: list[str] | None = None,
                 ready: bool = True, quiescent: bool = True) -> None:
        self.present, self.tokens, self.residual, self.ready, self.quiescent = present, tokens, residual or [], ready, quiescent
        self.clock: _Clock | None = None
        self.stopped = False

    def image_present(self, launch) -> bool:
        return self.present

    def running(self, launch) -> list[str]:
        return self.residual

    def start(self, launch, *, log_path):
        return object()

    def wait_ready(self, launch, *, timeout, process=None) -> bool:
        if self.ready and self.clock is not None:
            self.clock.advance(END - LAUNCH)  # the measured run window, not wall clock
        return self.ready

    def image_tokens(self, launch, *, edge) -> int | None:
        return self.tokens

    def stop(self, launch, *, process=None) -> dict:
        self.stopped = True
        return live_module.classify_stop(0, port_free=True, reclaimed=self.quiescent, kill_used=False)


def test_a_live_round_measures_the_window_and_records_a_proven_stop(tmp_path) -> None:
    clock = _Clock()
    driver = _FakeDriver()
    driver.clock = clock

    directory = live_module.run_live_round(_FakeLaunch(), output=tmp_path / "run1", driver=driver, edge=1024,
                                           sampler_factory=_FakeSampler, clock=clock, sleep=clock.advance)

    document = json.loads((directory / "round.json").read_text(encoding="utf-8"))
    assert (document["launch_ts"], document["end_ts"]) == (LAUNCH, END)
    assert document["stop"]["quiescent"] is True and document["image_digest"] == _FakeLaunch.image_digest
    assert document["cases"]["image_max"]["image_tokens_measured"] == 1227
    # The live round lands in the *same* accounting: §5 windows and the C02 bound.
    metrics = cal.evaluate_round(cal.load_round_material(directory))
    assert metrics["measurement_valid"] is True
    assert metrics["physical_upper_bound_bytes"] == TOTAL - RUN_MIN_FREE


def test_a_live_round_refuses_a_missing_image_or_a_busy_site(tmp_path) -> None:
    clock = _Clock()
    absent = _FakeDriver(present=False)
    absent.clock = clock

    with pytest.raises(live_module.LiveError, match="never pulls"):
        live_module.run_live_round(_FakeLaunch(), output=tmp_path / "a", driver=absent,
                                   sampler_factory=_FakeSampler, clock=clock, sleep=clock.advance)

    busy = _FakeDriver(residual=["sms-lab-qwen-small"])
    busy.clock = clock
    with pytest.raises(live_module.LiveError, match="already running"):
        live_module.run_live_round(_FakeLaunch(), output=tmp_path / "b", driver=busy,
                                   sampler_factory=_FakeSampler, clock=clock, sleep=clock.advance)


def _live_site(tmp_path, monkeypatch, *, rounds: int = 2):
    """A fresh-calibration site: v2 config, facts, maintenance, stubbed launch+rounds."""
    from model_scheduler.acceptance import collect as collect_module

    config_path, facts_path, maintenance = _site(tmp_path, rounds=rounds)

    class _Reader:
        def read_text(self, path: str) -> str:
            assert path == "/etc/machine-id"
            return "0123456789abcdef0123456789abcdef\n"

    monkeypatch.setattr(collect_module, "SystemFactsReader", _Reader)
    monkeypatch.setattr(cal, "verify_maintenance",
                        lambda *a, **k: {"live_verified_utc": "2026-09-18T00:00:00Z", "declared_by": "jtzn",
                                         "containers": [], "lock_path": str(tmp_path / "scheduler.lock")})
    monkeypatch.setattr("model_scheduler.runtime_profiles.render_container_launch",
                        lambda *a, **k: _FakeLaunch())

    def fake_round(launch, *, output, **kwargs):
        output.mkdir(parents=True, exist_ok=True)
        (output / "sampling").mkdir(exist_ok=True)
        (output / "sampling" / "meminfo.csv").write_text(_csv(_rows()), encoding="utf-8")
        (output / "round.json").write_text(json.dumps({
            "launch_ts": LAUNCH, "end_ts": END, "ready": True,
            "cases": {"image_max": {"image_tokens_measured": 1227}},
            "stop": {"quiescent": True, "exit_code": 0, "port_free": True, "reclaimed": True, "kill_used": False},
        }), encoding="utf-8")
        return output

    monkeypatch.setattr(live_module, "run_live_round", fake_round)
    return config_path, facts_path, maintenance


def test_the_cli_runs_a_fresh_calibration_through_the_official_launch(tmp_path, monkeypatch) -> None:
    config_path, facts_path, maintenance = _live_site(tmp_path, monkeypatch, rounds=2)
    output = tmp_path / "calibration"

    from model_scheduler.acceptance.__main__ import main

    code = main(["calibrate", "--config", str(config_path), "--facts", str(facts_path),
                 "--maintenance", str(maintenance), "--budget-bytes", "16000000000", "--runs", "2",
                 "--deployment-id", "sms-orin-lab", "--output", str(output)])

    assert code == EXIT_OK
    document = json.loads((output / "measurements.json").read_text(encoding="utf-8"))
    assert document["summary"]["verdict"] == "passed" and document["summary"]["model_id"] == "qwen-small"
    assert document["launch"]["image_digest"] == _FakeLaunch.image_digest
    assert document["launch"]["mode"] == "lab" and document["launch"]["temporary_budget_bytes"] == 16000000000
    assert document["rounds"][0]["physical_upper_bound_bytes"] == TOTAL - RUN_MIN_FREE
    assert (output / "raw" / "run1" / "sampling" / "meminfo.csv").is_file()


def test_the_live_identity_reader_is_concrete() -> None:
    """The bug the first target run found: a Protocol has no instance to read with."""
    from model_scheduler.acceptance.collect import SystemFactsReader

    reader = SystemFactsReader()  # instantiating the Protocol would raise TypeError
    assert callable(reader.read_text)


def test_a_fresh_calibration_requires_an_explicit_launch_identity(tmp_path, monkeypatch, capsys) -> None:
    config_path, facts_path, maintenance = _live_site(tmp_path, monkeypatch, rounds=1)

    from model_scheduler.acceptance.__main__ import main

    code = main(["calibrate", "--config", str(config_path), "--facts", str(facts_path),
                 "--maintenance", str(maintenance), "--budget-bytes", "16000000000", "--runs", "1",
                 "--output", str(tmp_path / "calibration")])

    assert code == EXIT_INPUT
    assert "--deployment-id" in capsys.readouterr().err
    assert not (tmp_path / "calibration" / "measurements.json").exists()


def test_a_fresh_calibration_refuses_another_machines_facts(tmp_path, monkeypatch, capsys) -> None:
    from model_scheduler.acceptance import collect as collect_module

    config_path, facts_path, maintenance = _live_site(tmp_path, monkeypatch, rounds=1)

    class _OtherReader:
        def read_text(self, path: str) -> str:
            return "ffffffffffffffffffffffffffffffff\n"

    monkeypatch.setattr(collect_module, "SystemFactsReader", _OtherReader)

    from model_scheduler.acceptance.__main__ import main

    code = main(["calibrate", "--config", str(config_path), "--facts", str(facts_path),
                 "--maintenance", str(maintenance), "--budget-bytes", "16000000000", "--runs", "1",
                 "--deployment-id", "sms-orin-lab", "--output", str(tmp_path / "calibration")])

    assert code == EXIT_FAILED  # identity before any model moves: a semantic refusal
    assert "different machine" in capsys.readouterr().err


def test_the_cli_recomputes_from_raw_material_and_writes_measurements(tmp_path) -> None:
    config_path, facts_path, maintenance = _site(tmp_path)
    evidence = tmp_path / "evidence"
    output = tmp_path / "calibration"

    from model_scheduler.acceptance.__main__ import main

    result = main(["calibrate", "--config", str(config_path), "--facts", str(facts_path),
                   "--maintenance", str(maintenance), "--budget-bytes", "16000000000", "--runs", "3",
                   "--output", str(output), "--from-evidence", str(evidence)])

    assert result == EXIT_OK
    measurement = json.loads((output / "measurements.json").read_text(encoding="utf-8"))
    assert measurement["summary"]["verdict"] == "passed"
    assert measurement["no_probe_tolerance"] is True
    assert len(measurement["rounds"]) == 3
    assert all(entry["physical_upper_bound_bytes"] == TOTAL - RUN_MIN_FREE for entry in measurement["rounds"])
    assert (output / "raw" / "round-1" / "meminfo.csv").is_file()  # raw material is copied, never re-summarised


def test_the_cli_refuses_an_evidence_count_that_disagrees_with_runs(tmp_path, capsys) -> None:
    config_path, facts_path, maintenance = _site(tmp_path, rounds=2)

    from model_scheduler.acceptance.__main__ import main

    code = main(["calibrate", "--config", str(config_path), "--facts", str(facts_path),
                 "--maintenance", str(maintenance), "--budget-bytes", "16000000000", "--runs", "3",
                 "--output", str(tmp_path / "calibration"), "--from-evidence", str(tmp_path / "evidence")])

    assert code == EXIT_INPUT
    assert "--runs says 3" in capsys.readouterr().err


def test_the_cli_blocks_when_the_site_is_not_really_closed(tmp_path, monkeypatch, capsys) -> None:
    config_path, facts_path, maintenance = _site(tmp_path, rounds=1)

    from model_scheduler.acceptance import calibrate as module

    original = module.verify_maintenance
    monkeypatch.setattr(module, "verify_maintenance",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            module.CalibrationError("managed instances are still running", semantic=True)))
    try:
        from model_scheduler.acceptance.__main__ import main

        code = main(["calibrate", "--config", str(config_path), "--facts", str(facts_path),
                     "--maintenance", str(maintenance), "--budget-bytes", "16000000000", "--runs", "1",
                     "--output", str(tmp_path / "calibration"), "--from-evidence", str(tmp_path / "evidence")])
    finally:
        monkeypatch.setattr(module, "verify_maintenance", original)

    assert code == EXIT_FAILED
    assert "still running" in capsys.readouterr().err
