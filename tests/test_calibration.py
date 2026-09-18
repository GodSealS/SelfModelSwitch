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


def test_a_command_without_an_implementation_refuses_with_exit_2(capsys) -> None:
    from model_scheduler.acceptance.__main__ import main

    assert main(["candidate"]) == EXIT_INPUT
    assert "not implemented yet" in capsys.readouterr().err
