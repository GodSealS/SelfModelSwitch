"""Managed execution end to end (M04/P16, K4): adapter + observer + service on one book.

The P14 suite proved the queue and writeback rules against a fake that fed
terminals by hand. This file removes that fiction: the scheduler's load/stop
goes through the real `ManagedLifecycle` bridge, dispatched executions settle
only through the trusted-termination loop the service now owns, and the last
section composes the REAL llama.cpp adapter over a scripted llama-server and a
real `DockerProcessObserver` over a fake docker CLI.

Rules under test (plan/08-execution-plan.md P16):

* a runtime without a trusted per-request sync protocol terminates an
  execution by proving an independent STOPPED of the shared instance;
* requests whose response already ended but whose STOPPED proof is pending
  (`awaiting_quiescence`) never block the stop and never count as waiting
  compute, while still-in-flight requests do;
* after the fallback stop the session stays ACTIVE, the model is UNLOADED and
  the next execution re-loads at a new generation with a re-signed fence;
* a StopAck whose facts never land keeps the ledger (never a fake release),
  and no partial output is ever published.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Any, Callable

import httpx
import pytest

from model_scheduler import control_protocol_v1 as cp
from model_scheduler import ports_v3 as pv
from model_scheduler.backend_control import ManagedLifecycle
from model_scheduler.blob_store import BlobStore
from model_scheduler.contracts import Capability, MemorySample, ModelSpec
from model_scheduler.control_protocol_v1 import Fence, InstanceIdentity
from model_scheduler.execution_service import ExecutionService
from model_scheduler.model_registry import Book
from model_scheduler.ports_v3 import CancelAck, ExecutionHandle
from model_scheduler.scheduler import ModelScheduler
from model_scheduler.session_manager import SessionManager


BODY = b'{"ok":true}'
IDENTITY = InstanceIdentity(
    container_id="c-managed",
    started_at="2026-09-18T05:00:00Z",
    deployment_id="orin-lab",
    model_id="chat",
    runtime_id="llama-cpp",
    candidate_digest="b" * 64,
    image_digest="repo/llama@sha256:" + "c" * 64,
)

FAST_POLICY = pv.LifecyclePolicy(verify_window_seconds=5.0, poll_seconds=0.01,
                                 observation_timeout_seconds=1.0, observation_max_age_seconds=1.0,
                                 recovery_seconds=5.0)


def advancing_sleep(clock: "ManualClock"):
    """Fake time: a poll that never moved the clock would spin forever."""

    async def _sleep(seconds: float) -> None:
        clock.now += seconds
        await asyncio.sleep(0)

    return _sleep


class ManualClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ClockedResources:
    def __init__(self, clock: ManualClock) -> None:
        self.clock = clock

    async def snapshot(self) -> MemorySample:
        return MemorySample(10_000, 9_000, self.clock.now)


class SwapControl:
    """The v3 lifecycle adapter role for these tests: llama-swap load/unload as facts.

    ``stop_kills_container`` scripts the two worlds P16 must not confuse: an ack
    whose instance really stops, and an ack whose instance defies the command
    (then nothing may be released).
    """

    def __init__(self, world: dict, *, stop_kills_container: bool = True, identity: InstanceIdentity = IDENTITY) -> None:
        self.world = world
        self.stop_kills = stop_kills_container
        self.identity = identity
        self.loads = 0
        self.stops: list[str] = []

    async def load(self, spec, fence, deadline) -> pv.Observation:
        self.loads += 1
        self.world["container"] = True
        return pv.Observation(state=pv.RUNNING, sampled_at_monotonic=0.0, sampled_at_utc=_utc(),
                              port_state="listening", subprocess_state="running",
                              instance=self.identity, launch_operation=None)

    async def stop(self, identity, fence, deadline) -> pv.StopAck:
        self.stops.append(identity.model_id)
        if self.stop_kills:
            self.world["container"] = False  # the ack itself is still not proof; the world decides
        return pv.StopAck(accepted=True)


def _utc() -> datetime:
    return datetime(2026, 9, 18, 5, 0, tzinfo=timezone.utc)


class WorldObserver:
    """A ports_v3.ObserverPort mapping the scripted world onto a full C03 observation."""

    def __init__(self, world: dict, identity: InstanceIdentity) -> None:
        self.world = world
        self.identity = identity
        self.observations = 0

    async def observe(self, target, deadline) -> pv.Observation:
        self.observations += 1
        if self.world["container"]:
            return pv.Observation(state=pv.RUNNING, sampled_at_monotonic=self.world["clock"].now,
                                  sampled_at_utc=_utc(), port_state="listening", subprocess_state="running",
                                  instance=self.identity, launch_operation=None)
        return pv.Observation(state=pv.STOPPED, sampled_at_monotonic=self.world["clock"].now,
                              sampled_at_utc=_utc(), port_state="closed", subprocess_state="exited",
                              instance=None, launch_operation=None)


class ManagedExec:
    """A BackendPort fake with the P16 surface: claims, current identity, captured results."""

    def __init__(self, *, claims: bool = False, result: bytes | None = BODY, raises: Exception | None = None,
                 gates: list[asyncio.Event] | None = None, identity: InstanceIdentity = IDENTITY) -> None:
        self.claims = claims
        self.result = result
        self.raises = raises
        self.gates = list(gates or [])  # execute n holds gate n open until the test releases it
        self.identity = identity
        self.requests: list = []
        self.cancels: list[str] = []
        self._gate_index = 0

    def claims_device_quiescence(self) -> bool:
        return self.claims

    @property
    def current_identity(self) -> InstanceIdentity:
        return self.identity

    async def execute(self, request, fence, deadline) -> ExecutionHandle:
        self.requests.append((request, fence))
        index = self._gate_index
        self._gate_index += 1  # claim the gate before awaiting: two in-flight requests wait separately
        if index < len(self.gates):
            await self.gates[index].wait()
        if self.raises is not None:
            raise self.raises
        return ExecutionHandle(execution_id=request.execution_id, instance=self.identity)

    def take_result(self, execution_id: str) -> bytes | None:
        return self.result

    async def cancel(self, handle, deadline) -> CancelAck:
        self.cancels.append(handle.execution_id)
        return CancelAck(execution_id=handle.execution_id, accepted=True)

    async def load(self, spec, fence, deadline):  # pragma: no cover
        raise AssertionError("the lifecycle owns load, not the execution service")

    async def stop(self, identity, fence, deadline):  # pragma: no cover
        raise AssertionError("the lifecycle owns stop, not the execution service")


@dataclass
class ManagedStack:
    clock: ManualClock
    scheduler: ModelScheduler
    book: Book
    control: SwapControl
    lifecycle: ManagedLifecycle
    backend: ManagedExec
    service: ExecutionService
    blobs: BlobStore
    world: dict

    def leases(self) -> set[str]:
        return {lease.request_id for runtime in self.book.runtime.values() for lease in runtime.leases.values()}


async def start_managed(tmp_path, *, claims: bool = False, result: bytes | None = BODY,
                        raises: Exception | None = None, gates: list[asyncio.Event] | None = None,
                        max_concurrency: int = 1, stop_grace_seconds: float = 10.0,
                        stop_kills_container: bool = True) -> ManagedStack:
    clock = ManualClock()
    sessions = SessionManager(wait_seconds=100.0, hard_deadline_seconds=3600.0, heartbeat_seconds=10.0,
                              ttl_seconds=30.0, prepare_seconds=100.0, drain_seconds=30.0, retry_seconds=30.0,
                              cleanup_seconds=60.0, cancel_seconds=10.0, stop_grace_seconds=30.0, reconcile_seconds=5.0)
    spec = ModelSpec("chat", "http://127.0.0.1:18080", frozenset({Capability.CHAT}), 100, max_concurrency=max_concurrency)
    book = Book({"chat": spec}, model_budget=1_000, free_floor=20, margin=0)
    book.bootstrap_stopped("chat")
    world: dict = {"container": False, "clock": clock}
    control = SwapControl(world, stop_kills_container=stop_kills_container)
    lifecycle = ManagedLifecycle(
        boot_id="boot-1", deployment_id="orin-lab", specs={"chat": object()},
        adapter_for=lambda model_id: control, observers={"chat": WorldObserver(world, IDENTITY)},
        instance_lookup=book.instance, policy=FAST_POLICY, now=clock, sleep=advancing_sleep(clock),
    )
    scheduler = ModelScheduler(book, ClockedResources(clock), lifecycle, sessions=sessions, clock=clock,
                               poll_interval_seconds=0.01)
    await asyncio.wait_for(scheduler.open_session("chat", "client-a", "session-1"), 5)
    blobs = BlobStore(tmp_path / "blobs", clock=clock)
    backend = ManagedExec(claims=claims, result=result, raises=raises, gates=gates)
    service = ExecutionService(
        scheduler, blobs=blobs, backend_for=lambda model_id: backend, boot_id="boot-1", clock=clock,
        poll_seconds=0.01, managed_termination=True, stop_grace_seconds=stop_grace_seconds,
    )
    return ManagedStack(clock=clock, scheduler=scheduler, book=book, control=control, lifecycle=lifecycle,
                        backend=backend, service=service, blobs=blobs, world=world)


def exec_document(key: str) -> dict:
    return {
        "session_token": "opaque-token",
        "operation": "chat",
        "input": {"inline": {"messages": [{"role": "user", "content": "hi"}]}},
        "parameters": {"max_tokens": 8},
        "idempotency_key": key,
    }


async def submit(stack: ManagedStack, key: str = "k-1") -> dict:
    return await stack.service.submit(session_id="session-1", owner="uid:1000", document=exec_document(key))


async def eventually(predicate: Callable[[], bool], *, attempts: int = 1200) -> bool:
    for _ in range(attempts):
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


@pytest.mark.asyncio
async def test_a_completed_request_terminates_by_proving_an_independent_stop(tmp_path) -> None:
    stack = await start_managed(tmp_path)
    assert stack.book.runtime["chat"].generation == 1  # the session prepare loaded the model
    assert stack.lifecycle.instance("chat") == IDENTITY

    view = await submit(stack)
    assert await eventually(lambda: stack.service.record(view["execution_id"]).state == "succeeded")
    record = stack.service.record(view["execution_id"])
    final = await stack.service.view(view["execution_id"], owner="uid:1000")
    assert cp.parse_execution_view(final)
    assert final["dispatch_state"] == "dispatched" and final["instance"] is not None
    assert record.evidence is not None and record.evidence.reason == "independent_STOPPED"
    # instance/Fence consistency: the terminal names the very identity load verified
    assert record.evidence.instance == IDENTITY == record.instance
    assert record.evidence.fence == record.fence
    # the stop was proven, so the books released the reservation and the model is UNLOADED
    assert not stack.leases()
    assert stack.book.runtime["chat"].state.value == "unloaded"
    assert stack.book.committed == 0
    assert stack.blobs.metadata.usage("uid-1000").total_bytes == len(BODY)
    assert await stack.blobs.read_all(record.output_blob_id, "uid-1000", "probe") == BODY


@pytest.mark.asyncio
async def test_the_next_execution_reloads_at_a_new_generation_with_a_re_signed_fence(tmp_path) -> None:
    stack = await start_managed(tmp_path)
    first = await submit(stack)
    assert await eventually(lambda: stack.service.record(first["execution_id"]).state == "succeeded")
    assert (await stack.scheduler.session_view("session-1"))["phase"] == "active"  # AC3: the session kept its grant

    stack.clock.advance(0.1)  # the post-stop resample window needs a later fact
    second = await submit(stack, key="k-2")
    assert await eventually(lambda: stack.service.record(second["execution_id"]).state == "succeeded")
    record = stack.service.record(second["execution_id"])
    assert record.fence.generation == 2  # UNLOADED -> load bumps the generation
    assert stack.control.loads == 2      # the second load really went through llama-swap again
    # the first execution stays what it was; a replayed stale-generation terminal changes nothing
    stale = pv.TerminationEvidence(fence=Fence("boot-1", "chat", 1, record.fence.operation_id,
                                               first["execution_id"], 1),
                                   dispatch_state="dispatched", compute_quiescent=True, device_synchronized=True,
                                   reason="late", instance=IDENTITY)
    assert await stack.service.report_terminal(first["execution_id"], stale) is None


@pytest.mark.asyncio
async def test_a_cancelled_request_finishes_by_the_same_stop_and_publishes_nothing(tmp_path) -> None:
    gate = asyncio.Event()
    stack = await start_managed(tmp_path, gates=[gate])
    view = await submit(stack)
    execution_id = view["execution_id"]
    assert await eventually(lambda: len(stack.backend.requests) == 1)  # in flight, response not ended

    cancelling = await stack.service.cancel(execution_id, owner="uid:1000")
    assert cancelling["state"] == "cancelling"  # P10: the lease is kept until the terminal
    gate.set()  # the response ends per protocol; only the stop can prove the terminal
    assert await eventually(lambda: stack.service.record(execution_id).state == "cancelled")
    record = stack.service.record(execution_id)
    assert record.evidence is not None and record.evidence.reason == "independent_STOPPED"
    assert record.result is None  # the finished answer is never published after a cancel
    assert stack.backend.cancels == [execution_id]
    await eventually(lambda: stack.blobs.metadata.usage("uid-1000").total_bytes == 0)
    assert stack.book.runtime["chat"].state.value == "unloaded"


@pytest.mark.asyncio
async def test_in_flight_requests_are_never_cut_off_and_new_dispatches_freeze(tmp_path) -> None:
    gates = [asyncio.Event(), asyncio.Event()]
    stack = await start_managed(tmp_path, max_concurrency=2, gates=gates)
    first = await submit(stack, key="k-1")
    second = await submit(stack, key="k-2")
    first_id, second_id = first["execution_id"], second["execution_id"]
    assert await eventually(lambda: len(stack.backend.requests) == 2)  # both computing

    await stack.service.cancel(first_id, owner="uid:1000")
    gates[0].set()  # one response ends per protocol; it may not cut off the other computation
    assert await eventually(lambda: stack.service.record(first_id).awaiting_quiescence
                            or stack.service.record(second_id).awaiting_quiescence)
    third = await submit(stack, key="k-3")
    await asyncio.sleep(0.05)
    assert stack.service.record(third["execution_id"]).state == "queued"  # AC2: dispatch is frozen
    assert len(stack.backend.requests) == 2  # ... and no inference started for it
    assert stack.control.stops == []  # no shared stop while one request is still computing

    gates[1].set()  # the last computation ends -> ONE shared stop settles both
    assert await eventually(lambda: stack.service.record(first_id).settled
                            and stack.service.record(second_id).settled)
    assert stack.service.record(first_id).state == "cancelled"  # the cancelled one never published
    assert stack.service.record(first_id).result is None
    assert stack.service.record(second_id).state == "succeeded"
    assert stack.control.stops == ["chat"]  # exactly one instance stop for the whole batch
    assert await eventually(lambda: stack.service.record(third["execution_id"]).state == "succeeded")
    assert stack.control.stops.count("chat") == 2  # the third needed its own load+stop cycle
    assert record_publication(stack, second_id)


def record_publication(stack: ManagedStack, execution_id: str) -> bool:
    entry = stack.service.record(execution_id)
    return entry.result is not None and entry.state == "succeeded"


@pytest.mark.asyncio
async def test_a_stop_ack_whose_facts_never_land_keeps_the_ledger_and_the_session(tmp_path) -> None:
    stack = await start_managed(tmp_path, stop_grace_seconds=0.2, stop_kills_container=False)
    view = await submit(stack)
    execution_id = view["execution_id"]
    assert await eventually(lambda: stack.service.record(execution_id).awaiting_quiescence)
    # the lease of an AWAITING request is not a waiting object: it returns early...
    assert await eventually(lambda: execution_id not in stack.leases())
    # ... but nothing may settle and no budget may move while the container defies the stop
    assert not stack.service.record(execution_id).settled
    await asyncio.sleep(0.5)  # the quiescer gives up past its grace window without inventing a terminal
    assert stack.book.runtime["chat"].state.value == "error"  # stop_unverified keeps the books honest
    assert stack.book.committed == 100  # the reservation is kept until an independent stop
    heartbeated = await stack.scheduler.heartbeat_session("session-1")  # AC3: heartbeats continue
    assert heartbeated["phase"] == "active"
    assert stack.service.record(execution_id).state == "running"  # still waiting for proof, never a fake result
    assert stack.service.record(execution_id).result is None


@pytest.mark.asyncio
async def test_a_backend_that_claims_trusted_sync_needs_no_instance_stop(tmp_path) -> None:
    stack = await start_managed(tmp_path, claims=True)
    view = await submit(stack)
    assert await eventually(lambda: stack.service.record(view["execution_id"]).state == "succeeded")
    record = stack.service.record(view["execution_id"])
    assert record.evidence is not None and record.evidence.reason == "request_protocol_terminated"
    assert stack.control.stops == []  # the shared instance stays loaded
    assert stack.book.runtime["chat"].state.value == "ready"


@pytest.mark.asyncio
async def test_a_broken_stream_publishes_nothing_and_waits_for_the_stop_proof(tmp_path) -> None:
    stack = await start_managed(tmp_path, raises=RuntimeError("stream broke mid-answer"))
    view = await submit(stack)
    execution_id = view["execution_id"]
    assert await eventually(lambda: stack.service.record(execution_id).awaiting_quiescence)
    assert await eventually(lambda: stack.service.record(execution_id).state == "failed")
    record = stack.service.record(execution_id)
    assert record.error is not None and record.error.code == "backend_failed"
    assert record.result is None and record.evidence is not None and record.evidence.reason == "independent_STOPPED"
    await eventually(lambda: stack.blobs.metadata.usage("uid-1000").total_bytes == 0)


# -- P16 AC1/AC3 end to end: the REAL llama.cpp adapter and the REAL docker observer --------

from model_scheduler.contracts_v2 import parse_deployment  # noqa: E402
from model_scheduler.process_observer import (  # noqa: E402
    CONFIG_LABEL,
    DEPLOYMENT_LABEL,
    MODEL_LABEL,
    RUNTIME_LABEL,
    DockerProcessObserver,
    SystemClock,
    canonical_utc,
)
from model_scheduler.runtime import build_managed_execution  # noqa: E402

CHAT_OUTPUT = {"model": "lab-qwen", "choices": [{"message": {"role": "assistant", "content": "hello"}}]}
LAB_MODEL = "lab-qwen"
LAB_DEPLOYMENT = "orin-lab"
LAB_RUNTIME = "llama-cpp-gguf-v1"
LAB_CONFIG_SHA = "f" * 64
LAB_IMAGE = "repo/llama@sha256:" + "c" * 64   # what the lab deployment launches


def lab_deployment():
    return parse_deployment({
        "schema_version": 2,
        "runtimes": [{
            "runtime_id": "rt-llama",
            "profile_id": "llama-cpp-gguf-v1",
            "image_digest": "repo/llama@sha256:" + "a" * 64,
            "adapter_sha256": "b" * 64,
            "lock_sha256": "c" * 64,
            "startup_args": ["--parallel", "--kv-unified-per-slot", "--no-warmup"],
        }],
        "models": [{
            "model_id": LAB_MODEL,
            "runtime_id": "rt-llama",
            "capabilities": ["chat"],
            "assets": [{"role": "model", "path": "lab/model.gguf", "sha256": "3f" + "0" * 62, "size_bytes": 1024}],
            "port": 18099,
            "envelope": {"ctx_size": 4096, "max_input_tokens": 2048, "max_output_tokens": 1024,
                         "max_parallel": 1, "max_image_tokens": 0, "max_image_edge_pixels": 0, "max_images": 0},
            "timeout_seconds": 3600,
            "reserved_bytes": 100,
            "measured": True,
            "measurement_ref": "e" * 64,
            "physical_resident_peak_bytes": 80,
        }],
    })


class LabDocker:
    """Fake docker CLI: a swap boot lands a NEW labelled container; an unload exits it."""

    def __init__(self) -> None:
        self.facts: dict[str, dict] = {}
        self.booted = 0

    def boot(self) -> str:
        self.booted += 1
        container_id = f"container-{self.booted}"
        self.facts[container_id] = {
            "Id": container_id,
            "Image": "sha256:" + "d" * 64,
            "Config": {"Image": LAB_IMAGE, "Labels": {DEPLOYMENT_LABEL: LAB_DEPLOYMENT, MODEL_LABEL: LAB_MODEL,
                                                      RUNTIME_LABEL: LAB_RUNTIME, CONFIG_LABEL: LAB_CONFIG_SHA}},
            "State": {"Running": True, "Status": "running", "ExitCode": 0,
                      "StartedAt": f"2026-09-18T05:0{self.booted}:00.000000000Z"},
        }
        return container_id

    def kill(self) -> None:
        for fact in self.facts.values():
            if fact["State"]["Running"]:
                fact["State"] = {**fact["State"], "Running": False, "Status": "exited", "ExitCode": 0}

    def __call__(self, argv):
        verb = argv[1] if len(argv) > 1 else ""
        if verb == "ps":
            wanted: dict[str, str] = {}
            for index, token in enumerate(argv):
                if token == "--filter":
                    label, _, value = argv[index + 1].partition("=")
                    if label == "label":
                        key, _, val = value.partition("=")
                        wanted[key] = val
            listed = [fid for fid, fact in self.facts.items()
                      if all(fact["Config"]["Labels"].get(k) == v for k, v in wanted.items())]
            return 0, "".join(f"{fid}\n" for fid in listed), ""
        if verb == "inspect":
            wanted_ids = argv[2:]
            if any(fid not in self.facts for fid in wanted_ids):
                return 1, "", f"Error: No such object: {wanted_ids[0]}"
            return 0, json.dumps([self.facts[fid] for fid in wanted_ids]), ""
        raise AssertionError(f"unexpected docker command: {argv}")

    def identity(self, container_id: str) -> InstanceIdentity:
        fact = self.facts[container_id]
        return InstanceIdentity(container_id=container_id, started_at=canonical_utc(fact["State"]["StartedAt"]),
                                deployment_id=LAB_DEPLOYMENT, model_id=LAB_MODEL, runtime_id=LAB_RUNTIME,
                                candidate_digest=LAB_CONFIG_SHA, image_digest=fact["Config"]["Image"])


class LabSwap:
    """llama-swap lifecycle: load boots a fresh container, unload exits it (load/unload only)."""

    def __init__(self, docker: LabDocker) -> None:
        self.docker = docker
        self.loads: list[str] = []
        self.unloads: list[str] = []

    async def load(self, model_id: str) -> None:
        self.loads.append(model_id)
        self.docker.boot()

    async def unload(self, model_id: str) -> None:
        self.unloads.append(model_id)
        self.docker.kill()


def lab_server(hang: asyncio.Event | None = None) -> tuple[httpx.AsyncClient, dict]:
    calls = {"chat": 0}

    async def chat(request: httpx.Request) -> httpx.Response:
        calls["chat"] += 1
        if hang is not None:
            await hang.wait()
        return httpx.Response(200, json=CHAT_OUTPUT)

    routes: dict[str, Any] = {
        "/health": (200, {"status": "ok"}),
        "/slots": (200, [{"id": 0, "is_processing": False, "n_ctx": 4096}]),
        "/apply-template": lambda request: httpx.Response(200, json={"prompt": "hi"}),
        "/tokenize": lambda request: httpx.Response(200, json={"tokens": [1]}),
        "/v1/chat/completions": chat,
    }

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            handler = routes.get(request.url.path)
            if handler is None:
                return httpx.Response(404, json={"error": "missing"})
            if callable(handler):
                result = handler(request)
                if asyncio.iscoroutine(result):
                    result = await result
                return result
            status, payload = handler
            return httpx.Response(status, json=payload)

    client = httpx.AsyncClient(transport=Transport(), follow_redirects=False, base_url="http://127.0.0.1:18099")
    return client, calls


class LabResources:
    """A fresh, ample memory sample on the REAL monotonic clock (the observer's time base)."""

    async def snapshot(self) -> MemorySample:
        return MemorySample(64 * 1024**3, 60 * 1024**3, time.monotonic())

    def snapshot_now(self) -> MemorySample:
        return MemorySample(64 * 1024**3, 60 * 1024**3, time.monotonic())


async def start_lab(tmp_path, *, hang: asyncio.Event | None = None):
    docker = LabDocker()  # noqa: F841 - returned for assertions
    swap = LabSwap(docker)
    client, calls = lab_server(hang=hang)
    v1_spec = ModelSpec(LAB_MODEL, "http://127.0.0.1:18099", frozenset({Capability.CHAT}), 100, max_concurrency=1)
    book = Book({LAB_MODEL: v1_spec}, model_budget=1_000, free_floor=20, margin=0)
    book.bootstrap_stopped(LAB_MODEL)

    def port_state(_port: int) -> str:
        return "listening" if any(f["State"]["Running"] for f in docker.facts.values()) else "closed"

    expected = pv.ExpectedInstance(deployment_id=LAB_DEPLOYMENT, model_id=LAB_MODEL, runtime_id=LAB_RUNTIME,
                                   image_digest=LAB_IMAGE, identity_digest=LAB_CONFIG_SHA)
    observer = DockerProcessObserver(
        LAB_DEPLOYMENT, LAB_MODEL, 18099, docker=docker, port_state=port_state,
        launch_lookup=lambda target: None, process_state=lambda pid: "absent", clock=SystemClock(),
        expected=expected,
    )
    sessions = SessionManager(wait_seconds=100.0, hard_deadline_seconds=3600.0, heartbeat_seconds=10.0,
                              ttl_seconds=120.0, prepare_seconds=100.0, drain_seconds=30.0, retry_seconds=30.0,
                              cleanup_seconds=60.0, cancel_seconds=10.0, stop_grace_seconds=30.0, reconcile_seconds=5.0)
    blobs = BlobStore(tmp_path / "blobs")
    runtime = build_managed_execution(
        boot_id="boot-lab", deployment=lab_deployment(), deployment_id=LAB_DEPLOYMENT, book=book,
        resources=LabResources(), control=swap, clients={LAB_MODEL: client},
        observers={LAB_MODEL: observer}, inference_base_urls={LAB_MODEL: "http://127.0.0.1:18099"},
        blobs=blobs, sessions=sessions, fixture_path=LAB_FIXTURE,
        scheduler_kwargs={"poll_interval_seconds": 0.01},
        execution_kwargs={"poll_seconds": 0.01, "wait_seconds": 600.0},
        lifecycle_policy=FAST_POLICY,
        expected_instances={LAB_MODEL: expected},
    )
    await asyncio.wait_for(runtime.scheduler.open_session(LAB_MODEL, "client-a", "session-lab"), 5)
    return runtime, docker, swap, calls, book, blobs, None


def lab_document(key: str) -> dict:
    return {"session_token": "t", "operation": "chat",
            "input": {"inline": {"messages": [{"role": "user", "content": "hi"}]}},
            "parameters": {"max_tokens": 8}, "idempotency_key": key}


LAB_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "llama_cpp_v1.json"


@pytest.mark.asyncio
async def test_managed_round_trip_boot_execute_stop_reload(tmp_path) -> None:
    runtime, docker, swap, calls, book, blobs, _clock = await start_lab(tmp_path)
    service = runtime.service

    # the session prepare booted ONE container; the bridge stores the docker-verified identity
    assert swap.loads == [LAB_MODEL] and docker.booted == 1
    instance_one = runtime.lifecycle.instance(LAB_MODEL)
    assert instance_one == docker.identity("container-1")
    assert book.runtime[LAB_MODEL].generation == 1 and book.committed == 100

    first = await service.submit(session_id="session-lab", owner="uid:1000", document=lab_document("k-1"))
    assert await eventually(lambda: service.record(first["execution_id"]).state == "succeeded")
    rec1 = service.record(first["execution_id"])
    assert calls["chat"] == 1  # the real adapter consumed the registered chat protocol
    assert rec1.evidence is not None and rec1.evidence.reason == "independent_STOPPED"
    assert rec1.evidence.instance == instance_one == rec1.instance
    assert rec1.evidence.fence == rec1.fence
    assert await blobs.read_all(rec1.output_blob_id, "uid-1000", "probe") == json.dumps(
        CHAT_OUTPUT, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    # AC3: the fallback stop left the model UNLOADED while the session kept its grant
    assert (await runtime.scheduler.session_view("session-lab"))["phase"] == "active"
    assert book.runtime[LAB_MODEL].state.value == "unloaded" and book.committed == 0
    assert runtime.lifecycle.instance(LAB_MODEL) is None
    assert swap.unloads == [LAB_MODEL]

    # the next execution re-admits and re-loads: new container, new identity, generation 2, re-signed fence
    second = await service.submit(session_id="session-lab", owner="uid:1000", document=lab_document("k-2"))
    assert await eventually(lambda: service.record(second["execution_id"]).state == "succeeded")
    rec2 = service.record(second["execution_id"])
    assert rec2.fence.generation == 2 and book.runtime[LAB_MODEL].generation == 2
    assert rec2.instance == docker.identity("container-2")
    assert rec2.evidence is not None and rec2.evidence.instance == rec2.instance
    assert swap.loads == [LAB_MODEL, LAB_MODEL]


@pytest.mark.asyncio
async def test_cancelled_round_trip_publishes_nothing_after_the_stop(tmp_path) -> None:
    hang = asyncio.Event()
    runtime, docker, swap, calls, book, blobs, _clock = await start_lab(tmp_path, hang=hang)
    service = runtime.service
    third = await service.submit(session_id="session-lab", owner="uid:1000", document=lab_document("k-1"))
    execution_id = third["execution_id"]
    assert await eventually(lambda: calls["chat"] == 1)  # in flight inside the real HTTP path

    cancelling = await service.cancel(execution_id, owner="uid:1000")
    assert cancelling["state"] == "cancelling"
    hang.set()  # the response ends per protocol; only the proven stop may settle it
    assert await eventually(lambda: service.record(execution_id).state == "cancelled")
    record = service.record(execution_id)
    assert record.result is None and record.evidence.reason == "independent_STOPPED"
    assert blobs.metadata.usage("uid-1000").published_bytes == 0  # no partial output, ever
    assert book.runtime[LAB_MODEL].state.value == "unloaded"
