from __future__ import annotations

from fastapi.testclient import TestClient

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


class Gateway:
    async def open(self, lease, capability, payload, deadline):
        assert capability.value == "chat"
        assert payload["model"] == "qwen-small"
        return Opened()


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
