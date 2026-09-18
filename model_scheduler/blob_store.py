"""Bounded blob staging, verified reads and leases (M04/P11, C07).

The store keeps one published file per blob under `<root>/<owner>/<blob_id>` and
one staging file under `<root>/.staging/<blob_id>.part`. Every directory and file
is opened with `O_NOFOLLOW` (and `O_EXCL` while staging), identifiers come from
the service, and a read is only served from a file descriptor whose fstat and
full SHA-256 match the published metadata: swapping the file underneath the path
is detected before a single byte leaves the process.

Uploads hash while receiving, publish with one rename, and abort back to the
quota books on any failure, so a refused or cancelled upload leaves no readable
half-product behind. All metadata work runs in a worker thread.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import stat
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import AsyncIterator, Callable, Iterable

from .blob_metadata import DELETED, PUBLISHED, BlobMetadata, BlobRecord, QuotaExceeded

MEDIA_TYPES = frozenset({"application/json", "image/png", "image/jpeg"})
MAX_BLOB_BYTES = 1024**3
CHUNKED_RESERVE_BYTES = 1024**3
_ID_RE = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,61}[a-z0-9])?\Z")


class BlobStoreError(RuntimeError):
    """A refusal with a stable code; the API layer maps it to a status."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


class BlobReader:
    """A verified, leased file descriptor; reads never re-open the path."""

    def __init__(self, fd: int, record: BlobRecord) -> None:
        self._fd = fd
        self.record = record
        self.offset = 0

    def read(self, chunk_size: int) -> bytes:
        data = os.pread(self._fd, chunk_size, self.offset)
        self.offset += len(data)
        return data


class BlobStore:
    def __init__(
        self,
        root: str | Path,
        *,
        metadata: BlobMetadata | None = None,
        chunk_size: int = 1024 * 1024,
        chunked_reserve_bytes: int = CHUNKED_RESERVE_BYTES,
        media_types: Iterable[str] = MEDIA_TYPES,
        clock: Callable[[], float] = time.time,
        **metadata_policy,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / ".staging").mkdir(exist_ok=True)
        self.chunk_size = chunk_size
        self.chunked_reserve_bytes = chunked_reserve_bytes
        self.media_types = frozenset(media_types)
        self.clock = clock
        self.metadata = metadata or BlobMetadata(self.root / "metadata.sqlite3", **metadata_policy)

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _checked_id(value: str, where: str) -> str:
        if not isinstance(value, str) or not _ID_RE.fullmatch(value):
            raise BlobStoreError("not_found" if where == "blob_id" else "forbidden", f"invalid {where}")
        return value

    def _open_root_fd(self) -> int:
        try:
            return os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError as exc:
            raise BlobStoreError("forbidden", f"the blob root is not a plain directory: {exc}") from exc

    def _owner_dir_fd(self, owner: str) -> int:
        root_fd = self._open_root_fd()
        try:
            with suppress(FileExistsError):
                os.mkdir(owner, dir_fd=root_fd)
            return os.open(owner, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=root_fd)
        except OSError as exc:
            raise BlobStoreError("forbidden", f"the owner directory is not usable: {exc}") from exc
        finally:
            os.close(root_fd)

    def _open_staging(self, blob_id: str) -> int:
        root_fd = self._open_root_fd()
        try:
            return os.open(
                f".staging/{blob_id}.part",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=root_fd,
            )
        finally:
            os.close(root_fd)

    def _publish_file(self, owner: str, blob_id: str) -> None:
        owner_fd = self._owner_dir_fd(owner)
        root_fd = self._open_root_fd()
        try:
            os.replace(f".staging/{blob_id}.part", f"{owner}/{blob_id}", src_dir_fd=root_fd, dst_dir_fd=root_fd)
            os.fsync(owner_fd)
        finally:
            os.close(owner_fd)
            os.close(root_fd)

    def _remove_staging(self, blob_id: str) -> None:
        root_fd = self._open_root_fd()
        try:
            with suppress(FileNotFoundError):
                os.unlink(f".staging/{blob_id}.part", dir_fd=root_fd)
        finally:
            os.close(root_fd)

    def _remove_published(self, owner: str, blob_id: str) -> None:
        owner_fd = self._owner_dir_fd(owner)
        try:
            with suppress(FileNotFoundError):
                os.unlink(blob_id, dir_fd=owner_fd)
        finally:
            os.close(owner_fd)

    def _open_verified(self, record: BlobRecord) -> int:
        """Open the published file and verify identity from the descriptor itself."""
        owner_fd = self._owner_dir_fd(record.owner)
        try:
            # O_NONBLOCK keeps a swapped-in FIFO from blocking the open; the
            # descriptor is checked as a regular file immediately afterwards.
            fd = os.open(record.blob_id, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=owner_fd)
        except OSError as exc:
            raise BlobStoreError("unreadable", f"cannot open blob: {exc}") from exc
        finally:
            os.close(owner_fd)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size != record.size_bytes:
                raise BlobStoreError("unreadable", "the blob file changed")
            digest = hashlib.sha256()
            with os.fdopen(os.dup(fd), "rb", closefd=True) as handle:
                for block in iter(lambda: handle.read(self.chunk_size), b""):
                    digest.update(block)
            if digest.hexdigest() != record.sha256:
                raise BlobStoreError("unreadable", "the blob file does not match its published hash")
        except BaseException:
            os.close(fd)
            self.metadata.mark_corrupt(record.blob_id)
            raise
        return fd

    async def _call(self, function: Callable, *args, **kwargs):
        """Run blocking metadata work outside the event loop."""
        return await asyncio.to_thread(function, *args, **kwargs)

    # -- upload --------------------------------------------------------------

    async def upload(
        self,
        *,
        blob_id: str,
        owner: str,
        media_type: str,
        chunks: AsyncIterator[bytes],
        expected_sha256: str | None,
        declared_size: int | None = None,
    ) -> BlobRecord:
        """Receive one bounded stream, hash it while receiving, then publish it."""
        self._checked_id(blob_id, "blob_id")
        self._checked_id(owner, "owner")
        if media_type not in self.media_types:
            raise BlobStoreError("unsupported_media_type")
        limit = self.chunked_reserve_bytes if declared_size is None else declared_size
        if limit <= 0 or limit > MAX_BLOB_BYTES:
            raise BlobStoreError("too_large")
        now = self.clock()
        try:
            await self._call(self.metadata.reserve, blob_id, owner, limit, media_type, now=now, path=f"{owner}/{blob_id}")
        except QuotaExceeded as exc:
            raise BlobStoreError("quota_exceeded", str(exc)) from exc
        fd = await asyncio.to_thread(self._open_staging, blob_id)
        digest, received = hashlib.sha256(), 0
        try:
            await self._call(self.metadata.begin_staging, blob_id)
            async for chunk in chunks:
                if not chunk:
                    continue
                received += len(chunk)
                if received > limit:
                    raise BlobStoreError("too_large", "the upload exceeded its reservation")
                digest.update(chunk)
                await self._call(os.write, fd, chunk)
            await self._call(os.fsync, fd)
            if declared_size is not None and received != declared_size:
                raise BlobStoreError("checksum_mismatch", "the received size does not match the declaration")
            if expected_sha256 is not None and digest.hexdigest() != expected_sha256:
                raise BlobStoreError("checksum_mismatch", "the received hash does not match X-Content-SHA256")
        except BaseException:
            await asyncio.to_thread(os.close, fd)
            await asyncio.to_thread(self._remove_staging, blob_id)
            await self._call(self.metadata.abort, blob_id)
            raise
        await asyncio.to_thread(os.close, fd)
        try:
            await asyncio.to_thread(self._publish_file, owner, blob_id)
        except BaseException:
            await asyncio.to_thread(self._remove_staging, blob_id)
            await self._call(self.metadata.abort, blob_id)
            raise
        try:
            return await self._call(self.metadata.publish, blob_id, digest.hexdigest(), received, now=self.clock())
        except BaseException:
            await asyncio.to_thread(self._remove_published, owner, blob_id)
            await self._call(self.metadata.abort, blob_id)
            raise

    # -- reads, leases and deletion -----------------------------------------

    async def read_all(self, blob_id: str, owner: str, holder: str) -> bytes:
        """Read one whole blob inside a verified lease."""
        async with self.lease(blob_id, owner, holder) as reader:
            blocks = []
            while True:
                block = reader.read(self.chunk_size)
                if not block:
                    break
                blocks.append(block)
            return b"".join(blocks)

    @asynccontextmanager
    async def lease(self, blob_id: str, owner: str, holder: str):
        """Hold a read lease: the file cannot be deleted while it is open."""
        self._checked_id(blob_id, "blob_id")
        self._checked_id(owner, "owner")
        record = await self._call(self.metadata.get, blob_id)
        if record is None or record.owner != owner or record.state != PUBLISHED:
            raise BlobStoreError("not_found")
        if record.expires_at is not None and self.clock() >= record.expires_at:
            raise BlobStoreError("expired")
        fd = await asyncio.to_thread(self._open_verified, record)
        if not await self._call(self.metadata.acquire_lease, blob_id, holder):
            await asyncio.to_thread(os.close, fd)
            raise BlobStoreError("not_found")
        try:
            yield BlobReader(fd, record)
        finally:
            await asyncio.to_thread(os.close, fd)
            await self._call(self.metadata.release_lease, blob_id, holder)

    async def delete(self, blob_id: str, owner: str, *, now: float | None = None) -> str:
        """DELETE semantics: `deleted` (204) or `held` (409)."""
        self._checked_id(blob_id, "blob_id")
        self._checked_id(owner, "owner")
        outcome = await self._call(self.metadata.delete, blob_id, owner, now=self.clock() if now is None else now)
        if outcome == "deleted":
            record = await self._call(self.metadata.get, blob_id)
            if record is not None:
                await asyncio.to_thread(self._remove_published, record.owner, blob_id)
        return outcome

    async def expire(self, now: float | None = None) -> tuple[str, ...]:
        """Expire published blobs whose retention ended and unlink their files."""
        moment = self.clock() if now is None else now
        expired = await self._call(self.metadata.expire, moment)
        for record in expired:
            await asyncio.to_thread(self._remove_published, record.owner, record.blob_id)
        return tuple(record.blob_id for record in expired)

    async def verify_published(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Re-validate every published file (restart gate): (ok, unreadable)."""
        ok: list[str] = []
        broken: list[str] = []
        for blob_id in await self._call(self.metadata.verify_all):
            record = await self._call(self.metadata.get, blob_id)
            if record is None:
                continue
            try:
                fd = await asyncio.to_thread(self._open_verified, record)
            except BlobStoreError:
                broken.append(blob_id)
                continue
            await asyncio.to_thread(os.close, fd)
            ok.append(blob_id)
        return tuple(ok), tuple(broken)
