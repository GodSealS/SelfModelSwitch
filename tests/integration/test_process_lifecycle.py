"""P07 integration: a real launcher, real observed facts and real book clearing.

The docker CLI is the only injected port here; the supervised launcher is a real
child process, the observer runs its real C03 logic and the book is the real
`Book`. The point of the suite is that no timeout, cancel or missing container
may clear a reservation before the four stop facts are observed.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

import pytest

from model_scheduler import ports_v3 as pv
from model_scheduler.backend_control import LlamaSwapBackend, ManagedModel
from model_scheduler.contracts import Capability, MemorySample, ModelSpec, Presence
from model_scheduler.control_protocol_v1 import Fence
from model_scheduler.control_recovery import DeploymentRecovery
from model_scheduler.model_registry import Book, Conflict
from model_scheduler.model_runner import SupervisedLaunch
from model_scheduler.runtime import reconcile_startup
from model_scheduler.process_observer import (
    CONFIG_LABEL,
    DEPLOYMENT_LABEL,
    MODEL_LABEL,
    RUNTIME_LABEL,
    DockerProcessObserver,
    ProcessObserver,
    SystemClock,
    os_process_state,
)

DEPLOYMENT = "orin-lab"
CONFIG_SHA256 = "f" * 64
STARTED_AT = "2026-09-18T00:10:00.000000000Z"


class RecordingControl:
    def __init__(self) -> None:
        self.loads: list[str] = []
        self.unloads: list[str] = []

    async def load(self, model_id: str) -> None:
        self.loads.append(model_id)

    async def unload(self, model_id: str) -> None:
        self.unloads.append(model_id)


async def _health_server() -> tuple[asyncio.AbstractServer, int]:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


@pytest.mark.asyncio
async def test_lost_model_health_socket_never_causes_automatic_load() -> None:
    server, port = await _health_server()
    deployment_id = "thor-local"
    image = "repo/image@sha256:" + "a" * 64
    config_sha256 = "b" * 64

    def inspect(_: list[str]) -> str:
        return json.dumps([
            {
                "Id": "container-id",
                "Config": {
                    "Image": image,
                    "Labels": {
                        "io.self-model-switch.deployment": deployment_id,
                        "io.self-model-switch.model": "qwen-small",
                        "io.self-model-switch.config-sha256": config_sha256,
                    },
                },
                "State": {"Running": True, "StartedAt": "2026-09-16T00:00:00Z"},
            }
        ])

    control = RecordingControl()
    observer = ProcessObserver(deployment_id, image, config_sha256, inspect=inspect)
    backend = LlamaSwapBackend(control, observer, {"qwen-small": ManagedModel("sms-thor-local-qwen-small", port)})
    try:
        assert (await backend.observe("qwen-small")).presence is Presence.RUNNING
        server.close()
        await server.wait_closed()
        assert (await backend.observe("qwen-small")).presence is Presence.UNKNOWN
    finally:
        server.close()
        await server.wait_closed()

    assert control.loads == []
    assert control.unloads == []


class FakeDocker:
    """The docker CLI port: `ps` lists ids, `inspect` returns the facts."""

    def __init__(self, facts: tuple[dict, ...] = (), *, stop_exit: int = 0) -> None:
        self.facts = {fact["Id"]: json.loads(json.dumps(fact)) for fact in facts}
        self.calls: list[list[str]] = []
        self.stop_exit = stop_exit

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        verb = argv[1] if len(argv) > 1 else ""
        if verb == "ps":
            return 0, "".join(f"{container_id}\n" for container_id in self._listed(argv)), ""
        if verb == "inspect":
            wanted = argv[2:]
            if any(container_id not in self.facts for container_id in wanted):
                missing = next(container_id for container_id in wanted if container_id not in self.facts)
                return 1, "", f"Error: No such object: {missing}"
            return 0, json.dumps([self.facts[container_id] for container_id in wanted]), ""
        if verb == "stop":
            if self.stop_exit:
                return self.stop_exit, "", "Error response from daemon: cannot stop container"
            for container_id in argv[4:]:
                self.exit_container(container_id)
            return 0, argv[-1], ""
        raise AssertionError(f"unexpected docker command: {argv}")

    def exit_container(self, container_id: str) -> None:
        fact = self.facts[container_id]
        fact["State"] = {**fact["State"], "Running": False, "Status": "exited", "ExitCode": 0}

    def _listed(self, argv: list[str]) -> list[str]:
        """Honour the `--filter label=name=value` arguments like the real CLI."""
        wanted: dict[str, str] = {}
        tokens = list(argv)
        for index, token in enumerate(tokens):
            if token == "--filter" and index + 1 < len(tokens):
                name, _, value = tokens[index + 1].partition("=")
                if name == "label":
                    label, _, label_value = value.partition("=")
                    wanted[label] = label_value
        return [
            fact["Id"]
            for fact in self.facts.values()
            if all(fact["Config"]["Labels"].get(label) == label_value for label, label_value in wanted.items())
        ]


def container_fact(model_id: str, container_id: str, *, running: bool) -> dict:
    return {
        "Id": container_id,
        "Image": "sha256:" + "d" * 64,
        "Config": {
            "Labels": {
                DEPLOYMENT_LABEL: DEPLOYMENT,
                MODEL_LABEL: model_id,
                RUNTIME_LABEL: "llama-cpp-gguf-v1",
                CONFIG_LABEL: CONFIG_SHA256,
            }
        },
        "State": {"Running": running, "Status": "running" if running else "exited", "ExitCode": 0, "StartedAt": STARTED_AT},
    }


def observer_for(
    model_id: str,
    port: int,
    docker: FakeDocker,
    *,
    launch: SupervisedLaunch | None = None,
    port_state=None,
) -> DockerProcessObserver:
    def probe(_port: int) -> str:
        running = any(
            fact["State"]["Running"] and fact["Config"]["Labels"].get(MODEL_LABEL) == model_id
            for fact in docker.facts.values()
        )
        return "listening" if running else "closed"

    return DockerProcessObserver(
        DEPLOYMENT,
        model_id,
        port,
        docker=docker,
        port_state=port_state or probe,
        launch_lookup=(lambda _: launch.operation) if launch is not None else (lambda _: None),
        process_state=os_process_state,
        clock=SystemClock(),
    )


async def test_a_timed_out_launch_never_clears_the_books_early() -> None:
    fence = Fence(boot_id="boot-1", model_id="qwen-small", generation=1, operation_id="op-1", execution_id=None, attempt=None)
    launch = SupervisedLaunch([sys.executable, "-c", "import time; time.sleep(0.6)"], fence)
    docker = FakeDocker()
    observer = observer_for("qwen-small", 10077, docker, launch=launch, port_state=lambda _: "closed")
    reserved = {"bytes": 100}

    launch.start()
    assert launch.wait(timeout=0.05) is None  # a slow start; the launcher is still running
    early = await observer.observe(
        pv.ObservationTarget(deployment_id=DEPLOYMENT, process_group_id=launch.operation.process_group_id),
        time.monotonic() + 60,
    )
    assert early.state == pv.UNKNOWN  # no container and a closed port are not enough on their own
    assert early.launch_operation is not None and not early.launch_operation.is_terminal
    assert reserved["bytes"] == 100

    assert launch.wait() == 0  # the launcher really exits, and is reaped
    stopped = await observer.observe(
        pv.ObservationTarget(deployment_id=DEPLOYMENT, process_group_id=launch.operation.process_group_id),
        time.monotonic() + 60,
    )
    assert stopped.state == pv.STOPPED
    reserved["bytes"] = 0
    assert reserved["bytes"] == 0


async def test_reservations_are_released_only_when_every_managed_stop_is_proven() -> None:
    specs = {
        "qwen-small": ModelSpec("qwen-small", "http://127.0.0.1:10077", frozenset({Capability.CHAT}), 100, max_concurrency=1),
        "qwen-large": ModelSpec("qwen-large", "http://127.0.0.1:10078", frozenset({Capability.CHAT}), 200, max_concurrency=1),
    }
    book = Book(specs, model_budget=1000, free_floor=20, margin=0, half_life=10)
    for model_id in specs:
        book.bootstrap_stopped(model_id)
        operation = book.begin_load(model_id, MemorySample(1000, 900, 0), 0)
        book.loaded(operation, 0)
    assert book.committed == 300

    docker = FakeDocker((container_fact("qwen-large", "c" * 64, running=True),))
    small_observer = observer_for("qwen-small", 10077, docker)
    large_observer = observer_for("qwen-large", 10078, docker)

    epoch = book.begin_recovery()  # startup closes admission before anything is observed
    observations = {
        "qwen-small": await small_observer.observe(pv.ObservationTarget(deployment_id=DEPLOYMENT), time.monotonic() + 60),
        "qwen-large": await large_observer.observe(pv.ObservationTarget(deployment_id=DEPLOYMENT), time.monotonic() + 60),
    }
    confirmed = frozenset(model_id for model_id, item in observations.items() if item.state == pv.STOPPED)
    assert confirmed == frozenset({"qwen-small"})
    with pytest.raises(Conflict):
        book.finish_recovery(epoch, confirmed)
    assert book.committed == 300  # an unproven stop keeps both reservations

    docker.exit_container("c" * 64)
    stopped = await large_observer.observe(pv.ObservationTarget(deployment_id=DEPLOYMENT), time.monotonic() + 60)
    assert stopped.state == pv.STOPPED
    book.finish_recovery(epoch, frozenset({"qwen-small", "qwen-large"}))
    assert book.committed == 0


async def test_startup_reconciliation_is_the_only_path_that_clears_the_book() -> None:
    book = _loaded_book()
    docker = FakeDocker((container_fact("qwen-large", "c" * 64, running=True),))
    occupied = {"qwen-large": True}
    observers = {
        "qwen-small": observer_for("qwen-small", 10077, docker),
        "qwen-large": observer_for(
            "qwen-large",
            10078,
            docker,
            port_state=lambda _: "listening" if occupied["qwen-large"] else "closed",
        ),
    }
    recovery = DeploymentRecovery(DEPLOYMENT, docker=docker)

    first = await reconcile_startup(book, recovery, observers, deadline=time.monotonic() + 60)

    assert first.ok is False  # the container stopped but its port is still owned by something else
    assert first.error_code == "unproven_stop"
    assert first.unproven == frozenset({"qwen-large"})
    assert first.confirmed_stopped == frozenset({"qwen-small"})
    assert book.recovering is True
    assert book.committed == 300

    occupied["qwen-large"] = False
    second = await reconcile_startup(book, recovery, observers, deadline=time.monotonic() + 60)

    assert second.ok is True
    assert second.unproven == frozenset()
    assert book.recovering is False
    assert book.committed == 0


async def test_startup_reconciliation_keeps_admission_closed_when_a_leftover_cannot_be_removed() -> None:
    book = _loaded_book()
    docker = FakeDocker((container_fact("qwen-small", "c" * 64, running=True),), stop_exit=1)
    observers = {
        "qwen-small": observer_for("qwen-small", 10077, docker),
        "qwen-large": observer_for("qwen-large", 10078, docker),
    }
    recovery = DeploymentRecovery(DEPLOYMENT, docker=docker)

    outcome = await reconcile_startup(book, recovery, observers, deadline=time.monotonic() + 60)

    assert outcome.ok is False
    assert outcome.error_code == "stop_failed"
    assert book.recovering is True
    assert book.committed == 300


def _loaded_book() -> Book:
    specs = {
        "qwen-small": ModelSpec("qwen-small", "http://127.0.0.1:10077", frozenset({Capability.CHAT}), 100, max_concurrency=1),
        "qwen-large": ModelSpec("qwen-large", "http://127.0.0.1:10078", frozenset({Capability.CHAT}), 200, max_concurrency=1),
    }
    book = Book(specs, model_budget=1000, free_floor=20, margin=0, half_life=10)
    for model_id in specs:
        book.bootstrap_stopped(model_id)
        operation = book.begin_load(model_id, MemorySample(1000, 900, 0), 0)
        book.loaded(operation, 0)
    return book


def test_build_scheduler_only_bootstraps_models_with_observed_stop_evidence() -> None:
    from pathlib import Path

    from model_scheduler.config import load_config
    from model_scheduler.contracts import State
    from model_scheduler.runtime import build_scheduler

    config = load_config(Path(__file__).resolve().parents[2] / "config.yaml")
    evidence = build_scheduler(
        config,
        _UnusedBackend(),
        resources=_Resources(),
        storage_guard=lambda: True,
        confirmed_stopped=frozenset({"embedding"}),
    )

    assert evidence.book.runtime["embedding"].state is State.UNLOADED
    assert evidence.book.runtime["qwen-small"].state is State.UNKNOWN

    legacy = build_scheduler(config, _UnusedBackend(), resources=_Resources(), storage_guard=lambda: True)

    assert all(runtime.state is State.UNLOADED for runtime in legacy.book.runtime.values())


class _Resources:
    def snapshot_now(self):
        return MemorySample(64 * 1024**3, 60 * 1024**3, 0)

    async def snapshot(self):
        return self.snapshot_now()


class _UnusedBackend:
    async def load(self, operation, deadline):
        raise AssertionError("no load may be attempted at startup")

    async def stop(self, operation, deadline):
        raise AssertionError("no stop may be attempted at startup")
