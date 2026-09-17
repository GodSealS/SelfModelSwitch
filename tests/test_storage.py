from __future__ import annotations

import json
import hashlib
from pathlib import Path

import asyncio

from model_scheduler.contracts_v2 import AssetRef
from model_scheduler.storage_monitor import StorageAdmissionGuard, StorageMonitor, StorageSnapshot
from test_asset_store import Clock, CountingHasher


def findmnt(uuid: str, target: str = "/mnt/model-ssd") -> str:
    return json.dumps({"filesystems": [{"target": target, "source": "/dev/sda1", "fstype": "ext4", "uuid": uuid}]})


def test_storage_requires_the_expected_uuid_and_regular_model_file(tmp_path: Path) -> None:
    mount = tmp_path / "ssd"
    models = mount / "models"
    models.mkdir(parents=True)
    model = models / "embedding.gguf"
    model.write_bytes(b"model")
    monitor = StorageMonitor(mount, models, "expected", "ext4", runner=lambda _: findmnt("expected", str(mount)))

    snapshot = monitor.check({"embedding": "embedding.gguf"})

    assert snapshot.ready is True
    assert snapshot.files["embedding"].size == 5


def test_storage_rejects_wrong_uuid_or_symlink(tmp_path: Path) -> None:
    mount = tmp_path / "ssd"
    models = mount / "models"
    models.mkdir(parents=True)
    target = tmp_path / "outside.gguf"
    target.write_bytes(b"model")
    (models / "embedding.gguf").symlink_to(target)
    monitor = StorageMonitor(mount, models, "expected", "ext4", runner=lambda _: findmnt("wrong", str(mount)))

    assert monitor.check({"embedding": "embedding.gguf"}).ready is False


def test_storage_hashes_expected_models_and_detects_content_change(tmp_path: Path) -> None:
    mount = tmp_path / "ssd"; models = mount / "models"; models.mkdir(parents=True)
    model = models / "embedding.gguf"; model.write_bytes(b"model")
    digest = hashlib.sha256(b"model").hexdigest()
    monitor = StorageMonitor(mount, models, "expected", "ext4", runner=lambda _: findmnt("expected", str(mount)))
    assert monitor.check({"embedding": ("embedding.gguf", digest)}).ready is True
    model.write_bytes(b"changed")
    changed = monitor.check({"embedding": ("embedding.gguf", digest)})
    assert changed.ready is False
    assert changed.reason == "asset_changed"


def test_storage_rejects_expected_hash_mismatch(tmp_path: Path) -> None:
    mount = tmp_path / "ssd"; models = mount / "models"; models.mkdir(parents=True)
    (models / "embedding.gguf").write_bytes(b"model")
    monitor = StorageMonitor(mount, models, "expected", "ext4", runner=lambda _: findmnt("expected", str(mount)))
    assert monitor.check({"embedding": ("embedding.gguf", "0" * 64)}).ready is False


def test_storage_rejects_a_symlinked_model_directory(tmp_path: Path) -> None:
    mount = tmp_path / "ssd"; mount.mkdir()
    outside = tmp_path / "outside"; outside.mkdir()
    (outside / "embedding.gguf").write_bytes(b"model")
    model_directory = mount / "models"; model_directory.symlink_to(outside, target_is_directory=True)
    monitor = StorageMonitor(mount, model_directory, "expected", "ext4", runner=lambda _: findmnt("expected", str(mount)))
    assert monitor.check({"embedding": "embedding.gguf"}).ready is False


def test_async_storage_guard_exposes_monitor_readiness(tmp_path: Path) -> None:
    mount = tmp_path / "ssd"; models = mount / "models"; models.mkdir(parents=True)
    (models / "embedding.gguf").write_bytes(b"model")
    monitor = StorageMonitor(mount, models, "expected", "ext4", runner=lambda _: findmnt("expected", str(mount)))
    assert asyncio.run(StorageAdmissionGuard(monitor, {"embedding": "embedding.gguf"})()) is True


def test_storage_guard_coalesces_concurrent_checks_within_its_sample_interval() -> None:
    class Monitor:
        calls = 0

        def check(self, models):
            self.calls += 1
            return StorageSnapshot(True, None, 0, {})

    monitor = Monitor()
    guard = StorageAdmissionGuard(monitor, {"embedding": "embedding.gguf"}, sample_interval_seconds=60)

    async def check_many() -> list[bool]:
        return await asyncio.gather(*(guard() for _ in range(8)))

    assert asyncio.run(check_many()) == [True] * 8
    assert monitor.calls == 1


def _asset(role: str, filename: str, payload: bytes = b"model") -> AssetRef:
    return AssetRef(role=role, path=filename, sha256=hashlib.sha256(payload).hexdigest(), size_bytes=len(payload))


def _multi_asset_models(tmp_path: Path) -> tuple[Path, Path]:
    mount = tmp_path / "ssd"
    models = mount / "models"
    models.mkdir(parents=True)
    (models / "qwen.gguf").write_bytes(b"model")
    (models / "mmproj.gguf").write_bytes(b"projector-bytes")
    return mount, models


def _monitor(mount: Path, models: Path, **kwargs) -> StorageMonitor:
    kwargs.setdefault("runner", lambda _: findmnt("expected", str(mount)))
    return StorageMonitor(mount, models, "expected", "ext4", **kwargs)


def test_monitor_verifies_every_file_of_a_registered_model(tmp_path: Path) -> None:
    mount, models = _multi_asset_models(tmp_path)
    hasher = CountingHasher()
    monitor = _monitor(mount, models, hasher=hasher)
    assets = {"qwen": [_asset("model", "qwen.gguf"), _asset("projector", "mmproj.gguf", b"projector-bytes")]}

    snapshot = monitor.check(assets)

    assert snapshot.ready is True
    assert len(hasher.calls) == 2
    assert snapshot.files["qwen"].sha256 == hashlib.sha256(b"model").hexdigest()


def test_monitor_runtime_check_does_not_rehash_the_whole_set(tmp_path: Path) -> None:
    mount, models = _multi_asset_models(tmp_path)
    hasher = CountingHasher()
    monitor = _monitor(mount, models, hasher=hasher)
    assets = {"qwen": [_asset("model", "qwen.gguf"), _asset("projector", "mmproj.gguf", b"projector-bytes")]}

    assert monitor.check(assets).ready is True
    assert monitor.check(assets).ready is True

    assert len(hasher.calls) == 2


def test_monitor_startup_verification_always_rehashes(tmp_path: Path) -> None:
    mount, models = _multi_asset_models(tmp_path)
    hasher = CountingHasher()
    monitor = _monitor(mount, models, hasher=hasher)
    assets = {"qwen": [_asset("model", "qwen.gguf"), _asset("projector", "mmproj.gguf", b"projector-bytes")]}

    assert monitor.verify_all(assets).ready is True
    assert monitor.verify_all(assets).ready is True

    assert len(hasher.calls) == 4


def test_monitor_uses_the_one_hash_deadline_and_keeps_admission_closed(tmp_path: Path) -> None:
    mount, models = _multi_asset_models(tmp_path)
    clock = Clock()
    monitor = _monitor(mount, models, clock=clock, verify_timeout_seconds=10, hasher=CountingHasher(clock=clock, cost_seconds=20))
    assets = {"qwen": [_asset("model", "qwen.gguf"), _asset("projector", "mmproj.gguf", b"projector-bytes")]}

    snapshot = monitor.check(assets)

    assert snapshot.ready is False
    assert snapshot.reason == "asset_verify_timeout"
    assert monitor.check(assets).ready is False
    assert asyncio.run(StorageAdmissionGuard(monitor, assets)()) is False


def test_a_changed_registration_rehashes_the_whole_registry(tmp_path: Path) -> None:
    mount, models = _multi_asset_models(tmp_path)
    hasher = CountingHasher()
    monitor = _monitor(mount, models, hasher=hasher)

    assert monitor.check({"qwen": [_asset("model", "qwen.gguf")]}).ready is True
    assert len(hasher.calls) == 1
    added = monitor.check(
        {"qwen": [_asset("model", "qwen.gguf")], "vision": [_asset("projector", "mmproj.gguf", b"projector-bytes")]}
    )

    assert added.ready is True
    # One baseline covers the whole registry, so any change invalidates all of it.
    assert len(hasher.calls) == 3


def test_monitor_default_hash_deadline_matches_the_configuration_default(tmp_path: Path) -> None:
    mount, models = _multi_asset_models(tmp_path)

    assert _monitor(mount, models).store.verify_timeout_seconds == 900
