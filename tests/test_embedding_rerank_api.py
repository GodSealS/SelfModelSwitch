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
    headers = {"content-type": "application/json"}
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


def test_json_routes_reject_a_non_json_upstream_success_before_parsing_it() -> None:
    class WrongContentTypeGateway:
        async def open(self, lease, capability, payload, deadline):
            class WrongContentType(Opened):
                headers = {"content-type": "text/plain"}
            return WrongContentType({"data": [{"index": 0, "embedding": [1.0]}]})

    scheduler = Scheduler()
    with TestClient(create_app(scheduler=scheduler, gateway=WrongContentTypeGateway())) as client:
        response = client.post("/v1/embeddings", json={"model": "embedding", "input": "hello"})
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


def test_capability_mismatch_on_the_json_routes_is_422() -> None:
    scheduler = Scheduler()
    with TestClient(create_app(scheduler=scheduler, gateway=Gateway())) as client:
        embeddings = client.post("/v1/embeddings", json={"model": "qwen-small", "input": "hello"})
        rerank = client.post("/v1/rerank", json={"model": "qwen-small", "query": "q", "documents": ["one"]})

    assert embeddings.status_code == 422  # plan/03-api.md §1: capability mismatch is 422
    assert embeddings.json()["error"]["code"] == "unsupported_capability"
    assert rerank.status_code == 422 and rerank.json()["error"]["code"] == "unsupported_capability"
    assert scheduler.outcomes == []  # refused before any lease


def test_an_over_cap_batch_and_document_list_are_422_envelope_exceeded() -> None:
    scheduler = Scheduler()
    with TestClient(create_app(scheduler=scheduler, gateway=Gateway())) as client:
        batch = client.post("/v1/embeddings", json={"model": "embedding", "input": ["x"] * 257})
        documents = client.post("/v1/rerank", json={"model": "reranker", "query": "q", "documents": ["d"] * 257})

    # plan/m00-envelope.md §3: any over-limit input is refused with 422 before dispatch
    assert batch.status_code == 422 and batch.json()["error"]["code"] == "envelope_exceeded"
    assert documents.status_code == 422 and documents.json()["error"]["code"] == "envelope_exceeded"
    assert scheduler.outcomes == []  # refused before any lease
