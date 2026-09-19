"""Docker process evidence adapters; they never scan or mutate foreign containers.

Two generations live here:

* the v1 `ProcessObserver` (fixed four ids, name-based inspect) kept for the
  legacy runtime composition, and
* the v3 `DockerProcessObserver` (M02/P07): one C03 observation over one
  deployment/model instance. It lists only its own deployment *and* model by
  label, verifies the rendered identity of a running container, and converts the
  raw facts into `running|stopped|unknown` through the single `stopped_is_proven`
  computation. Docker failures and ports owned by an unknown process stay
  UNKNOWN; absence is only ever reported from a successful listing.
"""
from __future__ import annotations

import asyncio
import errno
import json
import os
import re
import socket
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Callable, Mapping, Sequence
from urllib.error import URLError
from urllib.request import Request, urlopen

from . import ports_v3
from .contracts import Observation, Presence
from .control_protocol_v1 import InstanceIdentity
from .ports_v3 import STOPPED, UNKNOWN


class ProcessObserver:
    def __init__(self, deployment_id: str, image_digest: str, config_sha256: str, *, inspect: Callable[[list[str]], str] | None = None, port_open: Callable[[int], bool] | None = None, health: Callable[[int], bool] | None = None):
        self.deployment_id, self.image_digest, self.config_sha256 = deployment_id, image_digest, config_sha256
        self._inspect = inspect or self._docker_inspect
        self._port_open = port_open or self._port_is_open
        self._health = health or self._health_endpoint

    @staticmethod
    def _docker_inspect(args: list[str]) -> str:
        return subprocess.check_output(args, text=True, timeout=5)

    @staticmethod
    def _port_is_open(port: int) -> bool:
        with socket.socket() as sock:
            sock.settimeout(0.5)
            return sock.connect_ex(("127.0.0.1", port)) == 0

    @staticmethod
    def _health_endpoint(port: int) -> bool:
        """Probe only the model's fixed loopback health endpoint."""
        try:
            request = Request(f"http://127.0.0.1:{port}/health", method="GET")
            with urlopen(request, timeout=0.5) as response:  # noqa: S310 - fixed loopback URL
                return response.status == 200
        except (OSError, URLError):
            return False

    def observe(self, model_id: str, container_name: str, port: int) -> Observation:
        try:
            payload = json.loads(self._inspect(["docker", "inspect", container_name]))
        except (OSError, ValueError, subprocess.SubprocessError):
            return Observation(Presence.UNKNOWN, None, False, monotonic(), "docker_unavailable")
        if not isinstance(payload, list):
            return Observation(Presence.UNKNOWN, None, False, monotonic(), "inspect_protocol_error")
        if not payload:
            return Observation(Presence.STOPPED if not self._port_open(port) else Presence.UNKNOWN, None, False, monotonic(), None)
        if len(payload) != 1 or not isinstance(payload[0], dict):
            return Observation(Presence.UNKNOWN, None, False, monotonic(), "inspect_protocol_error")
        item = payload[0]
        labels = item.get("Config", {}).get("Labels", {})
        state = item.get("State", {})
        identity_ok = labels == {"io.self-model-switch.deployment": self.deployment_id, "io.self-model-switch.model": model_id, "io.self-model-switch.config-sha256": self.config_sha256}
        image_ok = item.get("Config", {}).get("Image") == self.image_digest
        running = state.get("Running") is True
        instance = f"{item.get('Id')}:{state.get('StartedAt')}" if item.get("Id") and state.get("StartedAt") else None
        if identity_ok and image_ok and running and self._port_open(port) and self._health(port):
            return Observation(Presence.RUNNING, instance, True, monotonic(), None)
        return Observation(Presence.UNKNOWN, instance, False, monotonic(), "container_identity_or_health_mismatch")


# ---------------------------------------------------------------------------
# C03 observer (M02/P07).
# ---------------------------------------------------------------------------

CONTAINER_LABEL_PREFIX = "io.self-model-switch"
DEPLOYMENT_LABEL = f"{CONTAINER_LABEL_PREFIX}.deployment"
MODEL_LABEL = f"{CONTAINER_LABEL_PREFIX}.model"
RUNTIME_LABEL = f"{CONTAINER_LABEL_PREFIX}.runtime"
CONFIG_LABEL = f"{CONTAINER_LABEL_PREFIX}.config-sha256"
CANDIDATE_LABEL = f"{CONTAINER_LABEL_PREFIX}.candidate-sha256"

DOCKER_TIMEOUT_SECONDS = 30
_STRUCTURED_NOT_FOUND = re.compile(r"\b(No such object|No such container):", re.IGNORECASE)
_ISO_INSTANT = re.compile(
    r"^(?P<base>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(?P<fraction>\d+))?(?P<offset>Z|[+-]\d{2}:\d{2})$"
)
_AMBIGUOUS = object()


class ObservationError(RuntimeError):
    """The world could not be observed; the caller must stay UNKNOWN."""


@dataclass(frozen=True)
class ContainerFact:
    """One container as reported by `docker inspect`."""

    container_id: str
    image_digest: str
    running: bool
    status: str
    exit_code: int
    started_at: str
    labels: Mapping[str, str]


class SystemClock:
    """The production Clock port: monotonic for deadlines, UTC for evidence."""

    def monotonic(self) -> float:
        return time.monotonic()

    def utc_now(self) -> datetime:
        return datetime.now(UTC)


def os_process_state(pid: int) -> str:
    """Observe one process through the OS; never from scheduler memory.

    A zombie still counts as running, and a foreign pid is reported as running:
    both directions keep a stop from being proven too early.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1:
        return "unknown"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "absent"
    except PermissionError:
        return "running"
    except OSError:
        return "unknown"
    return "running"


def docker_port_state(port: int) -> str:
    """`listening`, `closed` (refused) or `unknown`; an unknown owner is not closed."""
    with socket.socket() as sock:
        sock.settimeout(0.5)
        try:
            code = sock.connect_ex(("127.0.0.1", port))
        except OSError:
            return "unknown"
    if code == 0:
        return "listening"
    if code == errno.ECONNREFUSED:
        return "closed"
    return "unknown"


def structured_not_found(exit_code: int, stderr: str) -> bool:
    """Docker's exact not-found message; any other failure is not an absence."""
    return exit_code != 0 and _STRUCTURED_NOT_FOUND.search(stderr or "") is not None


def run_docker(argv: Sequence[str]) -> tuple[int, str, str]:
    """The production docker CLI port: exact argv in, exit code and streams out."""
    try:
        completed = subprocess.run(
            list(argv), capture_output=True, text=True, timeout=DOCKER_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ObservationError(f"docker command failed: {exc}") from exc
    return completed.returncode, completed.stdout, completed.stderr


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant: {value}")


def parse_inspect_payload(raw: str) -> tuple[ContainerFact, ...]:
    """Strictly parse one `docker inspect` payload; anything else is an error."""
    try:
        payload = json.loads(raw, parse_constant=_reject_json_constant)
    except (TypeError, ValueError) as exc:
        raise ObservationError(f"docker inspect returned invalid JSON: {exc}") from exc
    if not isinstance(payload, list) or not payload:
        raise ObservationError("docker inspect returned no container object")
    return tuple(_container_fact(item) for item in payload)


def _container_fact(item: object) -> ContainerFact:
    if not isinstance(item, dict):
        raise ObservationError("docker inspect item must be an object")
    container_id = item.get("Id")
    config, state = item.get("Config"), item.get("State")
    # The *reference* the container was started with (`Config.Image`), never the bare
    # image ID in `Image`: the contract and the registration both speak
    # `name@sha256:<64-hex>`, so an ID could never be compared or reported.
    image = config.get("Image") if isinstance(config, dict) else None
    if not isinstance(container_id, str) or not container_id:
        raise ObservationError("docker inspect item has no container id")
    if not isinstance(image, str) or not image:
        raise ObservationError("docker inspect item has no image reference")
    if not isinstance(config, dict) or not isinstance(state, dict):
        raise ObservationError("docker inspect item has no config or state")
    labels = config.get("Labels")
    if not isinstance(labels, dict) or any(type(key) is not str or type(value) is not str for key, value in labels.items()):
        raise ObservationError("docker inspect item has unreadable labels")
    running, status, exit_code, started_at = (
        state.get("Running"),
        state.get("Status"),
        state.get("ExitCode"),
        state.get("StartedAt"),
    )
    if type(running) is not bool:
        raise ObservationError("docker inspect item has no running flag")
    if not isinstance(status, str) or not status:
        raise ObservationError("docker inspect item has no status")
    if type(exit_code) is not int or exit_code < 0:
        raise ObservationError("docker inspect item has no exit code")
    if not isinstance(started_at, str) or not started_at:
        raise ObservationError("docker inspect item has no start time")
    return ContainerFact(
        container_id=container_id,
        image_digest=image,
        running=running,
        status=status,
        exit_code=exit_code,
        started_at=started_at,
        labels=labels,
    )


def canonical_utc(value: str) -> str:
    """A valid, timezone-aware UTC instant; the identity rule of C03.

    Docker emits nanosecond precision and either `Z` or a numeric offset, so the
    identity keeps one canonical `...THH:MM:SS[.fraction]Z` spelling (trailing
    zeros removed) that compares equal for equal instants.
    """
    match = _ISO_INSTANT.match(value.strip()) if isinstance(value, str) else None
    if match is None:
        raise ObservationError(f"invalid start time: {value!r}")
    fraction = (match.group("fraction") or "").rstrip("0")
    offset = "+00:00" if match.group("offset") == "Z" else match.group("offset")
    try:
        parsed = datetime.fromisoformat(f"{match.group('base')}.{fraction[:6] or '0'}{offset}")
    except ValueError as exc:
        raise ObservationError(f"invalid start time: {value!r}") from exc
    if parsed.utcoffset() is None:  # the regex already guarantees an explicit offset
        raise ObservationError(f"start time has no UTC offset: {value!r}")
    utc = parsed.astimezone(UTC)
    return f"{utc.strftime('%Y-%m-%dT%H:%M:%S')}{'.' + fraction if fraction else ''}Z"


def list_container_ids(deployment_id: str, model_id: str | None, docker: Callable[[Sequence[str]], tuple[int, str, str]]) -> tuple[str, ...]:
    """The authoritative listing: only this deployment (and optional model) ids."""
    argv = [
        "docker", "ps",
        "--all",
        "--quiet",
        "--no-trunc",
        "--filter", f"label={DEPLOYMENT_LABEL}={deployment_id}",
    ]
    if model_id is not None:
        argv += ["--filter", f"label={MODEL_LABEL}={model_id}"]
    exit_code, stdout, _stderr = docker(argv)
    if exit_code != 0:
        raise ObservationError("the container listing failed")
    return tuple(line.strip() for line in stdout.splitlines() if line.strip())


def inspect_container_ids(
    container_ids: Sequence[str], docker: Callable[[Sequence[str]], tuple[int, str, str]]
) -> tuple[ContainerFact, ...]:
    if not container_ids:
        return ()
    exit_code, stdout, stderr = docker(["docker", "inspect", *container_ids])
    if exit_code != 0:
        raise ObservationError(f"docker inspect failed with exit code {exit_code}: {stderr.strip()}")
    return parse_inspect_payload(stdout)


class DockerProcessObserver:
    """The v3 ObserverPort over one deployment/model instance (P07).

    The observer is constructed per model, so its listing can never widen to a
    foreign deployment or a sibling model. Facts come from the docker CLI, the
    loopback port and the OS process table; the verdict is the single
    `stopped_is_proven` computation. A launcher this boot never recorded leaves
    the container and port facts as the binding stop evidence.
    """

    def __init__(
        self,
        deployment_id: str,
        model_id: str,
        port: int,
        *,
        docker: Callable[[Sequence[str]], tuple[int, str, str]] | None = None,
        port_state: Callable[[int], str] | None = None,
        launch_lookup: Callable[[ports_v3.ObservationTarget], ports_v3.LaunchOperation | None] | None = None,
        process_state: Callable[[int], str] | None = None,
        clock: ports_v3.Clock | None = None,
    ) -> None:
        if not isinstance(deployment_id, str) or not deployment_id:
            raise ValueError("an observer requires a deployment id")
        if not isinstance(model_id, str) or not model_id:
            raise ValueError("an observer requires a model id")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("an observer requires a loopback port")
        self._deployment_id, self._model_id, self._port = deployment_id, model_id, port
        self._docker = docker or run_docker
        self._port_state = port_state or docker_port_state
        self._launch_lookup = launch_lookup
        self._process_state = process_state or os_process_state
        self._clock = clock or SystemClock()

    async def observe(self, target: ports_v3.ObservationTarget, deadline: float) -> ports_v3.Observation:
        if not isinstance(target, ports_v3.ObservationTarget) or target.deployment_id != self._deployment_id:
            raise ObservationError("the observation target belongs to another deployment")
        now_monotonic, now_utc = self._clock.monotonic(), self._clock.utc_now()
        launch = self._launch_lookup(target) if self._launch_lookup is not None else None
        if now_monotonic >= deadline:
            return self._observation(UNKNOWN, now_monotonic, now_utc, "unknown", "unknown", None, launch)
        port_state = await asyncio.to_thread(self._port_state, self._port)
        try:
            facts = await asyncio.to_thread(self._facts)
        except ObservationError:
            return self._observation(UNKNOWN, now_monotonic, now_utc, "unknown", "unknown", None, launch)
        matched = self._match(target, facts)
        subprocess_state = await asyncio.to_thread(self._subprocess_state, target, launch)
        if matched is _AMBIGUOUS:
            return self._observation(UNKNOWN, now_monotonic, now_utc, port_state, subprocess_state, None, launch)
        if matched is not None and matched.running:
            identity = self._identity(matched)
            if identity is not None and port_state == "listening":
                return self._observation(ports_v3.RUNNING, now_monotonic, now_utc, port_state, subprocess_state, identity, launch)
            return self._observation(UNKNOWN, now_monotonic, now_utc, port_state, subprocess_state, None, launch)
        container_gone = matched is None or not matched.running
        launcher_terminal = launch.is_terminal if launch is not None else True
        subprocess_exited = subprocess_state in {"exited", "absent"}
        if ports_v3.stopped_is_proven(
            container_absent=container_gone,
            launch_operation_terminal=launcher_terminal,
            subprocess_exited=subprocess_exited,
            port_listening=True if port_state == "listening" else False if port_state == "closed" else None,
        ):
            instance = self._identity(matched) if matched is not None else None
            return self._observation(STOPPED, now_monotonic, now_utc, port_state, subprocess_state, instance, launch)
        return self._observation(UNKNOWN, now_monotonic, now_utc, port_state, subprocess_state, None, launch)

    def _facts(self) -> tuple[ContainerFact, ...]:
        return inspect_container_ids(list_container_ids(self._deployment_id, self._model_id, self._docker), self._docker)

    def _match(self, target: ports_v3.ObservationTarget, facts: tuple[ContainerFact, ...]) -> ContainerFact | None | object:
        """Bind the target to one container fact.

        With no explicit container id, this boot's RUNNING instance is the
        candidate; exited containers for the same model are historical facts
        and never make a reload ambiguous. Two running instances of one model
        are always ambiguous. With an explicit container id the listing must
        contain that exact id (P16 AC1: reload after a fallback stop).
        """
        candidates = [fact for fact in facts if target.container_id is None or fact.container_id == target.container_id]
        if target.container_id is not None:
            if len(candidates) > 1:  # impossible with unique ids, kept fail-closed
                return _AMBIGUOUS
            return candidates[0] if candidates else None
        running = [fact for fact in candidates if fact.running]
        if len(running) > 1:
            return _AMBIGUOUS
        return running[0] if running else None

    def _subprocess_state(self, target: ports_v3.ObservationTarget, launch: ports_v3.LaunchOperation | None) -> str:
        pid = launch.pid if launch is not None else target.process_group_id
        if pid is None:
            return "absent"
        return self._process_state(pid)

    def _identity(self, fact: ContainerFact) -> InstanceIdentity | None:
        labels = fact.labels
        runtime_id = labels.get(RUNTIME_LABEL)
        digest = labels.get(CANDIDATE_LABEL) or labels.get(CONFIG_LABEL)
        if labels.get(DEPLOYMENT_LABEL) != self._deployment_id or labels.get(MODEL_LABEL) != self._model_id:
            return None
        if not runtime_id or not digest:
            return None
        try:
            started_at = canonical_utc(fact.started_at)
        except ObservationError:
            return None
        return InstanceIdentity(
            container_id=fact.container_id,
            started_at=started_at,
            deployment_id=self._deployment_id,
            model_id=self._model_id,
            runtime_id=runtime_id,
            candidate_digest=digest,
            image_digest=fact.image_digest,
        )

    def _observation(
        self,
        state: str,
        sampled_at_monotonic: float,
        sampled_at_utc: datetime,
        port_state: str,
        subprocess_state: str,
        instance: InstanceIdentity | None,
        launch: ports_v3.LaunchOperation | None,
    ) -> ports_v3.Observation:
        return ports_v3.Observation(
            state=state,
            sampled_at_monotonic=sampled_at_monotonic,
            sampled_at_utc=sampled_at_utc,
            port_state=port_state,
            subprocess_state=subprocess_state,
            instance=instance,
            launch_operation=launch,
        )
