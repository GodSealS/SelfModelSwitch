from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from app import create_app
from model_scheduler.contracts import Lease, Outcome


class Scheduler:
    def __init__(self):
        self.releases: list[Outcome] = []

    async def acquire(self, model_id: str, request_id: str, deadline: float) -> Lease:
        return Lease("lease", request_id, model_id, 1)

    async def release(self, lease: Lease, outcome: Outcome, tokens=None) -> None:
        self.releases.append(outcome)


class Opened:
    status_code = 200
    headers = {"content-type": "application/json"}

    async def json(self):
        return {"id": "chatcmpl-1", "model": "qwen-small", "choices": []}

    async def aclose(self):
        return None


class StreamOpened(Opened):
    def iter_bytes(self):
        async def iterator():
            yield b"data: first\n\n"
            yield b"data: [DONE]\n\n"
        return iterator()


class Gateway:
    async def open(self, lease, capability, payload, deadline):
        assert capability.value == "chat"
        assert payload["model"] == "qwen-small"
        return StreamOpened() if payload.get("stream") else Opened()


class MissingDoneGateway:
    async def open(self, lease, capability, payload, deadline):
        class MissingDone(StreamOpened):
            def iter_bytes(self):
                async def iterator():
                    yield b"data: partial\n\n"
                return iterator()
        return MissingDone()


class TextContainingDoneGateway:
    async def open(self, lease, capability, payload, deadline):
        class TextContainingDone(StreamOpened):
            def iter_bytes(self):
                async def iterator():
                    yield b'data: {"content":"data: [DONE]"}\n\n'
                return iterator()
        return TextContainingDone()


class BrokenCloseGateway:
    async def open(self, lease, capability, payload, deadline):
        class BrokenClose(StreamOpened):
            async def aclose(self):
                raise RuntimeError("close failed")
        return BrokenClose()


def test_chat_acquires_and_releases_lease_after_valid_direct_response() -> None:
    scheduler = Scheduler()
    app = create_app(scheduler=scheduler, gateway=Gateway())
    with TestClient(app) as client:
        response = client.post("/v1/chat/completions", json={"model": "qwen-small", "messages": [{"role": "user", "content": "hello"}]})

    assert response.status_code == 200
    assert response.json()["id"] == "chatcmpl-1"
    assert scheduler.releases == [Outcome.SUCCESS]
    assert response.headers["x-request-id"]


def test_chat_rejects_capability_before_acquiring_lease() -> None:
    scheduler = Scheduler()
    app = create_app(scheduler=scheduler, gateway=Gateway())
    with TestClient(app) as client:
        response = client.post("/v1/chat/completions", json={"model": "embedding", "messages": []})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_capability"
    assert scheduler.releases == []


def test_stream_response_owns_lease_until_done_event() -> None:
    scheduler = Scheduler()
    app = create_app(scheduler=scheduler, gateway=Gateway())
    with TestClient(app) as client:
        response = client.post("/v1/chat/completions", json={"model": "qwen-small", "messages": [], "stream": True})

    assert response.status_code == 200
    assert b"[DONE]" in response.content
    assert scheduler.releases == [Outcome.SUCCESS]


def test_stream_eof_without_done_aborts_lease() -> None:
    scheduler = Scheduler()
    with TestClient(create_app(scheduler=scheduler, gateway=MissingDoneGateway())) as client:
        response = client.post("/v1/chat/completions", json={"model": "qwen-small", "messages": [], "stream": True})
    assert response.status_code == 200
    assert scheduler.releases == [Outcome.ABORTED]


def test_done_text_inside_an_sse_payload_does_not_complete_lease() -> None:
    scheduler = Scheduler()
    with TestClient(create_app(scheduler=scheduler, gateway=TextContainingDoneGateway())) as client:
        response = client.post("/v1/chat/completions", json={"model": "qwen-small", "messages": [], "stream": True})
    assert response.status_code == 200
    assert scheduler.releases == [Outcome.ABORTED]


def test_stream_close_failure_still_releases_lease() -> None:
    scheduler = Scheduler()
    with TestClient(create_app(scheduler=scheduler, gateway=BrokenCloseGateway())) as client:
        with pytest.raises(RuntimeError, match="close failed"):
            client.post("/v1/chat/completions", json={"model": "qwen-small", "messages": [], "stream": True})
    assert scheduler.releases == [Outcome.SUCCESS]
