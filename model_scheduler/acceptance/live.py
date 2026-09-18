"""Live calibration rounds: launch the registered model, sample, stop with proof.

The launch is the *official* lab path (`runtime_profiles.render_container_launch`
with an explicit temporary budget), so a calibrated window measures exactly what a
later candidate would run. Every outside action (Docker, the model's HTTP API,
the clock) goes through `LiveDriver`, so the round logic is testable without a GPU
and the target device is the only place real Docker appears.

A round writes the same shape `calibrate.load_round_material` already validates
(`sampling/meminfo.csv` + `round.json` with a monotonic window and a stop record),
so the §5 criteria and the C02 physical bound are recomputed from raw rows by the
existing, verified code path — a live round never gets its own private accounting.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import struct
import subprocess
import time
import urllib.error
import urllib.request
import zlib
from typing import Callable

from ..runtime_profiles import ContainerLaunch
from .calibrate import MemorySampler, WINDOW_SECONDS

READY_POLL_SECONDS = 1.0
STOP_TIMEOUT_SECONDS = 45.0
PROBE_SEED = 12345  # mirrored from the M00 probe so image-token counts stay comparable


class LiveError(RuntimeError):
    """A live-round refusal; `semantic` separates 'could not do it' from 'it failed'."""

    def __init__(self, message: str, *, semantic: bool = True) -> None:
        super().__init__(message)
        self.semantic = semantic


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)


def render_max_edge_png(edge: int) -> bytes:
    """The deterministic maximum-size image input, identical to the M00 probe's."""
    if edge < 1:
        raise LiveError("the image edge must be positive", semantic=False)
    raw = bytearray()
    for y in range(edge):
        raw.append(0)
        offset = (y * 255 // max(1, edge - 1)) & 0xFF
        for x in range(edge):
            value = ((x * 255 // max(1, edge - 1)) + offset) & 0xFF
            raw.extend((value, (value * 3) & 0xFF, (value * 7) & 0xFF))
    data = b"\x89PNG\r\n\x1a\n"
    data += _png_chunk(b"IHDR", struct.pack(">IIBBBBB", edge, edge, 8, 2, 0, 0, 0))
    data += _png_chunk(b"IDAT", zlib.compress(bytes(raw), 6))
    data += _png_chunk(b"IEND", b"")
    return data


def build_image_case_payload(image_edge: int) -> dict:
    """The E2 case: one max-edge image plus a one-token answer, exactly resumable."""
    import base64

    url = "data:image/png;base64," + base64.b64encode(render_max_edge_png(image_edge)).decode("ascii")
    return {
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "describe"},
            {"type": "image_url", "image_url": {"url": url}},
        ]}],
        "max_tokens": 1,
        "temperature": 0,
        "seed": PROBE_SEED,
        "cache_prompt": False,
        "stream": False,
        "ignore_eos": True,
    }


def classify_stop(exit_code: int | None, *, port_free: bool, reclaimed: bool, kill_used: bool) -> dict:
    """The stop record every exit keeps (quiescent only when all three hold)."""
    return {
        "exit_code": exit_code,
        "port_free": port_free,
        "reclaimed": reclaimed,
        "kill_used": kill_used,
        "graceful_stop": exit_code is not None and not kill_used,
        "quiescent": exit_code is not None and port_free and reclaimed,
    }


def port_in_use(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class LiveDriver:
    """The real driver: Docker plus the model's HTTP API. Tests substitute a fake."""

    def image_present(self, launch: ContainerLaunch) -> bool:
        result = subprocess.run(["docker", "image", "inspect", launch.image_digest], capture_output=True, text=True)
        return result.returncode == 0

    def start(self, launch: ContainerLaunch, *, log_path: Path):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = log_path.open("ab")
        return subprocess.Popen(list(launch.argv), stdout=handle, stderr=subprocess.STDOUT)

    def wait_ready(self, launch: ContainerLaunch, *, timeout: float, process=None) -> bool:
        deadline = time.monotonic() + timeout
        url = f"http://127.0.0.1:{launch.port}/health"
        while time.monotonic() < deadline:
            if process is not None and process.poll() is not None:
                return False
            try:
                with urllib.request.urlopen(url, timeout=2.0) as response:
                    if response.status == 200:
                        return True
            except (urllib.error.URLError, OSError, TimeoutError):
                pass
            time.sleep(READY_POLL_SECONDS)
        return False

    def image_tokens(self, launch: ContainerLaunch, *, edge: int) -> int | None:
        payload = json.dumps(build_image_case_payload(edge)).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{launch.port}/v1/chat/completions", data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=600.0) as response:
            document = json.loads(response.read().decode("utf-8"))
        usage = document.get("usage") if isinstance(document, dict) else None
        value = usage.get("prompt_tokens") if isinstance(usage, dict) else None
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def stop(self, launch: ContainerLaunch, *, process=None) -> dict:
        """Stop and *prove* the stop; anything unconfirmed is recorded as UNKNOWN."""
        kill_used = False
        try:
            result = subprocess.run(["docker", "stop", "--time", str(int(STOP_TIMEOUT_SECONDS)), launch.container_name],
                                    capture_output=True, text=True, timeout=STOP_TIMEOUT_SECONDS + 30)
            if result.returncode != 0:
                kill_used = subprocess.run(["docker", "kill", launch.container_name], capture_output=True,
                                           text=True).returncode == 0
        except (OSError, subprocess.SubprocessError) as exc:
            return {"unknown": True, "note": f"the stop could not be performed: {exc}"}
        exit_code: int | None = None
        if process is not None:
            try:
                exit_code = process.wait(timeout=STOP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                exit_code = None
                kill_used = True
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and port_in_use(launch.port):
            time.sleep(0.5)
        try:
            listed = subprocess.run(["docker", "ps", "-a", "--filter", f"label=io.self-model-switch.model={launch.model_id}",
                                     "--filter", f"label=io.self-model-switch.config-sha256={launch.config_sha256}",
                                     "--format", "{{.Names}}"], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            return {"unknown": True, "note": f"the stop could not be confirmed: {exc}", "exit_code": exit_code,
                    "kill_used": kill_used}
        if listed.returncode != 0:
            return {"unknown": True, "note": "docker ps failed: the stop cannot be confirmed",
                    "exit_code": exit_code, "kill_used": kill_used}
        residual = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
        return classify_stop(exit_code, port_free=not port_in_use(launch.port), reclaimed=not residual,
                             kill_used=kill_used) | ({"residual": residual} if residual else {})

    def running(self, launch: ContainerLaunch) -> list[str]:
        try:
            result = subprocess.run(["docker", "ps", "--filter", f"label=io.self-model-switch.config-sha256={launch.config_sha256}",
                                     "--format", "{{.Names}}"], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            raise LiveError(f"cannot list the managed containers: {exc}") from exc
        if result.returncode != 0:
            raise LiveError("docker ps failed: the site cannot be checked before a round")
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]


@dataclass
class RoundWindow:
    """Where one live round was sampled: needed by the raw material, nothing else."""

    launch_ts: float
    end_ts: float


def run_live_round(launch: ContainerLaunch, *, output: Path, driver: LiveDriver | None = None,
                   window: float = WINDOW_SECONDS, ready_timeout: float = 1800.0,
                   edge: int | None = None, sampler_factory: Callable[..., MemorySampler] = MemorySampler,
                   clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> Path:
    """One live round: settle, launch, measure, stop, and keep every raw figure."""
    driver = driver if driver is not None else LiveDriver()
    if not driver.image_present(launch):
        raise LiveError(f"the runtime image {launch.image_digest} is not present locally: calibration never pulls")
    residual = driver.running(launch)
    if residual:
        raise LiveError(f"a managed container is already running: {residual}", semantic=True)

    output.mkdir(parents=True, exist_ok=True)
    sampler = sampler_factory(output / "sampling" / "meminfo.csv")
    sampler.start()
    ready = False
    tokens: int | None = None
    process = None
    try:
        sleep(window)  # the pre window: the baseline is sampled *before* the launch
        launch_ts = clock()
        process = driver.start(launch, log_path=output / "container.log")
        ready = driver.wait_ready(launch, timeout=ready_timeout, process=process)
        if ready and edge is not None:
            tokens = driver.image_tokens(launch, edge=edge)
        end_ts = clock()
        stop = driver.stop(launch, process=process)
    finally:
        sampler.stop(post_seconds=window)
    document = {
        "model_id": launch.model_id,
        "container_name": launch.container_name,
        "image_digest": launch.image_digest,
        "mode": launch.mode,
        "profile_id": launch.profile_id,
        "port": launch.port,
        "ready": ready,
        "launch_ts": launch_ts,
        "end_ts": end_ts,
        "stop": stop,
        "stop_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cases": {"image_max": {"image_tokens_measured": tokens}} if tokens is not None else {},
    }
    (output / "round.json").write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output
