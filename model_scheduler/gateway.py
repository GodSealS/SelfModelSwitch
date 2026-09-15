"""Direct inference gateway; llama-swap is never on this data path."""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping

import httpx

from .contracts import Capability, GatewayError, Lease, OpenedResponse, Outcome


class OpenedHTTPXResponse:
    def __init__(self, response: httpx.Response):
        self._response = response
        self.status_code = response.status_code
        self.headers = response.headers

    def iter_bytes(self) -> AsyncIterator[bytes]:
        return self._response.aiter_bytes()

    async def aclose(self) -> None:
        await self._response.aclose()

    async def json(self) -> object:
        data = await self._response.aread()
        return json.loads(data)


class DirectInferenceGateway:
    def __init__(self, upstreams: Mapping[str, str], client: httpx.AsyncClient):
        self._upstreams = dict(upstreams)
        self._client = client

    @staticmethod
    def _path(capability: Capability) -> str:
        return {Capability.CHAT: "/v1/chat/completions", Capability.EMBEDDINGS: "/v1/embeddings", Capability.RERANK: "/reranking"}[capability]

    async def open(self, lease: Lease, capability: Capability, payload: Mapping[str, object], deadline: float) -> OpenedHTTPXResponse:
        base = self._upstreams.get(lease.model_id)
        if base is None:
            raise GatewayError(502, "upstream_configuration_error", Outcome.ABORTED)
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise GatewayError(504, "inference_timeout", Outcome.ABORTED)
        request = self._client.build_request("POST", f"{base.rstrip('/')}{self._path(capability)}", json=dict(payload))
        try:
            async with asyncio.timeout(remaining):
                response = await self._client.send(request, stream=True)
        except (asyncio.TimeoutError, httpx.HTTPError) as exc:
            raise GatewayError(502, "upstream_unavailable", Outcome.ABORTED) from exc
        if 200 <= response.status_code < 300:
            return OpenedHTTPXResponse(response)
        await response.aclose()
        if response.status_code in {400, 413, 422, 429}:
            raise GatewayError(response.status_code, "upstream_invalid_request", Outcome.REJECTED)
        raise GatewayError(502, "upstream_error", Outcome.ABORTED)
