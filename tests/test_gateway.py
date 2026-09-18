from __future__ import annotations

import asyncio

import httpx
import pytest

from model_scheduler.contracts import Capability, GatewayError, Lease, Outcome
from model_scheduler.gateway import DirectInferenceGateway


@pytest.mark.asyncio
async def test_gateway_does_not_follow_a_redirect_to_an_external_url() -> None:
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://evil.example/steal"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    gateway = DirectInferenceGateway({"chat": "http://127.0.0.1:10003"}, client)
    with pytest.raises(GatewayError) as error:
        await gateway.open(
            Lease("lease", "request", "chat", 1),
            Capability.CHAT,
            {"model": "chat", "messages": []},
            asyncio.get_running_loop().time() + 10,
        )
    assert error.value.code == "upstream_configuration_error"
    assert seen == ["http://127.0.0.1:10003/v1/chat/completions"]
    await client.aclose()


@pytest.mark.asyncio
async def test_gateway_uses_model_direct_loopback_url_without_redirects() -> None:
    seen: list[httpx.URL] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    gateway = DirectInferenceGateway({"chat": "http://127.0.0.1:10003"}, client)
    opened = await gateway.open(Lease("lease", "request", "chat", 1), Capability.CHAT, {"model": "chat", "messages": []}, asyncio.get_running_loop().time() + 10)
    assert opened.status_code == 200
    assert str(seen[0]) == "http://127.0.0.1:10003/v1/chat/completions"
    await opened.aclose()
    await client.aclose()


@pytest.mark.asyncio
async def test_gateway_classifies_complete_upstream_400_as_rejected() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(400)), follow_redirects=False)
    gateway = DirectInferenceGateway({"chat": "http://127.0.0.1:10003"}, client)
    with pytest.raises(GatewayError) as error:
        await gateway.open(Lease("lease", "request", "chat", 1), Capability.CHAT, {}, asyncio.get_running_loop().time() + 10)
    assert error.value.outcome is Outcome.REJECTED
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected_status", "expected_code", "expected_outcome"),
    [
        (404, 502, "upstream_configuration_error", Outcome.ABORTED),
        (413, 413, "upstream_request_too_large", Outcome.REJECTED),
        (429, 429, "upstream_rate_limited", Outcome.REJECTED),
        (503, 503, "upstream_unavailable", Outcome.ABORTED),
    ],
)
async def test_gateway_preserves_the_public_error_class_for_known_upstream_statuses(status_code, expected_status, expected_code, expected_outcome) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(status_code)), follow_redirects=False)
    gateway = DirectInferenceGateway({"chat": "http://127.0.0.1:10003"}, client)
    with pytest.raises(GatewayError) as error:
        await gateway.open(Lease("lease", "request", "chat", 1), Capability.CHAT, {}, asyncio.get_running_loop().time() + 10)
    assert (error.value.http_status, error.value.code, error.value.outcome) == (expected_status, expected_code, expected_outcome)
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(("header", "expected_retry_after"), [("7", 7), ("0", 1), ("invalid", 1), ("61", 1)])
async def test_gateway_clamps_or_defaults_upstream_retry_after(header, expected_retry_after) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(429, headers={"retry-after": header})), follow_redirects=False)
    gateway = DirectInferenceGateway({"chat": "http://127.0.0.1:10003"}, client)
    with pytest.raises(GatewayError) as error:
        await gateway.open(Lease("lease", "request", "chat", 1), Capability.CHAT, {}, asyncio.get_running_loop().time() + 10)
    assert error.value.retry_after == expected_retry_after
    await client.aclose()


@pytest.mark.asyncio
async def test_gateway_reports_an_absolute_inference_deadline_as_a_timeout() -> None:
    async def slow_handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.1)
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(slow_handler), follow_redirects=False)
    gateway = DirectInferenceGateway({"chat": "http://127.0.0.1:10003"}, client)
    with pytest.raises(GatewayError) as error:
        await gateway.open(Lease("lease", "request", "chat", 1), Capability.CHAT, {}, asyncio.get_running_loop().time() + 0.001)
    assert (error.value.http_status, error.value.code, error.value.outcome) == (504, "inference_timeout", Outcome.ABORTED)
    await client.aclose()


@pytest.mark.asyncio
async def test_gateway_bounds_non_streaming_response_bytes_and_normalizes_invalid_json() -> None:
    async def assert_json_error(content: bytes, expected_code: str) -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=content)), follow_redirects=False)
        gateway = DirectInferenceGateway({"chat": "http://127.0.0.1:10003"}, client, max_response_body_bytes=4)
        opened = await gateway.open(Lease("lease", "request", "chat", 1), Capability.CHAT, {}, asyncio.get_running_loop().time() + 10)
        with pytest.raises(GatewayError) as error:
            await opened.json()
        assert (error.value.code, error.value.outcome) == (expected_code, Outcome.ABORTED)
        await client.aclose()

    await assert_json_error(b"12345", "upstream_response_too_large")
    await assert_json_error(b"nope", "upstream_protocol_error")


@pytest.mark.asyncio
async def test_gateway_enforces_the_absolute_deadline_while_reading_a_success_body() -> None:
    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(0.1)
            yield b'{"ok":true}'

        async def aclose(self):
            return None

    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=SlowBody())), follow_redirects=False)
    gateway = DirectInferenceGateway({"chat": "http://127.0.0.1:10003"}, client)
    opened = await gateway.open(Lease("lease", "request", "chat", 1), Capability.CHAT, {}, asyncio.get_running_loop().time() + 0.001)
    with pytest.raises(GatewayError) as error:
        await opened.json()
    assert (error.value.http_status, error.value.code, error.value.outcome) == (504, "inference_timeout", Outcome.ABORTED)

    opened = await gateway.open(Lease("lease-2", "request", "chat", 1), Capability.CHAT, {}, asyncio.get_running_loop().time() + 0.001)
    with pytest.raises(GatewayError) as error:
        _ = [chunk async for chunk in opened.iter_bytes()]
    assert (error.value.http_status, error.value.code, error.value.outcome) == (504, "inference_timeout", Outcome.ABORTED)
    await client.aclose()
