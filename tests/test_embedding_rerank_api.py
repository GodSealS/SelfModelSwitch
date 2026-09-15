from __future__ import annotations

from fastapi.testclient import TestClient

from app import create_app
from model_scheduler.contracts import Lease, Outcome


class Scheduler:
    def __init__(self): self.outcomes = []
    async def acquire(self, model_id, request_id, deadline): return Lease("lease", request_id, model_id, 1)
    async def release(self, lease, outcome, tokens=None): self.outcomes.append(outcome)


class Opened:
    status_code = 200
    headers = {}
    def __init__(self, data): self.data = data
    async def json(self): return self.data
    async def aclose(self): return None


class Gateway:
    async def open(self, lease, capability, payload, deadline):
        if capability.value == "embeddings":
            return Opened({"data": [{"index": 0, "embedding": [1.0, 2.0]}]})
        return Opened({"results": [{"index": 1, "relevance_score": 0.5}, {"index": 0, "relevance_score": 0.5}]})


def test_embeddings_validates_and_returns_indexed_direct_response() -> None:
    scheduler = Scheduler()
    with TestClient(create_app(scheduler=scheduler, gateway=Gateway())) as client:
        response = client.post("/v1/embeddings", json={"model": "embedding", "input": "hello"})
    assert response.status_code == 200
    assert response.json()["data"][0]["index"] == 0
    assert scheduler.outcomes == [Outcome.SUCCESS]


def test_rerank_stably_sorts_scores_and_optionally_returns_documents() -> None:
    scheduler = Scheduler()
    with TestClient(create_app(scheduler=scheduler, gateway=Gateway())) as client:
        response = client.post("/v1/rerank", json={"model": "reranker", "query": "q", "documents": ["one", "two"], "return_documents": True})
    assert response.status_code == 200
    assert response.json()["results"] == [
        {"index": 0, "relevance_score": 0.5, "document": {"text": "one"}},
        {"index": 1, "relevance_score": 0.5, "document": {"text": "two"}},
    ]
