from __future__ import annotations

import asyncio
from typing import Any, Callable

import httpx


class LlamaSwapError(RuntimeError):
    pass


class LlamaSwapProtocolError(LlamaSwapError):
    pass


class LlamaSwapClient:
    """
    Minimal client for the current llama-swap HTTP surface.

    The release-specific ``running_parser`` is mandatory.  llama-swap does not
    publish a stable response schema across releases, so a client constructed
    without a parser refuses to infer model residency from unverified JSON.
    """

    def __init__(self, base_url: str, timeout: float = 30.0, load_timeout: float = 900.0, *, running_parser: Callable[[Any], list[str]] | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.load_timeout = load_timeout
        self.running_parser = running_parser

    async def health(self) -> bool:
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.get(f"{self.base_url}/health")
            return r.status_code == 200

    async def running(self) -> list[str]:
        if self.running_parser is None:
            raise LlamaSwapProtocolError("fixed llama-swap running fixture is required")
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.get(f"{self.base_url}/running")
            r.raise_for_status()
            try:
                model_ids = self.running_parser(r.json())
            except (TypeError, ValueError, KeyError) as exc:
                raise LlamaSwapProtocolError("invalid fixed llama-swap running response") from exc
            if not isinstance(model_ids, list) or any(type(model_id) is not str or not model_id for model_id in model_ids):
                raise LlamaSwapProtocolError("invalid fixed llama-swap running response")
            return model_ids

    async def list_models(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.get(f"{self.base_url}/v1/models")
            r.raise_for_status()
            return r.json()

    async def load(self, model_id: str):
        """
        Activate a model without generating tokens.

        /props is a llama.cpp endpoint. If you later add a non-llama.cpp backend,
        add a backend-specific warmup method here.
        """
        url = f"{self.base_url}/props"
        async with httpx.AsyncClient(timeout=self.load_timeout) as c:
            try:
                r = await c.get(url, params={"model": model_id})
            except httpx.HTTPError as e:
                raise LlamaSwapError(f"load transport error for {model_id}: {e}") from e

            # Control responses are part of the pinned llama-swap contract.
            # A failed or redirected request cannot prove that the requested
            # operation was accepted, even if a stale /running response happens
            # to list the model.
            if not 200 <= r.status_code < 300:
                raise LlamaSwapError(f"load failed for {model_id}: {r.status_code} {r.text[:500]}")

        running = await self.running()
        if model_id not in running:
            raise LlamaSwapError(f"model {model_id} did not become running; running={running}")

    async def unload(self, model_id: str):
        async with httpx.AsyncClient(timeout=self.load_timeout) as c:
            r = await c.post(f"{self.base_url}/api/models/unload/{model_id}")
            if not 200 <= r.status_code < 300:
                raise LlamaSwapError(f"unload failed for {model_id}: {r.status_code} {r.text[:500]}")

    async def unload_all(self):
        async with httpx.AsyncClient(timeout=self.load_timeout) as c:
            r = await c.post(f"{self.base_url}/api/models/unload")
            if not 200 <= r.status_code < 300:
                raise LlamaSwapError(f"unload-all failed: {r.status_code} {r.text[:500]}")

    async def proxy_json(self, path: str, payload: dict[str, Any], timeout: float | None = None):
        t = timeout or self.load_timeout
        async with httpx.AsyncClient(timeout=t) as c:
            r = await c.post(f"{self.base_url}{path}", json=payload)
            r.raise_for_status()
            return r.json()
