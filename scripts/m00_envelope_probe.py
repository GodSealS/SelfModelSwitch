#!/usr/bin/env python3
"""M00 maximum-envelope probe for the Qwen2.5-VL-7B candidate on the AGX Orin.

Controlled probe harness. It never starts the production scheduler service,
never writes to the model disk, and never downloads assets. It boots the
pinned llama.cpp server from the probe runtime, drives the envelope cases
defined in plan/m00-envelope.md, samples host and GPU telemetry, stops the
server, and keeps one evidence directory per run with the raw artifacts.

Run on the target machine (Python 3.10, standard library only):

    python3 scripts/m00_envelope_probe.py check
    python3 scripts/m00_envelope_probe.py run --runs 3
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import math
import os
import re
import signal
import socket
import statistics
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

PROBE_SCHEMA = 1

DEFAULT_MODEL = "/media/jtzn/sandisk-ext4/models/qwen25vl-7b-q4/Qwen_Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf"
DEFAULT_MMPROJ = "/media/jtzn/sandisk-ext4/models/qwen25vl-7b-q4/mmproj-Qwen_Qwen2.5-VL-7B-Instruct-bf16.gguf"
DEFAULT_LLAMA_SERVER = "/opt/self-model-switch/probes/llama.cpp-4bc272fd729bd094c0422e4b8353da8d2fec91f8/llama-server"
DEFAULT_EVIDENCE_ROOT = "/home/jtzn/self-model-switch-evidence"

EXPECTED_MODEL_SHA256 = "3f4513330aa7f109922bd701d773575484ae2b4a4090d6511260a2a4f8e3d069"
EXPECTED_MMPROJ_SHA256 = "d1c7588c0bdf6e7889737c01cfd54309240d54042e102275880a514ae979aea3"

EXPECTED_HOSTNAME = "jtzn-desktop"
BINARY_HASH_NAMES = ("llama-server", "llama-cli")
MEASUREMENT_GAP_SECONDS = 0.5
BASELINE_SHIFT_LIMIT_BYTES = 256 * 1024**2
RECLAIM_MARGIN_BYTES = 256 * 1024**2
INPUT_TOLERANCE_TOKENS = 32
SEED = 1234


class ProbeError(RuntimeError):
    pass


@dataclass(frozen=True)
class Envelope:
    ctx_size: int = 32768
    max_input_tokens: int = 28672
    max_output_tokens: int = 4096
    parallel: int = 2
    image_max_tokens: int = 1280
    image_edge_pixels: int = 1024

    def validate(self) -> None:
        if self.ctx_size < 512:
            raise ValueError("ctx_size is too small")
        if self.max_input_tokens <= 0 or self.max_output_tokens <= 0:
            raise ValueError("input and output budgets must be positive")
        if self.max_input_tokens + self.max_output_tokens > self.ctx_size:
            raise ValueError("max_input_tokens + max_output_tokens exceeds ctx_size")
        if self.parallel < 1:
            raise ValueError("parallel must be at least 1")
        if not 0 < self.image_max_tokens < self.max_input_tokens:
            raise ValueError("image_max_tokens must fit inside the input budget")
        if self.image_edge_pixels < 28:
            raise ValueError("image_edge_pixels is too small")


@dataclass(frozen=True)
class Paths:
    model: str = DEFAULT_MODEL
    mmproj: str = DEFAULT_MMPROJ
    llama_server: str = DEFAULT_LLAMA_SERVER
    evidence_root: str = DEFAULT_EVIDENCE_ROOT
    port: int = 18081
    host: str = "127.0.0.1"

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def evidence_dir_name(now_utc: str) -> str:
    stamp = now_utc.replace("-", "").replace(":", "")
    return f"m00-qwen25vl-envelope-{stamp}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_sha256(path: Path, expected: str) -> str:
    actual = sha256_file(path)
    if actual != expected:
        raise ValueError(f"SHA-256 mismatch for {path}: expected {expected}, got {actual}")
    return actual


def build_server_command(envelope: Envelope, paths: Paths) -> list[str]:
    return [
        paths.llama_server,
        "--model", paths.model,
        "--mmproj", paths.mmproj,
        "--parallel", str(envelope.parallel),
        "--kv-unified-per-slot", str(envelope.ctx_size),
        "--image-max-tokens", str(envelope.image_max_tokens),
        "--n-gpu-layers", "99",
        "--flash-attn", "auto",
        "--no-warmup",
        "--no-webui",
        "--host", paths.host,
        "--port", str(paths.port),
    ]


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)


def render_test_png(width: int, height: int, seed: int = 0) -> bytes:
    """Deterministic RGB gradient used as the maximum-size image input."""
    x_values = [x * 255 // max(1, width - 1) for x in range(width)]
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        offset = (y * 255 // max(1, height - 1) + seed) & 0xFF
        row = bytearray()
        for x in x_values:
            value = (x + offset) & 0xFF
            row.extend((value, (value * 3) & 0xFF, (value * 7) & 0xFF))
        raw += row
    data = b"\x89PNG\r\n\x1a\n"
    data += _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    data += _png_chunk(b"IDAT", zlib.compress(bytes(raw), 6))
    data += _png_chunk(b"IEND", b"")
    return data


def filler_text(token_count: int) -> str:
    return " word" * token_count


def build_completion_payload(token_ids: list[int], n_predict: int) -> dict:
    return {
        "prompt": list(token_ids),
        "n_predict": n_predict,
        "temperature": 0,
        "seed": SEED,
        "cache_prompt": False,
        "stream": False,
        "add_special": False,
        "ignore_eos": True,
    }


def build_chat_payload(filler: str, image_data_url: str, max_tokens: int) -> dict:
    return {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": filler},
                    {"type": "image_url", "image_url": {"url": image_data_url}},
                ],
            }
        ],
        "max_tokens": max_tokens,
        "temperature": 0,
        "seed": SEED,
        "cache_prompt": False,
        "stream": False,
        "ignore_eos": True,
    }


def build_chat_text_payload(filler: str, max_tokens: int) -> dict:
    """Text-only twin of build_chat_payload, used to isolate image token cost."""
    return {
        "messages": [{"role": "user", "content": filler}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "seed": SEED,
        "cache_prompt": False,
        "stream": False,
    }


def compute_memory_metrics(
    samples: list[dict],
    launch_ts: float,
    end_ts: float,
    baseline_seconds: float = 10.0,
    post_seconds: float = 10.0,
) -> dict:
    base = {
        "sample_count": len(samples),
        "baseline_bytes": None,
        "min_bytes": None,
        "delta_bytes": None,
        "post_baseline_bytes": None,
        "baseline_shift_bytes": None,
        "max_gap_seconds": None,
        "gaps_over_500ms": None,
        "swap_used": None,
        "measurement_valid": False,
    }
    if not samples:
        return base
    max_gap = 0.0
    gaps = 0
    for first, second in zip(samples, samples[1:]):
        gap = second["t"] - first["t"]
        max_gap = max(max_gap, gap)
        if gap > MEASUREMENT_GAP_SECONDS:
            gaps += 1
    start = samples[0]["t"]
    baseline_window = [s for s in samples if launch_ts - baseline_seconds <= s["t"] < launch_ts]
    run_window = [s for s in samples if launch_ts <= s["t"] <= end_ts]
    post_window = [s for s in samples if end_ts < s["t"] <= end_ts + post_seconds]
    swap_free_min = min(s["swap_free_bytes"] for s in samples)
    swap_free_first = samples[0]["swap_free_bytes"]
    result = dict(base)
    result.update(
        {
            "sample_count": len(samples),
            "window_start": start,
            "max_gap_seconds": round(max_gap, 4),
            "gaps_over_500ms": gaps,
            "swap_used": swap_free_min < swap_free_first,
        }
    )
    if baseline_window:
        result["baseline_bytes"] = int(statistics.median(s["available_bytes"] for s in baseline_window))
    if run_window:
        result["min_bytes"] = min(s["available_bytes"] for s in run_window)
    if post_window:
        result["post_baseline_bytes"] = int(statistics.median(s["available_bytes"] for s in post_window))
    if result["baseline_bytes"] is not None and result["min_bytes"] is not None:
        result["delta_bytes"] = max(0, result["baseline_bytes"] - result["min_bytes"])
    if result["baseline_bytes"] is not None and result["post_baseline_bytes"] is not None:
        result["baseline_shift_bytes"] = abs(result["post_baseline_bytes"] - result["baseline_bytes"])
    result["measurement_valid"] = bool(
        result["baseline_bytes"] is not None
        and result["min_bytes"] is not None
        and result["post_baseline_bytes"] is not None
        and result["delta_bytes"] > 0
        and gaps == 0
        and result["baseline_shift_bytes"] <= BASELINE_SHIFT_LIMIT_BYTES
    )
    return result


def summarize_runs(runs: list[dict], concurrency: int) -> dict:
    deltas = [run.get("memory", {}).get("delta_bytes") or 0 for run in runs]
    peak = max(deltas) if deltas else 0
    quiescent = sum(1 for run in runs if run.get("stop", {}).get("quiescent"))
    return {
        "runs_completed": len(runs),
        "quiescent_runs": quiescent,
        "all_quiescent": bool(runs) and quiescent == len(runs),
        "measured_peak_bytes": peak,
        "reserved_r_bytes": math.ceil(peak * 1.15),
        "concurrency": concurrency,
    }


def parse_tegrastats_line(line: str) -> dict | None:
    ram = re.search(r"RAM (\d+)/(\d+)MB", line)
    swap = re.search(r"SWAP (\d+)/(\d+)MB", line)
    gr3d = re.search(r"GR3D_FREQ (\d+)%", line)
    if not (ram and swap and gr3d):
        return None
    return {
        "ram_used_mb": int(ram.group(1)),
        "ram_total_mb": int(ram.group(2)),
        "swap_used_mb": int(swap.group(1)),
        "gr3d_freq_pct": int(gr3d.group(1)),
    }


def max_gr3d_freq(path: Path) -> int | None:
    if not path.is_file():
        return None
    peak: int | None = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parsed = parse_tegrastats_line(line)
        if parsed:
            value = parsed["gr3d_freq_pct"]
            peak = value if peak is None else max(peak, value)
    return peak


def cuda_library_mapped(pid: int) -> bool:
    try:
        maps = Path(f"/proc/{pid}/maps").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "libggml-cuda" in maps


def classify_stop(exit_code: int | None, port_free: bool, reclaimed: bool, kill_used: bool) -> dict:
    return {
        "exit_code": exit_code,
        "port_free": port_free,
        "reclaimed": reclaimed,
        "kill_used": kill_used,
        "graceful_stop": exit_code is not None and not kill_used,
        "quiescent": exit_code is not None and port_free and reclaimed,
    }


def read_meminfo() -> dict:
    values: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        key, _, rest = line.partition(":")
        parts = rest.strip().split()
        if parts:
            values[key] = int(parts[0]) * 1024
    return {
        "available_bytes": values.get("MemAvailable", 0),
        "total_bytes": values.get("MemTotal", 0),
        "swap_free_bytes": values.get("SwapFree", 0),
    }


def port_in_use(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def list_residual_llama_servers(server_path: str) -> list[int]:
    result = subprocess.run(["pgrep", "-f", "llama-server"], capture_output=True, text=True)
    residuals = []
    for token in result.stdout.split():
        try:
            pid = int(token)
        except ValueError:
            continue
        if pid in (os.getpid(), os.getppid()):
            continue
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if cmdline and cmdline[0].decode(errors="replace") == server_path:
            residuals.append(pid)
    return residuals


class Sampler:
    def __init__(self, run_dir: Path, interval: float = 0.1):
        self.interval = interval
        self.run_dir = run_dir
        self.samples: list[dict] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._tegrastats: subprocess.Popen | None = None
        self.tegrastats_ok = False

    def start(self) -> None:
        sampling_dir = self.run_dir / "sampling"
        sampling_dir.mkdir(parents=True, exist_ok=True)
        self._tegrastats_path = sampling_dir / "tegrastats.log"
        self._samples_path = sampling_dir / "meminfo.csv"
        try:
            handle = self._tegrastats_path.open("w", encoding="utf-8")
            self._tegrastats = subprocess.Popen(
                ["tegrastats", "--interval", "100"], stdout=handle, stderr=subprocess.STDOUT
            )
        except OSError:
            self._tegrastats = None
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        with self._samples_path.open("w", encoding="utf-8") as handle:
            handle.write("t_mono,utc,mem_available_bytes,mem_total_bytes,swap_free_bytes\n")
            while not self._stop.is_set():
                try:
                    sample = read_meminfo()
                except OSError:
                    sample = {"available_bytes": 0, "total_bytes": 0, "swap_free_bytes": 0}
                t = time.monotonic()
                self.samples.append({"t": t, **sample})
                handle.write(
                    f"{t:.3f},{utc_now()},{sample['available_bytes']},{sample['total_bytes']},{sample['swap_free_bytes']}\n"
                )
                handle.flush()
                self._stop.wait(self.interval)

    def tegrastats_sample(self) -> dict | None:
        if not self._tegrastats_path or not self._tegrastats_path.is_file():
            return None
        tail = self._tegrastats_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        for line in reversed(tail):
            parsed = parse_tegrastats_line(line)
            if parsed:
                self.tegrastats_ok = True
                return parsed
        return None

    def stop(self, post_seconds: float = 10.0) -> None:
        time.sleep(post_seconds)
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._tegrastats is not None:
            self._tegrastats.terminate()
            try:
                self._tegrastats.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._tegrastats.kill()


def http_get_json(url: str, timeout: float = 10.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def http_post_bytes(url: str, body: bytes, timeout: float) -> dict:
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def http_post_json(url: str, payload: dict, timeout: float) -> dict:
    return http_post_bytes(url, json.dumps(payload).encode("utf-8"), timeout)


def utc_stamp(monotonic_ts: float) -> str:
    delta = time.time() - time.monotonic()
    return datetime.fromtimestamp(monotonic_ts + delta, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def capture_env(paths: Paths, envelope: Envelope) -> dict:
    def run(command: list[str]) -> str:
        result = subprocess.run(command, capture_output=True, text=True)
        return result.stdout.strip() or result.stderr.strip()

    return {
        "hostname": run(["hostname"]),
        "uname": run(["uname", "-a"]),
        "tegra_release": run(["cat", "/etc/nv_tegra_release"]) if Path("/etc/nv_tegra_release").is_file() else None,
        "nvidia_smi": run(["nvidia-smi"]) if _which("nvidia-smi") else None,
        "disk": run(["df", "-h"]),
        "mounts": run(["mount"]),
        "meminfo": read_meminfo(),
        "server_command": build_server_command(envelope, paths),
        "port_busy": port_in_use(paths.host, paths.port),
    }


def _which(name: str) -> str | None:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def read_runtime_records(runtime_dir: Path) -> dict:
    records: dict[str, dict] = {}
    for name in ("RUNTIME-SHA256", "BUILD-METADATA"):
        path = runtime_dir / name
        if not path.is_file():
            continue
        entries = {}
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            match = re.match(r"([0-9a-f]{64})\s+(\S+)", line)
            if match:
                entries[Path(match.group(2)).name] = match.group(1)
        records[name] = entries
    return records


def runtime_anomalies(records: dict, actual: dict[str, str]) -> list[str]:
    anomalies = []
    for name, digest in actual.items():
        claims = {source: entries[name] for source, entries in records.items() if name in entries}
        if claims and not any(value == digest for value in claims.values()):
            detail = ", ".join(f"{source}={value}" for source, value in claims.items())
            anomalies.append(f"runtime hash mismatch for {name}: on-disk={digest}; records: {detail}")
        elif len(set(claims.values())) > 1:
            anomalies.append(f"runtime records disagree for {name}: {claims}")
    return anomalies


def detect_execution_device(log_path: Path) -> str | None:
    if not log_path.is_file():
        return None
    text = log_path.read_text(encoding="utf-8", errors="replace")
    if "CUDA0" in text:
        return "CUDA0"
    return None


def preflight(paths: Paths) -> dict:
    issues = []
    facts: dict = {"utc": utc_now()}
    for label, path, expected in (
        ("model", Path(paths.model), EXPECTED_MODEL_SHA256),
        ("mmproj", Path(paths.mmproj), EXPECTED_MMPROJ_SHA256),
        ("llama_server", Path(paths.llama_server), None),
    ):
        if not path.is_file():
            issues.append(f"missing {label}: {path}")
            continue
        digest = sha256_file(path)
        facts[f"{label}_sha256"] = digest
        if expected and digest != expected:
            issues.append(f"{label} SHA-256 mismatch: expected {expected}, got {digest}")
    runtime_dir = Path(paths.llama_server).parent
    records = read_runtime_records(runtime_dir)
    facts["runtime_records"] = records
    actual = {}
    for name in BINARY_HASH_NAMES:
        candidate = runtime_dir / name
        if candidate.is_file():
            actual[name] = sha256_file(candidate)
    facts["actual_binary_sha256"] = actual
    anomalies = runtime_anomalies(records, actual)
    facts["anomalies"] = anomalies
    uname = subprocess.run(["uname", "-m"], capture_output=True, text=True).stdout.strip()
    if uname != "aarch64" or not Path("/etc/nv_tegra_release").is_file():
        issues.append(f"target does not look like a Jetson device (uname -m={uname})")
    hostname = subprocess.run(["hostname"], capture_output=True, text=True).stdout.strip()
    facts["hostname"] = hostname
    if hostname != EXPECTED_HOSTNAME:
        facts.setdefault("warnings", []).append(f"hostname {hostname} differs from {EXPECTED_HOSTNAME}")
    if port_in_use(paths.host, paths.port):
        issues.append(f"port {paths.port} is already in use")
    tegrastats = _which("tegrastats")
    facts["tegrastats"] = tegrastats
    if not tegrastats:
        issues.append("tegrastats is not available")
    else:
        facts["tegrastats_line"] = sample_tegrastats_line(tegrastats)
        if facts["tegrastats_line"] is None:
            issues.append("tegrastats did not produce a parseable line within 2.5s")
    facts["issues"] = issues
    return facts


def sample_tegrastats_line(tegrastats: str) -> str | None:
    try:
        probe = subprocess.Popen(
            [tegrastats, "--interval", "100"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
    except OSError:
        return None
    try:
        time.sleep(2.5)
        probe.terminate()
        output, _ = probe.communicate(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        probe.kill()
        return None
    lines = (output or "").strip().splitlines()
    if not lines:
        return None
    if parse_tegrastats_line(lines[-1]) is None:
        return None
    return lines[-1]


def check_probe(args: argparse.Namespace) -> int:
    envelope = Envelope(
        ctx_size=args.ctx_size,
        max_input_tokens=args.max_input_tokens,
        max_output_tokens=args.max_output_tokens,
        parallel=args.parallel,
        image_max_tokens=args.image_max_tokens,
        image_edge_pixels=args.image_edge_pixels,
    )
    paths = Paths(
        model=args.model,
        mmproj=args.mmproj,
        llama_server=args.llama_server,
        evidence_root=args.evidence_root,
        port=args.port,
        host=args.host,
    )
    try:
        envelope.validate()
    except ValueError as exc:
        print(f"envelope invalid: {exc}", file=sys.stderr)
        return 2
    facts = preflight(paths)
    print(json.dumps(facts, indent=2, sort_keys=True))
    for warning in facts.get("warnings", []):
        print(f"warning: {warning}", file=sys.stderr)
    if facts["anomalies"]:
        print("runtime anomalies kept for review:", file=sys.stderr)
        for anomaly in facts["anomalies"]:
            print(f"  - {anomaly}", file=sys.stderr)
    if facts["issues"]:
        for issue in facts["issues"]:
            print(f"check failed: {issue}", file=sys.stderr)
        return 2
    print("check ok: assets, device, runtime records, port and tegrastats verified")
    print("plan: " + json.dumps({"runs": args.runs, "envelope": asdict(envelope), "server": build_server_command(envelope, paths)}))
    return 0


class ProbeRun:
    def __init__(self, index: int, envelope: Envelope, paths: Paths, evidence_dir: Path, case_timeout: float):
        self.index = index
        self.envelope = envelope
        self.paths = paths
        self.run_dir = evidence_dir / f"run{index}"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.case_timeout = case_timeout
        self.result: dict = {"run": index, "status": "failed", "failure_stage": "setup", "error": None, "cases": {}}
        self.sampler = Sampler(self.run_dir)
        self.proc: subprocess.Popen | None = None
        self.launch_ts: float | None = None
        self.end_ts: float | None = None
        self.image_data_url = "data:image/png;base64," + base64.b64encode(
            render_test_png(envelope.image_edge_pixels, envelope.image_edge_pixels, seed=7)
        ).decode("ascii")
        self.calibration_overhead: int | None = None
        self.template_overhead: int | None = None
        self.image_tokens_measured: int | None = None
        self.pre_launch_available: int | None = None

    # -- lifecycle ---------------------------------------------------------

    def execute(self) -> dict:
        self.sampler.start()
        self.result["started_utc"] = utc_now()
        print(f"[run {self.index}] sampling baseline before launch")
        time.sleep(10.0)
        self.pre_launch_available = read_meminfo()["available_bytes"]
        env = capture_env(self.paths, self.envelope)
        env["tegrastats_start"] = self.sampler.tegrastats_sample()
        write_json(self.run_dir / "env.json", env)
        try:
            self._start_server()
            self._run_cases()
            self.result["status"] = "completed"
            self.result["failure_stage"] = None
        except Exception as exc:  # evidence must survive any failure of a probe case
            self.result["error"] = f"{type(exc).__name__}: {exc}"
            print(f"[run {self.index}] failed: {self.result['error']}", file=sys.stderr)
        finally:
            try:
                self._stop_server()
            except Exception as exc:  # stop evidence must never be lost silently
                self.result["stop_error"] = f"{type(exc).__name__}: {exc}"
        self._finalize()
        return self.result

    def _start_server(self) -> None:
        if port_in_use(self.paths.host, self.paths.port):
            raise ProbeError(f"port {self.paths.port} is busy before launch")
        command = build_server_command(self.envelope, self.paths)
        (self.run_dir / "server.cmd").write_text(" ".join(command) + "\n", encoding="utf-8")
        log = (self.run_dir / "server.log").open("w", encoding="utf-8")
        self.proc = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        self.launch_ts = time.monotonic()
        self.result["launch_utc"] = utc_now()
        print(f"[run {self.index}] launched pid={self.proc.pid}, waiting for readiness")
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise ProbeError(f"server exited early with code {self.proc.returncode}")
            try:
                health = http_get_json(f"{self.paths.base_url}/health", timeout=5)
                if health.get("status") == "ok":
                    break
            except (urllib.error.URLError, ConnectionError, OSError, json.JSONDecodeError):
                pass
            time.sleep(1.0)
        else:
            raise ProbeError("server did not become ready within 900s")
        props = http_get_json(f"{self.paths.base_url}/props", timeout=10)
        slots = http_get_json(f"{self.paths.base_url}/slots", timeout=10)
        self.result["ready_utc"] = utc_now()
        self.result["load_seconds"] = round(time.monotonic() - self.launch_ts, 3)
        self.result["slots"] = self._verify_slots(slots)
        self.result["props_n_ctx"] = (props.get("default_generation_settings") or {}).get("n_ctx")
        self.result["cuda_library_mapped"] = cuda_library_mapped(self.proc.pid)
        if not self.result["slots"]["ok"]:
            raise ProbeError(f"slot context below envelope: {self.result['slots']}")
        print(f"[run {self.index}] ready in {self.result['load_seconds']}s, slots={self.result['slots']}")

    def _verify_slots(self, slots_payload) -> dict:
        slot_list = slots_payload if isinstance(slots_payload, list) else slots_payload.get("slots", [])
        contexts = []
        for slot in slot_list:
            n_ctx = slot.get("n_ctx")
            if n_ctx is None:
                n_ctx = (slot.get("params") or {}).get("n_ctx")
            contexts.append(n_ctx)
        ok = (
            len(slot_list) >= self.envelope.parallel
            and all(isinstance(value, int) and value >= self.envelope.ctx_size for value in contexts)
        )
        return {"slot_count": len(slot_list), "slot_ctx": contexts, "ok": bool(ok)}

    def _stop_server(self) -> None:
        stop: dict = {"signaled_utc": utc_now()}
        if self.proc is None:
            self.end_ts = time.monotonic()
            self.sampler.stop(10.0)
            self.result["stop"] = classify_stop(None, True, False, False) | stop
            return
        self.proc.send_signal(signal.SIGTERM)
        kill_used = False
        exit_code = None
        try:
            exit_code = self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            kill_used = True
            self.proc.kill()
            try:
                exit_code = self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                exit_code = None
        port_free = False
        for _ in range(10):
            if not port_in_use(self.paths.host, self.paths.port):
                port_free = True
                break
            time.sleep(0.5)
        residuals = list_residual_llama_servers(self.paths.llama_server)
        baseline = self.pre_launch_available if self.pre_launch_available is not None else self._baseline_now()
        reclaimed = self._wait_reclaim(baseline, timeout=10.0)
        self.end_ts = time.monotonic()
        self.sampler.stop(10.0)
        stop.update(
            {
                "exit_code": exit_code,
                "kill_used": kill_used,
                "port_free": port_free,
                "reclaimed": reclaimed,
                "residual_pids": residuals,
                "stopped_utc": utc_now(),
                "tegrastats_stop": self.sampler.tegrastats_sample(),
            }
        )
        self.result["stop"] = classify_stop(exit_code, port_free, reclaimed, kill_used) | stop

    def _baseline_now(self) -> int:
        return read_meminfo()["available_bytes"]

    def _wait_reclaim(self, baseline: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._baseline_now() >= baseline - RECLAIM_MARGIN_BYTES:
                return True
            time.sleep(0.5)
        return False

    def _finalize(self) -> None:
        if self.launch_ts is not None:
            end = self.end_ts if self.end_ts is not None else time.monotonic()
            self.result["memory"] = compute_memory_metrics(self.sampler.samples, self.launch_ts, end)
        if self.proc is not None:
            self.result["server_exit_code"] = self.proc.returncode
        gr3d_peak = max_gr3d_freq(self.run_dir / "sampling" / "tegrastats.log")
        cuda_mapped = bool(self.result.get("cuda_library_mapped"))
        self.result["execution_device_evidence"] = {
            "cuda_library_mapped": cuda_mapped,
            "gr3d_peak_pct": gr3d_peak,
            "n_gpu_layers": 99,
            "log_device_marker": detect_execution_device(self.run_dir / "server.log"),
        }
        self.result["execution_device"] = "CUDA0" if cuda_mapped and (gr3d_peak or 0) >= 50 else None
        self.result["compliance"] = self._compliance()
        self.result["finished_utc"] = utc_now()
        write_json(self.run_dir / "run.json", self.result)
        server_log = self.run_dir / "server.log"
        if server_log.is_file() and server_log.stat().st_size > 4 * 1024**2:
            with server_log.open("rb") as source, gzip.open(str(server_log) + ".gz", "wb", compresslevel=6) as sink:
                while True:
                    block = source.read(1024 * 1024)
                    if not block:
                        break
                    sink.write(block)
            server_log.unlink()
        print(f"[run {self.index}] status={self.result['status']} compliance={self.result['compliance']}")

    def _compliance(self) -> dict:
        cases = self.result["cases"]
        text = cases.get("text_max") or {}
        image = cases.get("image_max") or {}
        concurrent = cases.get("concurrent_max") or {}
        concurrent_requests = concurrent.get("requests", [])
        image_tokens = image.get("image_tokens_measured")
        checks = {
            "text_max_input_exact": text.get("prompt_tokens") == self.envelope.max_input_tokens,
            "text_max_output_completed": (text.get("completion_tokens") or 0) >= self.envelope.max_output_tokens,
            "image_max_consumed": isinstance(image_tokens, int) and image_tokens > 0,
            "image_within_budget": isinstance(image_tokens, int)
            and image_tokens <= self.envelope.image_max_tokens * 1.05,
            "concurrent_inputs_ok": len(concurrent_requests) == self.envelope.parallel
            and all(
                abs((request.get("prompt_tokens") or 0) - self.envelope.max_input_tokens) <= INPUT_TOLERANCE_TOKENS
                for request in concurrent_requests
            ),
            "concurrent_outputs_completed": len(concurrent_requests) == self.envelope.parallel
            and all(
                (request.get("completion_tokens") or 0) >= self.envelope.max_output_tokens
                for request in concurrent_requests
            ),
            "send_skew_ms_ok": (concurrent.get("send_skew_ms") or 0) <= 1000,
        }
        checks["all_ok"] = all(checks.values())
        return checks

    # -- cases -------------------------------------------------------------

    def _run_cases(self) -> None:
        self._run_calibration()
        self._run_text_max()
        self._run_image_max()
        self._run_concurrent_max()

    def _record_case(self, name: str, payload: dict) -> None:
        self.result["cases"][name] = payload
        write_json(self.run_dir / f"case-{name}.json", payload)
        print(f"[run {self.index}] case {name}: " + json.dumps(payload.get("summary", payload))[:400])

    def _tokenize(self, text: str) -> list[int]:
        response = http_post_json(f"{self.paths.base_url}/tokenize", {"content": text}, timeout=300)
        tokens = response.get("tokens")
        if not isinstance(tokens, list) or not tokens:
            raise ProbeError(f"tokenize returned no tokens: {str(response)[:200]}")
        return tokens

    def _filler_tokens(self, target: int) -> list[int]:
        count = target
        for _ in range(4):
            tokens = self._tokenize(filler_text(count))
            if len(tokens) == target:
                return tokens
            count = max(1, round(target * target / len(tokens)))
        raise ProbeError(f"cannot calibrate filler text to {target} tokens")

    def _run_calibration(self) -> None:
        started = time.monotonic()
        filler_tokens = len(self._tokenize(filler_text(100)))
        image_response = http_post_json(
            f"{self.paths.base_url}/v1/chat/completions",
            build_chat_payload(filler_text(100), self.image_data_url, max_tokens=1),
            timeout=self.case_timeout,
        )
        text_response = http_post_json(
            f"{self.paths.base_url}/v1/chat/completions",
            build_chat_text_payload(filler_text(100), max_tokens=1),
            timeout=self.case_timeout,
        )
        prompt_with_image = (image_response.get("usage") or {}).get("prompt_tokens")
        prompt_text_only = (text_response.get("usage") or {}).get("prompt_tokens")
        self.template_overhead = (prompt_text_only or 0) - filler_tokens
        self.calibration_overhead = (prompt_with_image or 0) - filler_tokens
        self.image_tokens_measured = None
        if prompt_with_image is not None and prompt_text_only is not None:
            self.image_tokens_measured = prompt_with_image - prompt_text_only
        self._record_case(
            "calibration",
            {
                "started_utc": utc_stamp(started),
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "filler_tokens": filler_tokens,
                "prompt_tokens_with_image": prompt_with_image,
                "prompt_tokens_text_only": prompt_text_only,
                "template_overhead_tokens": self.template_overhead,
                "overhead_tokens": self.calibration_overhead,
                "image_tokens_measured": self.image_tokens_measured,
                "image_bytes": len(self.image_data_url),
                "timings": image_response.get("timings"),
                "summary": {
                    "image_tokens_measured": self.image_tokens_measured,
                    "overhead_tokens": self.calibration_overhead,
                },
            },
        )

    def _run_text_max(self) -> None:
        envelope = self.envelope
        started = time.monotonic()
        tokens = self._filler_tokens(envelope.max_input_tokens)
        payload = build_completion_payload(tokens, envelope.max_output_tokens)
        response = http_post_json(f"{self.paths.base_url}/completion", payload, timeout=self.case_timeout)
        usage = response.get("usage") or {}
        timings = response.get("timings") or {}
        prompt_tokens = usage.get("prompt_tokens", timings.get("prompt_n", response.get("tokens_evaluated")))
        completion_tokens = usage.get("completion_tokens", timings.get("predicted_n", response.get("tokens_predicted")))
        self._record_case(
            "text_max",
            {
                "started_utc": utc_stamp(started),
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "finish_reason": response.get("stop_type") or response.get("finish_reason"),
                "timings": timings,
                "summary": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "predicted_per_second": timings.get("predicted_per_second"),
                },
            },
        )

    def _run_image_max(self) -> None:
        envelope = self.envelope
        started = time.monotonic()
        filler_tokens = 512
        payload = build_chat_payload(filler_text(filler_tokens), self.image_data_url, max_tokens=512)
        response = http_post_json(f"{self.paths.base_url}/v1/chat/completions", payload, timeout=self.case_timeout)
        usage = response.get("usage") or {}
        prompt_tokens = usage.get("prompt_tokens")
        image_tokens = None
        if prompt_tokens is not None and self.template_overhead is not None:
            image_tokens = prompt_tokens - filler_tokens - self.template_overhead
        self._record_case(
            "image_max",
            {
                "started_utc": utc_stamp(started),
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "filler_tokens": filler_tokens,
                "prompt_tokens": prompt_tokens,
                "image_tokens_measured": image_tokens,
                "completion_tokens": usage.get("completion_tokens"),
                "timings": response.get("timings"),
                "summary": {"prompt_tokens": prompt_tokens, "image_tokens_measured": image_tokens},
            },
        )

    def _run_concurrent_max(self) -> None:
        envelope = self.envelope
        if self.calibration_overhead is None:
            raise ProbeError("calibration did not produce an overhead estimate")
        target_input = envelope.max_input_tokens
        filler_target = target_input - self.calibration_overhead
        self._filler_tokens(filler_target)  # fail fast if the filler cannot be tokenized exactly
        started = time.monotonic()
        barrier = threading.Barrier(envelope.parallel)
        sends: list[float | None] = [None] * envelope.parallel
        responses: list[dict | None] = [None] * envelope.parallel
        errors: list[str | None] = [None] * envelope.parallel

        def worker(index: int) -> None:
            payload = build_chat_payload(filler_text(filler_target), self.image_data_url, max_tokens=envelope.max_output_tokens)
            body = json.dumps(payload).encode("utf-8")
            try:
                barrier.wait(timeout=60)
                sends[index] = time.monotonic()
                responses[index] = http_post_bytes(
                    f"{self.paths.base_url}/v1/chat/completions", body, timeout=self.case_timeout
                )
            except Exception as exc:  # worker threads must not die silently
                errors[index] = f"{type(exc).__name__}: {exc}"

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(envelope.parallel)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        send_skew_ms = None
        if all(value is not None for value in sends):
            send_skew_ms = round(abs(sends[0] - sends[1]) * 1000, 1)
        requests = []
        for index in range(envelope.parallel):
            response = responses[index] or {}
            usage = response.get("usage") or {}
            requests.append(
                {
                    "index": index,
                    "error": errors[index],
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                    "timings": response.get("timings"),
                    "sent_utc": utc_stamp(sends[index]) if sends[index] else None,
                }
            )
        self._record_case(
            "concurrent_max",
            {
                "started_utc": utc_stamp(started),
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "filler_tokens": filler_target,
                "target_input_tokens": target_input,
                "send_skew_ms": send_skew_ms,
                "requests": requests,
                "summary": {"send_skew_ms": send_skew_ms, "requests": len(requests)},
            },
        )


def unique_evidence_dir(root: Path, started_utc: str) -> Path:
    base = evidence_dir_name(started_utc)
    candidate = root / base
    suffix = 2
    while candidate.exists():
        candidate = root / f"{base}-{suffix}"
        suffix += 1
    candidate.mkdir(parents=True)
    return candidate


def run_probe(args: argparse.Namespace) -> int:
    envelope = Envelope(
        ctx_size=args.ctx_size,
        max_input_tokens=args.max_input_tokens,
        max_output_tokens=args.max_output_tokens,
        parallel=args.parallel,
        image_max_tokens=args.image_max_tokens,
        image_edge_pixels=args.image_edge_pixels,
    )
    paths = Paths(
        model=args.model,
        mmproj=args.mmproj,
        llama_server=args.llama_server,
        evidence_root=args.evidence_root,
        port=args.port,
        host=args.host,
    )
    try:
        envelope.validate()
    except ValueError as exc:
        print(f"envelope invalid: {exc}", file=sys.stderr)
        return 2
    facts = preflight(paths)
    for issue in facts["issues"]:
        print(f"preflight failed: {issue}", file=sys.stderr)
    if facts["issues"]:
        return 2
    started_utc = utc_now()
    root = Path(paths.evidence_root)
    root.mkdir(parents=True, exist_ok=True)
    evidence_dir = unique_evidence_dir(root, started_utc)
    print(f"evidence directory: {evidence_dir}")
    runs = []
    setup_failures = 0
    for index in range(1, args.runs + 1):
        run = ProbeRun(index, envelope, paths, evidence_dir, args.case_timeout)
        result = run.execute()
        runs.append(result)
        if result["status"] == "failed" and result.get("failure_stage") == "setup":
            setup_failures += 1
            if setup_failures >= 2:
                print("two consecutive setup failures; stopping the probe", file=sys.stderr)
                break
        else:
            setup_failures = 0
    summary = summarize_runs(runs, envelope.parallel)
    passed = (
        summary["all_quiescent"]
        and all(run["status"] == "completed" for run in runs)
        and all((run.get("compliance") or {}).get("all_ok") for run in runs)
        and all((run.get("memory") or {}).get("measurement_valid") for run in runs)
    )
    metadata = {
        "probe": "m00-qwen25vl-envelope",
        "schema": PROBE_SCHEMA,
        "started_utc": started_utc,
        "finished_utc": utc_now(),
        "config": {"envelope": asdict(envelope), "paths": asdict(paths)},
        "preflight": facts,
        "runs": runs,
        "summary": summary,
        "result": "passed" if passed else "not_passed",
        "anomalies": facts.get("anomalies", []) + collect_run_anomalies(runs),
    }
    write_json(evidence_dir / "metadata.json", metadata)
    print(json.dumps({"result": metadata["result"], "summary": summary, "evidence": str(evidence_dir)}, indent=2))
    return 0 if passed else 1


def collect_run_anomalies(runs: list[dict]) -> list[str]:
    anomalies = []
    for run in runs:
        index = run.get("run")
        if run.get("status") != "completed":
            anomalies.append(f"run {index} status={run.get('status')} error={run.get('error')}")
        memory = run.get("memory") or {}
        if memory.get("swap_used"):
            anomalies.append(f"run {index} swapped (swap_free dropped)")
        if memory.get("gaps_over_500ms"):
            anomalies.append(f"run {index} sampling gaps over 500ms: {memory['gaps_over_500ms']}")
        stop = run.get("stop") or {}
        if stop and not stop.get("quiescent"):
            anomalies.append(f"run {index} stop not quiescent: {stop}")
        if run.get("execution_device") != "CUDA0":
            anomalies.append(f"run {index} execution device not confirmed as CUDA0")
    return anomalies


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--mmproj", default=DEFAULT_MMPROJ)
    parser.add_argument("--llama-server", default=DEFAULT_LLAMA_SERVER, dest="llama_server")
    parser.add_argument("--evidence-root", default=DEFAULT_EVIDENCE_ROOT, dest="evidence_root")
    parser.add_argument("--port", type=int, default=18081)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--ctx-size", type=int, default=32768, dest="ctx_size")
    parser.add_argument("--max-input-tokens", type=int, default=28672, dest="max_input_tokens")
    parser.add_argument("--max-output-tokens", type=int, default=4096, dest="max_output_tokens")
    parser.add_argument("--parallel", type=int, default=2)
    parser.add_argument("--image-max-tokens", type=int, default=1280, dest="image_max_tokens")
    parser.add_argument("--image-edge-pixels", type=int, default=1024, dest="image_edge_pixels")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    check_parser = subparsers.add_parser("check", help="verify assets, device and runtime without running inference")
    _add_common(check_parser)
    check_parser.add_argument("--runs", type=int, default=3)
    run_parser = subparsers.add_parser("run", help="execute the envelope probe and keep evidence")
    _add_common(run_parser)
    run_parser.add_argument("--runs", type=int, default=3)
    run_parser.add_argument("--case-timeout", type=float, default=3600.0, dest="case_timeout")
    args = parser.parse_args(argv)
    if args.command == "check":
        return check_probe(args)
    return run_probe(args)


if __name__ == "__main__":
    raise SystemExit(main())
