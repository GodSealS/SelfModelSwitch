"""Blob store contract tests (M04/P11, C07).

Covers streaming hashing and atomic publish, the single atomic quota check over
published+staging+reserved, owner isolation, read leases (409) and expiry (410),
and that metadata work never runs on the event loop.
"""
from __future__ import annotations

import asyncio
import hashlib

import pytest

from model_scheduler.blob_metadata import DELETED, PUBLISHED
from model_scheduler.blob_store import BlobStore, BlobStoreError


class Clock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def store(tmp_path, clock=None, **policy) -> BlobStore:
    return BlobStore(tmp_path / "blobs", clock=clock or Clock(), **policy)


async def stream(*blocks: bytes):
    for block in blocks:
        yield block


async def upload(blob: BlobStore, payload: bytes, *, blob_id="b-0001", owner="client-a", declared=True, sha256=None, media_type="image/png"):
    return await blob.upload(
        blob_id=blob_id,
        owner=owner,
        media_type=media_type,
        chunks=stream(payload),
        expected_sha256=sha256 or hashlib.sha256(payload).hexdigest(),
        declared_size=len(payload) if declared else None,
    )


@pytest.mark.asyncio
async def test_upload_hashes_while_receiving_and_publishes_one_file(tmp_path) -> None:
    blob = store(tmp_path)
    payload = b"hello blob"

    record = await upload(blob, payload)

    assert (record.state, record.size_bytes, record.sha256) == (PUBLISHED, len(payload), hashlib.sha256(payload).hexdigest())
    assert blob.metadata.usage("client-a").total_bytes == len(payload)
    assert list((tmp_path / "blobs/.staging").iterdir()) == []
    assert await blob.read_all("b-0001", "client-a", "exec-1") == payload


@pytest.mark.asyncio
async def test_a_checksum_mismatch_publishes_nothing_and_reclaims_the_quota(tmp_path) -> None:
    blob = store(tmp_path)

    with pytest.raises(BlobStoreError) as failure:
        await upload(blob, b"payload", sha256="0" * 64)

    assert failure.value.code == "checksum_mismatch"
    assert blob.metadata.usage("client-a").total_bytes == 0
    assert list((tmp_path / "blobs/.staging").iterdir()) == []
    with pytest.raises(BlobStoreError) as missing:
        await blob.read_all("b-0001", "client-a", "exec-1")
    assert missing.value.code == "not_found"


@pytest.mark.asyncio
async def test_a_chunked_upload_cannot_exceed_its_reservation(tmp_path) -> None:
    blob = store(tmp_path, chunked_reserve_bytes=4)

    with pytest.raises(BlobStoreError) as failure:
        await upload(blob, b"12345", declared=False)

    assert failure.value.code == "too_large"
    assert blob.metadata.usage("client-a").total_bytes == 0

    declared = store(tmp_path / "other")
    with pytest.raises(BlobStoreError) as too_big:
        await declared.upload(
            blob_id="b-0002", owner="client-a", media_type="image/png",
            chunks=stream(b"12345"), expected_sha256=None, declared_size=4,
        )
    assert too_big.value.code == "too_large"
    assert declared.metadata.usage("client-a").total_bytes == 0


@pytest.mark.asyncio
async def test_two_concurrent_uploads_cannot_oversell_the_owner_quota(tmp_path) -> None:
    blob = store(tmp_path, owner_quota_bytes=10)

    async def one(blob_id: str, payload: bytes):
        try:
            return await upload(blob, payload, blob_id=blob_id)
        except BlobStoreError as exc:
            return exc.code

    first, second = await asyncio.gather(one("b-0001", b"x" * 6), one("b-0002", b"y" * 6))

    outcomes = sorted(item if isinstance(item, str) else "published" for item in (first, second))
    assert outcomes == ["published", "quota_exceeded"]
    assert blob.metadata.usage("client-a").total_bytes == 6  # never oversold
    assert len(list((tmp_path / "blobs/client-a").iterdir())) == 1


@pytest.mark.asyncio
async def test_owner_isolation_leases_hold_deletes_and_deletes_are_idempotent(tmp_path) -> None:
    blob = store(tmp_path)
    await upload(blob, b"payload")

    with pytest.raises(BlobStoreError):
        await blob.read_all("b-0001", "client-b", "exec-1")  # another owner cannot read it
    assert await blob.delete("b-0001", "client-b") == "missing"  # and cannot remove it

    async with blob.lease("b-0001", "client-a", "exec-1") as reader:
        assert await blob.delete("b-0001", "client-a") == "held"  # an execution lease answers 409
        assert reader.read(64) == b"payload"
    assert await blob.read_all("b-0001", "client-a", "exec-2") == b"payload"  # still there after the lease

    assert await blob.delete("b-0001", "client-a") == "deleted"
    assert await blob.delete("b-0001", "client-a") == "deleted"  # the same owner still sees 204
    assert not list((tmp_path / "blobs/client-a").iterdir())
    with pytest.raises(BlobStoreError):
        await blob.read_all("b-0001", "client-a", "exec-3")


@pytest.mark.asyncio
async def test_expiry_answers_gone_and_keeps_an_active_lease_alive(tmp_path) -> None:
    clock = Clock()
    blob = store(tmp_path, clock, retention_seconds=10.0)
    await upload(blob, b"payload")
    clock.now += 11.0

    with pytest.raises(BlobStoreError) as expired:
        await blob.read_all("b-0001", "client-a", "exec-1")
    assert expired.value.code == "expired"  # 410 while the row is still published

    assert await blob.expire() == ("b-0001",)
    record = blob.metadata.get("b-0001")
    assert record is not None and record.state == DELETED
    assert not list((tmp_path / "blobs/client-a").iterdir())
    with pytest.raises(BlobStoreError) as gone:
        await blob.read_all("b-0001", "client-a", "exec-2")
    assert gone.value.code == "not_found"

    protected = store(tmp_path / "protected", Clock(), retention_seconds=10.0)
    await upload(protected, b"payload")
    async with protected.lease("b-0001", "client-a", "exec-9"):
        protected.clock.now += 11.0  # the lease was taken before the expiry
        assert await protected.expire() == ()  # so the file is still protected
        assert protected.metadata.get("b-0001").state == PUBLISHED
    assert await protected.expire() == ("b-0001",)  # once released, the retention applies


@pytest.mark.asyncio
async def test_metadata_transactions_never_block_the_event_loop(tmp_path, monkeypatch) -> None:
    blob = store(tmp_path)
    original = blob.metadata.reserve

    def slow_reserve(*args, **kwargs):
        import time

        time.sleep(0.05)  # a slow transaction must not stall the loop
        return original(*args, **kwargs)

    monkeypatch.setattr(blob.metadata, "reserve", slow_reserve)
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.001)

    spinning = asyncio.create_task(ticker())
    await upload(blob, b"payload")
    spinning.cancel()

    assert ticks > 5  # the loop kept running during the blocking transaction


@pytest.mark.asyncio
async def test_a_cancelled_upload_reclaims_its_reservation(tmp_path) -> None:
    blob = store(tmp_path)
    started = asyncio.Event()

    async def slow_chunks():
        yield b"first"
        started.set()
        await asyncio.Event().wait()

    uploading = asyncio.create_task(
        blob.upload(
            blob_id="b-0001", owner="client-a", media_type="image/png",
            chunks=slow_chunks(), expected_sha256=None, declared_size=16,
        )
    )
    await started.wait()
    assert blob.metadata.usage("client-a").total_bytes == 16  # reserved while receiving
    uploading.cancel()
    with pytest.raises(asyncio.CancelledError):
        await uploading

    assert blob.metadata.usage("client-a").total_bytes == 0  # the temporary quota is reclaimed
    assert list((tmp_path / "blobs/.staging").iterdir()) == []
    with pytest.raises(BlobStoreError):
        await blob.read_all("b-0001", "client-a", "exec-1")


@pytest.mark.asyncio
async def test_verify_published_rehashes_every_readable_blob(tmp_path) -> None:
    blob = store(tmp_path)
    await upload(blob, b"payload")
    ok, broken = await blob.verify_published()
    assert (ok, broken) == (("b-0001",), ())
    (tmp_path / "blobs/client-a/b-0001").write_bytes(b"tampered")
    ok, broken = await blob.verify_published()
    assert (ok, broken) == ((), ("b-0001",))
    with pytest.raises(BlobStoreError) as refused:
        await blob.read_all("b-0001", "client-a", "exec-1")
    assert refused.value.code == "not_found"  # a corrupt blob is no longer published
