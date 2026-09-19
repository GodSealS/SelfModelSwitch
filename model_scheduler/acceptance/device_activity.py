"""The device activity one B case needs: raw rows, never a boolean verdict.

A case may only be attributed when its own material says so (plan/06 §4): the
GPU really ran and the managed instance really mapped the CUDA libraries. The
evaluator recomputes exactly that later from `samples/tegrastats.jsonl` and
`samples/proc_maps.jsonl`, so the rows produced here are what reaches disk —
nothing is smoothed, averaged or summarised away.

Everything outside the process is injected: the tegrastats spawn, the container's
host PID and `/proc/<pid>/maps`. The real device only appears at the composition
root, and a missing tool yields missing rows, which leaves the case unknowable
instead of passed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

TEGRASTATS_ARGV: tuple[str, ...] = ("tegrastats", "--interval", "100")
STOP_GRACE_SECONDS = 5.0


class DeviceActivityError(RuntimeError):
    """The sampler refuses to claim activity it could not observe."""


class ActivitySampler(Protocol):
    """One sampling window: begun before the action, harvested afterwards."""

    def start(self, *, instance: Mapping[str, Any] | None = None) -> None: ...

    def stop(self) -> Sequence[Mapping[str, Any]]: ...


def _spawn(argv: Sequence[str], sink: Any) -> Any:
    import subprocess

    # The argv is fixed by this module and never shelled out to.
    return subprocess.Popen(list(argv), stdout=sink, stderr=subprocess.STDOUT)  # noqa: S603


def _wait(process: Any, seconds: float) -> None:
    process.wait(timeout=seconds)


def _container_pid(container_id: str) -> int | None:
    import subprocess

    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["docker", "inspect", "--format", "{{.State.Pid}}", container_id],
            capture_output=True, text=True, check=False, timeout=30)
        pid = int(result.stdout.strip().splitlines()[0])
    except (OSError, ValueError, IndexError):
        return None  # no docker, no PID, and therefore no borrowed evidence
    return pid if pid > 0 else None


def _read_proc_maps(pid: int) -> str:
    import pathlib

    try:
        return pathlib.Path(f"/proc/{pid}/maps").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""  # a process that vanished leaves no maps and supports no claim


def _discard(workspace: Any) -> None:
    try:
        Path(workspace).unlink(missing_ok=True)
    except (OSError, TypeError):
        pass


def _log_lines(workspace: Any) -> list[str]:
    try:
        return workspace.read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, TypeError, AttributeError):
        return []


class ManagedComputeSampler:
    """Sample one action's window: tegrastats rows plus the instance's maps.

    `spawn`, `pid_of`, `read_maps` and `wait_for` are the whole outside world, so
    the window can be exercised offline while the real run still observes the two
    sources the evaluator recomputes from.
    """

    def __init__(self, workspace: Any, *, spawn: Callable[[Sequence[str], Any], Any] = _spawn,
                 pid_of: Callable[[str], int | None] = _container_pid,
                 read_maps: Callable[[int], str] = _read_proc_maps,
                 wait_for: Callable[[Any, float], None] = _wait,
                 argv: Sequence[str] = TEGRASTATS_ARGV) -> None:
        self.workspace = workspace
        self._spawn = spawn
        self._pid_of = pid_of
        self._read_maps = read_maps
        self._wait_for = wait_for
        self._argv = tuple(argv)
        self._process: Any = None
        self._instance: Mapping[str, Any] | None = None
        self._available = True

    def start(self, *, instance: Mapping[str, Any] | None = None) -> None:
        self._instance = instance
        try:
            with self.workspace.open("wb") as sink:
                self._process = self._spawn(self._argv, sink)
        except OSError:
            # A device without tegrastats has nothing to attribute from: the window
            # still opens, but no rows will be invented here.
            self._available = False
            self._process = None

    def stop(self) -> Sequence[Mapping[str, Any]]:
        rows: list[Mapping[str, Any]] = []
        if self._process is not None:
            try:
                self._process.terminate()
                self._wait_for(self._process, STOP_GRACE_SECONDS)
            except Exception:  # noqa: BLE001 - an unwaitable sampler is still read
                self._process.kill()
            rows.extend({"kind": "tegrastats", "raw": line} for line in _log_lines(self.workspace) if line.strip())
            _discard(self.workspace)  # the captured file is scratch; the rows are the material
        container_id = None if self._instance is None else self._instance.get("container_id")
        if isinstance(container_id, str) and container_id:
            pid = self._pid_of(container_id)
            if isinstance(pid, int) and pid > 0:
                text = self._read_maps(pid)
                if text:
                    rows.append({"kind": "proc_maps", "raw": text})
        return tuple(rows)
