"""Validate that models are served from the configured mounted SSD only."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
from time import monotonic
from typing import Any, Callable, Mapping


class StorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelFile:
    inode: int
    size: int
    mtime_ns: int
    sha256: str


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

    def check(self, models: Mapping[str, Any]) -> StorageSnapshot:
        now = monotonic()
        try:
            data = json.loads(self._runner(["findmnt", "--json", "--target", str(self.mount_path)]))
            entries = data.get("filesystems")
            if not isinstance(entries, list) or len(entries) != 1:
                raise StorageError("mount_not_found")
            entry = entries[0]
            if entry.get("target") != str(self.mount_path) or entry.get("uuid") != self.expected_uuid or entry.get("fstype") != self.filesystem:
                raise StorageError("mount_identity_mismatch")
            directory_stat = os.lstat(self.model_directory)
            if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(directory_stat.st_mode):
                raise StorageError("model_directory_unsafe")
            files: dict[str, ModelFile] = {}
            for model_id, value in models.items():
                filename, expected_hash = self._model_file(value)
                candidate = self.model_directory / filename
                file_stat = os.lstat(candidate)
                if not candidate.is_file() or candidate.is_symlink() or not os.access(candidate, os.R_OK):
                    raise StorageError("model_file_unsafe")
                digest = self._sha256(candidate)
                after = os.lstat(candidate)
                if (after.st_ino, after.st_size, after.st_mtime_ns) != (file_stat.st_ino, file_stat.st_size, file_stat.st_mtime_ns):
                    raise StorageError("model_file_changed")
                observed = ModelFile(file_stat.st_ino, file_stat.st_size, file_stat.st_mtime_ns, digest)
                if self._baseline is not None and self._baseline.get(model_id) != observed:
                    raise StorageError("model_file_changed")
                if expected_hash is not None and digest != expected_hash:
                    raise StorageError("model_hash_mismatch")
                files[model_id] = observed
            if self._baseline is not None and files != self._baseline:
                raise StorageError("model_file_changed")
            self._baseline = files
            return StorageSnapshot(True, None, now, files)
        except (OSError, ValueError, subprocess.SubprocessError, StorageError) as exc:
            return StorageSnapshot(False, str(exc), now, {})

    @staticmethod
    def _model_file(value: Any) -> tuple[str, str | None]:
        if isinstance(value, str):
            return value, None
        if isinstance(value, tuple) and len(value) == 2 and all(isinstance(item, str) for item in value):
            return value
        filename, digest = getattr(value, "file", None), getattr(value, "sha256", None)
        if isinstance(filename, str) and isinstance(digest, str):
            return filename, digest
        raise StorageError("invalid_model_file")

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()


class StorageAdmissionGuard:
    """Async scheduler port that fails closed on mount or model-file uncertainty."""

    def __init__(self, monitor: StorageMonitor, models: Mapping[str, Any], *, sample_interval_seconds: float = 1):
        if sample_interval_seconds <= 0:
            raise ValueError("sample_interval_seconds must be positive")
        self.monitor = monitor
        self.models = dict(models)
        self.sample_interval_seconds = sample_interval_seconds
        self._lock = asyncio.Lock()
        self._last_check_at = float("-inf")
        self._last_snapshot: StorageSnapshot | None = None

    async def __call__(self) -> bool:
        async with self._lock:
            now = monotonic()
            if self._last_snapshot is not None and now - self._last_check_at < self.sample_interval_seconds:
                return self._last_snapshot.ready
            self._last_snapshot = await asyncio.to_thread(self.monitor.check, self.models)
            self._last_check_at = now
            return self._last_snapshot.ready
