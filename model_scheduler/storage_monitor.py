"""Validate that models are served from the configured mounted SSD only."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
from time import monotonic
from typing import Callable


class StorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelFile:
    inode: int
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class StorageSnapshot:
    ready: bool
    reason: str | None
    sampled_at: float
    files: dict[str, ModelFile]


class StorageMonitor:
    def __init__(self, mount_path: Path, model_directory: Path, expected_uuid: str, filesystem: str, *, runner: Callable[[list[str]], str] | None = None):
        self.mount_path = mount_path
        self.model_directory = model_directory
        self.expected_uuid = expected_uuid
        self.filesystem = filesystem
        self._runner = runner or self._run_findmnt
        self._baseline: dict[str, ModelFile] | None = None

    @staticmethod
    def _run_findmnt(args: list[str]) -> str:
        return subprocess.check_output(args, text=True, timeout=2)

    def check(self, models: dict[str, str]) -> StorageSnapshot:
        now = monotonic()
        try:
            data = json.loads(self._runner(["findmnt", "--json", "--target", str(self.mount_path)]))
            entries = data.get("filesystems")
            if not isinstance(entries, list) or len(entries) != 1:
                raise StorageError("mount_not_found")
            entry = entries[0]
            if entry.get("target") != str(self.mount_path) or entry.get("uuid") != self.expected_uuid or entry.get("fstype") != self.filesystem:
                raise StorageError("mount_identity_mismatch")
            files: dict[str, ModelFile] = {}
            for model_id, filename in models.items():
                candidate = self.model_directory / filename
                stat = os.lstat(candidate)
                if not candidate.is_file() or candidate.is_symlink() or not os.access(candidate, os.R_OK):
                    raise StorageError("model_file_unsafe")
                files[model_id] = ModelFile(stat.st_ino, stat.st_size, stat.st_mtime_ns)
            if self._baseline is not None and files != self._baseline:
                raise StorageError("model_file_changed")
            self._baseline = files
            return StorageSnapshot(True, None, now, files)
        except (OSError, ValueError, subprocess.SubprocessError, StorageError) as exc:
            return StorageSnapshot(False, str(exc), now, {})
