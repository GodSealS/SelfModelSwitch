"""Blob restart recovery tests (M04/P12, C07).

Each crash point has exactly one deterministic outcome, the owner never changes
across a restart, a hash-mismatched blob is unreadable, GC never touches a file
that is still leased, and the old boot's read protection is only released after
the old instance is observed STOPPED.
"""
from __future__ import annotations

import asyncio
import hashlib

import pytest

from model_scheduler.blob_metadata import DELETED, PUBLISHED, STAGING, STEP_PENDING
from model_scheduler.blob_store import BlobStore, BlobStoreError


class Clock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def store(tmp_path, clock=None, **policy) -> BlobStore:
    return BlobStore(tmp_path / "blobs", clock=clock or Clock(), **policy)


async def stream(payload: bytes):
    yield payload


async def publish(blob: BlobStore, payload: bytes, *, blob_id="b-0001", owner="client-a"):
    return await blob.upload(
        blob_id=blob_id, owner=owner, media_type="image/png", chunks=stream(payload),
        expected_sha256=hashlib.sha256(payload).hexdigest(), declared_size=len(payload),
    )


@pytest.mark.asyncio
async def test_restart_completes_a_rename_that_never_reached_its_commit(tmp_path) -> None:
    blob = store(tmp_path)
    record = await publish(blob, b"payload")
    # The crash: the file was renamed into place, but the metadata transaction
    # never committed (the row is still staging and the journal says pending).
    blob.metadata._connection.execute("UPDATE blobs SET state = ? WHERE blob_id = ?", (STAGING, "b-0001"))
    blob.metadata.journal("b-0001", "client-a", STEP_PENDING, sha256=record.sha256, size_bytes=record.size_bytes, now=1_001.0)

    report = await blob.recover(instances_running=False)

    assert report.verified == ("b-0001",)
    assert report.purged == () and report.unreadable == ()
    assert blob.metadata.get("b-0001").state == PUBLISHED
    assert blob.metadata.journal_entry("b-0001") is None
    assert await blob.read_all("b-0001", "client-a", "exec-1") == b"payload"


@pytest.mark.asyncio
async def test_restart_purges_unfinished_uploads_and_orphan_staging(tmp_path) -> None:
    blob = store(tmp_path)
    blob.metadata.reserve("b-0001", "client-a", 32, "image/png", now=1_000.0, path="client-a/b-0001")
    blob.metadata.journal("b-0001", "client-a", "staging", sha256=None, size_bytes=32, now=1_000.0)
    staging = tmp_path / "blobs/.staging/orphan.part"
    staging.write_bytes(b"leftover")

    report = await blob.recover(instances_running=False)

    assert report.purged == ("b-0001", "orphan")
    assert blob.metadata.usage("client-a").total_bytes == 0  # the reservation came back
    assert blob.metadata.journal_entry("b-0001") is None
    assert list((tmp_path / "blobs/.staging").iterdir()) == []


@pytest.mark.asyncio
async def test_restart_marks_a_lost_or_tampered_blob_unreadable_and_reclaims_its_bytes(tmp_path) -> None:
    blob = store(tmp_path)
    await publish(blob, b"payload")
    await publish(blob, b"second", blob_id="b-0002")
    (tmp_path / "blobs/client-a/b-0001").unlink()  # committed, but the file is gone
    (tmp_path / "blobs/client-a/b-0002").write_bytes(b"SECOND")  # same length, wrong bytes

    report = await blob.recover(instances_running=False)

    assert sorted(report.unreadable) == ["b-0001", "b-0002"]
    assert report.verified == ()
    assert blob.metadata.usage("client-a").total_bytes == 0  # nothing readable keeps bytes on the books
    assert blob.metadata.get("b-0001").state == DELETED  # the owner still sees a tombstone
    for blob_id in ("b-0001", "b-0002"):
        with pytest.raises(BlobStoreError):
            await blob.read_all(blob_id, "client-a", "exec-1")


@pytest.mark.asyncio
async def test_restart_protection_keeps_files_until_the_old_instance_is_stopped(tmp_path) -> None:
    clock = Clock()
    blob = store(tmp_path, clock, retention_seconds=10.0)
    await publish(blob, b"payload")

    report = await blob.recover(instances_running=True, boot_id="boot-1")
    assert report.protected == ("b-0001",)
    clock.now += 11.0
    assert await blob.expire() == ()  # GC never touches a protected (leased) file
    assert blob.metadata.get("b-0001").state == PUBLISHED

    assert await blob.release_restart_protection("boot-1") == ("b-0001",)
    assert await blob.expire() == ("b-0001",)


@pytest.mark.asyncio
async def test_input_and_output_retention_start_at_their_own_instant(tmp_path) -> None:
    clock = Clock()
    blob = store(tmp_path, clock, retention_seconds=100.0, tombstone_seconds=50.0)
    await publish(blob, b"payload")
    assert blob.metadata.get("b-0001").expires_at == 1_100.0  # input: upload success + 24h

    clock.now += 10.0
    reservation = await blob.reserve_output(
        blob_id="o-0001", owner="client-a", media_type="application/json", limit_bytes=64,
        fence=_fence(execution_id="e-1"),
    )
    clock.now += 40.0  # the execution ran
    await blob.write_output(reservation, chunks=stream(b"{}"), expected_sha256=hashlib.sha256(b"{}").hexdigest(), now=clock.now)
    assert blob.metadata.get("o-0001").expires_at == clock.now + 100.0  # output: terminal + 24h

    clock.now += 100.0
    assert sorted(await blob.expire()) == ["b-0001", "o-0001"]
    assert blob.metadata.get("o-0001").state == DELETED
    assert blob.metadata.purge_tombstones(clock.now + 50.0) == ("b-0001", "o-0001")


@pytest.mark.asyncio
async def test_the_owner_survives_a_restart_and_other_owners_stay_out(tmp_path) -> None:
    blob = store(tmp_path)
    await publish(blob, b"payload")

    await blob.recover(instances_running=False)

    assert blob.metadata.get("b-0001").owner == "client-a"
    assert await blob.read_all("b-0001", "client-a", "exec-1") == b"payload"
    with pytest.raises(BlobStoreError):
        await blob.read_all("b-0001", "client-b", "exec-2")


def _fence(**overrides):
    from model_scheduler.control_protocol_v1 import Fence

    values = {
        "boot_id": "boot-0001",
        "model_id": "qwen25vl-7b-q4",
        "generation": 1,
        "operation_id": "op-0001",
        "execution_id": "e-0001",
        "attempt": 1,
    }
    values.update(overrides)
    return Fence(**values)
