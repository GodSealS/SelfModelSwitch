"""Per-file asset verification and identity checks for registered models (M02/P05).

The store distinguishes three outcomes instead of answering with a boolean: a
full pass that opens and hashes every registered file under a single deadline, a
cheap metadata observation for the runtime loop, and an UNKNOWN outcome when the
deadline expires and further files must not be opened.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time

import pytest

from model_scheduler.asset_store import AssetFault, AssetState, AssetStore, hash_file_descriptor, normalize_assets
from model_scheduler.contracts_v2 import AssetRef

CONTENT = b"model-bytes"


class Clock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class CountingHasher:
    """Stands in for the real reader so tests can prove what was opened."""

    def __init__(self, *, cost_seconds: float = 0.0, clock: Clock | None = None) -> None:
        self.calls: list[int] = []
        self.cost_seconds = cost_seconds
        self.clock = clock

    def __call__(self, fd: int, tick) -> str:
        self.calls.append(fd)
        if self.clock is not None:
            self.clock.advance(self.cost_seconds)
        # The store's deadline runs first; whatever survives it is hashed for real.
        tick()
        return hash_file_descriptor(fd, lambda: None)


def _digest(payload: bytes = CONTENT) -> str:
    return hashlib.sha256(payload).hexdigest()


def _asset(role: str, filename: str, payload: bytes = CONTENT) -> AssetRef:
    return AssetRef(role=role, path=filename, sha256=_digest(payload), size_bytes=len(payload))


def _layout(tmp_path: Path) -> tuple[Path, Path]:
    mount = tmp_path / "ssd"
    models = mount / "models"
    models.mkdir(parents=True)
    return mount, models


def _mount_report(mount: Path, uuid: str = "expected", target: Path | None = None, fstype: str = "ext4") -> str:
    return json.dumps(
        {"filesystems": [{"target": str(target if target is not None else mount), "source": "/dev/sda1", "fstype": fstype, "uuid": uuid}]}
    )


def _write(models: Path, relative: str, payload: bytes = CONTENT) -> None:
    target = models / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)


def _store(models: Path, mount: Path, **kwargs) -> AssetStore:
    kwargs.setdefault("runner", lambda _: _mount_report(mount))
    return AssetStore(mount, models, "expected", "ext4", **kwargs)


def test_full_verification_hashes_every_registered_file(tmp_path: Path) -> None:
    mount, models = _layout(tmp_path)
    _write(models, "qwen.gguf")
    _write(models, "mmproj.gguf")
    _write(models, "sub/shard-1.gguf")
    hasher = CountingHasher()
    store = _store(models, mount, hasher=hasher)

    snapshot = store.verify(
        {"qwen": [_asset("model", "qwen.gguf"), _asset("projector", "mmproj.gguf"), _asset("model", "sub/shard-1.gguf")]}
    )

    assert snapshot.state is AssetState.VERIFIED
    assert snapshot.ready is True
    assert len(hasher.calls) == 3
    assert [fact.role for fact in snapshot.facts["qwen"]] == ["model", "projector", "model"]


def test_verification_records_inode_size_and_mtime(tmp_path: Path) -> None:
    mount, models = _layout(tmp_path)
    _write(models, "qwen.gguf")
    store = _store(models, mount)

    snapshot = store.verify({"qwen": [_asset("model", "qwen.gguf")]})
    fact = snapshot.facts["qwen"][0]
    stat = (models / "qwen.gguf").stat()

    assert fact.size_bytes == len(CONTENT)
    assert fact.inode == stat.st_ino
    assert fact.mtime_ns == stat.st_mtime_ns
    assert fact.sha256 == _digest()


def test_runtime_observation_does_not_rehash_every_second(tmp_path: Path) -> None:
    mount, models = _layout(tmp_path)
    _write(models, "qwen.gguf")
    _write(models, "mmproj.gguf")
    hasher = CountingHasher()
    store = _store(models, mount, hasher=hasher)
    assets = {"qwen": [_asset("model", "qwen.gguf"), _asset("projector", "mmproj.gguf")]}

    assert store.verify(assets).ready is True
    assert len(hasher.calls) == 2
    assert store.observe().ready is True
    assert store.observe().ready is True
    assert len(hasher.calls) == 2


def test_observation_detects_a_replaced_file(tmp_path: Path) -> None:
    mount, models = _layout(tmp_path)
    _write(models, "qwen.gguf")
    store = _store(models, mount)
    assert store.verify({"qwen": [_asset("model", "qwen.gguf")]}).ready is True

    (models / "qwen.gguf").unlink()
    _write(models, "qwen.gguf", b"replaced-bytes")
    snapshot = store.observe()

    assert snapshot.ready is False
    assert snapshot.reason == "asset_changed"


def test_observation_detects_an_in_place_rewrite(tmp_path: Path) -> None:
    mount, models = _layout(tmp_path)
    _write(models, "qwen.gguf")
    store = _store(models, mount)
    assert store.verify({"qwen": [_asset("model", "qwen.gguf")]}).ready is True

    # A same-size rewrite is caught by mtime, so the observer never has to read bytes.
    time.sleep(0.01)
    (models / "qwen.gguf").write_bytes(b"same-size!!")

    assert store.observe().reason == "asset_changed"


def test_observing_before_verification_is_not_ready(tmp_path: Path) -> None:
    mount, models = _layout(tmp_path)
    _write(models, "qwen.gguf")
    store = _store(models, mount)

    snapshot = store.observe()

    assert snapshot.state is AssetState.UNVERIFIED
    assert snapshot.ready is False
    assert snapshot.reason == "assets_unverified"


def test_a_symlinked_parent_directory_closes_admission(tmp_path: Path) -> None:
    mount, models = _layout(tmp_path)
    outside = tmp_path / "outside-content"
    outside.mkdir()
    (outside / "qwen.gguf").write_bytes(CONTENT)
    (models / "vision").symlink_to(outside, target_is_directory=True)
    store = _store(models, mount)

    snapshot = store.verify({"qwen": [_asset("model", "vision/qwen.gguf")]})

    assert snapshot.state is AssetState.FAULT
    assert snapshot.reason == "asset_path_unsafe"


def test_a_symlinked_asset_closes_admission(tmp_path: Path) -> None:
    mount, models = _layout(tmp_path)
    outside = tmp_path / "outside.gguf"
    outside.write_bytes(CONTENT)
    (models / "qwen.gguf").symlink_to(outside)
    store = _store(models, mount)

    snapshot = store.verify({"qwen": [_asset("model", "qwen.gguf")]})

    assert snapshot.state is AssetState.FAULT
    assert snapshot.reason == "asset_symlink"


def test_unsafe_relative_paths_close_admission(tmp_path: Path) -> None:
    mount, models = _layout(tmp_path)
    store = _store(models, mount)

    assert store.verify({"qwen": [_asset("model", "../qwen.gguf")]}).reason == "asset_path_unsafe"
    assert store.verify({"qwen": [_asset("model", "/etc/passwd")]}).reason == "asset_path_unsafe"
    assert store.verify({"qwen": [_asset("model", "sub/../../qwen.gguf")]}).reason == "asset_path_unsafe"


def test_a_same_name_directory_on_another_disk_is_refused(tmp_path: Path) -> None:
    mount, models = _layout(tmp_path)
    _write(models, "qwen.gguf")
    store = _store(models, mount, runner=lambda _: _mount_report(mount, target=Path("/")))

    snapshot = store.verify({"qwen": [_asset("model", "qwen.gguf")]})

    assert snapshot.ready is False
    assert snapshot.reason == "mount_not_found"


def test_mount_identity_mismatch_closes_admission(tmp_path: Path) -> None:
    mount, models = _layout(tmp_path)
    _write(models, "qwen.gguf")
    wrong_uuid = AssetStore(mount, models, "expected", "ext4", runner=lambda _: _mount_report(mount, uuid="other"))
    wrong_fs = AssetStore(mount, models, "expected", "ext4", runner=lambda _: _mount_report(mount, fstype="ntfs"))

    assert wrong_uuid.verify({"qwen": [_asset("model", "qwen.gguf")]}).reason == "mount_identity_mismatch"
    assert wrong_fs.verify({"qwen": [_asset("model", "qwen.gguf")]}).reason == "mount_identity_mismatch"


def test_registered_size_mismatch_is_rejected(tmp_path: Path) -> None:
    mount, models = _layout(tmp_path)
    _write(models, "qwen.gguf")
    store = _store(models, mount)
    assets = {"qwen": [AssetRef(role="model", path="qwen.gguf", sha256=_digest(), size_bytes=999)]}

    assert store.verify(assets).reason == "asset_size_mismatch"


def test_registered_hash_mismatch_is_rejected(tmp_path: Path) -> None:
    mount, models = _layout(tmp_path)
    _write(models, "qwen.gguf")
    store = _store(models, mount)
    same_size = b"XXXXXXXXXXX"

    assert store.verify({"qwen": [_asset("model", "qwen.gguf", same_size)]}).reason == "asset_hash_mismatch"


def test_a_missing_file_is_rejected(tmp_path: Path) -> None:
    mount, models = _layout(tmp_path)
    store = _store(models, mount)

    assert store.verify({"qwen": [_asset("model", "missing.gguf")]}).reason == "asset_unavailable"


def test_the_total_deadline_stops_opening_further_assets(tmp_path: Path) -> None:
    mount, models = _layout(tmp_path)
    _write(models, "qwen.gguf")
    _write(models, "second.gguf")
    clock = Clock()
    hasher = CountingHasher(cost_seconds=20.0, clock=clock)
    store = _store(models, mount, clock=clock, hasher=hasher, verify_timeout_seconds=10)

    snapshot = store.verify({"qwen": [_asset("model", "qwen.gguf"), _asset("model", "second.gguf")]})

    assert snapshot.state is AssetState.UNKNOWN
    assert snapshot.ready is False
    assert snapshot.reason == "asset_verify_timeout"
    assert len(hasher.calls) == 1


def test_a_stalled_read_keeps_the_result_unknown(tmp_path: Path) -> None:
    mount, models = _layout(tmp_path)
    _write(models, "qwen.gguf")
    clock = Clock()

    def stalling_hasher(fd: int, tick) -> str:
        while True:
            clock.advance(5.0)
            tick()

    store = _store(models, mount, clock=clock, hasher=stalling_hasher, verify_timeout_seconds=12)
    snapshot = store.verify({"qwen": [_asset("model", "qwen.gguf")]})

    assert snapshot.state is AssetState.UNKNOWN
    assert snapshot.reason == "asset_verify_timeout"


def test_after_a_timeout_the_baseline_is_not_reused(tmp_path: Path) -> None:
    mount, models = _layout(tmp_path)
    _write(models, "qwen.gguf")
    clock = Clock()
    store = _store(models, mount, clock=clock, hasher=CountingHasher(cost_seconds=20.0, clock=clock), verify_timeout_seconds=10)

    assert store.verify({"qwen": [_asset("model", "qwen.gguf")]}).state is AssetState.UNKNOWN
    assert store.observe().reason == "assets_unverified"


def test_a_fault_clears_the_baseline_and_forces_a_full_rehash(tmp_path: Path) -> None:
    mount, models = _layout(tmp_path)
    _write(models, "qwen.gguf")
    hasher = CountingHasher()
    store = _store(models, mount, hasher=hasher)
    assets = {"qwen": [_asset("model", "qwen.gguf")]}
    assert store.verify(assets).ready is True

    (models / "qwen.gguf").unlink()
    _write(models, "qwen.gguf", b"different-value")
    assert store.observe().ready is False
    _write(models, "qwen.gguf")
    repaired = store.verify(assets)

    assert repaired.ready is True
    assert len(hasher.calls) == 2


def test_sharded_registrations_hash_every_shard(tmp_path: Path) -> None:
    mount, models = _layout(tmp_path)
    for index in range(4):
        _write(models, f"shard-{index:05d}.gguf", f"shard-{index}".encode())
    hasher = CountingHasher()
    store = _store(models, mount, hasher=hasher)
    assets = {"big": [_asset("model", f"shard-{index:05d}.gguf", f"shard-{index}".encode()) for index in range(4)]}

    assert store.verify(assets).ready is True
    assert len(hasher.calls) == 4


def test_a_lost_mount_closes_admission_during_observation(tmp_path: Path) -> None:
    mount, models = _layout(tmp_path)
    _write(models, "qwen.gguf")
    reports = [{"target": str(mount), "source": "/dev/sda1", "fstype": "ext4", "uuid": "expected"}]

    def runner(_: list[str]) -> str:
        return json.dumps({"filesystems": reports})

    store = AssetStore(mount, models, "expected", "ext4", runner=runner)
    assert store.verify({"qwen": [_asset("model", "qwen.gguf")]}).ready is True

    # The disk is unplugged, leaving only the same-name directory behind.
    reports[0] = {"target": "/", "source": "/dev/root", "fstype": "ext4", "uuid": "expected"}
    snapshot = store.observe()

    assert snapshot.ready is False
    assert snapshot.reason == "mount_not_found"


def test_normalize_assets_accepts_the_legacy_single_file_forms() -> None:
    class LegacyModel:
        file = "embedding.gguf"
        sha256 = _digest()

    entries = normalize_assets(LegacyModel())
    assert [(entry.role, entry.path, entry.sha256, entry.size_bytes) for entry in entries] == [
        ("model", "embedding.gguf", _digest(), None)
    ]
    assert [entry.path for entry in normalize_assets("plain.gguf")] == ["plain.gguf"]
    assert [entry.sha256 for entry in normalize_assets(("pair.gguf", _digest()))] == [_digest()]


def test_normalize_assets_rejects_an_unsupported_registration() -> None:
    with pytest.raises(AssetFault, match="invalid_asset"):
        normalize_assets(object())
