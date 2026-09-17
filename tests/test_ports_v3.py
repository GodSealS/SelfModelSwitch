"""Ports-v3 contract tests (M01/P03, C03).

The point of these tests is the contract every consumer relies on: fake ports
satisfy the structural protocols, observations express starting/unknown/stopped
facts without ever claiming a stop, terminal evidence covers dispatched and
locally terminated requests, and the single STOPPED computation needs all four
facts (a port occupied by an unknown process stays UNKNOWN).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from model_scheduler import ports_v3 as pv
from model_scheduler.contracts_v2 import ContractError, parse_model_spec
from model_scheduler.control_protocol_v1 import Fence, InstanceIdentity

UTC = timezone.utc


def _fence(**overrides) -> Fence:
    values = {
        "boot_id": "boot-0001",
        "model_id": "qwen25vl-7b-q4",
        "generation": 3,
        "operation_id": "op-0001",
        "execution_id": "e-1",
        "attempt": 1,
    }
    values.update(overrides)
    return Fence(**values)


def _instance(**overrides) -> InstanceIdentity:
    values = {
        "container_id": "c-abc",
        "started_at": "2026-09-17T05:00:00Z",
        "deployment_id": "orin-local",
        "model_id": "qwen25vl-7b-q4",
        "runtime_id": "llama-cpp-cuda-sm87-4bc272f",
        "candidate_digest": "b" * 64,
        "image_digest": "ghcr.io/example/llama-cuda@sha256:" + "c" * 64,
    }
    values.update(overrides)
    return InstanceIdentity(**values)


def _model_spec():
    return parse_model_spec(
        {
            "model_id": "qwen25vl-7b-q4",
            "runtime_id": "llama-cpp-cuda-sm87-4bc272f",
            "capabilities": ["chat", "vision"],
            "assets": [
                {
                    "role": "model",
                    "path": "qwen25vl-7b-q4/model.gguf",
                    "sha256": "3f" + "0" * 62,
                    "size_bytes": 4683072320,
                },
                {
                    "role": "projector",
                    "path": "qwen25vl-7b-q4/mmproj.gguf",
                    "sha256": "d1" + "0" * 62,
                    "size_bytes": 1354162912,
                },
            ],
            "port": 18081,
            "envelope": {
                "ctx_size": 32768,
                "max_input_tokens": 28672,
                "max_output_tokens": 4096,
                "max_parallel": 2,
                "max_image_tokens": 1280,
                "max_image_edge_pixels": 1024,
                "max_images": 1,
            },
            "timeout_seconds": 3600,
            "reserved_bytes": 6106148045,
            "measured": True,
            "measurement_ref": "e" * 64,
            "physical_resident_peak_bytes": 5000000000,
        }
    )


def _observation(**overrides) -> pv.Observation:
    values = {
        "state": pv.RUNNING,
        "sampled_at_monotonic": 100.0,
        "sampled_at_utc": datetime(2026, 9, 17, 5, 0, tzinfo=UTC),
        "port_state": "listening",
        "subprocess_state": "running",
        "instance": _instance(),
        "launch_operation": None,
    }
    values.update(overrides)
    return pv.Observation(**values)


def _launch(**overrides) -> pv.LaunchOperation:
    values = {
        "operation_id": "op-0001",
        "fence": _fence(),
        "started_at_monotonic": 90.0,
        "state": "starting",
    }
    values.update(overrides)
    return pv.LaunchOperation(**values)


class FakeBackend:
    """A fake backend: returns port-shaped values and records calls."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def load(self, spec, fence, deadline):
        self.calls.append(("load", spec.model_id, fence.operation_id, deadline))
        return _observation()

    async def execute(self, request, fence, deadline):
        self.calls.append(("execute", request.execution_id, fence.execution_id, deadline))
        return pv.ExecutionHandle(execution_id=request.execution_id, instance=_instance())

    async def cancel(self, handle, deadline):
        self.calls.append(("cancel", handle.execution_id, deadline))
        return pv.CancelAck(execution_id=handle.execution_id, accepted=True)

    async def stop(self, identity, fence, deadline):
        self.calls.append(("stop", identity.container_id, fence.operation_id, deadline))
        return pv.StopAck(accepted=True)


class FakeObserver:
    async def observe(self, target, deadline):
        return _observation()


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def utc_now(self) -> datetime:
        return datetime(2026, 9, 17, 5, 0, tzinfo=UTC)


class FakeSink:
    def __init__(self) -> None:
        self.events = []

    def emit(self, event) -> None:
        self.events.append(event)


def test_fake_ports_satisfy_the_structural_protocols():
    assert isinstance(FakeBackend(), pv.BackendPort)
    assert isinstance(FakeObserver(), pv.ObserverPort)
    assert isinstance(FakeClock(), pv.Clock)
    assert isinstance(FakeSink(), pv.EventSink)

    class Incomplete:
        async def load(self, spec, fence, deadline):
            return _observation()

    assert not isinstance(Incomplete(), pv.BackendPort)
    assert not isinstance(object(), pv.ObserverPort)


def test_fake_backend_returns_port_shaped_values():
    backend = FakeBackend()
    observation = asyncio.run(backend.load(_model_spec(), _fence(), 100.0))
    assert isinstance(observation, pv.Observation)

    handle = asyncio.run(
        backend.execute(
            pv.ExecutionRequest(execution_id="e-1", operation="chat", inline_input={"messages": []}),
            _fence(),
            100.0,
        )
    )
    assert handle.instance.container_id == "c-abc"

    ack = asyncio.run(backend.cancel(handle, 100.0))
    assert isinstance(ack, pv.CancelAck) and ack.accepted is True

    stop_ack = asyncio.run(backend.stop(_instance(), _fence(), 100.0))
    assert isinstance(stop_ack, pv.StopAck) and stop_ack.accepted is True


def test_observation_expresses_starting_unknown_and_running_facts():
    starting = _observation(launch_operation=_launch(state="starting", pid=1234, process_group_id=1234))
    assert starting.launch_operation is not None
    assert not starting.launch_operation.is_terminal

    finished = _observation(
        launch_operation=_launch(state="completed", pid=1234, process_group_id=1234, terminal_at_monotonic=95.0)
    )
    assert finished.launch_operation is not None and finished.launch_operation.is_terminal

    unknown = _observation(state=pv.UNKNOWN, port_state="unknown", subprocess_state="unknown", instance=None)
    assert unknown.state == pv.UNKNOWN

    with pytest.raises(ContractError):
        _observation(state="probably-stopped")
    with pytest.raises(ContractError):
        _observation(port_state="maybe")


def test_launch_operation_terminal_rules():
    with pytest.raises(ContractError):  # a terminal state needs a terminal timestamp
        _launch(state="completed")
    with pytest.raises(ContractError):  # a starting operation has no terminal timestamp
        _launch(state="starting", terminal_at_monotonic=95.0)
    with pytest.raises(ContractError):
        _launch(state="unknown")
    with pytest.raises(ContractError):
        _launch(pid=0)


def test_stopped_is_proven_needs_all_four_facts():
    assert pv.stopped_is_proven(
        container_absent=True,
        launch_operation_terminal=True,
        subprocess_exited=True,
        port_listening=False,
    )
    for overrides in (
        {"container_absent": False},
        {"launch_operation_terminal": False},
        {"subprocess_exited": False},
        {"port_listening": None},
        {"port_listening": True},
    ):
        facts = {
            "container_absent": True,
            "launch_operation_terminal": True,
            "subprocess_exited": True,
            "port_listening": False,
        }
        facts.update(overrides)
        assert not pv.stopped_is_proven(**facts)


def test_termination_evidence_covers_dispatched_and_not_started():
    dispatched = pv.TerminationEvidence(
        fence=_fence(),
        dispatch_state="dispatched",
        compute_quiescent=True,
        device_synchronized=True,
        reason="process exited with code 0",
        instance=_instance(),
    )
    assert dispatched.instance is not None

    local = pv.TerminationEvidence(
        fence=_fence(execution_id=None, attempt=None),
        dispatch_state="not_started",
        compute_quiescent=True,
        device_synchronized=False,
    )
    assert local.instance is None
    assert local.reason is None

    with pytest.raises(ContractError):  # dispatched without instance identity
        pv.TerminationEvidence(
            fence=_fence(),
            dispatch_state="dispatched",
            compute_quiescent=True,
            device_synchronized=True,
            reason="exit",
        )
    with pytest.raises(ContractError):  # not_started must not invent a container
        pv.TerminationEvidence(
            fence=_fence(),
            dispatch_state="not_started",
            compute_quiescent=True,
            device_synchronized=False,
            instance=_instance(),
        )
    with pytest.raises(ContractError):  # no quiescence claim
        pv.TerminationEvidence(
            fence=_fence(),
            dispatch_state="dispatched",
            compute_quiescent=False,
            device_synchronized=True,
            reason="exit",
            instance=_instance(),
        )
    with pytest.raises(ContractError):  # a non-execution fence cannot prove an execution terminal
        pv.TerminationEvidence(
            fence=_fence(execution_id=None, attempt=None),
            dispatch_state="dispatched",
            compute_quiescent=True,
            device_synchronized=True,
            reason="exit",
            instance=_instance(),
        )


def test_memory_sample_freshness_boundaries():
    sample = pv.MemorySample(
        mem_total_bytes=64_000_000_000,
        mem_free_bytes=20_000_000_000,
        mem_available_bytes=40_000_000_000,
        sampled_at_monotonic=100.0,
    )
    assert pv.sample_is_fresh(sample, 100.0)
    assert pv.sample_is_fresh(sample, 102.0)  # the 2s boundary is inclusive
    assert not pv.sample_is_fresh(sample, 102.001)
    assert not pv.sample_is_fresh(sample, 99.0)  # a future sample is invalid

    with pytest.raises(ContractError):
        pv.MemorySample(mem_total_bytes=-1, mem_free_bytes=0, mem_available_bytes=0, sampled_at_monotonic=1.0)
    with pytest.raises(ContractError):
        pv.MemorySample(mem_total_bytes=True, mem_free_bytes=0, mem_available_bytes=0, sampled_at_monotonic=1.0)


def test_event_record_shape():
    event = pv.EventRecord(
        schema_version=1,
        event_id="ev-0001",
        sequence=1,
        utc_time=datetime(2026, 9, 17, 5, 0, tzinfo=UTC),
        monotonic_time=100.0,
        fence=_fence(),
        type="execution.queued",
        payload={"execution_id": "e-1"},
    )
    assert event.sequence == 1

    with pytest.raises(ContractError):
        pv.EventRecord(
            schema_version=2,
            event_id="ev-0001",
            sequence=1,
            utc_time=datetime(2026, 9, 17, 5, 0, tzinfo=UTC),
            monotonic_time=100.0,
            fence=_fence(),
            type="execution.queued",
        )
    with pytest.raises(ContractError):
        pv.EventRecord(
            schema_version=1,
            event_id="ev-0001",
            sequence=0,
            utc_time=datetime(2026, 9, 17, 5, 0, tzinfo=UTC),
            monotonic_time=100.0,
            fence=_fence(),
            type="execution.queued",
        )
    with pytest.raises(ContractError):  # a naive datetime is not acceptable evidence
        pv.EventRecord(
            schema_version=1,
            event_id="ev-0001",
            sequence=1,
            utc_time=datetime(2026, 9, 17, 5, 0),
            monotonic_time=100.0,
            fence=_fence(),
            type="execution.queued",
        )


def test_execution_request_requires_exactly_one_input():
    inline = pv.ExecutionRequest(execution_id="e-1", operation="chat", inline_input={"messages": []})
    assert inline.blob_path is None

    blob = pv.ExecutionRequest(execution_id="e-1", operation="vision", blob_path="/work/blobs/b-1/data.png")
    assert blob.inline_input is None

    with pytest.raises(ContractError):  # neither input
        pv.ExecutionRequest(execution_id="e-1", operation="chat")
    with pytest.raises(ContractError):  # both inputs
        pv.ExecutionRequest(
            execution_id="e-1",
            operation="chat",
            inline_input={"messages": []},
            blob_path="/work/blobs/b-1/data.json",
        )


def test_observation_target_shape():
    target = pv.ObservationTarget(deployment_id="orin-local", container_id="c-abc", process_group_id=1234)
    assert target.container_id == "c-abc"
    with pytest.raises(ContractError):
        pv.ObservationTarget(deployment_id="")
    with pytest.raises(ContractError):
        pv.ObservationTarget(deployment_id="orin-local", process_group_id=0)


def test_acknowledgements_are_not_stop_proofs():
    cancel = pv.CancelAck(execution_id="e-1", accepted=True)
    stop = pv.StopAck(accepted=True)
    assert cancel.accepted is True and stop.accepted is True
    with pytest.raises(ContractError):
        pv.CancelAck(execution_id="e-1", accepted=1)
    with pytest.raises(ContractError):
        pv.StopAck(accepted="yes")
