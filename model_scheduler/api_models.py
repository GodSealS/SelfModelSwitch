"""Strict HTTP DTOs; route code applies model capability and size limits."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, StrictStr, field_validator


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    model: StrictStr
    messages: list[dict[str, Any]]
    stream: StrictBool = False


class EmbeddingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    model: StrictStr
    input: StrictStr | list[StrictStr]
    encoding_format: Literal["float", "base64"] = "float"


class RerankRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    model: StrictStr
    query: StrictStr
    documents: list[StrictStr]
    top_n: StrictInt | None = None
    return_documents: StrictBool = False

    @field_validator("top_n", mode="before")
    @classmethod
    def reject_explicit_null(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("top_n must be an integer when supplied")
        return value
