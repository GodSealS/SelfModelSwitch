from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import inspect
import json
import threading
import time

import pytest

from model_scheduler import ports_v3 as pv
from model_scheduler.control_protocol_v1 import InstanceIdentity
from model_scheduler.control_recovery import ControlRecoveryClient, DeploymentRecovery, DeploymentRecoveryPort
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


class Clock:
    """A controllable monotonic source for the recovery helper."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_a_stage_that_consumes_the_budget_prevents_the_next_one() -> None:
    """K4: the deadline is checked before every stage, not only around the loop."""
    docker = FakeDocker((container_fact(running=True),))
    clock = Clock(0.0)

    def spending(argv: list[str]) -> tuple[int, str, str]:
        clock.now += 10.0  # the listing alone ate the whole budget
        return docker(argv)

    outcome = DeploymentRecovery(DEPLOYMENT, docker=spending, monotonic=clock).reconcile(
        close_admission=lambda: None, deadline=1.0)

    assert outcome.ok is False
    assert outcome.error_code == "recovery_timeout"
    assert docker.verbs() == ["ps"]  # no inspect, and certainly no stop


def test_the_stop_grace_is_the_remaining_budget_not_a_fixed_constant() -> None:
    docker = FakeDocker((container_fact(running=True),))

    DeploymentRecovery(DEPLOYMENT, docker=docker, monotonic=Clock(100.0)).stop_instance(
        identity(), deadline=112.0)  # twelve seconds left

    assert docker.stop_arguments() == [["docker", "stop", "--time", "12", CONTAINER_ID]]


def test_a_recheck_that_cannot_be_made_is_not_a_stop() -> None:
    """The unload can land and the re-check can still fail; that proves nothing."""
    docker = FakeDocker((container_fact(running=True),))
    inspected = 0

    def flaky(argv: list[str]) -> tuple[int, str, str]:
        nonlocal inspected
        if argv[1] == "inspect":
            inspected += 1
            if inspected > 1:
                return 1, "", "Cannot connect to the Docker daemon at unix:///var/run/docker.sock"
        return docker(argv)

    outcome = DeploymentRecovery(DEPLOYMENT, docker=flaky).stop_instance(
        identity(), deadline=time.monotonic() + 60)

    assert outcome.accepted is False
    assert outcome.stopped is False
    assert outcome.error_code == "docker_unavailable"


def test_stop_instance_does_not_treat_a_docker_failure_as_a_stop() -> None:
    def docker(argv: list[str]) -> tuple[int, str, str]:
        return 1, "", "Cannot connect to the Docker daemon at unix:///var/run/docker.sock"

    outcome = DeploymentRecovery(DEPLOYMENT, docker=docker).stop_instance(identity(), deadline=time.monotonic() + 60)

    assert outcome.accepted is False
    assert outcome.stopped is False
    assert outcome.error_code == "docker_unavailable"


# ---------------------------------------------------------------------------
# K5: the asynchronous recovery port the scheduler calls once admission has
# been closed and the old actions have drained. It proves, or refuses; it never
# writes to a book and it never removes a container.
# ---------------------------------------------------------------------------

K5_MODELS = ("embedding", "qwen-small")
RECOVERY_DEADLINE = 1060.0
OTHER_MODEL = "reranker"


class FakeObserver:
    """An ObserverPort whose facts are scripted by the test."""

    def __init__(self, *, observation: pv.Observation | None = None, error: Exception | None = None) -> None:
        self._observation = observation
        self._error = error
        self.calls: list[tuple[pv.ObservationTarget, float]] = []

    async def observe(self, target: pv.ObservationTarget, deadline: float) -> pv.Observation:
        self.calls.append((target, deadline))
        if self._error is not None:
            raise self._error
        return self._observation


class LateObserver(FakeObserver):
    """A sample that only lands after the caller's deadline has already passed."""

    def __init__(self, clock: Clock, **kwargs) -> None:
        super().__init__(**kwargs)
        self._test_clock = clock

    async def observe(self, target: pv.ObservationTarget, deadline: float) -> pv.Observation:
        observation = await super().observe(target, deadline)
        self._test_clock.now = deadline + 1.0
        return observation


def stopped_observation(
    clock: Clock,
    *,
    launch_resolved: bool = True,
    age: float = 0.0,
    instance: InstanceIdentity | None = None,
) -> pv.Observation:
    """A STOPPED sample stamped with the test clock's *now*, never a zero timestamp."""
    return pv.Observation(
        state=pv.STOPPED,
        sampled_at_monotonic=clock.now - age,
        sampled_at_utc=datetime.now(timezone.utc),
        port_state="closed",
        subprocess_state="exited",
        instance=instance,
        launch_operation=None,
        launch_resolved=launch_resolved,
    )


def unknown_observation(clock: Clock) -> pv.Observation:
    return pv.Observation(
        state=pv.UNKNOWN,
        sampled_at_monotonic=clock.now,
        sampled_at_utc=datetime.now(timezone.utc),
        port_state="unknown",
        subprocess_state="unknown",
    )


def build_recovery_port(
    observers: dict[str, FakeObserver],
    *,
    clock: Clock | None = None,
    recovery: DeploymentRecovery | None = None,
    models: tuple[str, ...] = K5_MODELS,
) -> DeploymentRecoveryPort:
    """The helper and the port share one clock domain, as they do in production."""
    clock = clock or Clock(1000.0)
    if recovery is None:
        recovery = DeploymentRecovery(DEPLOYMENT, docker=FakeDocker(), monotonic=clock)
    return DeploymentRecoveryPort(
        deployment_id=DEPLOYMENT,
        recovery=recovery,
        observers=observers,
        models=models,
        clock=clock,
        policy=pv.LifecyclePolicy(),
    )


def test_the_recovery_port_requires_exactly_one_observer_for_every_registered_model() -> None:
    observers = {model: FakeObserver(observation=stopped_observation(Clock())) for model in K5_MODELS}

    with pytest.raises(ValueError):  # a registered model without its observer
        build_recovery_port({K5_MODELS[0]: observers[K5_MODELS[0]]})
    with pytest.raises(ValueError):  # an observer for a model that is not registered
        build_recovery_port({**observers, "ghost": FakeObserver()})
    with pytest.raises(ValueError):  # the same model registered twice
        build_recovery_port(observers, models=(K5_MODELS[0], K5_MODELS[0]))
    with pytest.raises(ValueError):  # nothing registered at all
        build_recovery_port({}, models=())


def test_the_recovery_port_refuses_a_helper_that_reconciles_another_deployment() -> None:
    observers = {model: FakeObserver() for model in K5_MODELS}

    with pytest.raises(ValueError):
        build_recovery_port(observers, recovery=DeploymentRecovery(OTHER_DEPLOYMENT, docker=FakeDocker()))


def test_the_recovery_port_is_the_existing_recovery_port_without_a_book_or_a_callback() -> None:
    parameters = set(inspect.signature(DeploymentRecoveryPort).parameters)

    assert inspect.iscoroutinefunction(DeploymentRecoveryPort.recover)
    assert list(inspect.signature(DeploymentRecoveryPort.recover).parameters) == ["self", "deadline"]
    assert not {"book", "scheduler", "close_admission", "callback", "on_stop", "write_back"} & parameters


async def test_the_recovery_port_proves_success_only_when_every_registered_model_is_stopped() -> None:
    clock = Clock(1000.0)
    observers = {model: FakeObserver(observation=stopped_observation(clock)) for model in K5_MODELS}

    result = await build_recovery_port(observers, clock=clock).recover(RECOVERY_DEADLINE)

    assert result.ok is True
    assert result.phase == "complete"
    assert result.error_code is None
    assert result.stopped_models == K5_MODELS
    assert all(target.deployment_id == DEPLOYMENT for observer in observers.values() for target, _ in observer.calls)
    assert [deadline for observer in observers.values() for _, deadline in observer.calls] == [RECOVERY_DEADLINE] * len(K5_MODELS)


async def test_one_unknown_observation_fails_the_recovery_before_later_models_are_asked() -> None:
    clock = Clock(1000.0)
    models = ("embedding", OTHER_MODEL, "qwen-small")
    observers = {
        "embedding": FakeObserver(observation=stopped_observation(clock)),
        OTHER_MODEL: FakeObserver(observation=unknown_observation(clock)),
        "qwen-small": FakeObserver(observation=stopped_observation(clock)),
    }

    result = await build_recovery_port(observers, clock=clock, models=models).recover(RECOVERY_DEADLINE)

    assert result.ok is False
    assert result.phase == "failed"
    assert result.error_code == "observation_unknown"
    assert result.stopped_models == ("embedding",)
    assert observers["qwen-small"].calls == []  # a refusal is not a partial success


async def test_an_unresolved_launch_is_never_a_proven_stop() -> None:
    clock = Clock(1000.0)
    observers = {
        model: FakeObserver(observation=stopped_observation(clock, launch_resolved=False)) for model in K5_MODELS
    }

    result = await build_recovery_port(observers, clock=clock).recover(RECOVERY_DEADLINE)

    assert result.ok is False
    assert result.phase == "failed"
    assert result.error_code == "launch_unresolved"
    assert result.stopped_models == ()


@pytest.mark.parametrize("age", [3.0, -1.0])
async def test_a_sample_from_outside_the_freshness_window_cannot_prove_a_stop(age: float) -> None:
    clock = Clock(1000.0)
    observers = {model: FakeObserver(observation=stopped_observation(clock, age=age)) for model in K5_MODELS}

    result = await build_recovery_port(observers, clock=clock).recover(RECOVERY_DEADLINE)

    assert result.ok is False
    assert result.error_code == "observation_stale"


async def test_an_observer_that_raises_is_a_conservative_failure() -> None:
    clock = Clock(1000.0)
    observers = {
        "embedding": FakeObserver(error=RuntimeError("the observer connection dropped")),
        "qwen-small": FakeObserver(observation=stopped_observation(clock)),
    }

    result = await build_recovery_port(observers, clock=clock).recover(RECOVERY_DEADLINE)

    assert result.ok is False
    assert result.error_code == "observer_failed"
    assert observers["qwen-small"].calls == []


async def test_a_stop_belonging_to_another_model_is_not_this_models_stop() -> None:
    clock = Clock(1000.0)
    foreign = InstanceIdentity(
        container_id=CONTAINER_ID,
        started_at=STARTED_AT,
        deployment_id=DEPLOYMENT,
        model_id=OTHER_MODEL,
        runtime_id="llama-cpp-gguf-v1",
        candidate_digest=CONFIG_SHA256,
        image_digest="sha256:" + "d" * 64,
    )
    observers = {
        "embedding": FakeObserver(observation=stopped_observation(clock, instance=foreign)),
        "qwen-small": FakeObserver(observation=stopped_observation(clock)),
    }

    result = await build_recovery_port(observers, clock=clock).recover(RECOVERY_DEADLINE)

    assert result.ok is False
    assert result.error_code == "observation_identity_mismatch"


async def test_a_helper_that_cannot_verify_a_stop_is_reported_without_asking_the_observers() -> None:
    clock = Clock(1000.0)
    docker = FakeDocker((container_fact(running=True),), stop_exit=1)
    observers = {model: FakeObserver(observation=stopped_observation(clock)) for model in K5_MODELS}

    result = await build_recovery_port(
        observers, clock=clock, recovery=DeploymentRecovery(DEPLOYMENT, docker=docker, monotonic=clock)
    ).recover(RECOVERY_DEADLINE)

    assert result.ok is False
    assert result.error_code == "stop_failed"
    assert all(observer.calls == [] for observer in observers.values())


async def test_an_expired_deadline_dispatches_neither_the_helper_nor_a_sample() -> None:
    clock = Clock(1000.0)
    docker = FakeDocker((container_fact(running=True),))
    observers = {model: FakeObserver(observation=stopped_observation(clock)) for model in K5_MODELS}

    result = await build_recovery_port(
        observers, clock=clock, recovery=DeploymentRecovery(DEPLOYMENT, docker=docker, monotonic=clock)
    ).recover(999.0)

    assert result.ok is False
    assert result.error_code == "recovery_timeout"
    assert docker.calls == []
    assert all(observer.calls == [] for observer in observers.values())


async def test_a_sample_that_only_lands_after_the_deadline_cannot_prove_a_stop() -> None:
    clock = Clock(1000.0)
    observers = {model: LateObserver(clock, observation=stopped_observation(clock)) for model in K5_MODELS}

    result = await build_recovery_port(observers, clock=clock).recover(RECOVERY_DEADLINE)

    assert result.ok is False
    assert result.error_code == "recovery_timeout"
    assert observers["qwen-small"].calls == []


async def test_a_stopped_container_that_was_not_removed_is_a_valid_result() -> None:
    clock = Clock(1000.0)
    docker = FakeDocker((container_fact(running=False),))
    observers = {model: FakeObserver(observation=stopped_observation(clock)) for model in K5_MODELS}

    result = await build_recovery_port(
        observers, clock=clock, recovery=DeploymentRecovery(DEPLOYMENT, docker=docker, monotonic=clock)
    ).recover(RECOVERY_DEADLINE)

    assert result.ok is True
    assert result.stopped_models == K5_MODELS
    assert "rm" not in docker.verbs()


async def test_a_cancelled_recovery_keeps_the_running_helper_worker_tracked() -> None:
    clock = Clock(1000.0)
    entered, release = threading.Event(), threading.Event()

    def blocking_docker(argv: list[str]) -> tuple[int, str, str]:
        entered.set()
        assert release.wait(timeout=5)
        return 0, "", ""

    observers = {model: FakeObserver(observation=stopped_observation(clock)) for model in K5_MODELS}
    port = build_recovery_port(
        observers, clock=clock, recovery=DeploymentRecovery(DEPLOYMENT, docker=blocking_docker, monotonic=clock)
    )
    task = asyncio.create_task(port.recover(RECOVERY_DEADLINE))
    assert await asyncio.to_thread(entered.wait, 5)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(port.pending_workers()) == 1  # the helper thread is still running and still tracked

    release.set()
    for _ in range(1000):
        if not port.pending_workers():
            break
        await asyncio.sleep(0.01)
    assert port.pending_workers() == ()
    assert all(observer.calls == [] for observer in observers.values())  # a cancelled attempt proves nothing
