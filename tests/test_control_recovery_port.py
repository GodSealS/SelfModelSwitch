from __future__ import annotations

import asyncio

import pytest

from model_scheduler.control_recovery import ControlRecoveryClient


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
