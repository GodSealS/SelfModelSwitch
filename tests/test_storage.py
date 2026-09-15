from __future__ import annotations

import json
import hashlib
from pathlib import Path

import asyncio

from model_scheduler.storage_monitor import StorageAdmissionGuard, StorageMonitor


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
    assert changed.reason == "model_file_changed"


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
