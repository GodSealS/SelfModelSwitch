from __future__ import annotations

from model_scheduler.resource_monitor import ResourceMonitor, ResourceSnapshot


def test_psutil_is_the_only_admission_resource_source(monkeypatch) -> None:
    class Memory:
        total = 1000
        available = 400

    monkeypatch.setattr("model_scheduler.resource_monitor.psutil.virtual_memory", lambda: Memory())
    snapshot = ResourceMonitor().snapshot_now(now=10)

    assert snapshot.source == "psutil"
    assert snapshot.available_bytes == 400
    assert snapshot.sampled_at == 10


def test_snapshot_age_is_checked_by_scheduler_contract() -> None:
    snapshot = ResourceSnapshot(total_bytes=1000, available_bytes=400, used_bytes=600, source="psutil", sampled_at=1)

    assert snapshot.age_at(2) == 1
    assert snapshot.age_at(0) is None
