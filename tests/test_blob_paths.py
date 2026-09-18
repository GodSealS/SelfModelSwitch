"""Blob path safety tests (M04/P11, C07): no-follow, no escape, no TOCTOU swap.

Every identifier is service-generated, every parent directory is opened with
O_NOFOLLOW, the published file is verified from its own descriptor (regular file,
published size, full hash) before any byte is served, and staging files are
created exclusively.
"""
from __future__ import annotations

import hashlib
import os

import pytest

from model_scheduler.blob_store import BlobStore, BlobStoreError


async def stream(payload: bytes):
    yield payload


async def publish(blob: BlobStore, payload: bytes, *, blob_id="b-0001", owner="client-a"):
    return await blob.upload(
        blob_id=blob_id,
        owner=owner,
        media_type="image/png",
        chunks=stream(payload),
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        declared_size=len(payload),
    )


def store(tmp_path) -> BlobStore:
    return BlobStore(tmp_path / "blobs")


@pytest.mark.asyncio
async def test_identifiers_cannot_escape_the_root(tmp_path) -> None:
    blob = store(tmp_path)

    for owner, blob_id in (("../escape", "b-0001"), ("client-a", "../../etc/passwd"), ("client-a", "/etc/passwd"), ("client-a", "..")):
        with pytest.raises(BlobStoreError):
            await publish(blob, b"payload", blob_id=blob_id, owner=owner)

    assert not (tmp_path / "escape").exists()
    assert not (tmp_path / "blobs/client-a").exists()


@pytest.mark.asyncio
async def test_a_symlinked_owner_directory_is_refused(tmp_path) -> None:
    blob = store(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "blobs/client-a").symlink_to(elsewhere)

    with pytest.raises(BlobStoreError) as failure:
        await publish(blob, b"payload")

    assert failure.value.code == "forbidden"
    assert not list(elsewhere.iterdir())


@pytest.mark.asyncio
async def test_a_symlinked_blob_file_is_never_followed(tmp_path) -> None:
    blob = store(tmp_path)
    await publish(blob, b"payload")
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"payload")
    published = tmp_path / "blobs/client-a/b-0001"
    published.unlink()
    published.symlink_to(secret)

    with pytest.raises(BlobStoreError) as failure:
        await blob.read_all("b-0001", "client-a", "exec-1")

    assert failure.value.code == "unreadable"
    assert secret.read_bytes() == b"payload"  # the target was never opened for writing


@pytest.mark.asyncio
async def test_a_swapped_file_is_detected_before_any_byte_is_served(tmp_path) -> None:
    blob = store(tmp_path)
    await publish(blob, b"payload")
    (tmp_path / "blobs/client-a/b-0001").write_bytes(b"swapped")  # same length, different content

    with pytest.raises(BlobStoreError) as failure:
        await blob.read_all("b-0001", "client-a", "exec-1")

    assert failure.value.code == "unreadable"
    assert blob.metadata.get("b-0001").state == "corrupt"
    ok, broken = await blob.verify_published()
    assert (ok, broken) == ((), ())


@pytest.mark.asyncio
async def test_a_non_regular_file_is_refused(tmp_path) -> None:
    blob = store(tmp_path)
    await publish(blob, b"payload")
    published = tmp_path / "blobs/client-a/b-0001"
    published.unlink()
    os.mkfifo(published)

    with pytest.raises(BlobStoreError) as failure:
        await blob.read_all("b-0001", "client-a", "exec-1")

    assert failure.value.code == "unreadable"
    published.unlink()


@pytest.mark.asyncio
async def test_staging_creation_is_exclusive(tmp_path) -> None:
    blob = store(tmp_path)
    await publish(blob, b"payload")

    with pytest.raises(BlobStoreError) as failure:
        await publish(blob, b"payload")  # the same blob id is never staged twice

    assert failure.value.code == "quota_exceeded"
    assert blob.metadata.usage("client-a").total_bytes == 7
    assert [item.name for item in (tmp_path / "blobs/client-a").iterdir()] == ["b-0001"]
