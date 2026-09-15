from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from model_scheduler.llama_swap_client import LlamaSwapClient, LlamaSwapError, LlamaSwapProtocolError


@pytest.mark.asyncio
async def test_running_refuses_to_guess_an_unpinned_llama_swap_schema() -> None:
    client = LlamaSwapClient("http://127.0.0.1:8080")
    with pytest.raises(LlamaSwapProtocolError, match="fixture"):
        await client.running()


@pytest.mark.asyncio
async def test_load_http_404_is_not_accepted_even_if_running_lists_the_model(monkeypatch) -> None:
    class Response:
        status_code = 404
        text = "not found"

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def get(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr("model_scheduler.llama_swap_client.httpx.AsyncClient", lambda **_kwargs: Client())
    client = LlamaSwapClient("http://127.0.0.1:8080", running_parser=lambda _: ["qwen-small"])
    client.running = AsyncMock(return_value=["qwen-small"])

    with pytest.raises(LlamaSwapError, match="load failed"):
        await client.load("qwen-small")
    client.running.assert_not_awaited()


@pytest.mark.asyncio
async def test_unload_redirect_is_not_accepted_as_stop_success(monkeypatch) -> None:
    class Response:
        status_code = 302
        text = "redirect"

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr("model_scheduler.llama_swap_client.httpx.AsyncClient", lambda **_kwargs: Client())
    client = LlamaSwapClient("http://127.0.0.1:8080")
    with pytest.raises(LlamaSwapError, match="unload failed"):
        await client.unload("qwen-small")
