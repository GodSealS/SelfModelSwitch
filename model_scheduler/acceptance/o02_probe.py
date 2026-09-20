"""O02's probe: every fault happens inside this process' private mount namespace.

The launcher starts it as

    unshare --user --map-root-user --mount --propagation private \
        python -m model_scheduler.acceptance.o02_probe --config <path> --filesystem ext4

and then hands it one JSON action per line on stdin, reading one JSON result per
line on stdout. Nothing outside this namespace changes:

* the model disk is **never unmounted** — it is shadowed by a bind mount inside
  this namespace only, so the machine-wide disk keeps serving every other user;
* the scratch fault runs on a **dedicated tmpfs** with an explicit quota, never
  on the root filesystem;
* every write the probe performs is checked to live on a `tmpfs`, which is what
  `root_disk_writes: 0` means.

The verdicts come from the product's own `StorageMonitor`: the fault is real
only if the registered assets stop verifying, and recovery is real only if a
full re-hash pass succeeds again.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any

from ..config import ConfigError, load_config
from ..storage_monitor import StorageMonitor

WORKSPACE_BYTES = 256 * 1024 * 1024
FILL_BLOCK = 64 * 1024


def _mount(argv: list[str]) -> None:
    result = subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603
    if result.returncode != 0:
        raise RuntimeError(f"{argv[0]} failed: {result.stderr.strip() or result.stdout.strip()}")


def _umount(path: Path) -> None:
    result = subprocess.run(["umount", str(path)], capture_output=True, text=True, check=False)  # noqa: S603
    if result.returncode != 0:
        raise RuntimeError(f"umount {path} failed: {result.stderr.strip()}")


def _fstype(path: Path) -> str:
    result = subprocess.run(["findmnt", "-no", "FSTYPE", "--target", str(path)],  # noqa: S603
                            capture_output=True, text=True, check=False)
    return result.stdout.strip().splitlines()[0].strip() if result.stdout.strip() else ""


def _namespace_of(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/ns/mnt")
    except OSError:
        return ""


class Probe:
    """The namespace-local state machine behind O02's four steps."""

    def __init__(self, *, config_path: Path, filesystem: str, parent_pid: int) -> None:
        self._config_path = Path(config_path)
        self._filesystem = filesystem
        self._parent_namespace = _namespace_of(parent_pid)
        self._config = None
        self._workspace: Path | None = None
        self._shadowed = False
        self._scratch_mounted = False
        self._writes: list[Path] = []

    # -- helpers ---------------------------------------------------------

    def _loaded(self):
        if self._config is None:
            try:
                self._config = load_config(self._config_path)
            except ConfigError as exc:
                raise RuntimeError(f"cannot load {self._config_path}: {exc}") from exc
        return self._config

    def _monitor(self) -> StorageMonitor:
        config = self._loaded()
        storage = config.storage
        return StorageMonitor(storage.mount_path, storage.model_directory, storage.expected_uuid,
                              self._filesystem)

    def _model_directory(self) -> Path:
        return Path(self._loaded().storage.model_directory)

    def _workspace_dir(self) -> Path:
        if self._workspace is None:
            raise RuntimeError("the namespace has not been isolated yet")
        return self._workspace

    def _record_write(self, path: Path) -> None:
        self._writes.append(Path(path))

    def _writes_off_tmpfs(self) -> list[str]:
        """Every path this probe wrote, unless its filesystem really is a tmpfs."""
        return [str(path) for path in self._writes if _fstype(path.parent) != "tmpfs"]

    # -- the four O02 steps ----------------------------------------------

    def isolate(self) -> dict:
        workspace = Path(tempfile.mkdtemp(prefix="sms-o02-"))
        _mount(["mount", "-t", "tmpfs", "-o", f"size={WORKSPACE_BYTES}", "tmpfs", str(workspace)])
        self._workspace = workspace
        (workspace / "empty").mkdir()
        mine = _namespace_of(os.getpid())
        return {
            "private_mount_namespace": bool(mine) and mine != self._parent_namespace,
            "namespace": mine, "parent_namespace": self._parent_namespace,
            "unmounted_shared_disk": False,  # the disk is shadowed, never unmounted
            "workspace": str(workspace), "workspace_filesystem": _fstype(workspace),
        }

    def fail_model_disk(self) -> dict:
        directory = self._model_directory()
        empty = self._workspace_dir() / "empty"
        _mount(["mount", "--bind", str(empty), str(directory)])
        self._shadowed = True
        snapshot = self._monitor().verify_all(self._loaded().models)
        return {
            "model_disk_unavailable": snapshot.ready is False,
            "reason": snapshot.reason,
            "files_seen": len(snapshot.files),
            "shadow": str(directory),
            "root_disk_writes": len(self._writes_off_tmpfs()),
            "writes": [str(path) for path in self._writes],
        }

    def fill_scratch(self, quota_bytes: int) -> dict:
        scratch = self._workspace_dir() / "scratch"
        scratch.mkdir(exist_ok=True)
        _mount(["mount", "-t", "tmpfs", "-o", f"size={quota_bytes}", "tmpfs", str(scratch)])
        self._scratch_mounted = True
        payload = b"x" * FILL_BLOCK
        written, full = 0, False
        self._record_write(scratch / "fill.bin")
        with (scratch / "fill.bin").open("wb") as handle:
            while written <= quota_bytes * 2:
                try:
                    handle.write(payload)
                    written += len(payload)
                except OSError:  # ENOSPC: the dedicated quota is exhausted
                    full = True
                    break
        return {
            "dedicated_quota_fs": _fstype(scratch) == "tmpfs" and quota_bytes > 0,
            "scratch_full": full,
            "bytes_written": written,
            "quota_bytes": quota_bytes,
            "scratch_mount": str(scratch),
            "filesystem": _fstype(scratch),
            "root_disk_writes": len(self._writes_off_tmpfs()),
        }

    def recover(self) -> dict:
        directory = self._model_directory()
        if self._shadowed:
            _umount(directory)
            self._shadowed = False
        if self._scratch_mounted:
            _umount(self._workspace_dir() / "scratch")
            self._scratch_mounted = False
        snapshot = self._monitor().verify_all(self._loaded().models)  # a full re-hash pass
        return {
            "recovered": snapshot.ready is True,
            "reason": snapshot.reason,
            "rehashed": snapshot.ready is True and len(snapshot.files) > 0,
            "files": sorted(snapshot.files),
            "root_disk_writes": len(self._writes_off_tmpfs()),
        }

    def teardown(self) -> dict:
        """Best effort: unmount what is still mounted, then let the namespace exit."""
        if self._shadowed:
            try:
                _umount(self._model_directory())
            except RuntimeError:
                pass
            self._shadowed = False
        if self._scratch_mounted and self._workspace is not None:
            try:
                _umount(self._workspace / "scratch")
            except RuntimeError:
                pass
            self._scratch_mounted = False
        return {"torn_down": True}


ACTIONS = {"isolate": lambda probe, _payload: probe.isolate(),
           "fail_model_disk": lambda probe, _payload: probe.fail_model_disk(),
           "fill_scratch": lambda probe, payload: probe.fill_scratch(int(payload["quota_bytes"])),
           "recover": lambda probe, _payload: probe.recover(),
           "teardown": lambda probe, _payload: probe.teardown()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="O02's private-namespace storage fault probe")
    parser.add_argument("--config", required=True)
    parser.add_argument("--filesystem", required=True)
    parser.add_argument("--parent-pid", type=int, default=1)
    args = parser.parse_args(argv)
    probe = Probe(config_path=Path(args.config), filesystem=args.filesystem, parent_pid=args.parent_pid)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            command = json.loads(line)
            action = str(command.get("action") or "")
        except ValueError:
            _emit({"error": "the command is not JSON"})
            continue
        if action == "quit":
            _emit({"bye": True})
            return 0
        handler = ACTIONS.get(action)
        if handler is None:
            _emit({"error": f"unknown action {action!r}"})
            continue
        try:
            _emit(handler(probe, command))
        except Exception as exc:  # noqa: BLE001 - a refused step is material, not a crash
            _emit({"error": f"{type(exc).__name__}: {exc}"})
    return 0


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    raise SystemExit(main())
