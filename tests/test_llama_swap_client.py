from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from model_scheduler.llama_swap_client import ControlRequest, LlamaSwapClient, LlamaSwapControlContract, LlamaSwapError, LlamaSwapProtocolError


def contract() -> LlamaSwapControlContract:
    return LlamaSwapControlContract(
        running_parser=lambda _: ["qwen-small"],
        load_request=lambda model_id: ControlRequest("GET", "/props", params={"model": model_id}),
        unload_path="/api/models/unload/{model_id}",
        validate_load_response=lambda _: None,
        validate_unload_response=lambda _: None,
    )


def test_control_contract_rejects_paths_with_unbound_template_fields() -> None:
    with pytest.raises(ValueError, match="unload path"):
        LlamaSwapControlContract(
            running_parser=lambda _: [],
            load_request=lambda model_id: ControlRequest("GET", "/props", params={"model": model_id}),
            unload_path="/api/{deployment}/unload/{model_id}",
            validate_load_response=lambda _: None,
            validate_unload_response=lambda _: None,
        )


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
    client = LlamaSwapClient("http://127.0.0.1:8080", contract=contract())
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
    client = LlamaSwapClient("http://127.0.0.1:8080", contract=contract())
    with pytest.raises(LlamaSwapError, match="unload failed"):
        await client.unload("qwen-small")


@pytest.mark.asyncio
async def test_control_calls_refuse_to_send_without_a_complete_fixed_contract(monkeypatch) -> None:
    called = False

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def get(self, *_args, **_kwargs):
            nonlocal called
            called = True
            raise AssertionError("unverified control request was sent")

        async def post(self, *_args, **_kwargs):
            nonlocal called
            called = True
            raise AssertionError("unverified control request was sent")

    monkeypatch.setattr("model_scheduler.llama_swap_client.httpx.AsyncClient", lambda **_kwargs: Client())
    client = LlamaSwapClient("http://127.0.0.1:8080")

    with pytest.raises(LlamaSwapProtocolError, match="fixed llama-swap control contract"):
        await client.load("qwen-small")
    with pytest.raises(LlamaSwapProtocolError, match="fixed llama-swap control contract"):
        await client.unload("qwen-small")
    assert called is False


@pytest.mark.asyncio
async def test_successful_control_status_without_the_pinned_response_shape_is_a_protocol_error(monkeypatch) -> None:
    class Response:
        status_code = 200
        text = "{}"
        content = b"{}"
        headers = {"content-type": "application/json"}

        @staticmethod
        def json():
            return {}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def get(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr("model_scheduler.llama_swap_client.httpx.AsyncClient", lambda **_kwargs: Client())
    strict_contract = LlamaSwapControlContract(
        running_parser=lambda _: ["qwen-small"],
        load_request=lambda model_id: ControlRequest("GET", "/props", params={"model": model_id}),
        unload_path="/api/models/unload/{model_id}",
        validate_load_response=lambda response: response.json()["accepted"],
        validate_unload_response=lambda _: None,
    )

    with pytest.raises(LlamaSwapProtocolError, match="invalid fixed llama-swap load response"):
        await LlamaSwapClient("http://127.0.0.1:8080", contract=strict_contract).load("qwen-small")


@pytest.mark.asyncio
async def test_contract_can_validate_a_pinned_non_json_unload_success_body(monkeypatch) -> None:
    class Response:
        status_code = 200
        text = "OK"
        content = b"OK"
        headers = {"content-type": "text/plain; charset=utf-8"}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def post(self, *_args, **_kwargs):
            return Response()

    def validate_ok(response):
        if response.content_type != "text/plain" or response.body != b"OK":
            raise ValueError("unexpected unload response")

    monkeypatch.setattr("model_scheduler.llama_swap_client.httpx.AsyncClient", lambda **_kwargs: Client())
    text_contract = LlamaSwapControlContract(
        running_parser=lambda _: [],
        load_request=lambda model_id: ControlRequest("GET", "/props", params={"model": model_id}),
        unload_path="/api/models/unload/{model_id}",
        validate_load_response=lambda _: None,
        validate_unload_response=validate_ok,
    )

    await LlamaSwapClient("http://127.0.0.1:8080", contract=text_contract).unload("qwen-small")


@pytest.mark.asyncio
async def test_contract_can_express_a_fixture_derived_post_load_request(monkeypatch) -> None:
    seen = {}

    class Response:
        status_code = 200
        text = "{}"
        content = b"{}"
        headers = {"content-type": "application/json"}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def post(self, url, **kwargs):
            seen["url"] = url
            seen.update(kwargs)
            return Response()

    monkeypatch.setattr("model_scheduler.llama_swap_client.httpx.AsyncClient", lambda **_kwargs: Client())
    post_contract = LlamaSwapControlContract(
        running_parser=lambda _: ["embedding"],
        load_request=lambda model_id: ControlRequest("POST", "/v1/embeddings", json_body={"model": model_id, "input": "fixture-warmup"}),
        unload_path="/api/models/unload/{model_id}",
        validate_load_response=lambda _: None,
        validate_unload_response=lambda _: None,
    )
    client = LlamaSwapClient("http://127.0.0.1:8080", contract=post_contract)
    client.running = AsyncMock(return_value=["embedding"])

    await client.load("embedding")

    assert seen == {
        "url": "http://127.0.0.1:8080/v1/embeddings",
        "json": {"model": "embedding", "input": "fixture-warmup"},
    }
