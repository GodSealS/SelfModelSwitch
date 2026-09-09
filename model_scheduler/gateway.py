from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx


class Gateway:
    def __init__(self, base_url: str, timeout: float = 1800):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def chat(self, payload: dict[str, Any]):
        """
        Non-streaming proxy. For streaming clients use stream_chat().
        """
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.post(f"{self.base_url}/v1/chat/completions", json=payload)
            r.raise_for_status()
            return r.json()

    async def stream_chat(self, payload: dict[str, Any]) -> AsyncIterator[bytes]:
        client = httpx.AsyncClient(timeout=self.timeout)
        req = client.build_request("POST", f"{self.base_url}/v1/chat/completions", json=payload)
        resp = await client.send(req, stream=True)
        if resp.status_code >= 400:
            body = await resp.aread()
            await resp.aclose()
            await client.aclose()
            raise RuntimeError(f"upstream {resp.status_code}: {body.decode(errors='replace')}")
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()
