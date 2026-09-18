"""Control-plane recovery: the root helper port and deployment reconciliation.

`ControlRecoveryClient` keeps the legacy fixed-command helper port. The P07
`DeploymentRecovery` performs the C03 startup sequence locally: admission is
closed first, only this deployment's containers are listed by label, every
candidate is verified against its rendered identity, and the only stop command
ever issued addresses an immutable container id. A stale `InstanceIdentity`
(from an earlier boot or an earlier container) can never stop a newer instance,
and any container whose exit cannot be verified stays on the books.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
import json
import time
from time import monotonic
from typing import Awaitable, Callable, Sequence

from .contracts import RecoveryResult
from .control_protocol_v1 import InstanceIdentity
from .process_observer import (
    DEPLOYMENT_LABEL,
    MODEL_LABEL,
    ContainerFact,
    ObservationError,
    canonical_utc,
    list_container_ids,
    parse_inspect_payload,
    run_docker,
    structured_not_found,
)

DOCKER_RUNNER = Callable[[Sequence[str]], tuple[int, str, str]]
STOP_GRACE_SECONDS = 30


_HELPER_ARGV = ("sudo", "-n", "/usr/local/libexec/sms-control-recover")
_MAX_OUTPUT_BYTES = 16 * 1024
_Runner = Callable[[tuple[str, ...]], Awaitable[tuple[int, str]]]


class ControlRecoveryClient:
    """Expose the root helper as a strict, deadline-bounded scheduler port."""

    def __init__(self, runner: _Runner | None = None) -> None:
        self._runner = runner or self._run_helper

    async def recover(self, deadline: float) -> RecoveryResult:
        remaining = deadline - monotonic()
        if remaining <= 0:
            return _failed("control_recovery_timeout")
        try:
            returncode, output = await asyncio.wait_for(self._runner(_HELPER_ARGV), remaining)
        except asyncio.TimeoutError:
            return _failed("control_recovery_timeout")
        except UnicodeDecodeError:
            return _failed("control_recovery_protocol_error")
        except (OSError, ValueError, asyncio.SubprocessError):
            return _failed("control_recovery_failed")
        if returncode != 0:
            return _failed("control_recovery_failed")
        try:
            return _parse_result(output)
        except (TypeError, ValueError, json.JSONDecodeError):
            return _failed("control_recovery_protocol_error")

    @staticmethod
    async def _run_helper(argv: tuple[str, ...]) -> tuple[int, str]:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            assert process.stdout is not None
            output = await process.stdout.read(_MAX_OUTPUT_BYTES + 1)
            if len(output) > _MAX_OUTPUT_BYTES:
                raise ValueError("recovery helper output exceeds limit")
            returncode = await process.wait()
            return returncode, output.decode("utf-8")
        finally:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
                asyncio.create_task(_reap(process))


def _failed(error_code: str) -> RecoveryResult:
    return RecoveryResult(False, "failed", error_code, ())


def _parse_result(output: str) -> RecoveryResult:
    data = json.loads(output, object_pairs_hook=_unique_object, parse_constant=_reject_json_constant)
    required = {"ok", "phase", "error_code", "stopped_models"}
    if not isinstance(data, dict) or set(data) != required:
        raise ValueError("invalid helper result")
    ok, phase, error_code, stopped_models = (
        data["ok"],
        data["phase"],
        data["error_code"],
        data["stopped_models"],
    )
    if (
        type(ok) is not bool
        or type(phase) is not str
        or (error_code is not None and type(error_code) is not str)
        or not isinstance(stopped_models, list)
        or any(type(model_id) is not str or not model_id for model_id in stopped_models)
        or len(set(stopped_models)) != len(stopped_models)
        or stopped_models != sorted(stopped_models)
    ):
        raise ValueError("invalid helper result")
    if ok and (phase != "complete" or error_code is not None):
        raise ValueError("invalid successful helper result")
    if not ok and (phase not in {"failed", "already_running"} or not error_code or stopped_models):
        raise ValueError("invalid failed helper result")
    return RecoveryResult(ok, phase, error_code, tuple(stopped_models))


async def _reap(process: asyncio.subprocess.Process) -> None:
    with suppress(OSError, asyncio.SubprocessError):
        await process.wait()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    output: dict[str, object] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError("duplicate JSON key")
        output[key] = value
    return output


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant: {value}")


# ---------------------------------------------------------------------------
# C03 startup reconciliation (M02/P07).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReconcileOutcome:
    """What startup proved about this deployment's old instances.

    `stopped_container_ids` were verified not running; `remaining_container_ids`
    could not be proven stopped, so admission must stay closed.
    """

    ok: bool
    error_code: str | None
    stopped_container_ids: tuple[str, ...]
    remaining_container_ids: tuple[str, ...]


@dataclass(frozen=True)
class StopOutcome:
    """The result of one instance stop; `accepted` is not a stop proof."""

    accepted: bool
    stopped: bool
    error_code: str | None
    container_id: str | None


_ABSENT = object()


class DeploymentRecovery:
    """Reconcile exactly one deployment's own containers at startup (C03)."""

    def __init__(
        self,
        deployment_id: str,
        *,
        docker: DOCKER_RUNNER | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        if not isinstance(deployment_id, str) or not deployment_id:
            raise ValueError("reconciliation requires a deployment id")
        self._deployment_id = deployment_id
        self._docker = docker or run_docker
        self._monotonic = monotonic or time.monotonic

    @property
    def deployment_id(self) -> str:
        return self._deployment_id

    def reconcile(self, *, close_admission: Callable[[], None], deadline: float) -> ReconcileOutcome:
        """Close admission first, then stop this deployment's own leftovers by id."""
        close_admission()
        if self._monotonic() >= deadline:
            return ReconcileOutcome(False, "recovery_timeout", (), ())
        try:
            container_ids = list_container_ids(self._deployment_id, None, self._docker)
        except ObservationError:
            return ReconcileOutcome(False, "container_listing_failed", (), ())
        stopped: list[str] = []
        remaining: list[str] = []
        error_code: str | None = None
        for container_id in container_ids:
            if self._monotonic() >= deadline:
                remaining.append(container_id)
                error_code = error_code or "recovery_timeout"
                continue
            proven, failure = self._verified_stop(container_id)
            if proven:
                stopped.append(container_id)
            else:
                remaining.append(container_id)
                error_code = error_code or failure
        return ReconcileOutcome(not remaining, error_code, tuple(stopped), tuple(remaining))

    def stop_instance(self, identity: InstanceIdentity, *, deadline: float) -> StopOutcome:
        """Stop one exact instance; a stale identity is refused, not retargeted."""
        if not isinstance(identity, InstanceIdentity) or identity.deployment_id != self._deployment_id:
            return StopOutcome(False, False, "foreign_instance", None)
        if self._monotonic() >= deadline:
            return StopOutcome(False, False, "recovery_timeout", identity.container_id)
        fact = self._inspect(identity.container_id)
        if fact is _ABSENT:
            return StopOutcome(True, True, None, identity.container_id)
        if fact is None:
            return StopOutcome(False, False, "docker_unavailable", identity.container_id)
        if not self._identity_matches(fact, identity):
            return StopOutcome(False, False, "stale_identity", identity.container_id)
        if not fact.running:
            return StopOutcome(True, True, None, identity.container_id)
        exit_code, _stdout, _stderr = self._docker(["docker", "stop", "--time", str(STOP_GRACE_SECONDS), identity.container_id])
        if exit_code != 0:
            return StopOutcome(False, False, "stop_failed", identity.container_id)
        after = self._inspect(identity.container_id)
        if after is _ABSENT or (after is not None and not after.running):
            return StopOutcome(True, True, None, identity.container_id)
        return StopOutcome(False, False, "stop_not_verified", identity.container_id)

    def _verified_stop(self, container_id: str) -> tuple[bool, str | None]:
        fact = self._inspect(container_id)
        if fact is _ABSENT:
            return True, None
        if fact is None:
            return False, "docker_unavailable"
        if fact.container_id != container_id or fact.labels.get(DEPLOYMENT_LABEL) != self._deployment_id:
            return False, "foreign_instance"
        if not fact.running:
            return True, None
        exit_code, _stdout, _stderr = self._docker(["docker", "stop", "--time", str(STOP_GRACE_SECONDS), container_id])
        if exit_code != 0:
            return False, "stop_failed"
        after = self._inspect(container_id)
        if after is _ABSENT or (after is not None and not after.running):
            return True, None
        return False, "stop_not_verified"

    def _identity_matches(self, fact: ContainerFact, identity: InstanceIdentity) -> bool:
        if fact.container_id != identity.container_id or fact.labels.get(DEPLOYMENT_LABEL) != self._deployment_id:
            return False
        if fact.labels.get(MODEL_LABEL) != identity.model_id:
            return False
        try:
            return canonical_utc(fact.started_at) == canonical_utc(identity.started_at)
        except ObservationError:
            return False

    def _inspect(self, container_id: str) -> ContainerFact | None | object:
        exit_code, stdout, stderr = self._docker(["docker", "inspect", container_id])
        if exit_code != 0:
            if structured_not_found(exit_code, stderr):
                return _ABSENT
            return None
        try:
            facts = parse_inspect_payload(stdout)
        except ObservationError:
            return None
        if len(facts) != 1 or facts[0].container_id != container_id:
            return None
        return facts[0]
