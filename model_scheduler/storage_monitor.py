"""Validate that models are served from the configured mounted SSD only.

The per-file identity and integrity work lives in `asset_store.AssetStore`: every
registered asset of a model is opened no-follow and hashed during the full
startup/recovery/release pass, while the per-second runtime check only compares
the mount identity with each file's inode, size and mtime. A deadline covers the
whole hashed set, and exceeding it leaves the result UNKNOWN instead of opening
more assets (M02/P05).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import monotonic
from typing import Any, Callable, Mapping

from .asset_store import (
    DEFAULT_VERIFY_TIMEOUT_SECONDS,
    AssetFault,
    AssetSnapshot,
    AssetStore,
    normalize_assets,
)


class StorageError(RuntimeError):
    """Legacy name kept for callers that catch it; the store now raises AssetFault."""


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
    def __init__(
        self,
        mount_path: Any,
        model_directory: Any,
        expected_uuid: str,
        filesystem: str,
        *,
        runner: Callable[[list[str]], str] | None = None,
        verify_timeout_seconds: int = DEFAULT_VERIFY_TIMEOUT_SECONDS,
        clock: Callable[[], float] = monotonic,
        hasher: Callable[[int, Callable[[], None]], str] | None = None,
    ):
        self.mount_path = mount_path
        self.model_directory = model_directory
        self.expected_uuid = expected_uuid
        self.filesystem = filesystem
        self.store = AssetStore(
            mount_path,
            model_directory,
            expected_uuid,
            filesystem,
            runner=runner,
            clock=clock,
            verify_timeout_seconds=verify_timeout_seconds,
            hasher=hasher,
        )
        self._clock = clock

    def check(self, models: Mapping[str, Any]) -> StorageSnapshot:
        """Admission check: hash the whole set once, then observe only its metadata."""
        return self._run(models, force=False)

    def verify_all(self, models: Mapping[str, Any]) -> StorageSnapshot:
        """Startup, recovery and release always re-hash every registered asset."""
        return self._run(models, force=True)

    def _run(self, models: Mapping[str, Any], *, force: bool) -> StorageSnapshot:
        now = self._clock()
        try:
            entries = {model_id: normalize_assets(value) for model_id, value in models.items()}
            unchanged = self.store.verified and entries == self.store.entries
            snapshot = self.store.observe() if not force and unchanged else self.store.verify(entries)
        except (OSError, ValueError, AssetFault) as exc:
            return StorageSnapshot(False, str(exc), now, {})
        return self._to_snapshot(snapshot)

    def _to_snapshot(self, snapshot: AssetSnapshot) -> StorageSnapshot:
        if not snapshot.ready:
            return StorageSnapshot(False, snapshot.reason, snapshot.sampled_at, {})
        files: dict[str, ModelFile] = {}
        for model_id, facts in snapshot.facts.items():
            primary = next((fact for fact in facts if fact.role == "model"), facts[0] if facts else None)
            if primary is not None:
                files[model_id] = ModelFile(primary.inode, primary.size_bytes, primary.mtime_ns, primary.sha256 or "")
        return StorageSnapshot(True, None, snapshot.sampled_at, files)


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
