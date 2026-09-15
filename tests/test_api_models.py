from __future__ import annotations

import pytest
from pydantic import ValidationError

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
