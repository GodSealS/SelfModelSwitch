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
import re
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

# TC03/TC04: the compat chat request contract. Shapes are refused with
# `contract_violation`; their byte and count budgets with `envelope_exceeded`.
MAX_TOOLS = 32
MAX_TOOL_OBJECT_BYTES = 8_192
MAX_TOOLS_ARRAY_BYTES = 65_536
MAX_CALLS_PER_ASSISTANT = 32
MAX_HISTORY_ARGUMENTS_BYTES = 262_144
MAX_CALL_ID_BYTES = 64
TOOL_CHOICE_STRINGS = frozenset({"auto", "none", "required"})
TOOL_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
#: Capabilities that change the request contract itself: their models refuse
#: every template override and are the only ones that consult the effort set.
_NEW_MODEL_CAPABILITIES = frozenset({"tools", "thinking"})


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
        content = item.get("content")
        # TC04: an assistant turn that carries tool calls may leave its content
        # out (absent or null); such a turn contributes no parts to scan.
        if content is None and item.get("role") == "assistant" and item.get("tool_calls"):
            continue
        for part in _message_parts(content):
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
    effort_values: frozenset[str]
    denied_template_fields: frozenset[str]


@dataclass(frozen=True)
class FixedOutputBudgetPolicy:
    """The one runtime policy CT02/CT04 know; CT06 keys it per runtime (TC01)."""

    policy_id: str
    recognized_output_fields: frozenset[str]
    supported_output_fields: frozenset[str]
    effort_values: frozenset[str] = frozenset()
    denied_template_fields: frozenset[str] = frozenset()


#: Recognised and supported come from CT01's probe-e material: on image
#: `4bc272f` each of the three aliases was proven to bound the output (the run
#: exhausted the budget and reported `finish_reason=length`). Until CT06 binds
#: the policy to `(profile_id, image_digest, model_sha256)`, this is the fixed
#: image's own policy and no other runtime may be assumed to share it.
#:
#: `effort_values` is CT01's candidate empty list: D06 did not run, so the
#: allowed values are NOT established. The empty set is the fail-closed software
#: default — an explicit effort is refused, never forwarded — and it is not a
#: claim about the image; CT06 must register the probed set before serving a
#: thinking model. `denied_template_fields` is CT01's D09 material: every
#: template/parse override the fixed image forwards.
DEFAULT_OUTPUT_POLICY = FixedOutputBudgetPolicy(
    policy_id="llama-cpp-4bc272f-baseline",
    recognized_output_fields=frozenset({"max_tokens", "max_completion_tokens", "n_predict"}),
    supported_output_fields=frozenset({"max_tokens", "max_completion_tokens", "n_predict"}),
    effort_values=frozenset(),
    denied_template_fields=frozenset(
        {"chat_template", "chat_template_kwargs", "generation_prompt", "parse_tool_calls", "reasoning_format"}
    ),
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


# ---------------------------------------------------------------------------
# TC03/TC04: the request contract
#
# The stage order is fixed (contracts.md TC03): one output budget and n (TC02),
# then the new field shapes, then their byte/count budgets, then what the whole
# request needs of the model (capability demand and the template-override
# refusal), and last the cross-field rules and the tool-history state machine.
# Within one array the index decides; across fields the documented order does.


def _contract(param: str, message: str) -> ChatContractError:
    return ChatContractError("contract_violation", param, message)


def _limit(param: str, message: str) -> ChatContractError:
    return ChatContractError("envelope_exceeded", param, message)


def _object(value: Any, param: str) -> dict:
    if not isinstance(value, dict):
        raise _contract(param, "expected an object")
    return value


def _call_id(value: Any, param: str) -> str:
    if not isinstance(value, str) or not value:
        raise _contract(param, "expected a non-empty string")
    if "\x00" in value:
        raise _contract(param, "must not contain NUL")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise _contract(param, "must be valid UTF-8") from exc
    return value


_TOOL_KEYS = frozenset({"type", "function"})
_TOOL_FUNCTION_KEYS = frozenset({"name", "description", "parameters", "strict"})
_TOOL_CALL_KEYS = frozenset({"id", "type", "function"})
_TOOL_CALL_FUNCTION_KEYS = frozenset({"name", "arguments"})


def _validate_tools_shape(body: Mapping) -> list[Any] | None:
    """Stage 2: the tools array and every tool/function object in it."""
    if "tools" not in body:
        return None
    raw = body["tools"]
    if not isinstance(raw, list):
        raise _contract("tools", "expected an array")
    names: set[str] = set()
    for index, tool in enumerate(raw):
        where = f"tools[{index}]"
        item = _object(tool, where)
        unknown = sorted(set(item) - _TOOL_KEYS)
        if unknown:
            raise _contract(where, f"unknown fields: {', '.join(unknown)}")
        if set(item) != _TOOL_KEYS:
            raise _contract(where, "type and function are required")
        if item["type"] != "function":
            raise _contract(f"{where}.type", "only function tools are supported")
        function = _object(item["function"], f"{where}.function")
        unknown = sorted(set(function) - _TOOL_FUNCTION_KEYS)
        if unknown:
            raise _contract(f"{where}.function", f"unknown fields: {', '.join(unknown)}")
        name = function.get("name")
        if not isinstance(name, str) or not TOOL_NAME_RE.fullmatch(name):
            raise _contract(f"{where}.function.name", "expected 1..64 of [A-Za-z0-9_-]")
        if name in names:
            raise _contract("tools", f"duplicate tool name {name!r}")
        names.add(name)
        if "description" in function and not isinstance(function["description"], str):
            raise _contract(f"{where}.function.description", "expected a string")
        if "parameters" in function and not isinstance(function["parameters"], dict):
            raise _contract(f"{where}.function.parameters", "expected an object")
        if "strict" in function:
            strict = function["strict"]
            if isinstance(strict, bool) and strict:
                raise _contract(f"{where}.function.strict", "strict tools are not supported")
            if not isinstance(strict, bool):
                raise _contract(f"{where}.function.strict", "expected a boolean")
    return raw


def _validate_tool_choice_shape(body: Mapping) -> None:
    """Stage 2: structure only; the association with tools is a stage-5 rule."""
    if "tool_choice" not in body:
        return
    choice = body["tool_choice"]
    if isinstance(choice, str):
        if choice not in TOOL_CHOICE_STRINGS:
            raise _contract("tool_choice", "expected auto, none, required or a function object")
        return
    item = _object(choice, "tool_choice")
    if set(item) != {"type", "function"}:
        raise _contract("tool_choice", "a named choice has exactly type and function")
    if item["type"] != "function":
        raise _contract("tool_choice.type", "only function choices are supported")
    function = _object(item["function"], "tool_choice.function")
    if set(function) != {"name"}:
        raise _contract("tool_choice.function", "a named choice has exactly name")
    name = function["name"]
    if not isinstance(name, str) or not name:
        raise _contract("tool_choice.function.name", "expected a non-empty string")


def _validate_parallel_tool_calls_shape(body: Mapping) -> None:
    if "parallel_tool_calls" in body and body["parallel_tool_calls"] is not False:
        raise _contract("parallel_tool_calls", "only false is supported")


def _validate_reasoning_effort_shape(body: Mapping) -> None:
    if "reasoning_effort" not in body:
        return
    value = body["reasoning_effort"]
    if not isinstance(value, str) or not value:
        raise _contract("reasoning_effort", "expected a non-empty string")


def _validate_message_shapes(messages: list) -> None:
    """Stage 2: the new per-message fields and their pairing with the role."""
    for index, message in enumerate(messages):
        where = f"messages[{index}]"
        item = _object(message, where)
        role = item.get("role")
        if "tool_calls" in item:
            if role != "assistant":
                raise _contract(f"{where}.tool_calls", "tool_calls is only allowed on assistant messages")
            calls = item["tool_calls"]
            if not isinstance(calls, list) or not calls:
                raise _contract(f"{where}.tool_calls", "expected a non-empty array")
            for call_index, call in enumerate(calls):
                call_where = f"{where}.tool_calls[{call_index}]"
                entry = _object(call, call_where)
                unknown = sorted(set(entry) - _TOOL_CALL_KEYS)
                if unknown:
                    raise _contract(call_where, f"unknown fields: {', '.join(unknown)}")
                if set(entry) != _TOOL_CALL_KEYS:
                    raise _contract(call_where, "id, type and function are required")
                if entry["type"] != "function":
                    raise _contract(f"{call_where}.type", "only function calls are supported")
                _call_id(entry["id"], f"{call_where}.id")
                function = _object(entry["function"], f"{call_where}.function")
                if set(function) != _TOOL_CALL_FUNCTION_KEYS:
                    raise _contract(f"{call_where}.function", "name and arguments are required")
                if not isinstance(function["name"], str) or not function["name"]:
                    raise _contract(f"{call_where}.function.name", "expected a non-empty string")
                if not isinstance(function["arguments"], str):
                    raise _contract(f"{call_where}.function.arguments", "expected a string")
        if "tool_call_id" in item:
            if role != "tool":
                raise _contract(f"{where}.tool_call_id", "tool_call_id is only allowed on tool messages")
            _call_id(item["tool_call_id"], f"{where}.tool_call_id")
        if role == "tool" and not isinstance(item.get("content"), str):
            raise _contract(f"{where}.content", "tool content must be a string")
        if "reasoning_content" in item:
            if role != "assistant":
                raise _contract(f"{where}.reasoning_content",
                                "reasoning_content is only allowed on assistant messages")
            value = item["reasoning_content"]
            if value is not None and not isinstance(value, str):
                raise _contract(f"{where}.reasoning_content", "expected a string or null")


def _check_tools_limits(tools: list[Any] | None) -> None:
    """Stage 3: the tools count and the two byte budgets, on the canonical JSON."""
    if tools is None:
        return
    if len(tools) > MAX_TOOLS:
        raise _limit("tools", f"at most {MAX_TOOLS} tools")
    for index, tool in enumerate(tools):
        if len(canonical_json_bytes(tool)) > MAX_TOOL_OBJECT_BYTES:
            raise _limit(f"tools[{index}]", f"a tool must fit in {MAX_TOOL_OBJECT_BYTES} bytes")
    if len(canonical_json_bytes(tools)) > MAX_TOOLS_ARRAY_BYTES:
        raise _limit("tools", f"the tools array must fit in {MAX_TOOLS_ARRAY_BYTES} bytes")


def _check_message_limits(messages: list) -> None:
    """Stage 3: per-assistant call counts, the argument total and call-id bytes."""
    for index, item in enumerate(messages):
        calls = item.get("tool_calls")
        if calls and len(calls) > MAX_CALLS_PER_ASSISTANT:
            raise _limit(f"messages[{index}].tool_calls", f"at most {MAX_CALLS_PER_ASSISTANT} calls")
    total = 0
    for item in messages:
        for call in item.get("tool_calls") or []:
            total += len(call["function"]["arguments"].encode("utf-8"))
    if total > MAX_HISTORY_ARGUMENTS_BYTES:
        raise _limit("messages", f"history arguments must fit in {MAX_HISTORY_ARGUMENTS_BYTES} bytes")
    for index, item in enumerate(messages):
        for call_index, call in enumerate(item.get("tool_calls") or []):
            if len(call["id"].encode("utf-8")) > MAX_CALL_ID_BYTES:
                raise _limit(f"messages[{index}].tool_calls[{call_index}].id",
                             f"a call id must fit in {MAX_CALL_ID_BYTES} bytes")
        if "tool_call_id" in item and len(item["tool_call_id"].encode("utf-8")) > MAX_CALL_ID_BYTES:
            raise _limit(f"messages[{index}].tool_call_id",
                         f"a call id must fit in {MAX_CALL_ID_BYTES} bytes")


def _capability_demand(body: Mapping, messages: list) -> tuple[bool, bool]:
    """What the whole request needs of the model (TC03 stage 4)."""
    tools = body.get("tools")
    choice = body.get("tool_choice")
    requires_tools = bool(tools) or isinstance(choice, dict) or choice == "required" or any(
        item.get("tool_calls") or item.get("role") == "tool" for item in messages)
    requires_thinking = "reasoning_effort" in body or any(
        item.get("role") == "assistant" and item.get("reasoning_content") is not None for item in messages)
    return requires_tools, requires_thinking


def _check_capability_demand(requires_tools: bool, requires_thinking: bool, body: Mapping,
                             capabilities: Iterable[str]) -> None:
    caps = frozenset(str(item) for item in capabilities)
    if requires_tools and "tools" not in caps:
        raise EnvelopeError("this request requires the tools capability", "capability_mismatch", "tools")
    if requires_thinking and "thinking" not in caps:
        param = "reasoning_effort" if "reasoning_effort" in body else "messages"
        raise EnvelopeError("this request requires the thinking capability", "capability_mismatch", param)


def _check_denied_template_fields(body: Mapping, capabilities: Iterable[str],
                                  policy: OutputBudgetPolicy) -> None:
    """A model that turns on tools/thinking refuses every template override (TC03)."""
    if not (frozenset(str(item) for item in capabilities) & _NEW_MODEL_CAPABILITIES):
        return
    for field in sorted(set(body) & policy.denied_template_fields):
        raise _contract(field, f"{field} must not be overridden on a tools/thinking model")


def _check_effort_values(body: Mapping, capabilities: Iterable[str], policy: OutputBudgetPolicy) -> None:
    if "reasoning_effort" not in body:
        return
    if "thinking" not in frozenset(str(item) for item in capabilities):
        return  # stage 4 already refused this request
    value = body["reasoning_effort"]
    if value not in policy.effort_values:
        raise _contract("reasoning_effort", f"unsupported reasoning_effort {value!r}")


def _check_tool_choice_association(body: Mapping, tools: list[Any] | None) -> None:
    """Stage 5: a choice may only reference the tools this request carries."""
    choice = body.get("tool_choice")
    names = {tool["function"]["name"] for tool in tools} if tools else set()
    if names:
        if choice is None or (isinstance(choice, str) and choice in TOOL_CHOICE_STRINGS):
            return
        if isinstance(choice, dict):
            if choice["function"]["name"] not in names:
                raise _contract("tool_choice.function.name", "the named function is not in tools")
            return
        raise _contract("tool_choice", "unsupported tool choice")  # shapes are closed; defensive
    if choice is None or choice == "none":
        return
    raise _contract("tool_choice", "this tool_choice needs a non-empty tools array")


def validate_tool_history(messages: list) -> None:
    """TC04: the submitted history must pair every call with exactly one result.

    Results may arrive in any order inside their batch, but a batch is never
    interrupted and a call is never left open at the end.
    """
    seen_ids: set[str] = set()
    pending: dict[str, str] = {}
    for index, item in enumerate(messages):
        where = f"messages[{index}]"
        role = item.get("role")
        if role == "tool":
            call_id = item["tool_call_id"]
            if call_id not in pending:
                raise _contract(f"{where}.tool_call_id", "orphan or duplicate result")
            del pending[call_id]
            continue
        if pending:
            raise _contract(f"{where}.role", "tool results are incomplete")
        for call in item.get("tool_calls") or []:
            if call["id"] in seen_ids:
                raise _contract(f"{where}.tool_calls", "duplicate call id")
            seen_ids.add(call["id"])
            pending[call["id"]] = call["function"]["name"]
    if pending:
        raise _contract("messages", "tool results are incomplete")


def prepare_chat(payload: Mapping, *, capabilities: Iterable[str], envelope,
                 policy: OutputBudgetPolicy) -> PreparedChat:
    """Prepare one compat chat/vision body: every local rule is decided here (TC02/TC03/TC04).

    Pure: no I/O, no counting. The caller's payload is never modified — the
    prepared body is a deep copy, so the count and the dispatch can be compared
    byte for byte later, and a local refusal has made no runtime call at all.
    """
    if not isinstance(payload, Mapping):
        raise EnvelopeError("chat input requires an object")
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise EnvelopeError("chat input requires a non-empty messages list")
    # Stage 1 (TC02): the one output budget and n.
    body, field, output_tokens = normalize_output(payload, envelope, policy)
    body_messages = body["messages"]
    # Stage 2 (TC03): the new field shapes.
    tools = _validate_tools_shape(body)
    _validate_tool_choice_shape(body)
    _validate_parallel_tool_calls_shape(body)
    _validate_reasoning_effort_shape(body)
    _validate_message_shapes(body_messages)
    # Stage 3 (TC03): their byte and count budgets.
    _check_tools_limits(tools)
    _check_message_limits(body_messages)
    # Stage 4 (TC03): what the request needs, then the template-override refusal.
    requires_tools, requires_thinking = _capability_demand(body, body_messages)
    _check_capability_demand(requires_tools, requires_thinking, body, capabilities)
    _check_denied_template_fields(body, capabilities, policy)
    # Stage 5 (TC03/TC04): effort values, the tools association, the history and
    # the existing image rules.
    _check_effort_values(body, capabilities, policy)
    _check_tool_choice_association(body, tools)
    validate_tool_history(body_messages)
    sizes = collect_image_sizes(body_messages)
    check_images(sizes, capabilities=capabilities, envelope=envelope,
                 operation="vision" if sizes else "chat")
    body_json = canonical_json_bytes(body)
    return PreparedChat(body_json=body_json,
                        body_sha256=hashlib.sha256(body_json).hexdigest(),
                        model_id=str(payload.get("model") or ""),
                        image_count=len(sizes),
                        output_field=field,
                        output_tokens=output_tokens,
                        requires_tools=requires_tools,
                        requires_thinking=requires_thinking,
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
