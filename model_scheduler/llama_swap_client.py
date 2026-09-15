from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import quote

import httpx


class LlamaSwapError(RuntimeError):
    pass


class LlamaSwapProtocolError(LlamaSwapError):
    pass


@dataclass(frozen=True)
class LlamaSwapControlContract:
    """Pinned request paths and response validators for one llama-swap release."""

    running_parser: Callable[[Any], list[str]]
    load_path: str
    unload_path: str
    validate_load_response: Callable[[Any], None]
    validate_unload_response: Callable[[Any], None]

    def __post_init__(self) -> None:
        if not self.load_path.startswith("/") or not self.unload_path.startswith("/"):
            raise ValueError("control paths must be absolute")
        if (self.unload_path.count("{model_id}") != 1
                or "{" in self.unload_path.replace("{model_id}", "")
                or "}" in self.unload_path.replace("{model_id}", "")):
            raise ValueError("unload path must contain exactly one model_id placeholder")


class LlamaSwapClient:
    """
    Minimal client for the current llama-swap HTTP surface.

    A complete, release-specific control contract is mandatory. llama-swap
    does not publish stable control endpoints or response schemas, so a client
    without a fixture-derived contract refuses to send state-changing requests.
    """

    def __init__(self, base_url: str, timeout: float = 30.0, load_timeout: float = 900.0, *, contract: LlamaSwapControlContract | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.load_timeout = load_timeout
        self.contract = contract

    def _contract(self) -> LlamaSwapControlContract:
        if self.contract is None:
            raise LlamaSwapProtocolError("fixed llama-swap control contract is required")
        return self.contract

    async def health(self) -> bool:
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.get(f"{self.base_url}/health")
            return r.status_code == 200

    async def running(self) -> list[str]:
        if self.contract is None:
            raise LlamaSwapProtocolError("fixed llama-swap running fixture is required")
        contract = self.contract
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.get(f"{self.base_url}/running")
            r.raise_for_status()
            try:
                model_ids = contract.running_parser(r.json())
            except Exception as exc:
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
        contract = self._contract()
        url = f"{self.base_url}{contract.load_path}"
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
            self._validate_response(r, contract.validate_load_response, "load")

        running = await self.running()
        if model_id not in running:
            raise LlamaSwapError(f"model {model_id} did not become running; running={running}")

    async def unload(self, model_id: str):
        contract = self._contract()
        async with httpx.AsyncClient(timeout=self.load_timeout) as c:
            path = contract.unload_path.format(model_id=quote(model_id, safe=""))
            r = await c.post(f"{self.base_url}{path}")
            if not 200 <= r.status_code < 300:
                raise LlamaSwapError(f"unload failed for {model_id}: {r.status_code} {r.text[:500]}")
            self._validate_response(r, contract.validate_unload_response, "unload")

    async def unload_all(self):
        raise LlamaSwapProtocolError("unload-all is not in the fixed llama-swap control contract")

    async def proxy_json(self, path: str, payload: dict[str, Any], timeout: float | None = None):
        t = timeout or self.load_timeout
        async with httpx.AsyncClient(timeout=t) as c:
            r = await c.post(f"{self.base_url}{path}", json=payload)
            r.raise_for_status()
            return r.json()

    @staticmethod
    def _validate_response(response: Any, validator: Callable[[Any], None], action: str) -> None:
        try:
            validator(response.json())
        except Exception as exc:
            raise LlamaSwapProtocolError(f"invalid fixed llama-swap {action} response") from exc
