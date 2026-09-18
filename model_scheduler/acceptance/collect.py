"""C09 site facts: device, runtime stack and disk identity with provenance (P21).

`collect` never starts a model (plan/08-execution-plan.md §5). Every value is
read through a named inspection of this site — a file, a command's stdout — and
the reading is hashed into `provenance`, so a candidate can always be traced
back to what was actually observed. A value that cannot be read is an input
error (exit 2), never a guess: the plan's §6 forbids filling device facts from
old templates or host assumptions.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
import re
import subprocess
from typing import Mapping, Protocol, Sequence

from ..evidence_contracts import DeviceFact, RuntimeStackFact

FACTS_SCHEMA = 1

_MEMTOTAL = re.compile(r"^MemTotal:\s+(\d+)\s*kB\s*$", re.MULTILINE)
_PRETTY_NAME = re.compile(r'^PRETTY_NAME="?([^"\n]+)"?\s*$', re.MULTILINE)
_CUDA_VERSION = re.compile(r"CUDA Version:\s*([0-9]+(?:\.[0-9]+)*)")
_UUIDISH = re.compile(r"[0-9A-Fa-f][0-9A-Fa-f-]{7,}")


class FactsError(RuntimeError):
    """A site fact could not be read: an input error, never a guessed value."""


class FactsReader(Protocol):
    """The site inspections `collect` is allowed to make."""

    def read_text(self, path: str) -> str: ...

    def run(self, argv: Sequence[str]) -> str: ...

    def machine(self) -> str: ...

    def kernel(self) -> str: ...

    def python(self) -> str: ...


class SystemFactsReader:
    """The real site: files, commands and the running platform."""

    def read_text(self, path: str) -> str:
        try:
            with open(path, "r", encoding="utf-8", errors="strict") as handle:
                return handle.read()
        except OSError as exc:
            raise FactsError(f"cannot read {path}: {exc}") from exc
        except UnicodeDecodeError as exc:
            raise FactsError(f"{path} is not valid UTF-8: {exc}") from exc

    def run(self, argv: Sequence[str]) -> str:
        try:
            result = subprocess.run(list(argv), capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            raise FactsError(f"command failed: {' '.join(argv)}: {exc}") from exc
        if result.returncode != 0:
            raise FactsError(f"command failed ({result.returncode}): {' '.join(argv)}")
        return result.stdout

    def machine(self) -> str:
        import platform

        return platform.machine()

    def kernel(self) -> str:
        import platform

        return platform.release()

    def python(self) -> str:
        import platform

        return platform.python_version()


@dataclass(frozen=True)
class Reading:
    """Where one fact came from and the digest of the raw reading behind it."""

    source: str
    value_sha256: str


@dataclass(frozen=True)
class CollectedFacts:
    device: DeviceFact
    runtime_stack: RuntimeStackFact
    provenance: Mapping[str, Reading]

    def document(self) -> dict:
        return {
            "schema_version": FACTS_SCHEMA,
            "device": asdict(self.device),
            "runtime_stack": asdict(self.runtime_stack),
            "provenance": {
                key: {"source": entry.source, "value_sha256": entry.value_sha256}
                for key, entry in sorted(self.provenance.items())
            },
        }


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _required_text(value: str, label: str) -> str:
    text = value.strip()
    if not text:
        raise FactsError(f"{label} is empty: the fact cannot be read and will not be guessed")
    return text


def _required_match(pattern: re.Pattern, text: str, label: str) -> str:
    match = pattern.search(text)
    if match is None:
        raise FactsError(f"{label} is not present in the reading")
    return match.group(1).strip() if match.groups() else match.group(0).strip()


def collect_facts(*, model_disk: str, scratch_disk: str, reader: FactsReader | None = None) -> CollectedFacts:
    """Read every C09 fact. A missing reading raises `FactsError` (exit 2)."""
    site: FactsReader = SystemFactsReader() if reader is None else reader
    provenance: dict[str, Reading] = {}

    def record(key: str, source: str, raw: str) -> None:
        provenance[key] = Reading(source=source, value_sha256=_digest(raw))

    machine_id_raw = site.read_text("/etc/machine-id")
    machine_id = hashlib.sha256(_required_text(machine_id_raw, "/etc/machine-id").encode("utf-8")).hexdigest()
    record("device.machine_id_sha256", "file:/etc/machine-id", machine_id_raw)

    model_blob = site.read_text("/proc/device-tree/model")
    compatible_blob = site.read_text("/proc/device-tree/compatible")
    device_tree = hashlib.sha256(b"\x00".join(
        (model_blob.encode("utf-8"), compatible_blob.encode("utf-8")))).hexdigest()
    record("device.device_tree_sha256", "file:/proc/device-tree/model+compatible", model_blob + "\x00" + compatible_blob)

    meminfo = site.read_text("/proc/meminfo")
    mem_total_kb = int(_required_match(_MEMTOTAL, meminfo, "MemTotal"))
    record("device.mem_total_bytes", "file:/proc/meminfo", meminfo)

    os_release = site.read_text("/etc/os-release")
    record("device.os_release", "file:/etc/os-release", os_release)

    tegra = site.read_text("/etc/nv_tegra_release")
    jetpack = _required_text(tegra.splitlines()[0] if tegra.splitlines() else "", "/etc/nv_tegra_release")
    record("device.jetpack_release", "file:/etc/nv_tegra_release", tegra)

    gpu_output = site.run(["nvidia-smi", "--query-gpu=name,uuid", "--format=csv,noheader"])
    gpu_identity = _required_text(gpu_output.splitlines()[0] if gpu_output.splitlines() else "", "nvidia-smi gpu")
    record("device.gpu_identity", "command:nvidia-smi --query-gpu=name,uuid --format=csv,noheader", gpu_output)

    docker_version = site.run(["docker", "--version"])
    record("device.container_runtime_version", "command:docker --version", docker_version)

    power_output = site.run(["nvpmodel", "-q"])
    power_mode = " ".join(part.strip() for part in power_output.splitlines() if part.strip())
    _required_text(power_mode, "nvpmodel -q")
    record("device.power_mode", "command:nvpmodel -q", power_output)

    governor = _required_text(site.read_text("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"), "cpu0 governor")
    record("device.clock_mode", "file:/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor", governor)

    def disk_uuid(path: str, label: str) -> str:
        source = f"command:findmnt -no UUID --target {path}"
        output = site.run(["findmnt", "-no", "UUID", "--target", path])
        record(label, source, output)
        text = output.strip()
        if not _UUIDISH.fullmatch(text):
            raise FactsError(f"no filesystem UUID for {path}: the disk identity cannot be read")
        return text

    model_disk_uuid = disk_uuid(model_disk, "device.model_disk_uuid")
    scratch_disk_uuid = disk_uuid(scratch_disk, "device.scratch_disk_uuid")

    smi_full = site.run(["nvidia-smi"])
    cuda_version = _required_match(_CUDA_VERSION, smi_full, "CUDA Version")
    record("runtime_stack.cuda_version", "command:nvidia-smi", smi_full)

    compute = _required_text(
        site.run(["nvidia-smi", "--query-gpu=compute_cap", "--format=csv,noheader"]), "nvidia-smi compute_cap")
    record("runtime_stack.compute_capability", "command:nvidia-smi --query-gpu=compute_cap --format=csv,noheader",
           compute)

    architecture = _required_text(site.machine(), "architecture")
    record("device.architecture", "platform:machine", architecture)
    kernel_release = _required_text(site.kernel(), "kernel release")
    record("device.kernel_release", "platform:kernel", kernel_release)
    record("runtime_stack.python_version", "platform:python", site.python())

    device = DeviceFact(
        machine_id_sha256=machine_id,
        architecture=architecture,
        device_tree_sha256=device_tree,
        mem_total_bytes=mem_total_kb * 1024,
        os_release=_required_match(_PRETTY_NAME, os_release, "PRETTY_NAME"),
        kernel_release=kernel_release,
        gpu_identity=gpu_identity,
        jetpack_release=jetpack,
        container_runtime_version=_required_text(docker_version, "docker --version"),
        power_mode=power_mode,
        clock_mode=governor,
        model_disk_uuid=model_disk_uuid,
        scratch_disk_uuid=scratch_disk_uuid,
    )
    stack = RuntimeStackFact(cuda_version=cuda_version, compute_capability=compute, python_version=site.python())
    return CollectedFacts(device=device, runtime_stack=stack, provenance=provenance)
