"""The device-activity window: only ever rows the evaluator can recompute."""

from __future__ import annotations

from pathlib import Path

import pytest

from model_scheduler.acceptance import device_activity as da

TEGRA_ROWS = ("RAM 1200/32000MB SWAP 0/16000MB GR3D_FREQ 41%",
              "RAM 3400/32000MB SWAP 0/16000MB GR3D_FREQ 87%")
CUDA_MAPS = "7f5c0aa00000-7f5c0ab00000 r-xp 00000000 08:01 393216 /usr/lib/aarch64-linux-gnu/libcudart.so.12"


class _FakeProcess:
    def __init__(self) -> None:
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout=None):
        return None

    def kill(self) -> None:
        self.killed = True


def _spawn_writing(text: str, process: _FakeProcess | None = None):
    """Stand in for tegrastats: whatever it writes during the window is the row set."""
    def spawn(command, sink):
        sink.write(text.encode("utf-8"))
        return process if process is not None else _FakeProcess()

    return spawn


def _sampler(tmp_path: Path, *, rows: tuple[str, ...] = TEGRA_ROWS, pid: int | None = 4321,
             maps: str = CUDA_MAPS, spawn=None, wait_for=None, process=None):
    return da.ManagedComputeSampler(tmp_path / "tegrastats.log", argv=("tegrastats",),
                                    spawn=spawn or _spawn_writing("\n".join(rows) + "\n", process),
                                    pid_of=lambda _container: pid, read_maps=lambda _pid: maps,
                                    wait_for=wait_for or (lambda target, seconds: target.wait(seconds)))


def test_a_window_produces_tegrastats_rows_and_the_instance_maps(tmp_path: Path) -> None:
    sampler = _sampler(tmp_path)

    sampler.start(instance={"container_id": "abc123"})
    rows = sampler.stop()

    assert [row["kind"] for row in rows] == ["tegrastats", "tegrastats", "proc_maps"]
    assert rows[1]["raw"] == TEGRA_ROWS[1] and rows[2]["raw"] == CUDA_MAPS


def test_maps_of_a_missing_instance_are_never_borrowed(tmp_path: Path) -> None:
    sampler = _sampler(tmp_path, pid=None)

    sampler.start(instance={"container_id": "abc123"})
    rows = sampler.stop()

    assert [row["kind"] for row in rows] == ["tegrastats", "tegrastats"]  # no maps, no claim


def test_a_window_without_an_instance_records_no_instance_rows(tmp_path: Path) -> None:
    sampler = _sampler(tmp_path)

    sampler.start()
    rows = sampler.stop()

    assert "proc_maps" not in [row["kind"] for row in rows]


def test_a_device_without_tegrastats_yields_no_rows_and_no_error(tmp_path: Path) -> None:
    def missing(command, sink):
        raise FileNotFoundError(command[0])

    sampler = da.ManagedComputeSampler(tmp_path / "tegrastats.log", argv=("tegrastats",), spawn=missing,
                                       pid_of=lambda _c: 4321, read_maps=lambda _p: CUDA_MAPS,
                                       wait_for=lambda process, seconds: None)

    sampler.start(instance={"container_id": "abc123"})
    rows = sampler.stop()

    # The maps still say which libraries the instance loaded; the missing GPU rows
    # leave the peak unknown, so no case can be declared passed on this material.
    assert [(row["kind"], row["raw"]) for row in rows] == [("proc_maps", CUDA_MAPS)]


def test_a_window_that_will_not_exit_is_killed_and_read(tmp_path: Path) -> None:
    process = _FakeProcess()

    def never_returns(target, seconds):
        raise TimeoutError(seconds)

    sampler = _sampler(tmp_path, wait_for=never_returns, process=process)

    sampler.start(instance={"container_id": "abc123"})
    rows = sampler.stop()

    assert process.terminated and process.killed  # a stuck sampler cannot swallow the case
    assert len(rows) == 3


def test_an_unopened_window_reports_nothing(tmp_path: Path) -> None:
    assert da.ManagedComputeSampler(tmp_path / "tegrastats.log").stop() == ()


def test_a_process_that_vanished_supports_no_claim() -> None:
    assert da._read_proc_maps(999999) == ""
    assert da._log_lines(None) == []
