from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from model_scheduler import ports_v3 as pv
from model_scheduler.contracts import Presence
from model_scheduler.control_protocol_v1 import Fence, InstanceIdentity
from model_scheduler.process_observer import (
    CONFIG_LABEL,
    DEPLOYMENT_LABEL,
    MODEL_LABEL,
    RUNTIME_LABEL,
    DockerProcessObserver,
    ObservationError,
    ProcessObserver,
    parse_inspect_payload,
)

DEPLOYMENT = "orin-lab"
MODEL_ID = "qwen-small"
PORT = 10077
CONTAINER_ID = "c" * 64
IMAGE_DIGEST = "sha256:" + "d" * 64
CONFIG_SHA256 = "f" * 64
STARTED_AT = "2026-09-18T00:10:00.000000000Z"
CANONICAL_STARTED_AT = "2026-09-18T00:10:00Z"


def test_running_requires_matching_identity_labels_and_direct_health() -> None:
    payload = [{"Id": "abc", "State": {"Running": True, "StartedAt": "2026-01-01T00:00:00Z"}, "Config": {"Image": "image@sha256:abc", "Labels": {"io.self-model-switch.deployment": "thor-local", "io.self-model-switch.model": "qwen-small", "io.self-model-switch.config-sha256": "cfg"}}}]
    observer = ProcessObserver("thor-local", "image@sha256:abc", "cfg", inspect=lambda _: json.dumps(payload), port_open=lambda _: True, health=lambda _: True)

    result = observer.observe("qwen-small", "sms-thor-local-qwen-small", 10003)

    assert result.presence is Presence.RUNNING
    assert result.instance_id == "abc:2026-01-01T00:00:00Z"


def test_stopped_needs_both_no_container_and_closed_port() -> None:
    observer = ProcessObserver("thor-local", "image", "cfg", inspect=lambda _: "[]", port_open=lambda _: True, health=lambda _: False)

    result = observer.observe("qwen-small", "sms-thor-local-qwen-small", 10003)

    assert result.presence is Presence.UNKNOWN


def test_default_health_probe_accepts_only_loopback_http_200() -> None:
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    with patch("model_scheduler.process_observer.urlopen", return_value=Response()) as request:
        assert ProcessObserver._health_endpoint(10003) is True
    assert request.call_args.args[0].full_url == "http://127.0.0.1:10003/health"


# ---------------------------------------------------------------------------
# P07: the C03 observer over one deployment/model instance.
# ---------------------------------------------------------------------------


class ManualClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now

    def utc_now(self) -> datetime:
        return datetime(2026, 9, 18, 5, 0, tzinfo=UTC)


class FakeDocker:
    """A minimal docker CLI: `ps` lists ids, `inspect` returns facts, `stop` exits."""

    def __init__(self, facts: tuple[dict, ...] = (), *, stop_exit: int = 0, list_exit: int = 0) -> None:
        self.facts = {fact["Id"]: json.loads(json.dumps(fact)) for fact in facts}
        self.calls: list[list[str]] = []
        self.stop_exit = stop_exit
        self.list_exit = list_exit

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        verb = argv[1] if len(argv) > 1 else ""
        if verb == "ps":
            if self.list_exit:
                return self.list_exit, "", "Error response from daemon: Cannot connect to the Docker daemon"
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
                fact = self.facts.get(container_id)
                if fact is not None:
                    fact["State"] = {**fact["State"], "Running": False, "Status": "exited", "ExitCode": 0}
            return 0, argv[-1], ""
        raise AssertionError(f"unexpected docker command: {argv}")

    def stopped_ids(self) -> list[str]:
        return [call[4] for call in self.calls if len(call) > 1 and call[1] == "stop"]

    def _listed(self, argv: list[str]) -> list[str]:
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


def container_fact(*, running: bool, status: str | None = None, exit_code: int = 0, started_at: str = STARTED_AT, labels: dict | None = None, container_id: str = CONTAINER_ID, image_digest: str = IMAGE_DIGEST) -> dict:
    rendered = {
        DEPLOYMENT_LABEL: DEPLOYMENT,
        MODEL_LABEL: MODEL_ID,
        RUNTIME_LABEL: "llama-cpp-gguf-v1",
        CONFIG_LABEL: CONFIG_SHA256,
        "io.self-model-switch.mode": "lab",
    }
    if labels is not None:
        rendered = {**rendered, **labels}
    return {
        "Id": container_id,
        "Image": image_digest,
        "Config": {"Labels": rendered},
        "State": {
            "Running": running,
            "Status": status or ("running" if running else "exited"),
            "ExitCode": exit_code,
            "StartedAt": started_at,
        },
    }


def launch_operation(state: str, *, pid: int | None = 4321) -> pv.LaunchOperation:
    fence = Fence(boot_id="boot-1", model_id=MODEL_ID, generation=1, operation_id="op-1", execution_id=None, attempt=None)
    return pv.LaunchOperation(
        operation_id="op-1",
        fence=fence,
        started_at_monotonic=90.0,
        state=state,
        pid=pid,
        process_group_id=pid,
        terminal_at_monotonic=None if state == "starting" else 95.0,
    )


def v3_observer(docker: FakeDocker, *, port_state="closed", launch=None, process_state=lambda _: "absent") -> DockerProcessObserver:
    return DockerProcessObserver(
        DEPLOYMENT,
        MODEL_ID,
        PORT,
        docker=docker,
        port_state=lambda _: port_state,
        launch_lookup=(lambda _: launch) if launch is not None else (lambda _: None),
        process_state=process_state,
        clock=ManualClock(),
    )


async def test_v3_observer_reports_running_only_with_a_complete_verified_identity() -> None:
    docker = FakeDocker((container_fact(running=True),))
    observer = v3_observer(docker, port_state="listening")

    observation = await observer.observe(pv.ObservationTarget(deployment_id=DEPLOYMENT), 200.0)

    assert observation.state == pv.RUNNING
    assert observation.port_state == "listening"
    assert observation.instance == InstanceIdentity(
        container_id=CONTAINER_ID,
        started_at=CANONICAL_STARTED_AT,
        deployment_id=DEPLOYMENT,
        model_id=MODEL_ID,
        runtime_id="llama-cpp-gguf-v1",
        candidate_digest=CONFIG_SHA256,
        image_digest=IMAGE_DIGEST,
    )


async def test_v3_observer_canonicalises_equivalent_start_time_spellings() -> None:
    spellings = ("2026-09-18T00:10:00Z", "2026-09-18T00:10:00.000000000Z", "2026-09-18T08:10:00+08:00")
    identities = []
    for spelling in spellings:
        docker = FakeDocker((container_fact(running=True, started_at=spelling),))
        observation = await v3_observer(docker, port_state="listening").observe(
            pv.ObservationTarget(deployment_id=DEPLOYMENT), 200.0
        )
        assert observation.instance is not None
        identities.append(observation.instance.started_at)

    assert identities == [CANONICAL_STARTED_AT] * len(spellings)


async def test_v3_observer_does_not_call_a_container_running_without_its_runtime_label() -> None:
    docker = FakeDocker((container_fact(running=True, labels={RUNTIME_LABEL: ""}),))
    observer = v3_observer(docker, port_state="listening")

    observation = await observer.observe(pv.ObservationTarget(deployment_id=DEPLOYMENT), 200.0)

    assert observation.state == pv.UNKNOWN
    assert observation.instance is None


async def test_v3_observer_proves_stopped_from_exit_launcher_exit_and_closed_port() -> None:
    docker = FakeDocker((container_fact(running=False, status="exited", exit_code=0),))
    observer = v3_observer(docker, launch=launch_operation("completed"))

    observation = await observer.observe(
        pv.ObservationTarget(deployment_id=DEPLOYMENT, container_id=CONTAINER_ID, process_group_id=4321), 200.0
    )

    assert observation.state == pv.STOPPED
    assert observation.port_state == "closed"
    assert observation.subprocess_state == "absent"


async def test_v3_observer_never_proves_stopped_while_one_fact_is_unresolved() -> None:
    stopped_fact = (container_fact(running=False),)
    cases = {
        "launcher still starting": {"launch": launch_operation("starting")},
        "launcher process still alive": {"launch": launch_operation("completed"), "process_state": lambda _: "running"},
        "port occupied by an unknown process": {"port_state": "listening"},
        "port state unknown": {"port_state": "unknown"},
    }
    for name, overrides in cases.items():
        docker = FakeDocker(stopped_fact)
        observer = v3_observer(docker, **overrides)
        observation = await observer.observe(
            pv.ObservationTarget(deployment_id=DEPLOYMENT, container_id=CONTAINER_ID, process_group_id=4321), 200.0
        )
        assert observation.state == pv.UNKNOWN, name


async def test_v3_observer_reports_unknown_when_the_container_listing_fails() -> None:
    docker = FakeDocker((container_fact(running=True),), list_exit=1)
    observer = v3_observer(docker, port_state="listening")

    observation = await observer.observe(pv.ObservationTarget(deployment_id=DEPLOYMENT), 200.0)

    assert observation.state == pv.UNKNOWN
    assert observation.instance is None


async def test_v3_observer_stays_unknown_when_duplicate_instances_are_listed() -> None:
    docker = FakeDocker(
        (container_fact(running=True, container_id="a" * 64), container_fact(running=True, container_id="b" * 64))
    )
    observer = v3_observer(docker, port_state="listening")

    observation = await observer.observe(pv.ObservationTarget(deployment_id=DEPLOYMENT), 200.0)

    assert observation.state == pv.UNKNOWN


async def test_v3_observer_lists_only_this_deployment_and_model_by_label() -> None:
    docker = FakeDocker((container_fact(running=True),))
    observer = v3_observer(docker, port_state="listening")

    await observer.observe(pv.ObservationTarget(deployment_id=DEPLOYMENT), 200.0)

    listing = docker.calls[0]
    assert listing[:2] == ["docker", "ps"]
    assert "--no-trunc" in listing
    assert f"label={DEPLOYMENT_LABEL}={DEPLOYMENT}" in listing
    assert f"label={MODEL_LABEL}={MODEL_ID}" in listing


async def test_v3_observer_refuses_a_target_from_another_deployment() -> None:
    observer = v3_observer(FakeDocker(), port_state="closed")

    with pytest.raises(ObservationError):
        await observer.observe(pv.ObservationTarget(deployment_id="somewhere-else"), 200.0)


def test_parse_inspect_payload_rejects_truncated_or_unstructured_facts() -> None:
    with pytest.raises(ObservationError):
        parse_inspect_payload("{not json")
    with pytest.raises(ObservationError):
        parse_inspect_payload(json.dumps({"Id": CONTAINER_ID}))
    with pytest.raises(ObservationError):
        parse_inspect_payload(json.dumps([{"Id": CONTAINER_ID, "Config": {}, "State": {}, "Image": ""}]))
    with pytest.raises(ObservationError):
        parse_inspect_payload(json.dumps([{**container_fact(running=True), "State": {"Running": True}}]))
