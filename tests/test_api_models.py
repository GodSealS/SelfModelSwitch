from __future__ import annotations

import pytest
from pydantic import ValidationError

from app import create_app
from model_scheduler.api_models import EmbeddingRequest, RerankRequest


def test_embedding_rejects_coerced_values_and_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        EmbeddingRequest.model_validate({"model": "embedding", "input": 1})
    with pytest.raises(ValidationError):
        EmbeddingRequest.model_validate({"model": "embedding", "input": "ok", "dimensions": 10})


def test_rerank_rejects_explicit_null_top_n_but_keeps_default() -> None:
    request = RerankRequest.model_validate({"model": "reranker", "query": "q", "documents": ["a"]})
    assert request.top_n is None
    with pytest.raises(ValidationError):
        RerankRequest.model_validate({"model": "reranker", "query": "q", "documents": ["a"], "top_n": None})


def test_openapi_includes_the_strict_pydantic_body_contracts_for_all_inference_routes() -> None:
    schema = create_app().openapi()
    expected_fields = {
        "/v1/chat/completions": {"model", "messages", "stream"},
        "/v1/embeddings": {"model", "input", "encoding_format"},
        "/v1/rerank": {"model", "query", "documents", "top_n", "return_documents"},
    }
    for path, fields in expected_fields.items():
        body_schema = schema["paths"][path]["post"]["requestBody"]["content"]["application/json"]["schema"]
        assert fields <= set(body_schema["properties"])
