from __future__ import annotations

import pytest

from model_scheduler.resource_monitor import (
    ResourceMonitor,
    ResourceSnapshot,
    admission_sample,
    memory_sample_from,
    system_nonfree_upper_bound_v1,
)


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


def test_memory_sample_keeps_total_free_and_available_for_both_ledgers(monkeypatch) -> None:
    class Memory:
        total = 65_000
        free = 20_000
        available = 40_000

    monkeypatch.setattr("model_scheduler.resource_monitor.psutil.virtual_memory", lambda: Memory())

    sample = ResourceMonitor().memory_sample(now=12.5)

    assert (sample.mem_total_bytes, sample.mem_free_bytes, sample.mem_available_bytes) == (65_000, 20_000, 40_000)
    assert sample.sampled_at_monotonic == 12.5


def test_memory_sample_refuses_impossible_figures() -> None:
    with pytest.raises(ValueError):
        memory_sample_from(total=0, free=0, available=0, sampled_at=1.0)
    with pytest.raises(ValueError):
        memory_sample_from(total=100, free=101, available=50, sampled_at=1.0)
    with pytest.raises(ValueError):
        memory_sample_from(total=100, free=50, available=101, sampled_at=1.0)
    with pytest.raises(ValueError):
        memory_sample_from(total=100, free=-1, available=50, sampled_at=1.0)
    with pytest.raises(ValueError):
        memory_sample_from(total=100, free=True, available=50, sampled_at=1.0)


def test_system_nonfree_upper_bound_takes_the_window_maximum() -> None:
    samples = [
        memory_sample_from(total=1_000, free=300, available=400, sampled_at=1.0),
        memory_sample_from(total=1_000, free=250, available=400, sampled_at=1.5),
        memory_sample_from(total=1_000, free=260, available=400, sampled_at=2.0),
    ]

    assert system_nonfree_upper_bound_v1(samples) == 750
    with pytest.raises(ValueError):
        system_nonfree_upper_bound_v1([])


def test_admission_sample_maps_the_available_figure_to_the_book_sample() -> None:
    sample = memory_sample_from(total=1_000, free=100, available=700, sampled_at=9.0)

    admission = admission_sample(sample)

    assert (admission.total_bytes, admission.available_bytes, admission.sampled_at) == (1_000, 700, 9.0)
