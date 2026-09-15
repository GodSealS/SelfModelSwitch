from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from typing import Any, Callable
from urllib.parse import quote

import httpx


class LlamaSwapError(RuntimeError):
    pass


class LlamaSwapProtocolError(LlamaSwapError):
    pass


@dataclass(frozen=True)
class LlamaSwapResponse:
    """Immutable response evidence passed to a fixture-derived validator."""

    status_code: int
    content_type: str | None
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)


@dataclass(frozen=True)
class ControlRequest:
    """A release-fixture-derived, non-shell request used only for model load."""

    method: str
    path: str
    params: dict[str, str] | None = None
    json_body: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.method not in {"GET", "POST"} or not self.path.startswith("/") or "?" in self.path or "#" in self.path:
            raise ValueError("invalid fixed control request")
        if self.method == "GET" and self.json_body is not None:
            raise ValueError("GET control request cannot have a JSON body")
        if self.params is not None and any(type(key) is not str or type(value) is not str for key, value in self.params.items()):
            raise ValueError("control request params must be strings")
        if self.json_body is not None and not isinstance(self.json_body, dict):
            raise ValueError("control request JSON body must be an object")


@dataclass(frozen=True)
class LlamaSwapControlContract:
    """Pinned request paths and response validators for one llama-swap release."""

    running_parser: Callable[[Any], list[str]]
    load_request: Callable[[str], ControlRequest]
    unload_path: str
    validate_load_response: Callable[[LlamaSwapResponse], None]
    validate_unload_response: Callable[[LlamaSwapResponse], None]

    def __post_init__(self) -> None:
        if not self.unload_path.startswith("/"):
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
        """Activate a model through the fixture-derived control request."""
        contract = self._contract()
        try:
            request = contract.load_request(model_id)
        except Exception as exc:
            raise LlamaSwapProtocolError("invalid fixed llama-swap load request") from exc
        if not isinstance(request, ControlRequest):
            raise LlamaSwapProtocolError("invalid fixed llama-swap load request")
        url = f"{self.base_url}{request.path}"
        async with httpx.AsyncClient(timeout=self.load_timeout) as c:
            try:
                if request.method == "GET":
                    r = await c.get(url, params=request.params)
                else:
                    kwargs: dict[str, Any] = {"json": request.json_body}
                    if request.params is not None:
                        kwargs["params"] = request.params
                    r = await c.post(url, **kwargs)
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
            content_type = response.headers.get("content-type")
            media_type = content_type.split(";", 1)[0].strip().lower() if isinstance(content_type, str) else None
            body = response.content
            if not isinstance(body, bytes):
                raise TypeError("control response body must be bytes")
            validator(LlamaSwapResponse(response.status_code, media_type, body))
        except Exception as exc:
            raise LlamaSwapProtocolError(f"invalid fixed llama-swap {action} response") from exc
