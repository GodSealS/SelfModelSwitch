"""Per-file asset identity and integrity checks for registered models (M02/P05).

The store answers with an explicit outcome rather than a boolean: a full pass that
opens and hashes every registered file under one deadline, a cheap metadata
observation for the runtime loop, and an UNKNOWN result when that deadline
expires so that no further asset is opened.

Everything is opened relative to directory file descriptors with `O_NOFOLLOW`,
so a symlinked parent, a replaced asset or a same-name directory living on
another disk all close admission instead of silently serving different bytes.
"""
from __future__ import annotations

import enum
import hashlib
import json
import os
from dataclasses import dataclass
import stat
import subprocess
from time import monotonic
from typing import Any, Callable, Mapping

READ_CHUNK_BYTES = 1024 * 1024
# Default from `storage.verify_timeout_seconds`; the whole set shares one budget.
DEFAULT_VERIFY_TIMEOUT_SECONDS = 900
# `findmnt --json` prints target/source/fstype/options by default and leaves the
# UUID out, so the identity check has to ask for it explicitly.
FINDMNT_COLUMNS = "TARGET,SOURCE,FSTYPE,UUID"


class AssetState(str, enum.Enum):
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    FAULT = "fault"
    UNKNOWN = "unknown"


class AssetFault(RuntimeError):
    """A fact that closes admission: the identity or integrity is untrustworthy."""


class AssetTimeout(RuntimeError):
    """The single verification deadline expired before the set was hashed."""


@dataclass(frozen=True)
class AssetEntry:
    """One registered asset path with whatever the registration promises about it."""

    role: str
    path: str
    sha256: str | None
    size_bytes: int | None


@dataclass(frozen=True)
class AssetFact:
    """Everything observed about one asset file, including its device identity."""

    role: str
    path: str
    inode: int
    size_bytes: int
    mtime_ns: int
    sha256: str | None


@dataclass(frozen=True)
class AssetSnapshot:
    state: AssetState
    reason: str | None
    sampled_at: float
    facts: Mapping[str, tuple[AssetFact, ...]]

    @property
    def ready(self) -> bool:
        return self.state is AssetState.VERIFIED


def _directory_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _file_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


def hash_file_descriptor(fd: int, tick: Callable[[], None]) -> str:
    """Hash an open descriptor, checking the shared deadline between reads."""
    digest = hashlib.sha256()
    while True:
        tick()
        chunk = os.read(fd, READ_CHUNK_BYTES)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)


def _as_entry(item: Any) -> AssetEntry:
    if isinstance(item, str):
        return AssetEntry("model", item, None, None)
    path = getattr(item, "path", None)
    if not isinstance(path, str) or not path:
        raise AssetFault("invalid_asset")
    role = getattr(item, "role", "model")
    sha256 = getattr(item, "sha256", None)
    size_bytes = getattr(item, "size_bytes", None)
    return AssetEntry(
        role if isinstance(role, str) and role else "model",
        path,
        sha256 if isinstance(sha256, str) else None,
        size_bytes if isinstance(size_bytes, int) and not isinstance(size_bytes, bool) else None,
    )


def normalize_assets(value: Any) -> tuple[AssetEntry, ...]:
    """Accept a v2 asset list, a v1 `(file, sha256)` pair, a filename or a v1 model."""
    if isinstance(value, str):
        return (AssetEntry("model", value, None, None),)
    if isinstance(value, tuple) and len(value) == 2 and all(isinstance(item, str) for item in value):
        return (AssetEntry("model", value[0], value[1], None),)
    if isinstance(value, (list, tuple)):
        return tuple(_as_entry(item) for item in value)
    filename = getattr(value, "file", None)
    if isinstance(filename, str):
        digest = getattr(value, "sha256", None)
        return (AssetEntry("model", filename, digest if isinstance(digest, str) else None, None),)
    assets = getattr(value, "assets", None)
    if isinstance(assets, (list, tuple)) and assets:
        return tuple(_as_entry(item) for item in assets)
    raise AssetFault("invalid_asset")


class AssetStore:
    """Verifies registered asset files against one mount identity and one deadline."""

    def __init__(
        self,
        mount_path: Any,
        model_directory: Any,
        expected_uuid: str,
        filesystem: str,
        *,
        runner: Callable[[list[str]], str] | None = None,
        clock: Callable[[], float] = monotonic,
        verify_timeout_seconds: int = DEFAULT_VERIFY_TIMEOUT_SECONDS,
        hasher: Callable[[int, Callable[[], None]], str] | None = None,
    ) -> None:
        if verify_timeout_seconds <= 0:
            raise ValueError("verify_timeout_seconds must be positive")
        self.mount_path = str(mount_path)
        self.model_directory = str(model_directory)
        self.expected_uuid = expected_uuid
        self.filesystem = filesystem
        self.verify_timeout_seconds = int(verify_timeout_seconds)
        self._runner = runner or self._run_findmnt
        self._clock = clock
        self._hasher = hasher or hash_file_descriptor
        self.state = AssetState.UNVERIFIED
        self._entries: dict[str, tuple[AssetEntry, ...]] = {}
        self._baseline: dict[str, tuple[AssetFact, ...]] | None = None

    @staticmethod
    def _run_findmnt(args: list[str]) -> str:
        return subprocess.check_output(args, text=True, timeout=2)

    @property
    def verified(self) -> bool:
        return self.state is AssetState.VERIFIED and self._baseline is not None

    @property
    def entries(self) -> Mapping[str, tuple[AssetEntry, ...]]:
        return dict(self._entries)

    def verify(self, assets: Mapping[str, Any] | None = None) -> AssetSnapshot:
        """Full pass: every registered asset is opened and hashed under one deadline."""
        if assets is not None:
            self._entries = {model_id: normalize_assets(value) for model_id, value in assets.items()}
        now = self._clock()
        deadline = now + self.verify_timeout_seconds

        def tick() -> None:
            if self._clock() > deadline:
                raise AssetTimeout("asset_verify_timeout")

        try:
            self._check_mount()
            facts = {
                model_id: tuple(self._hash_asset(entry, tick) for entry in entries)
                for model_id, entries in sorted(self._entries.items())
            }
        except AssetTimeout:
            self._baseline = None
            self.state = AssetState.UNKNOWN
            return AssetSnapshot(AssetState.UNKNOWN, "asset_verify_timeout", now, {})
        except AssetFault as exc:
            self._baseline = None
            self.state = AssetState.FAULT
            return AssetSnapshot(AssetState.FAULT, str(exc), now, {})
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            self._baseline = None
            self.state = AssetState.UNKNOWN
            return AssetSnapshot(AssetState.UNKNOWN, str(exc), now, {})
        self._baseline = facts
        self.state = AssetState.VERIFIED
        return AssetSnapshot(AssetState.VERIFIED, None, now, facts)

    def observe(self) -> AssetSnapshot:
        """Runtime check: mount identity plus inode/size/mtime, never a re-hash."""
        now = self._clock()
        baseline = self._baseline
        if baseline is None:
            return AssetSnapshot(self.state, "assets_unverified", now, {})
        try:
            self._check_mount()
            facts = {
                model_id: tuple(self._stat_asset(entry, known) for entry, known in zip(self._entries[model_id], knowns, strict=False))
                for model_id, knowns in sorted(baseline.items())
            }
        except AssetFault as exc:
            self._baseline = None
            self.state = AssetState.FAULT
            return AssetSnapshot(AssetState.FAULT, str(exc), now, {})
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            self._baseline = None
            self.state = AssetState.UNKNOWN
            return AssetSnapshot(AssetState.UNKNOWN, str(exc), now, {})
        self.state = AssetState.VERIFIED
        return AssetSnapshot(AssetState.VERIFIED, None, now, facts)

    # ------------------------------------------------------------------
    # facts

    def _check_mount(self) -> None:
        # `findmnt --json` does not print the UUID unless it is asked for by name
        # (util-linux 2.37 prints target/source/fstype/options only), and an
        # identity that is absent must never be treated as the expected one.
        data = json.loads(self._runner(["findmnt", "--json", "--output", FINDMNT_COLUMNS,
                                        "--target", self.mount_path]))
        entries = data.get("filesystems")
        if not isinstance(entries, list) or len(entries) != 1:
            raise AssetFault("mount_not_found")
        entry = entries[0]
        # A same-name directory left over on another disk reports a different target.
        if entry.get("target") != self.mount_path:
            raise AssetFault("mount_not_found")
        if entry.get("uuid") != self.expected_uuid or entry.get("fstype") != self.filesystem:
            raise AssetFault("mount_identity_mismatch")
        try:
            directory = os.lstat(self.model_directory)
        except OSError as exc:
            raise AssetFault("model_directory_unsafe") from exc
        if stat.S_ISLNK(directory.st_mode) or not stat.S_ISDIR(directory.st_mode):
            raise AssetFault("model_directory_unsafe")

    def _open_parent(self, parts: list[str]) -> int:
        """Walk intermediate directories one level at a time, never following links."""
        flags = _directory_flags()
        try:
            fd = os.open(self.model_directory, flags)
        except OSError as exc:
            raise AssetFault("model_directory_unsafe") from exc
        try:
            for name in parts:
                try:
                    info = os.lstat(name, dir_fd=fd)
                except OSError as exc:
                    raise AssetFault("asset_path_unsafe") from exc
                if stat.S_ISLNK(info.st_mode):
                    raise AssetFault("asset_path_unsafe")
                try:
                    child = os.open(name, flags, dir_fd=fd)
                except OSError as exc:
                    raise AssetFault("asset_path_unsafe") from exc
                os.close(fd)
                fd = child
            return fd
        except BaseException:
            os.close(fd)
            raise

    @staticmethod
    def _split(path: str) -> list[str]:
        if not path or path.startswith("/") or any(ord(char) < 0x20 for char in path):
            raise AssetFault("asset_path_unsafe")
        parts = path.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise AssetFault("asset_path_unsafe")
        return parts

    def _lstat_asset(self, parts: list[str]) -> os.stat_result:
        parent = self._open_parent(parts[:-1])
        try:
            info = os.lstat(parts[-1], dir_fd=parent)
        except OSError as exc:
            raise AssetFault("asset_unavailable") from exc
        finally:
            os.close(parent)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise AssetFault("asset_symlink")
        return info

    def _stat_asset(self, entry: AssetEntry, known: AssetFact) -> AssetFact:
        info = self._lstat_asset(self._split(entry.path))
        observed = (info.st_ino, info.st_size, info.st_mtime_ns)
        if observed != (known.inode, known.size_bytes, known.mtime_ns):
            raise AssetFault("asset_changed")
        return AssetFact(entry.role, entry.path, observed[0], observed[1], observed[2], known.sha256)

    def _hash_asset(self, entry: AssetEntry, tick: Callable[[], None]) -> AssetFact:
        # Checked before opening: once expired, no further asset may be opened.
        tick()
        parts = self._split(entry.path)
        info = self._lstat_asset(parts)
        parent = self._open_parent(parts[:-1])
        try:
            try:
                fd = os.open(parts[-1], _file_flags(), dir_fd=parent)
            except OSError as exc:
                raise AssetFault("asset_symlink") from exc
            try:
                before = os.fstat(fd)
                digest = self._hasher(fd, tick)
                after = os.fstat(fd)
            finally:
                os.close(fd)
        finally:
            os.close(parent)
        # Reading through the descriptor must describe the same object from start to end.
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise AssetFault("asset_replaced")
        if (info.st_ino, info.st_size, info.st_mtime_ns) != (before.st_ino, before.st_size, before.st_mtime_ns):
            raise AssetFault("asset_replaced")
        if entry.size_bytes is not None and before.st_size != entry.size_bytes:
            raise AssetFault("asset_size_mismatch")
        if entry.sha256 is not None and digest != entry.sha256:
            raise AssetFault("asset_hash_mismatch")
        return AssetFact(entry.role, entry.path, before.st_ino, before.st_size, before.st_mtime_ns, digest)
