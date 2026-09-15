from __future__ import annotations

import asyncio

import httpx
import pytest

from model_scheduler.contracts import Capability, GatewayError, Lease, Outcome
from model_scheduler.gateway import DirectInferenceGateway


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
