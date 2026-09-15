"""Fixed-command adapter for the root-owned control-plane recovery helper."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import json
from time import monotonic
from typing import Awaitable, Callable

from .contracts import RecoveryResult


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
