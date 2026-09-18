"""P22: the collector persists raw events and raw samples, decoupled from the executor.

Every persisted row must carry both clocks, the run/case/attempt context, the fence
and the instance/device attribution; device activity is only ever derived from raw
samples, never accepted as a boolean claim, and nothing is ever deleted.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from model_scheduler.acceptance import collector as pc
from model_scheduler.control_protocol_v1 import Fence, InstanceIdentity
from model_scheduler.ports_v3 import EventRecord


def _fence(**overrides) -> Fence:
    base = dict(boot_id="boot-1", model_id="qwen-small", generation=1, operation_id="op-1",
                execution_id="exec-1", attempt=1)
    base.update(overrides)
    return Fence(**base)


def _event(sequence: int = 1, **overrides) -> EventRecord:
    base = dict(schema_version=1, event_id="evt-1", sequence=sequence,
                utc_time=datetime(2026, 9, 18, 0, 0, sequence, tzinfo=timezone.utc),
                monotonic_time=1000.0 + sequence, fence=_fence(), type="load_started",
                payload={"stage": "setup"})
    base.update(overrides)
    return EventRecord(**base)


def _collector(tmp_path: Path, **overrides) -> pc.FileCollector:
    base = dict(run_id="run-1", candidate_sha256="a" * 64, device_digest="b" * 64, boot_id="boot-1")
    base.update(overrides)
    return pc.FileCollector(tmp_path, **base)


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_a_persisted_event_carries_both_clocks_fence_context_and_instance(tmp_path) -> None:
    sink = _collector(tmp_path)
    sink.begin_case("B:qwen-small:load", attempt=1, instance=InstanceIdentity(
        container_id="c1", started_at="2026-09-18T00:00:00Z", deployment_id="dep-1", model_id="qwen-small",
        runtime_id="llama-cpp", candidate_digest="a" * 64, image_digest="sha256:" + "c" * 64))
    sink.emit(_event(1))
    sink.emit(_event(2, fence=_fence(execution_id=None, attempt=None), type="load_finished"))
    manifest = sink.close()

    rows = _rows(tmp_path / "events.jsonl")
    assert [row["event"]["sequence"] for row in rows] == [1, 2]
    first = rows[0]
    assert first["run_id"] == "run-1"
    assert first["case_id"] == "B:qwen-small:load" and first["attempt"] == 1
    assert first["candidate_sha256"] == "a" * 64 and first["device_digest"] == "b" * 64
    assert first["boot_id"] == "boot-1"
    assert first["instance"]["container_id"] == "c1" and first["instance"]["image_digest"] == "sha256:" + "c" * 64
    assert first["event"]["monotonic_time"] == 1001.0
    assert first["event"]["utc_time"] == "2026-09-18T00:00:01+00:00"
    assert first["event"]["fence"]["operation_id"] == "op-1"
    assert first["persisted_monotonic"] >= first["event"]["monotonic_time"]
    assert first["persisted_utc"].endswith("+00:00")

    files = {entry["relative_path"]: entry for entry in manifest["files"]}
    payload = (tmp_path / "events.jsonl").read_bytes()
    assert files["events.jsonl"]["size_bytes"] == len(payload)
    assert files["events.jsonl"]["sha256"] == hashlib.sha256(payload).hexdigest()
    assert manifest["collector_version"] == pc.COLLECTOR_VERSION
    assert manifest["collector_sha256"] == pc.collector_sha256()
    assert manifest["candidate_sha256"] == "a" * 64 and manifest["device_digest"] == "b" * 64


def test_events_outside_a_case_and_out_of_order_sequences_are_refused(tmp_path) -> None:
    sink = _collector(tmp_path)

    with pytest.raises(pc.CollectorError, match="case context"):
        sink.emit(_event(1))  # no begin_case: the event would carry no case attribution

    sink.begin_case("B:qwen-small:load", attempt=1)
    sink.emit(_event(5))
    with pytest.raises(pc.CollectorError, match="strictly increasing"):
        sink.emit(_event(5))
    with pytest.raises(pc.CollectorError, match="strictly increasing"):
        sink.emit(_event(4))
    sink.emit(_event(6))  # the order is enforced, not repaired
    assert [row["event"]["sequence"] for row in _rows(tmp_path / "events.jsonl")] == [5, 6]


def test_a_failure_is_recorded_and_nothing_already_written_is_deleted(tmp_path) -> None:
    sink = _collector(tmp_path)
    sink.begin_case("B:qwen-small:load", attempt=1)
    sink.emit(_event(1))
    sink.record_failure(stage="load", error="the container exited before ready", detail={"exit_code": 1})
    manifest = sink.close()

    rows = _rows(tmp_path / "failures.jsonl")
    assert rows[0]["stage"] == "load" and rows[0]["detail"]["exit_code"] == 1 and rows[0]["case_id"].endswith(":load")
    assert (tmp_path / "events.jsonl").is_file()  # failed material is kept, never pruned
    assert {entry["relative_path"] for entry in manifest["files"]} >= {"events.jsonl", "failures.jsonl"}


def test_device_attribution_comes_from_raw_samples_not_a_boolean(tmp_path) -> None:
    sink = _collector(tmp_path)
    sink.record_sample("tegrastats", "RAM 1234/32000MB SWAP 0/16000MB GR3D_FREQ 41% cpu@1")
    sink.record_sample("tegrastats", "RAM 4321/32000MB SWAP 0/16000MB GR3D_FREQ 87% cpu@1")
    sink.record_sample("proc_maps", "7f2b0000-7f2b1000 r-xp /usr/lib/aarch64-linux-gnu/libcuda.so.1")
    sink.record_sample("nvidia_smi", "Orin (nvgpu), 8.7")

    attribution = sink.attribution()

    assert attribution["gr3d_peak_pct"] == 87
    assert attribution["cuda_library_mapped"] is True
    assert attribution["raw_samples"] == {"nvidia_smi": 1, "proc_maps": 1, "tegrastats": 2}
    assert "gpu_verified" not in attribution  # a boolean is never the evidence


def test_attribution_without_raw_samples_is_refused(tmp_path) -> None:
    sink = _collector(tmp_path)

    with pytest.raises(pc.CollectorError, match="raw sample"):
        sink.attribution()


def test_raw_samples_keep_the_original_text_and_count(tmp_path) -> None:
    sink = _collector(tmp_path)
    raw = "RAM 1234/32000MB SWAP 0/16000MB GR3D_FREQ 12% cpu@1"
    sink.record_sample("tegrastats", raw, note="baseline window")

    rows = _rows(tmp_path / "samples" / "tegrastats.jsonl")
    assert rows[0]["raw"] == raw and rows[0]["note"] == "baseline window"
    assert rows[0]["run_id"] == "run-1" and rows[0]["persisted_utc"].endswith("+00:00")


def test_the_collector_satisfies_the_event_sink_protocol(tmp_path) -> None:
    from model_scheduler.ports_v3 import EventSink

    assert isinstance(_collector(tmp_path), EventSink)


def test_the_collector_hash_is_the_source_of_this_module() -> None:
    source = Path(pc.__file__).read_bytes()

    assert pc.collector_sha256() == hashlib.sha256(source).hexdigest()
    assert pc.COLLECTOR_VERSION == 1
