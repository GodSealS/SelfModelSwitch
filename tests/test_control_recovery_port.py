from __future__ import annotations

import asyncio
import json
import time

import pytest

from model_scheduler.control_protocol_v1 import InstanceIdentity
from model_scheduler.control_recovery import ControlRecoveryClient, DeploymentRecovery
from model_scheduler.process_observer import CONFIG_LABEL, DEPLOYMENT_LABEL, MODEL_LABEL, RUNTIME_LABEL


@pytest.mark.asyncio
async def test_recovery_port_runs_only_the_fixed_no_argument_helper_and_parses_its_dto() -> None:
    seen: list[tuple[str, ...]] = []

    async def runner(argv: tuple[str, ...]) -> tuple[int, str]:
        seen.append(argv)
        return 0, '{"ok":true,"phase":"complete","error_code":null,"stopped_models":["embedding","qwen-large","qwen-small","reranker"]}'

    result = await ControlRecoveryClient(runner).recover(asyncio.get_running_loop().time() + 1)

    assert seen == [("sudo", "-n", "/usr/local/libexec/sms-control-recover")]
    assert result.ok is True
    assert result.phase == "complete"
    assert result.error_code is None
    assert result.stopped_models == ("embedding", "qwen-large", "qwen-small", "reranker")


@pytest.mark.asyncio
async def test_recovery_port_rejects_nonzero_exit_and_untrusted_helper_dtos() -> None:
    async def failed(_: tuple[str, ...]) -> tuple[int, str]:
        return 1, '{"ok":true,"phase":"complete","error_code":null,"stopped_models":["embedding"]}'

    async def malformed(_: tuple[str, ...]) -> tuple[int, str]:
        return 0, '{"ok":true,"phase":"complete","error_code":null,"stopped_models":["embedding"],"unexpected":true}'

    deadline = asyncio.get_running_loop().time() + 1
    failed_result = await ControlRecoveryClient(failed).recover(deadline)
    malformed_result = await ControlRecoveryClient(malformed).recover(deadline)

    assert failed_result.ok is False
    assert failed_result.error_code == "control_recovery_failed"
    assert malformed_result.ok is False
    assert malformed_result.error_code == "control_recovery_protocol_error"


@pytest.mark.asyncio
async def test_recovery_port_does_not_run_after_the_absolute_deadline() -> None:
    called = False

    async def runner(_: tuple[str, ...]) -> tuple[int, str]:
        nonlocal called
        called = True
        return 0, "{}"

    result = await ControlRecoveryClient(runner).recover(asyncio.get_running_loop().time())

    assert called is False
    assert result.ok is False
    assert result.error_code == "control_recovery_timeout"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        '{"ok":false,"phase":"failed","error_code":"control_recovery_failed","stopped_models":["embedding"]}',
        '{"ok":true,"phase":"complete","error_code":null,"stopped_models":["reranker","embedding"]}',
        '{"ok":true,"phase":"complete","error_code":null,"stopped_models":["embedding","embedding"]}',
        '{"ok":true,"phase":"complete","error_code":null,"stopped_models":[],"ok":true}',
        '{"ok":true,"phase":"complete","error_code":null,"stopped_models":NaN}',
    ],
)
async def test_recovery_port_rejects_ambiguous_or_inconsistent_helper_json(payload: str) -> None:
    async def runner(_: tuple[str, ...]) -> tuple[int, str]:
        return 0, payload

    result = await ControlRecoveryClient(runner).recover(asyncio.get_running_loop().time() + 1)

    assert result.ok is False
    assert result.error_code == "control_recovery_protocol_error"


# ---------------------------------------------------------------------------
# P07: startup reconciliation of this deployment's own instances.
# ---------------------------------------------------------------------------

DEPLOYMENT = "orin-lab"
OTHER_DEPLOYMENT = "someone-else"
CONTAINER_ID = "c" * 64
CONFIG_SHA256 = "f" * 64
STARTED_AT = "2026-09-18T00:10:00.000000000Z"


class FakeDocker:
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
            return 0, "".join(f"{container_id}\n" for container_id in self.facts), ""
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

    def verbs(self) -> list[str]:
        return [call[1] for call in self.calls if len(call) > 1]

    def stop_arguments(self) -> list[list[str]]:
        return [call for call in self.calls if len(call) > 1 and call[1] == "stop"]


def container_fact(*, running: bool, deployment: str = DEPLOYMENT, container_id: str = CONTAINER_ID, started_at: str = STARTED_AT) -> dict:
    return {
        "Id": container_id,
        "Image": "sha256:" + "d" * 64,          # the bare ID, as docker reports it
        "Config": {
            "Image": "test-image@sha256:" + "d" * 64,   # the reference the container was started with
            "Labels": {
                DEPLOYMENT_LABEL: deployment,
                MODEL_LABEL: "qwen-small",
                RUNTIME_LABEL: "llama-cpp-gguf-v1",
                CONFIG_LABEL: CONFIG_SHA256,
            }
        },
        "State": {"Running": running, "Status": "running" if running else "exited", "ExitCode": 0, "StartedAt": started_at},
    }


def identity(*, container_id: str = CONTAINER_ID, started_at: str = STARTED_AT) -> InstanceIdentity:
    return InstanceIdentity(
        container_id=container_id,
        started_at=started_at,
        deployment_id=DEPLOYMENT,
        model_id="qwen-small",
        runtime_id="llama-cpp-gguf-v1",
        candidate_digest=CONFIG_SHA256,
        image_digest="sha256:" + "d" * 64,
    )


def test_startup_reconciliation_closes_admission_first_and_stops_own_instances_by_id() -> None:
    events: list[str] = []
    docker = FakeDocker((container_fact(running=True),))

    class Recording:
        def __call__(self, argv):
            events.append(f"docker-{argv[1]}")
            return docker(argv)

    recovery = DeploymentRecovery(DEPLOYMENT, docker=Recording())
    outcome = recovery.reconcile(close_admission=lambda: events.append("admission-closed"), deadline=time.monotonic() + 60)

    assert events[0] == "admission-closed"
    assert outcome.ok is True
    assert outcome.remaining_container_ids == ()
    assert outcome.stopped_container_ids == (CONTAINER_ID,)
    assert docker.stop_arguments() == [["docker", "stop", "--time", "30", CONTAINER_ID]]


def test_startup_reconciliation_never_touches_another_deployments_container() -> None:
    docker = FakeDocker((container_fact(running=True, deployment=OTHER_DEPLOYMENT),))

    outcome = DeploymentRecovery(DEPLOYMENT, docker=docker).reconcile(close_admission=lambda: None, deadline=time.monotonic() + 60)

    assert outcome.ok is False
    assert outcome.error_code == "foreign_instance"
    assert outcome.remaining_container_ids == (CONTAINER_ID,)
    assert "stop" not in docker.verbs()


def test_startup_reconciliation_fails_closed_when_a_stop_cannot_be_verified() -> None:
    docker = FakeDocker((container_fact(running=True),), stop_exit=1)

    outcome = DeploymentRecovery(DEPLOYMENT, docker=docker).reconcile(close_admission=lambda: None, deadline=time.monotonic() + 60)

    assert outcome.ok is False
    assert outcome.error_code == "stop_failed"
    assert outcome.remaining_container_ids == (CONTAINER_ID,)


def test_startup_reconciliation_reports_an_unavailable_listing_without_stopping_anything() -> None:
    docker = FakeDocker((container_fact(running=True),), list_exit=1)

    outcome = DeploymentRecovery(DEPLOYMENT, docker=docker).reconcile(close_admission=lambda: None, deadline=time.monotonic() + 60)

    assert outcome.ok is False
    assert outcome.error_code == "container_listing_failed"
    assert docker.verbs() == ["ps"]


def test_reconciliation_deadline_stops_before_touching_the_world() -> None:
    docker = FakeDocker((container_fact(running=True),))

    outcome = DeploymentRecovery(DEPLOYMENT, docker=docker, monotonic=lambda: 500.0).reconcile(
        close_admission=lambda: None, deadline=499.0
    )

    assert outcome.ok is False
    assert outcome.error_code == "recovery_timeout"
    assert docker.calls == []


def test_stop_instance_uses_the_matching_identity_and_verifies_the_exit() -> None:
    docker = FakeDocker((container_fact(running=True),))

    outcome = DeploymentRecovery(DEPLOYMENT, docker=docker).stop_instance(identity(), deadline=time.monotonic() + 60)

    assert outcome.accepted is True
    assert outcome.stopped is True
    assert outcome.container_id == CONTAINER_ID
    assert docker.stop_arguments() == [["docker", "stop", "--time", "30", CONTAINER_ID]]


def test_stale_identity_cannot_stop_a_newer_instance() -> None:
    docker = FakeDocker((container_fact(running=True, started_at="2026-09-18T01:00:00.000000000Z"),))

    outcome = DeploymentRecovery(DEPLOYMENT, docker=docker).stop_instance(identity(), deadline=time.monotonic() + 60)

    assert outcome.accepted is False
    assert outcome.stopped is False
    assert outcome.error_code == "stale_identity"
    assert docker.stop_arguments() == []


def test_stop_instance_treats_a_structured_not_found_as_already_stopped() -> None:
    docker = FakeDocker()

    outcome = DeploymentRecovery(DEPLOYMENT, docker=docker).stop_instance(identity(), deadline=time.monotonic() + 60)

    assert outcome.accepted is True
    assert outcome.stopped is True
    assert outcome.error_code is None


def test_stop_instance_does_not_treat_a_docker_failure_as_a_stop() -> None:
    def docker(argv: list[str]) -> tuple[int, str, str]:
        return 1, "", "Cannot connect to the Docker daemon at unix:///var/run/docker.sock"

    outcome = DeploymentRecovery(DEPLOYMENT, docker=docker).stop_instance(identity(), deadline=time.monotonic() + 60)

    assert outcome.accepted is False
    assert outcome.stopped is False
    assert outcome.error_code == "docker_unavailable"
