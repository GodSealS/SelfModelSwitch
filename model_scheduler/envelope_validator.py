"""C06 pre-dispatch input checks shared by the compatibility API and the adapter (M05/P20).

`plan/08-execution-plan.md` (C06) and `plan/m00-envelope.md` §3 fix the rules
this module owns:

* chat/vision consume OpenAI `messages`; embeddings consume `input`; rerank
  consumes `query`/`documents`;
* only `data:image/png` and `data:image/jpeg` data URLs are accepted — a remote
  URL is refused, never fetched — and the DECODED pixel edges and the image
  count are checked against the registered envelope, so a small compressed file
  cannot smuggle a huge image past the limit;
* the token budget (input <= max_input_tokens, output <= max_output_tokens,
  input + output <= ctx_size; an image is charged `max_image_tokens` when the
  runtime cannot pin its real count) is checked BEFORE dispatch, so a refusal
  never reaches the inference path;
* an over-limit request answers 422 `envelope_exceeded` (m00-envelope §3);
* batch/document caps default conservatively here; the measured per-capability
  value is bound to a candidate (M06) and may only tighten the cap;
* every capability must have an input fixture before it can appear in a
  production candidate (`fixture_coverage`, C09).

Token counting needs the runtime's own tokenizer, so callers inject it: the
llama.cpp adapter counts through `/apply-template` + `/tokenize`, and the
compatibility API injects the same counter when the deployment provides one.
The scheduler process stays free of Torch/ORT: this module only uses the
stdlib.
"""
from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, Mapping, Protocol

ALLOWED_IMAGE_MEDIA = frozenset({"image/png", "image/jpeg"})
DEFAULT_MAX_BATCH = 256  # embeddings batch cap until a candidate binds a measured one
DEFAULT_MAX_DOCUMENTS = 256  # rerank document cap until a candidate binds a measured one

# The input shape each capability consumes (C06). A production candidate needs
# a fixture for every capability it turns on. tools/thinking (TC01/CT03) are chat
# features: they arrive through the same messages, and tools additionally carries
# the tool definitions.
CAPABILITY_INPUT_KEYS: Mapping[str, frozenset[str]] = {
    "chat": frozenset({"messages"}),
    "vision": frozenset({"messages"}),
    "embeddings": frozenset({"input"}),
    "rerank": frozenset({"query", "documents"}),
    "tools": frozenset({"messages", "tools"}),
    "thinking": frozenset({"messages"}),
}

TokenCounter = Callable[[list[Any], int], Awaitable[int]]


DEFAULT_OUTPUT_BUDGET = 4096  # what an unbudgeted request may consume at most (TC02)


class EnvelopeError(ValueError):
    """A refused input carrying a C05 code; the routes map it to a status."""

    def __init__(self, message: str, code: str = "contract_violation", param: str = "messages") -> None:
        super().__init__(message)
        self.code = code
        self.param = param


class ChatContractError(EnvelopeError):
    """A TC02/TC03 contract refusal: one code, one field path, no prose about the payload."""

    def __init__(self, code: str, param: str, message: str) -> None:
        super().__init__(message, code=code, param=param)


def _json_object(value: Any, where: str) -> dict:
    if not isinstance(value, dict):
        raise EnvelopeError(f"{where}: expected an object")
    return value


def _message_parts(content: Any) -> list[Any]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return list(content)
    raise EnvelopeError("message content must be a string or part list")


def _png_size(data: bytes) -> tuple[int, int]:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise EnvelopeError("image is not a PNG", "envelope_exceeded")
    if data[12:16] != b"IHDR":
        raise EnvelopeError("PNG missing IHDR", "envelope_exceeded")
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    if width < 1 or height < 1:
        raise EnvelopeError("PNG has invalid dimensions", "envelope_exceeded")
    return width, height


def _jpeg_size(data: bytes) -> tuple[int, int]:
    if data[:2] != b"\xff\xd8":
        raise EnvelopeError("image is not a JPEG", "envelope_exceeded")
    index = 2
    while index + 9 <= len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in {0xC0, 0xC1, 0xC2}:
            height = int.from_bytes(data[index + 5 : index + 7], "big")
            width = int.from_bytes(data[index + 7 : index + 9], "big")
            if width < 1 or height < 1:
                raise EnvelopeError("JPEG has invalid dimensions", "envelope_exceeded")
            return width, height
        if marker in {0xD8, 0xD9} or marker < 0xC0:
            index += 2
            continue
        length = int.from_bytes(data[index + 2 : index + 4], "big")
        index += 2 + length
    raise EnvelopeError("JPEG missing size marker", "envelope_exceeded")


def decode_image_data_url(url: str) -> tuple[str, bytes]:
    """Decode one data URL; a remote URL is refused here and never fetched."""
    if not url.startswith("data:") or ";base64," not in url:
        raise EnvelopeError(f"unsupported image url {url[:48]!r}")
    header, payload = url.split(";base64,", 1)
    media = header[len("data:") :].split(";", 1)[0].strip().lower()
    if media not in ALLOWED_IMAGE_MEDIA:
        raise EnvelopeError(f"unsupported image media type {media!r}", "unsupported_media_type")
    try:
        raw = base64.b64decode(payload, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise EnvelopeError("image data URL is not valid base64") from exc
    return media, raw


def image_dimensions(media: str, raw: bytes) -> tuple[int, int]:
    """Decoded pixel edges; the check runs on the bytes, never on the file size."""
    return _png_size(raw) if media == "image/png" else _jpeg_size(raw)


def collect_image_sizes(messages: Any) -> list[tuple[int, int]]:
    """Every image in an OpenAI `messages` body, decoded to its real pixel size."""
    sizes: list[tuple[int, int]] = []
    if not isinstance(messages, list) or not messages:
        raise EnvelopeError("messages must be a non-empty list")
    for message in messages:
        item = _json_object(message, "message")
        for part in _message_parts(item.get("content")):
            if not isinstance(part, dict):
                raise EnvelopeError("message part must be an object")
            kind = part.get("type")
            if kind == "text":
                continue
            if kind != "image_url":
                raise EnvelopeError(f"unsupported message part {kind!r}")
            image = _json_object(part.get("image_url"), "image_url")
            url = image.get("url")
            if not isinstance(url, str) or not url:
                raise EnvelopeError("image url is required")
            if url.startswith("http://") or url.startswith("https://") or not url.startswith("data:"):
                raise EnvelopeError("remote image url is not allowed")
            media, raw = decode_image_data_url(url)
            sizes.append(image_dimensions(media, raw))
    return sizes


def check_images(sizes: list[tuple[int, int]], *, capabilities: Iterable[str], operation: str, envelope) -> None:
    caps = frozenset(str(item) for item in capabilities)
    if sizes and "vision" not in caps:
        raise EnvelopeError("image input requires the vision capability", "capability_mismatch")
    if operation == "vision" and not sizes:
        raise EnvelopeError("vision requires an image")
    if len(sizes) > envelope.max_images:
        raise EnvelopeError("image count exceeds envelope.max_images", "envelope_exceeded")
    for width, height in sizes:
        if max(width, height) > envelope.max_image_edge_pixels:
            raise EnvelopeError("image edge exceeds envelope.max_image_edge_pixels", "envelope_exceeded")


class OutputBudgetPolicy(Protocol):
    """What `prepare_chat` needs from a runtime policy (TC01's RuntimeChatPolicy satisfies it)."""

    policy_id: str
    recognized_output_fields: frozenset[str]
    supported_output_fields: frozenset[str]


@dataclass(frozen=True)
class FixedOutputBudgetPolicy:
    """The one runtime policy CT02 knows; CT06 keys it per runtime (TC01)."""

    policy_id: str
    recognized_output_fields: frozenset[str]
    supported_output_fields: frozenset[str]


#: Recognised and supported come from CT01's probe-e material: on image
#: `4bc272f` each of the three aliases was proven to bound the output (the run
#: exhausted the budget and reported `finish_reason=length`). Until CT06 binds
#: the policy to `(profile_id, image_digest, model_sha256)`, this is the fixed
#: image's own policy and no other runtime may be assumed to share it.
DEFAULT_OUTPUT_POLICY = FixedOutputBudgetPolicy(
    policy_id="llama-cpp-4bc272f-baseline",
    recognized_output_fields=frozenset({"max_tokens", "max_completion_tokens", "n_predict"}),
    supported_output_fields=frozenset({"max_tokens", "max_completion_tokens", "n_predict"}),
)


@dataclass(frozen=True)
class PreparedChat:
    """The one body the compat route counts and dispatches (TC02).

    `body_json` is frozen bytes: nothing may change between the count that
    admitted the request and the dispatch that spends the budget, and every
    consumer gets the body by decoding these bytes.
    """

    body_json: bytes
    body_sha256: str
    model_id: str
    image_count: int
    output_field: str
    output_tokens: int
    requires_tools: bool
    requires_thinking: bool
    policy_id: str

    def decoded_body(self) -> dict:
        """The only way to obtain the dispatchable body (TC02)."""
        return json.loads(self.body_json.decode("utf-8"))


def canonical_json_bytes(document: Mapping[str, Any]) -> bytes:
    """UTF-8, sorted keys, compact separators, no NaN: one body, one digest (TC02)."""
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def normalize_output(payload: Mapping, envelope, policy: OutputBudgetPolicy) -> tuple[dict, str, int]:
    """Clip the one output-budget field a request may carry; never touch the caller's payload."""
    body = copy.deepcopy(dict(payload))
    keys = sorted(set(body) & policy.recognized_output_fields)
    if len(keys) > 1:
        raise ChatContractError("contract_violation", keys[0], "multiple output budget fields")
    field = keys[0] if keys else "max_tokens"
    if field not in policy.supported_output_fields:
        raise ChatContractError("contract_violation", field, "unsupported output budget field")
    value = body[field] if keys else min(DEFAULT_OUTPUT_BUDGET, envelope.max_output_tokens)
    if type(value) is not int or value < 1:
        raise ChatContractError("contract_violation", field, "expected a positive integer")
    if "n" in body and (type(body["n"]) is not int or body["n"] != 1):
        raise ChatContractError("contract_violation", "n", "only n=1 is supported")
    effective = min(value, envelope.max_output_tokens)
    body[field] = effective
    return body, field, effective


def _requires_tools(payload: Mapping) -> bool:
    """Projection only: TC04 owns the authoritative rule and the capability refusal."""
    if payload.get("tools"):
        return True
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return False
    return any(isinstance(message, Mapping) and (message.get("tool_calls") or message.get("role") == "tool")
               for message in messages)


def _requires_thinking(payload: Mapping) -> bool:
    if "reasoning_effort" in payload:
        return True
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return False
    return any(isinstance(message, Mapping) and message.get("reasoning_content") is not None
               for message in messages)


def prepare_chat(payload: Mapping, *, capabilities: Iterable[str], envelope,
                 policy: OutputBudgetPolicy) -> PreparedChat:
    """Prepare one compat chat/vision body: the budget is decided here or nowhere (TC02).

    Pure: no I/O, no counting, no capability refusal beyond what the shape gives.
    The caller's payload is never modified — the prepared body is a deep copy, so
    the count and the dispatch can be compared byte for byte later.
    """
    if not isinstance(payload, Mapping):
        raise EnvelopeError("chat input requires an object")
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise EnvelopeError("chat input requires a non-empty messages list")
    image_count = len(collect_image_sizes(messages))
    body, field, output_tokens = normalize_output(payload, envelope, policy)
    body_json = canonical_json_bytes(body)
    # `capabilities` is part of the contract: TC04's stage 4 turns it into the
    # capability refusal. CT02 only projects the body, so it is unused here.
    return PreparedChat(body_json=body_json,
                        body_sha256=hashlib.sha256(body_json).hexdigest(),
                        model_id=str(payload.get("model") or ""),
                        image_count=image_count,
                        output_field=field,
                        output_tokens=output_tokens,
                        requires_tools=_requires_tools(body),
                        requires_thinking=_requires_thinking(body),
                        policy_id=policy.policy_id)


def effective_max_tokens(parameters: Mapping | None, envelope) -> int:
    """The output budget the request will actually use, clipped to the envelope."""
    requested = (parameters or {}).get("max_tokens", min(DEFAULT_OUTPUT_BUDGET, envelope.max_output_tokens))
    if isinstance(requested, bool) or not isinstance(requested, int) or requested < 1:
        raise EnvelopeError("max_tokens must be a positive integer")
    return min(requested, envelope.max_output_tokens)


def check_chat_budget(input_tokens: int, max_tokens: int, envelope) -> None:
    """Every refusal carries the figures: a bare 'exceeded' cannot be diagnosed offline."""
    if input_tokens > envelope.max_input_tokens:
        raise EnvelopeError(f"input tokens exceed envelope.max_input_tokens "
                            f"({input_tokens} > {envelope.max_input_tokens})", "envelope_exceeded")
    if max_tokens > envelope.max_output_tokens:
        raise EnvelopeError(f"max_tokens exceeds envelope.max_output_tokens "
                            f"({max_tokens} > {envelope.max_output_tokens})", "envelope_exceeded")
    if input_tokens + max_tokens > envelope.ctx_size:
        raise EnvelopeError(f"input plus output exceeds ctx_size "
                            f"({input_tokens} + {max_tokens} > {envelope.ctx_size})", "envelope_exceeded")


async def check_chat_input(payload: Mapping, *, capabilities: Iterable[str], envelope,
                           token_counter: TokenCounter | None = None, operation: str | None = None) -> dict:
    """Check one chat/vision body against one registered envelope; returns its budget facts.

    Without an injected counter the format, image and output-budget checks still
    run; the token budget is checked whenever the caller can count through the
    runtime's own tokenizer (never estimated from characters).
    """
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise EnvelopeError("chat input requires a non-empty messages list")
    sizes = collect_image_sizes(messages)
    chosen = operation if operation is not None else ("vision" if sizes else "chat")
    check_images(sizes, capabilities=capabilities, operation=chosen, envelope=envelope)
    max_tokens = effective_max_tokens(payload, envelope)
    input_tokens: int | None = None
    if token_counter is not None:
        input_tokens = int(await token_counter(messages, len(sizes)))
        check_chat_budget(input_tokens, max_tokens, envelope)
    return {"image_count": len(sizes), "input_tokens": input_tokens, "max_tokens": max_tokens}


def check_embeddings_input(payload: Mapping, *, maximum: int | None = None) -> list[str]:
    """The embeddings batch, checked against the cap (422 when the cap is exceeded)."""
    raw = payload.get("input")
    inputs: Any = [raw] if isinstance(raw, str) else raw
    if not isinstance(inputs, list) or not inputs or any(not isinstance(item, str) or not item for item in inputs):
        raise EnvelopeError("embeddings input must be a non-empty string or string list")
    if any(not item.strip() for item in inputs):
        raise EnvelopeError("embeddings input must not be blank")
    limit = DEFAULT_MAX_BATCH if maximum is None else max(1, int(maximum))
    if len(inputs) > limit:
        raise EnvelopeError(f"embeddings batch exceeds the registered cap of {limit}", "envelope_exceeded")
    return list(inputs)


def check_rerank_input(payload: Mapping, *, maximum: int | None = None) -> tuple[str, list[str]]:
    """The rerank query/documents, checked against the cap (422 when the cap is exceeded)."""
    query = payload.get("query")
    documents = payload.get("documents")
    if not isinstance(query, str) or not query.strip():
        raise EnvelopeError("rerank requires a non-empty query")
    if not isinstance(documents, list) or not documents or any(not isinstance(item, str) or not item for item in documents):
        raise EnvelopeError("rerank requires a non-empty document list")
    if any(not item.strip() for item in documents):
        raise EnvelopeError("rerank documents must not be blank")
    limit = DEFAULT_MAX_DOCUMENTS if maximum is None else max(1, int(maximum))
    if len(documents) > limit:
        raise EnvelopeError(f"rerank documents exceed the registered cap of {limit}", "envelope_exceeded")
    return query, list(documents)


def fixture_coverage(*, capabilities: Iterable[str], fixtures: Mapping[str, Any]) -> tuple[str, ...]:
    """The capabilities without an input fixture: any of them blocks a production candidate (C09)."""
    return tuple(sorted(capability for capability in capabilities if capability not in fixtures))
