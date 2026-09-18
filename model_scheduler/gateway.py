"""Direct inference gateway; llama-swap is never on this data path."""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping

import httpx

from .contracts import Capability, GatewayError, Lease, OpenedResponse, Outcome


class OpenedHTTPXResponse:
    def __init__(self, response: httpx.Response, max_response_body_bytes: int, deadline: float):
        self._response = response
        self._max_response_body_bytes = max_response_body_bytes
        self._deadline = deadline
        self.status_code = response.status_code
        self.headers = response.headers

    def iter_bytes(self) -> AsyncIterator[bytes]:
        async def bounded() -> AsyncIterator[bytes]:
            try:
                async with asyncio.timeout_at(self._deadline):
                    async for chunk in self._response.aiter_bytes():
                        yield chunk
            except asyncio.TimeoutError as exc:
                raise GatewayError(504, "inference_timeout", Outcome.ABORTED) from exc

        return bounded()

    async def aclose(self) -> None:
        await self._response.aclose()

    async def json(self) -> object:
        data = bytearray()
        try:
            async with asyncio.timeout_at(self._deadline):
                async for chunk in self._response.aiter_bytes():
                    if len(data) + len(chunk) > self._max_response_body_bytes:
                        try:
                            await self.aclose()
                        finally:
                            raise GatewayError(502, "upstream_response_too_large", Outcome.ABORTED)
                    data.extend(chunk)
        except asyncio.TimeoutError as exc:
            try:
                await self.aclose()
            finally:
                raise GatewayError(504, "inference_timeout", Outcome.ABORTED) from exc
        except httpx.HTTPError as exc:
            raise GatewayError(502, "upstream_unavailable", Outcome.ABORTED) from exc
        try:
            return json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GatewayError(502, "upstream_protocol_error", Outcome.ABORTED) from exc


class DirectInferenceGateway:
    def __init__(self, upstreams: Mapping[str, str], client: httpx.AsyncClient, *, max_response_body_bytes: int = 16 * 1024 * 1024):
        if max_response_body_bytes <= 0:
            raise ValueError("max_response_body_bytes must be positive")
        self._upstreams = dict(upstreams)
        self._client = client
        self._max_response_body_bytes = max_response_body_bytes

    @staticmethod
    def _path(capability: Capability) -> str:
        return {Capability.CHAT: "/v1/chat/completions", Capability.EMBEDDINGS: "/v1/embeddings", Capability.RERANK: "/reranking"}[capability]

    @staticmethod
    def _retry_after(response: httpx.Response) -> int:
        try:
            value = int(response.headers.get("retry-after", ""))
        except ValueError:
            return 1
        return value if 1 <= value <= 60 else 1

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
                response = await self._client.send(request, stream=True, follow_redirects=False)
        except asyncio.TimeoutError as exc:
            raise GatewayError(504, "inference_timeout", Outcome.ABORTED) from exc
        except httpx.HTTPError as exc:
            raise GatewayError(502, "upstream_unavailable", Outcome.ABORTED) from exc
        if 200 <= response.status_code < 300:
            return OpenedHTTPXResponse(response, self._max_response_body_bytes, deadline)
        status_code = response.status_code
        retry_after = self._retry_after(response) if status_code == 429 else None
        await response.aclose()
        if status_code in {400, 422}:
            raise GatewayError(status_code, "upstream_invalid_request", Outcome.REJECTED)
        if status_code == 413:
            raise GatewayError(413, "upstream_request_too_large", Outcome.REJECTED)
        if status_code == 429:
            raise GatewayError(429, "upstream_rate_limited", Outcome.REJECTED, retry_after=retry_after)
        if status_code == 503:
            raise GatewayError(503, "upstream_unavailable", Outcome.ABORTED)
        if status_code in {401, 403, 404} or 300 <= status_code < 400:
            raise GatewayError(502, "upstream_configuration_error", Outcome.ABORTED)
        raise GatewayError(502, "upstream_error", Outcome.ABORTED)
