from __future__ import annotations

import base64
import struct

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


def test_embeddings_convert_finite_float_vectors_to_little_endian_base64() -> None:
    class RecordingGateway(Gateway):
        async def open(self, lease, capability, payload, deadline):
            assert payload["encoding_format"] == "float"
            return await super().open(lease, capability, payload, deadline)

    scheduler = Scheduler()
    with TestClient(create_app(scheduler=scheduler, gateway=RecordingGateway())) as client:
        response = client.post("/v1/embeddings", json={"model": "embedding", "input": "hello", "encoding_format": "base64"})
    embedding = base64.b64decode(response.json()["data"][0]["embedding"])
    assert response.status_code == 200
    assert struct.unpack("<2f", embedding) == (1.0, 2.0)
    assert scheduler.outcomes == [Outcome.SUCCESS]


def test_embeddings_reject_vectors_with_inconsistent_dimensions() -> None:
    class BadDimensionsGateway:
        async def open(self, lease, capability, payload, deadline):
            return Opened({"data": [{"index": 0, "embedding": [1.0]}, {"index": 1, "embedding": [2.0, 3.0]}]})

    scheduler = Scheduler()
    with TestClient(create_app(scheduler=scheduler, gateway=BadDimensionsGateway())) as client:
        response = client.post("/v1/embeddings", json={"model": "embedding", "input": ["one", "two"]})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_protocol_error"
    assert scheduler.outcomes == [Outcome.ABORTED]


def test_rerank_stably_sorts_scores_and_optionally_returns_documents() -> None:
    scheduler = Scheduler()
    with TestClient(create_app(scheduler=scheduler, gateway=Gateway())) as client:
        response = client.post("/v1/rerank", json={"model": "reranker", "query": "q", "documents": ["one", "two"], "return_documents": True})
    assert response.status_code == 200
    assert response.json()["results"] == [
        {"index": 0, "relevance_score": 0.5, "document": {"text": "one"}},
        {"index": 1, "relevance_score": 0.5, "document": {"text": "two"}},
    ]


def test_rerank_rejects_duplicate_or_non_integer_upstream_indexes() -> None:
    class InvalidIndexGateway:
        async def open(self, lease, capability, payload, deadline):
            return Opened({"results": [{"index": 0, "relevance_score": 0.5}, {"index": True, "relevance_score": 0.4}]})

    scheduler = Scheduler()
    with TestClient(create_app(scheduler=scheduler, gateway=InvalidIndexGateway())) as client:
        response = client.post("/v1/rerank", json={"model": "reranker", "query": "q", "documents": ["one", "two"]})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_protocol_error"
    assert scheduler.outcomes == [Outcome.ABORTED]
