"""Blob metadata and the single atomic quota accounting (M04/P11, C07).

The service owns one SQLite database that stores only transport facts:
`blob_id/owner/sha256/size_bytes/media_type/state/created_at/expires_at/path`.
No client business task is stored here.

Quota is checked once per write inside one `BEGIN IMMEDIATE` transaction over
`published + staging + reserved`, so two concurrent uploads cannot both read a
free-space figure and oversell; the disk free check is passed in by the caller
and evaluated inside the same transaction.

Every method is synchronous: the store runs them through a worker thread, so a
transaction never blocks the event loop.
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

RESERVED = "reserved"
STAGING = "staging"
PUBLISHED = "published"
DELETED = "deleted"
CORRUPT = "corrupt"
BLOB_STATES = frozenset({RESERVED, STAGING, PUBLISHED, DELETED, CORRUPT})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS blobs (
    blob_id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    sha256 TEXT,
    size_bytes INTEGER NOT NULL,
    media_type TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL,
    path TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS blobs_owner_state ON blobs (owner, state);
CREATE TABLE IF NOT EXISTS leases (
    blob_id TEXT NOT NULL,
    holder TEXT NOT NULL,
    PRIMARY KEY (blob_id, holder)
);
CREATE TABLE IF NOT EXISTS recovery (
    blob_id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    step TEXT NOT NULL,
    sha256 TEXT,
    size_bytes INTEGER NOT NULL,
    created_at REAL NOT NULL
);
"""

# The publish sequence is journalled so every crash point has one deterministic
# outcome: `staging` (bytes are being received), `publish_pending` (the file is
# about to be renamed), `published` (the metadata transaction committed).
STEP_STAGING = "staging"
STEP_PENDING = "publish_pending"
STEP_PUBLISHED = "published"
JOURNAL_STEPS = frozenset({STEP_STAGING, STEP_PENDING, STEP_PUBLISHED})
RESTART_HOLDER_PREFIX = "restart:"


class QuotaExceeded(RuntimeError):
    """The write cannot be accounted for; nothing readable may be left behind."""


@dataclass(frozen=True)
class BlobRecord:
    blob_id: str
    owner: str
    sha256: str | None
    size_bytes: int
    media_type: str
    state: str
    created_at: float
    expires_at: float | None
    path: str


@dataclass(frozen=True)
class OwnerUsage:
    published_bytes: int
    staging_bytes: int
    reserved_bytes: int

    @property
    def total_bytes(self) -> int:
        return self.published_bytes + self.staging_bytes + self.reserved_bytes


class BlobMetadata:
    """The service-owned metadata database; every call is one transaction."""

    def __init__(
        self,
        path: str | Path,
        *,
        owner_quota_bytes: int = 4 * 1024**3,
        global_quota_bytes: int = 16 * 1024**3,
        min_free_bytes: int = 2 * 1024**3,
        retention_seconds: float = 24 * 3600.0,
        tombstone_seconds: float = 24 * 3600.0,
        disk_free_bytes: Callable[[], int] | None = None,
    ) -> None:
        if min(owner_quota_bytes, global_quota_bytes, min_free_bytes) < 0 or retention_seconds <= 0 or tombstone_seconds <= 0:
            raise ValueError("invalid blob policy")
        self.owner_quota_bytes = owner_quota_bytes
        self.global_quota_bytes = global_quota_bytes
        self.min_free_bytes = min_free_bytes
        self.retention_seconds = retention_seconds
        self.tombstone_seconds = tombstone_seconds
        self._disk_free_bytes = disk_free_bytes
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        # The connection is used from worker threads (one at a time, guarded by
        # the lock), so it must not be bound to the creating thread.
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(str(path), isolation_level=None, timeout=30, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        with self._connection:
            self._connection.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

        # -- accounting ----------------------------------------------------------

    def usage(self, owner: str) -> OwnerUsage:
        with self._lock:
            row = self._connection.execute(
                "SELECT "
                "COALESCE(SUM(CASE WHEN state = ? THEN size_bytes END), 0) AS published, "
                "COALESCE(SUM(CASE WHEN state = ? THEN size_bytes END), 0) AS staging, "
                "COALESCE(SUM(CASE WHEN state = ? THEN size_bytes END), 0) AS reserved "
                "FROM blobs WHERE owner = ?",
                (PUBLISHED, STAGING, RESERVED, owner),
            ).fetchone()
            return OwnerUsage(row["published"], row["staging"], row["reserved"])

    def global_bytes(self) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COALESCE(SUM(size_bytes), 0) AS total FROM blobs WHERE state IN (?, ?, ?)",
                (PUBLISHED, STAGING, RESERVED),
            ).fetchone()
            return int(row["total"])

    def reserve(self, blob_id: str, owner: str, size_bytes: int, media_type: str, *, now: float, path: str) -> BlobRecord:
        """Reserve the whole declared size before a single byte is received."""
        with self._lock:
            if size_bytes <= 0:
                raise QuotaExceeded("a blob must declare a positive size")
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                if self.get(blob_id) is not None:
                    raise QuotaExceeded("duplicate blob id")
                owner_bytes = self.usage(owner).total_bytes
                global_bytes = self.global_bytes()
                if self._disk_free_bytes is not None and self._disk_free_bytes() < self.min_free_bytes + size_bytes:
                    raise QuotaExceeded("disk free space is below the reserve")
                if owner_bytes + size_bytes > self.owner_quota_bytes:
                    raise QuotaExceeded("owner quota exceeded")
                if global_bytes + size_bytes > self.global_quota_bytes:
                    raise QuotaExceeded("global quota exceeded")
                self._connection.execute(
                    "INSERT INTO blobs (blob_id, owner, sha256, size_bytes, media_type, state, created_at, expires_at, path) "
                    "VALUES (?, ?, NULL, ?, ?, ?, ?, NULL, ?)",
                    (blob_id, owner, size_bytes, media_type, RESERVED, now, path),
                )
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            return self._require(blob_id)

    def begin_staging(self, blob_id: str) -> BlobRecord:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute("UPDATE blobs SET state = ? WHERE blob_id = ? AND state = ?", (STAGING, blob_id, RESERVED))
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            return self._require(blob_id)

    def publish(self, blob_id: str, sha256: str, size_bytes: int, *, now: float, retention_seconds: float | None = None) -> BlobRecord:
        """One transaction turns a staging row into a published one.

        `now` is the instant retention starts from: the upload success for an
        input blob, the execution terminal for an output blob.
        """
        with self._lock:
            retention = self.retention_seconds if retention_seconds is None else retention_seconds
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    "UPDATE blobs SET state = ?, sha256 = ?, size_bytes = ?, expires_at = ? WHERE blob_id = ? AND state IN (?, ?)",
                    (PUBLISHED, sha256, size_bytes, now + retention, blob_id, RESERVED, STAGING),
                )
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            record = self._require(blob_id)
            if record.state != PUBLISHED:
                raise QuotaExceeded("the upload was not reserved")
            return record

    def abort(self, blob_id: str) -> None:
        """Release the reservation/temp accounting; the caller removes the file."""
        with self._lock:
            self._connection.execute("DELETE FROM blobs WHERE blob_id = ? AND state IN (?, ?)", (blob_id, RESERVED, STAGING))

    def get(self, blob_id: str) -> BlobRecord | None:
        with self._lock:
            row = self._connection.execute("SELECT * FROM blobs WHERE blob_id = ?", (blob_id,)).fetchone()
            return _record(row) if row is not None else None

        # -- leases, expiry and deletion ----------------------------------------

    def leases(self, blob_id: str) -> tuple[str, ...]:
        with self._lock:
            rows = self._connection.execute("SELECT holder FROM leases WHERE blob_id = ? ORDER BY holder", (blob_id,)).fetchall()
            return tuple(row["holder"] for row in rows)

    def acquire_lease(self, blob_id: str, holder: str) -> bool:
        with self._lock:
            record = self.get(blob_id)
            if record is None or record.state != PUBLISHED:
                return False
            self._connection.execute("INSERT OR IGNORE INTO leases (blob_id, holder) VALUES (?, ?)", (blob_id, holder))
            return True

    def release_lease(self, blob_id: str, holder: str) -> None:
        with self._lock:
            self._connection.execute("DELETE FROM leases WHERE blob_id = ? AND holder = ?", (blob_id, holder))

    def delete(self, blob_id: str, owner: str, *, now: float) -> str:
        """`deleted`, `held`, `missing` or `forbidden`; a held blob is never removed."""
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                record = self.get(blob_id)
                if record is None or record.owner != owner:
                    self._connection.execute("COMMIT")
                    return "missing"
                if record.state == DELETED:
                    self._connection.execute("COMMIT")
                    return "deleted"
                if self.leases(blob_id):
                    self._connection.execute("COMMIT")
                    return "held"
                self._connection.execute(
                    "UPDATE blobs SET state = ?, expires_at = ? WHERE blob_id = ?",
                    (DELETED, now + self.tombstone_seconds, blob_id),
                )
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            return "deleted"

    def expire(self, now: float) -> tuple[BlobRecord, ...]:
        """Move expired published blobs to tombstones and return them for unlinking."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM blobs WHERE state = ? AND expires_at IS NOT NULL AND expires_at <= ?",
                (PUBLISHED, now),
            ).fetchall()
            expired: list[BlobRecord] = []
            for row in rows:
                record = _record(row)
                if self.leases(record.blob_id):
                    continue  # an active lease keeps protecting the file
                self._connection.execute(
                    "UPDATE blobs SET state = ?, expires_at = ? WHERE blob_id = ?",
                    (DELETED, now + self.tombstone_seconds, record.blob_id),
                )
                expired.append(record)
            return tuple(expired)

    def mark_corrupt(self, blob_id: str) -> None:
        with self._lock:
            self._connection.execute("UPDATE blobs SET state = ? WHERE blob_id = ? AND state = ?", (CORRUPT, blob_id, PUBLISHED))

    def purge_tombstones(self, now: float) -> tuple[str, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT blob_id FROM blobs WHERE state IN (?, ?) AND expires_at IS NOT NULL AND expires_at <= ?",
                (DELETED, CORRUPT, now),
            ).fetchall()
            for row in rows:
                self._connection.execute("DELETE FROM blobs WHERE blob_id = ?", (row["blob_id"],))
            return tuple(row["blob_id"] for row in rows)

    def verify_all(self) -> tuple[str, ...]:
        """Rows whose published file must be re-hashed before reads are opened."""
        with self._lock:
            rows = self._connection.execute("SELECT * FROM blobs WHERE state = ?", (PUBLISHED,)).fetchall()
            return tuple(_record(row).blob_id for row in rows)

    # -- recovery journal ----------------------------------------------------

    def journal(self, blob_id: str, owner: str, step: str, *, sha256: str | None, size_bytes: int, now: float) -> None:
        with self._lock:
            if step not in JOURNAL_STEPS:
                raise ValueError(f"unknown journal step {step!r}")
            self._connection.execute(
                "INSERT INTO recovery (blob_id, owner, step, sha256, size_bytes, created_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(blob_id) DO UPDATE SET step = excluded.step, sha256 = excluded.sha256, "
                "size_bytes = excluded.size_bytes, created_at = excluded.created_at",
                (blob_id, owner, step, sha256, size_bytes, now),
            )

    def journal_entry(self, blob_id: str) -> dict[str, object] | None:
        with self._lock:
            row = self._connection.execute("SELECT * FROM recovery WHERE blob_id = ?", (blob_id,)).fetchone()
            return None if row is None else {
                "blob_id": row["blob_id"],
                "owner": row["owner"],
                "step": row["step"],
                "sha256": row["sha256"],
                "size_bytes": int(row["size_bytes"]),
                "created_at": float(row["created_at"]),
            }

    def clear_journal(self, blob_id: str) -> None:
        with self._lock:
            self._connection.execute("DELETE FROM recovery WHERE blob_id = ?", (blob_id,))

    def journal_entries(self) -> tuple[dict[str, object], ...]:
        with self._lock:
            rows = self._connection.execute("SELECT * FROM recovery ORDER BY created_at").fetchall()
            return tuple(
                {
                    "blob_id": row["blob_id"],
                    "owner": row["owner"],
                    "step": row["step"],
                    "sha256": row["sha256"],
                    "size_bytes": int(row["size_bytes"]),
                    "created_at": float(row["created_at"]),
                }
                for row in rows
            )

    def unfinished_rows(self) -> tuple[BlobRecord, ...]:
        """Rows that never reached `published`: their reservations are reclaimable."""
        with self._lock:
            rows = self._connection.execute("SELECT * FROM blobs WHERE state IN (?, ?)", (RESERVED, STAGING)).fetchall()
            return tuple(_record(row) for row in rows)

    def release_missing(self, blob_id: str, *, now: float, keep_tombstone: bool) -> None:
        """A published row whose file is gone or wrong: unreadable, and reclaimable.

        The bytes are not on disk any more, so the reservation is dropped while a
        tombstone keeps answering 410 to the owner.
        """
        with self._lock:
            state = DELETED if keep_tombstone else CORRUPT
            self._connection.execute(
                "UPDATE blobs SET state = ?, size_bytes = 0, expires_at = ? WHERE blob_id = ?",
                (state, now + self.tombstone_seconds, blob_id),
            )

    def protect_all_published(self, holder: str) -> tuple[str, ...]:
        """Hold a restart lease on every published blob (old boot may still read)."""
        with self._lock:
            rows = self._connection.execute("SELECT blob_id FROM blobs WHERE state = ?", (PUBLISHED,)).fetchall()
            for row in rows:
                self._connection.execute("INSERT OR IGNORE INTO leases (blob_id, holder) VALUES (?, ?)", (row["blob_id"], holder))
            return tuple(row["blob_id"] for row in rows)

    def release_protection(self, holder: str) -> tuple[str, ...]:
        with self._lock:
            rows = self._connection.execute("SELECT blob_id FROM leases WHERE holder = ?", (holder,)).fetchall()
            self._connection.execute("DELETE FROM leases WHERE holder = ?", (holder,))
            return tuple(row["blob_id"] for row in rows)

    def _require(self, blob_id: str) -> BlobRecord:
        with self._lock:
            record = self.get(blob_id)
            if record is None:
                raise QuotaExceeded(f"unknown blob {blob_id!r}")
            return record


def _record(row: sqlite3.Row) -> BlobRecord:
    return BlobRecord(
        blob_id=row["blob_id"],
        owner=row["owner"],
        sha256=row["sha256"],
        size_bytes=int(row["size_bytes"]),
        media_type=row["media_type"],
        state=row["state"],
        created_at=float(row["created_at"]),
        expires_at=None if row["expires_at"] is None else float(row["expires_at"]),
        path=row["path"],
    )


def iter_states(records: Iterable[BlobRecord]) -> tuple[str, ...]:
    return tuple(record.state for record in records)
