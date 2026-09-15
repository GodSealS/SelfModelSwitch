from __future__ import annotations

import json
from pathlib import Path

from model_scheduler.storage_monitor import StorageMonitor


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
