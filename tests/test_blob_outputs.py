"""Execution output tests (M04/P12, C07).

An output is reserved before dispatch, published from its terminal instant, and
arbitrated by the same fence as a cancel: a cancelled or late result only cleans
its staging up, and a full disk never produces a successful result.
"""
from __future__ import annotations

import asyncio
import hashlib

import pytest

from model_scheduler.blob_store import BlobStore, BlobStoreError
from model_scheduler.control_protocol_v1 import Fence


class Clock:
    def __init__(self, now: float = 2_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def fence(**overrides) -> Fence:
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


def store(tmp_path, clock=None, **policy) -> BlobStore:
    return BlobStore(tmp_path / "blobs", clock=clock or Clock(), **policy)


async def stream(payload: bytes):
    yield payload


async def reserve(blob: BlobStore, *, blob_id="o-0001", limit=64, execution_fence=None):
    return await blob.reserve_output(
        blob_id=blob_id, owner="client-a", media_type="application/json",
        limit_bytes=limit, fence=execution_fence or fence(),
    )


@pytest.mark.asyncio
async def test_an_output_is_reserved_before_dispatch_and_published_from_its_terminal(tmp_path) -> None:
    blob = store(tmp_path)
    reservation = await reserve(blob, limit=64)

    assert blob.metadata.usage("client-a").total_bytes == 64  # the ceiling is on the books

    payload = b'{"ok":true}'
    record = await blob.write_output(
        reservation, chunks=stream(payload), expected_sha256=hashlib.sha256(payload).hexdigest(),
    )

    assert record.state == "published" and record.size_bytes == len(payload)
    assert blob.metadata.usage("client-a").total_bytes == len(payload)
    assert await blob.read_all("o-0001", "client-a", "exec-1") == payload


@pytest.mark.asyncio
async def test_an_output_over_its_limit_publishes_nothing_and_returns_the_reservation(tmp_path) -> None:
    blob = store(tmp_path)
    reservation = await reserve(blob, limit=4)

    with pytest.raises(BlobStoreError) as failure:
        await blob.write_output(reservation, chunks=stream(b"12345"), expected_sha256=None)

    assert failure.value.code == "too_large"
    assert blob.metadata.usage("client-a").total_bytes == 0
    assert list((tmp_path / "blobs/.staging").iterdir()) == []


@pytest.mark.asyncio
async def test_a_cancelled_execution_can_only_clean_its_staging(tmp_path) -> None:
    blob = store(tmp_path)
    reservation = await reserve(blob)

    assert await blob.cancel_output(reservation, fence=fence()) is True
    with pytest.raises(BlobStoreError) as failure:
        await blob.write_output(reservation, chunks=stream(b"late"), expected_sha256=None)

    assert failure.value.code == "cancelled"
    assert blob.metadata.usage("client-a").total_bytes == 0
    with pytest.raises(BlobStoreError):
        await blob.read_all("o-0001", "client-a", "exec-1")


@pytest.mark.asyncio
async def test_a_cancel_from_another_fence_cannot_kill_the_output(tmp_path) -> None:
    blob = store(tmp_path)
    reservation = await reserve(blob)

    assert await blob.cancel_output(reservation, fence=fence(operation_id="op-0002")) is False  # stale fence
    payload = b"{}"
    record = await blob.write_output(reservation, chunks=stream(payload), expected_sha256=hashlib.sha256(payload).hexdigest())

    assert record.state == "published"


@pytest.mark.asyncio
async def test_a_full_disk_never_produces_a_success_result_and_reservations_are_reclaimable(tmp_path) -> None:
    full = store(tmp_path / "full", disk_free_bytes=lambda: 0)
    with pytest.raises(BlobStoreError) as failure:
        await reserve(full)
    assert failure.value.code == "quota_exceeded"
    assert full.metadata.usage("client-a").total_bytes == 0
    assert list((tmp_path / "full/blobs/.staging").iterdir()) == []

    blob = store(tmp_path / "ok")
    reservation = await reserve(blob)
    await blob.abandon_output(reservation)
    assert blob.metadata.usage("client-a").total_bytes == 0  # the reservation is always reclaimable
