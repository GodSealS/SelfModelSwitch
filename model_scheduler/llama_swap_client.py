from __future__ import annotations

import asyncio
from typing import Any

import httpx


class LlamaSwapError(RuntimeError):
    pass


class LlamaSwapClient:
    """
    Minimal client for the current llama-swap HTTP surface.

    Current documented endpoints used here:
      GET  /health
      GET  /running
      GET  /v1/models
      GET  /props?model=<id>       -> dispatches the model and waits for readiness
      POST /api/models/unload/<id>
      POST /api/models/unload      -> unload all

    llama-swap does not need a separate load endpoint for this design: a model
    is activated by dispatching a request to it. For llama.cpp-backed models,
    /props?model= is a lightweight warm-up/dispatch route.
    """

    def __init__(self, base_url: str, timeout: float = 30.0, load_timeout: float = 900.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.load_timeout = load_timeout

    async def health(self) -> bool:
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.get(f"{self.base_url}/health")
            return r.status_code == 200

    async def running(self) -> list[str]:
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.get(f"{self.base_url}/running")
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):
                return [x if isinstance(x, str) else x.get("id", x.get("model", "")) for x in data]
            if isinstance(data, dict):
                models = data.get("models", data.get("running", []))
                return [x if isinstance(x, str) else x.get("id", x.get("model", "")) for x in models]
            return []

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

            # A llama.cpp upstream may return 200. A non-llama.cpp upstream can
            # return 404 after llama-swap has already performed the model swap.
            # We therefore confirm with /running before treating 404 as fatal.
            if r.status_code >= 500:
                raise LlamaSwapError(f"load failed for {model_id}: {r.status_code} {r.text[:500]}")

        running = await self.running()
        if model_id not in running:
            raise LlamaSwapError(f"model {model_id} did not become running; running={running}")

    async def unload(self, model_id: str):
        async with httpx.AsyncClient(timeout=self.load_timeout) as c:
            r = await c.post(f"{self.base_url}/api/models/unload/{model_id}")
            if r.status_code >= 400:
                raise LlamaSwapError(f"unload failed for {model_id}: {r.status_code} {r.text[:500]}")

    async def unload_all(self):
        async with httpx.AsyncClient(timeout=self.load_timeout) as c:
            r = await c.post(f"{self.base_url}/api/models/unload")
            if r.status_code >= 400:
                raise LlamaSwapError(f"unload-all failed: {r.status_code} {r.text[:500]}")

    async def proxy_json(self, path: str, payload: dict[str, Any], timeout: float | None = None):
        t = timeout or self.load_timeout
        async with httpx.AsyncClient(timeout=t) as c:
            r = await c.post(f"{self.base_url}{path}", json=payload)
            r.raise_for_status()
            return r.json()
