"""P20: the C06 pre-dispatch input envelope, exercised through real fixtures.

Every capability the matrix turns on has an input fixture here, and the checks
are proven to CONSUME that fixture: the injected counter receives the exact
messages and image count, the image bytes are decoded to their real pixels, and
an over-limit request never reaches a lease or a gateway call.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import struct
import zlib
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from app import create_app
from model_scheduler.contracts import Lease, Outcome
from model_scheduler.contracts_v2 import Envelope
from model_scheduler.envelope_validator import (
    CAPABILITY_INPUT_KEYS,
    DEFAULT_MAX_BATCH,
    DEFAULT_MAX_DOCUMENTS,
    EnvelopeError,
    FixedOutputBudgetPolicy,
    canonical_json_bytes,
    check_chat_budget,
    check_chat_input,
    check_embeddings_input,
    check_images,
    check_rerank_input,
    collect_image_sizes,
    effective_max_tokens,
    fixture_coverage,
    normalize_output,
    prepare_chat,
)

# The first candidate's measured envelope (plan/m00-envelope.md §3).
ENVELOPE = Envelope(ctx_size=32768, max_input_tokens=28672, max_output_tokens=4096, max_parallel=2,
                    max_image_tokens=1280, max_image_edge_pixels=1024, max_images=1)

# The tool-calling extension's public software fixture (acceptance §4).
CHAT_ENVELOPE = Envelope(ctx_size=8192, max_input_tokens=4096, max_output_tokens=1024, max_parallel=1,
                         max_image_tokens=1280, max_image_edge_pixels=1024, max_images=1)

#: Recognises every alias, supports two of them: unit tests must be able to tell
#: "recognised" from "supported" apart (acceptance §4). The effort set and the
#: denied template fields are unit-test material only (acceptance §4).
SYNTHETIC_POLICY = FixedOutputBudgetPolicy(
    policy_id="synthetic-unit-policy",
    recognized_output_fields=frozenset({"max_tokens", "max_completion_tokens", "n_predict"}),
    supported_output_fields=frozenset({"max_tokens", "max_completion_tokens"}),
    effort_values=frozenset({"none", "low"}),
    denied_template_fields=frozenset({"chat_template", "chat_template_kwargs", "reasoning_format",
                                      "parse_tool_calls", "generation_prompt"}),
)


def _png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00" * (width * 3) for _ in range(height))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def _jpeg(width: int, height: int) -> bytes:
    sof = (b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", height, width)
           + b"\x03\x01\x11\x00\x02\x11\x01\x03\x11\x01")
    return b"\xff\xd8" + sof + b"\xff\xd9"


def _data_url(media: str, raw: bytes) -> str:
    return f"data:{media};base64,{base64.b64encode(raw).decode('ascii')}"


def _chat_messages(text: str = "hello") -> list[dict]:
    return [{"role": "user", "content": text}]


def _vision_messages(raw: bytes, media: str = "image/png") -> list[dict]:
    return [{"role": "user", "content": [{"type": "text", "text": "describe"},
                                         {"type": "image_url", "image_url": {"url": _data_url(media, raw)}}]}]


def _tool_definitions() -> list[dict]:
    return [{"type": "function", "function": {"name": "get_weather",
                                              "parameters": {"type": "object", "properties": {}}}}]


CAPABILITY_FIXTURES: dict[str, dict] = {
    "chat": {"messages": _chat_messages()},
    "vision": {"messages": _vision_messages(_png(64, 64))},
    "embeddings": {"input": ["one", "two"]},
    "rerank": {"query": "q", "documents": ["one", "two"]},
    # TC01: tools/thinking are consumed through the same chat messages; tools
    # additionally carries the tool definitions.
    "tools": {"messages": _chat_messages(), "tools": _tool_definitions()},
    "thinking": {"messages": _chat_messages()},
}


def test_every_capability_has_an_input_fixture_and_none_may_be_missing() -> None:
    assert set(CAPABILITY_FIXTURES) == set(CAPABILITY_INPUT_KEYS)  # the matrix is covered by fixtures

    assert fixture_coverage(capabilities=CAPABILITY_FIXTURES, fixtures=CAPABILITY_FIXTURES) == ()
    assert fixture_coverage(capabilities=["chat", "vision"], fixtures={"chat": CAPABILITY_FIXTURES["chat"]}) == ("vision",)


@pytest.mark.asyncio
async def test_chat_tokens_at_the_boundary_pass_and_one_over_is_refused() -> None:
    seen: dict = {}

    async def counter(messages, image_count):
        seen["messages"] = messages
        seen["images"] = image_count
        return seen["value"]

    messages = _chat_messages("boundary")
    seen["value"] = ENVELOPE.max_input_tokens
    facts = await check_chat_input({"messages": messages}, capabilities={"chat"}, envelope=ENVELOPE, token_counter=counter)

    assert facts == {"image_count": 0, "input_tokens": ENVELOPE.max_input_tokens, "max_tokens": 4096}
    assert seen["messages"] == messages and seen["images"] == 0  # the fixture was really consumed

    seen["value"] = ENVELOPE.max_input_tokens + 1
    with pytest.raises(EnvelopeError) as refused:
        await check_chat_input({"messages": messages}, capabilities={"chat"}, envelope=ENVELOPE, token_counter=counter)
    assert refused.value.code == "envelope_exceeded"


def test_the_ctx_budget_covers_input_plus_output() -> None:
    check_chat_budget(ENVELOPE.max_input_tokens, 4096, ENVELOPE)  # exactly the ctx boundary

    tight = Envelope(ctx_size=1000, max_input_tokens=900, max_output_tokens=200, max_parallel=1,
                     max_image_tokens=0, max_image_edge_pixels=0, max_images=0)
    check_chat_budget(900, 100, tight)
    with pytest.raises(EnvelopeError) as refused:
        check_chat_budget(850, 200, tight)  # 1050 > ctx_size
    assert refused.value.code == "envelope_exceeded"


def test_max_tokens_is_clipped_to_the_registered_output_budget() -> None:
    assert effective_max_tokens({}, ENVELOPE) == 4096
    assert effective_max_tokens({"max_tokens": 10_000}, ENVELOPE) == ENVELOPE.max_output_tokens

    with pytest.raises(EnvelopeError):
        effective_max_tokens({"max_tokens": 0}, ENVELOPE)
    with pytest.raises(EnvelopeError):
        effective_max_tokens({"max_tokens": True}, ENVELOPE)  # a bool never poses as an integer


def test_vision_images_are_decoded_and_the_edge_and_count_limits_are_exact() -> None:
    at_edge = collect_image_sizes(_vision_messages(_png(1024, 1024)))
    assert at_edge == [(1024, 1024)]  # the decoded pixels, not the compressed size

    check_images(at_edge, capabilities={"vision"}, operation="vision", envelope=ENVELOPE)  # exactly at the edge

    over_edge = collect_image_sizes(_vision_messages(_png(1025, 1024)))
    with pytest.raises(EnvelopeError) as edge:
        check_images(over_edge, capabilities={"vision"}, operation="vision", envelope=ENVELOPE)
    assert edge.value.code == "envelope_exceeded"

    twice = at_edge + at_edge
    with pytest.raises(EnvelopeError) as count:
        check_images(twice, capabilities={"vision"}, operation="vision", envelope=ENVELOPE)  # max_images=1
    assert count.value.code == "envelope_exceeded"

    with pytest.raises(EnvelopeError) as no_capability:
        check_images(at_edge, capabilities={"chat"}, operation="chat", envelope=ENVELOPE)
    assert no_capability.value.code == "capability_mismatch"

    with pytest.raises(EnvelopeError):
        check_images([], capabilities={"vision"}, operation="vision", envelope=ENVELOPE)  # vision needs its image


def test_jpeg_edges_are_read_from_the_real_marker_not_the_file_size() -> None:
    assert collect_image_sizes(_vision_messages(_jpeg(2048, 64), media="image/jpeg")) == [(2048, 64)]

    with pytest.raises(EnvelopeError) as refused:
        check_images([(2048, 64)], capabilities={"vision"}, operation="vision", envelope=ENVELOPE)
    assert refused.value.code == "envelope_exceeded"


def test_remote_and_malformed_images_are_refused_without_any_fetch() -> None:
    remote = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://example.invalid/x.png"}}]}]
    with pytest.raises(EnvelopeError) as refused:
        collect_image_sizes(remote)
    assert "remote image url" in str(refused.value)

    unsupported = [{"role": "user", "content": [{"type": "image_url",
                                                 "image_url": {"url": _data_url("image/gif", b"GIF89a")}}]}]
    with pytest.raises(EnvelopeError) as media:
        collect_image_sizes(unsupported)
    assert media.value.code == "unsupported_media_type"

    broken = [{"role": "user", "content": [{"type": "image_url",
                                            "image_url": {"url": "data:image/png;base64,bm90LXBuZw=="}}]}]
    with pytest.raises(EnvelopeError) as malformed:
        collect_image_sizes(broken)
    assert malformed.value.code == "envelope_exceeded"  # a malformed header never becomes a huge image


def test_embeddings_batch_cap_boundary_and_the_candidate_may_only_tighten_it() -> None:
    assert len(check_embeddings_input({"input": ["x"] * DEFAULT_MAX_BATCH})) == DEFAULT_MAX_BATCH

    with pytest.raises(EnvelopeError) as refused:
        check_embeddings_input({"input": ["x"] * (DEFAULT_MAX_BATCH + 1)})
    assert refused.value.code == "envelope_exceeded"

    with pytest.raises(EnvelopeError):
        check_embeddings_input({"input": ["x", "y", "z"]}, maximum=2)  # the measured cap only tightens

    with pytest.raises(EnvelopeError):
        check_embeddings_input({"input": []})
    with pytest.raises(EnvelopeError):
        check_embeddings_input({"input": "   "})


def test_rerank_document_cap_boundary_and_the_candidate_may_only_tighten_it() -> None:
    query, documents = check_rerank_input({"query": "q", "documents": ["d"] * DEFAULT_MAX_DOCUMENTS})
    assert query == "q" and len(documents) == DEFAULT_MAX_DOCUMENTS

    with pytest.raises(EnvelopeError) as refused:
        check_rerank_input({"query": "q", "documents": ["d"] * (DEFAULT_MAX_DOCUMENTS + 1)})
    assert refused.value.code == "envelope_exceeded"

    with pytest.raises(EnvelopeError):
        check_rerank_input({"query": "q", "documents": ["a", "b"]}, maximum=1)
    with pytest.raises(EnvelopeError):
        check_rerank_input({"query": " ", "documents": ["a"]})


def _load_v2_config(tmp_path: Path):
    spec = importlib.util.spec_from_file_location("sms_v2_envelope_config", Path(__file__).resolve().parent / "test_config.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = tmp_path / "config.yaml"
    path.write_text(module.V2, encoding="utf-8")
    from model_scheduler.config import load_config

    return load_config(path)


class _Opened:
    status_code = 200
    headers = {"content-type": "application/json"}

    async def json(self):
        return {"id": "chatcmpl-1", "model": "qwen-small", "choices": []}

    async def aclose(self) -> None:
        return None


class _Scheduler:
    def __init__(self, *, warm_error: Exception | None = None) -> None:
        self.leases: list[str] = []
        self.outcomes: list[Outcome] = []
        self.warmed: list[str] = []
        self._warm_error = warm_error

    async def acquire(self, model_id, request_id, deadline):
        self.leases.append(model_id)
        return Lease("lease", request_id, model_id, 1)

    async def release(self, lease, outcome, tokens=None):
        self.outcomes.append(outcome)

    async def warm(self, model_id, deadline):
        self.warmed.append(model_id)
        if self._warm_error is not None:
            raise self._warm_error


class _Gateway:
    def __init__(self) -> None:
        self.opened = 0

    async def open(self, lease, capability, payload, deadline):
        self.opened += 1
        return _Opened()


def _compat_app(config, *, counter_value: int):
    scheduler, gateway = _Scheduler(), _Gateway()

    async def counter(model_id, messages, image_count):
        assert model_id == "qwen-small" and messages and image_count == 0  # the fixture reached the runtime counter
        return counter_value

    app = create_app(config=config, scheduler=scheduler, gateway=gateway, token_counter=counter)
    return app, scheduler, gateway


def test_the_compat_chat_route_passes_exactly_at_the_token_budget(tmp_path) -> None:
    config = _load_v2_config(tmp_path)
    boundary = config.models["qwen-small"].envelope.max_input_tokens
    app, scheduler, gateway = _compat_app(config, counter_value=boundary)

    with TestClient(app) as client:
        response = client.post("/v1/chat/completions",
                               json={"model": "qwen-small", "messages": _chat_messages()})

    assert response.status_code == 200, response.text
    assert gateway.opened == 1 and scheduler.outcomes == [Outcome.SUCCESS]


def test_one_token_over_the_budget_is_422_and_never_reaches_lease_or_gateway(tmp_path) -> None:
    config = _load_v2_config(tmp_path)
    boundary = config.models["qwen-small"].envelope.max_input_tokens
    app, scheduler, gateway = _compat_app(config, counter_value=boundary + 1)

    with TestClient(app) as client:
        response = client.post("/v1/chat/completions",
                               json={"model": "qwen-small", "messages": _chat_messages()})

    assert response.status_code == 422 and response.json()["error"]["code"] == "envelope_exceeded"
    assert scheduler.leases == [] and gateway.opened == 0  # no dispatch happened at all


def test_the_compat_chat_route_refuses_an_image_for_a_model_without_vision(tmp_path) -> None:
    config = _load_v2_config(tmp_path)
    app, scheduler, gateway = _compat_app(config, counter_value=1)

    with TestClient(app) as client:
        response = client.post("/v1/chat/completions",
                               json={"model": "qwen-small", "messages": _vision_messages(_png(64, 64))})

    assert response.status_code == 422 and response.json()["error"]["code"] == "capability_mismatch"
    assert scheduler.leases == [] and gateway.opened == 0


def test_a_cold_counter_warms_the_model_then_counts_again(tmp_path) -> None:
    """A cold model has no tokenizer, so the count can never succeed before a load."""
    config = _load_v2_config(tmp_path)
    boundary = config.models["qwen-small"].envelope.max_input_tokens
    calls = {"n": 0}

    async def counter(model_id, messages, image_count):
        calls["n"] += 1
        if calls["n"] == 1:  # the model is cold, so the runtime cannot tokenize yet
            raise RuntimeError("upstream is not loaded")
        return boundary

    scheduler, gateway = _Scheduler(), _Gateway()
    app = create_app(config=config, scheduler=scheduler, gateway=gateway, token_counter=counter)

    with TestClient(app) as client:
        response = client.post("/v1/chat/completions",
                               json={"model": "qwen-small", "messages": _chat_messages()})

    assert response.status_code == 200, response.text
    assert scheduler.warmed == ["qwen-small"]  # loading happened, and only once
    assert calls["n"] == 2 and gateway.opened == 1


def test_a_model_that_cannot_be_warmed_is_still_refused(tmp_path) -> None:
    config = _load_v2_config(tmp_path)
    scheduler = _Scheduler(warm_error=RuntimeError("the disk is gone"))
    gateway = _Gateway()

    async def counter(model_id, messages, image_count):
        raise RuntimeError("upstream is not loaded")

    app = create_app(config=config, scheduler=scheduler, gateway=gateway, token_counter=counter)

    with TestClient(app) as client:
        response = client.post("/v1/chat/completions",
                               json={"model": "qwen-small", "messages": _chat_messages()})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"
    assert scheduler.leases == [] and gateway.opened == 0  # compliance is still unproven


def test_no_audio_or_video_route_exists_on_the_compatibility_surface() -> None:
    app = create_app(scheduler=None)
    paths = {getattr(route, "path", "") for route in app.routes}

    assert not any("audio" in path or "video" in path for path in paths)


# --------------------------------------------------------------------------- TC02: PreparedChat


def test_a01_the_prepared_body_is_one_canonical_byte_string() -> None:
    payload = {"model": "qwen-small", "messages": _chat_messages(), "max_tokens": 64,
               "extra": {"b": 1, "a": "ü"}}
    prepared = prepare_chat(payload, capabilities={"chat"}, envelope=CHAT_ENVELOPE, policy=SYNTHETIC_POLICY)

    assert prepared.output_field == "max_tokens" and prepared.output_tokens == 64
    assert prepared.image_count == 0 and prepared.model_id == "qwen-small"
    assert prepared.policy_id == SYNTHETIC_POLICY.policy_id
    assert prepared.body_sha256 == hashlib.sha256(prepared.body_json).hexdigest()
    # sorted keys, compact separators, real UTF-8: one body, one digest (TC02)
    assert prepared.body_json == (
        b'{"extra":{"a":"\xc3\xbc","b":1},"max_tokens":64,'
        b'"messages":[{"content":"hello","role":"user"}],"model":"qwen-small"}'
    )
    assert prepared.decoded_body()["max_tokens"] == 64


def test_a01_prepare_chat_leaves_the_caller_payload_untouched() -> None:
    payload = {"model": "qwen-small", "messages": _chat_messages("hello"), "max_tokens": 999_999}
    prepared = prepare_chat(payload, capabilities={"chat"}, envelope=CHAT_ENVELOPE, policy=SYNTHETIC_POLICY)

    assert payload["max_tokens"] == 999_999  # the request the caller sent is never rewritten
    assert prepared.decoded_body()["max_tokens"] == CHAT_ENVELOPE.max_output_tokens
    prepared.decoded_body()["messages"].append({"role": "user", "content": "injected"})
    assert payload["messages"] == _chat_messages("hello")  # decoding gives a private copy too


def test_a01_an_unbudgeted_request_uses_the_envelope_output_cap() -> None:
    body, field, tokens = normalize_output({"model": "m", "messages": _chat_messages()}, CHAT_ENVELOPE,
                                           SYNTHETIC_POLICY)
    assert field == "max_tokens" and tokens == 1024 and body["max_tokens"] == 1024


def test_a02_a_recognised_but_unsupported_alias_is_refused_not_ignored() -> None:
    with pytest.raises(EnvelopeError) as refused:
        normalize_output({"model": "m", "messages": [], "n_predict": 64}, CHAT_ENVELOPE, SYNTHETIC_POLICY)
    assert refused.value.code == "contract_violation" and refused.value.param == "n_predict"


def test_a02_two_budget_fields_are_refused_even_when_they_agree() -> None:
    with pytest.raises(EnvelopeError) as refused:
        normalize_output({"model": "m", "messages": [], "max_tokens": 64, "max_completion_tokens": 64},
                         CHAT_ENVELOPE, SYNTHETIC_POLICY)
    assert refused.value.param == "max_completion_tokens"  # the first sorted alias names the conflict


def test_a02_n_may_be_one_or_absent_and_nothing_else() -> None:
    body, _, _ = normalize_output({"model": "m", "messages": [], "n": 1}, CHAT_ENVELOPE, SYNTHETIC_POLICY)
    assert body["n"] == 1
    for value in (0, 2, True, None, "1"):
        with pytest.raises(EnvelopeError) as refused:
            normalize_output({"model": "m", "messages": [], "n": value}, CHAT_ENVELOPE, SYNTHETIC_POLICY)
        assert refused.value.param == "n"


def test_a04_the_capability_demand_is_authoritative_in_prepare_chat() -> None:
    # CT04 supersedes the CT02 projection: the same pure step now also refuses a
    # model that does not carry the capability the request needs.
    tools = {"model": "m", "messages": _chat_messages(), "tools": [_tool()]}
    history = {"model": "m", "messages": _exchange(_call())}
    thinking = {"model": "m", "messages": [{"role": "assistant", "content": "42", "reasoning_content": "why"}]}

    for body in (tools, history):
        assert _prepare(body).requires_tools
        with pytest.raises(EnvelopeError) as refused:
            _prepare(body, capabilities=("chat",))
        assert refused.value.code == "capability_mismatch"
    assert not _prepare(_chat_body()).requires_tools
    assert _prepare(thinking).requires_thinking


def _chat_body() -> dict:
    return {"model": "qwen-small", "messages": _chat_messages()}


# --------------------------------------------------------------------------- TC03/TC04: the request contract (A03/A04/A06-local)

NEW_CAPABILITIES = ("chat", "tools", "thinking")


def _tool(name: str = "get_weather", *, description=None, parameters=None, strict=None) -> dict:
    function: dict = {"name": name}
    if description is not None:
        function["description"] = description
    if parameters is not None:
        function["parameters"] = parameters
    if strict is not None:
        function["strict"] = strict
    return {"type": "function", "function": function}


def _call(call_id: str = "call-1", name: str = "get_weather", arguments: str = "{}") -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def _exchange(*calls: dict, assistant: dict | None = None, results: bool = True) -> list[dict]:
    """One user turn, one assistant turn carrying the calls, and one result each."""
    assistant_turn = {"role": "assistant", "tool_calls": list(calls)}
    if assistant is not None:
        assistant_turn.update(assistant)
    messages = [_chat_messages()[0], assistant_turn]
    if results:
        messages += [{"role": "tool", "tool_call_id": call.get("id", "fallback-id"), "content": "{}"}
                     for call in calls]
    return messages


def _prepare(body: dict, *, capabilities=NEW_CAPABILITIES, policy=None):
    prepared_body = {"model": "m", **body}
    return prepare_chat(prepared_body, capabilities=frozenset(capabilities),
                        envelope=CHAT_ENVELOPE, policy=SYNTHETIC_POLICY if policy is None else policy)


def _sized_tool(prefix: str, target_bytes: int) -> dict:
    """One tool whose canonical JSON is exactly `target_bytes` (descriptions pad it)."""
    tool = _tool(f"{prefix}-tool", description="")
    overhead = len(canonical_json_bytes(tool))
    assert target_bytes >= overhead
    tool["function"]["description"] = "x" * (target_bytes - overhead)
    assert len(canonical_json_bytes(tool)) == target_bytes
    return tool


def _tools_of_canonical_bytes(total: int, count: int = 9) -> list[dict]:
    """`count` tools whose whole array is exactly `total` canonical bytes.

    The padding is spread over every tool so the per-object budget stays out of
    the way: only the array budget under test may react.
    """
    base = 128
    residual = total - (2 + (count - 1) + count * base)  # brackets, commas, base sizes
    assert residual >= count
    share, extra = divmod(residual, count)
    tools = [_sized_tool(f"tool-{index}", base + share + (extra if index == count - 1 else 0))
             for index in range(count)]
    assert len(canonical_json_bytes(tools)) == total
    return tools


@pytest.mark.parametrize("name", ["a", "get_weather-2", "A" * 64, "with_underscore"])
def test_a03_tool_names_of_the_right_shape_pass(name) -> None:
    assert _prepare({"messages": _chat_messages(), "tools": [_tool(name)]}).requires_tools


@pytest.mark.parametrize("name", ["A" * 65, "get.weather", "get_weather\n", "", "天气"])
def test_a03_bad_tool_names_are_refused(name) -> None:
    with pytest.raises(EnvelopeError) as refused:
        _prepare({"messages": _chat_messages(), "tools": [_tool(name)]})
    assert refused.value.code == "contract_violation"
    assert refused.value.param == "tools[0].function.name"


def test_a03_tools_null_is_refused_and_an_empty_array_is_allowed() -> None:
    with pytest.raises(EnvelopeError) as refused:
        _prepare({"messages": _chat_messages(), "tools": None})
    assert refused.value.code == "contract_violation" and refused.value.param == "tools"

    assert not _prepare({"messages": _chat_messages(), "tools": []}).requires_tools


def test_a03_a_tool_is_exactly_type_and_function() -> None:
    for tool in ({"function": _tool()["function"]}, {"type": "function"},
                 {"type": "function", "function": {"name": "f"}, "extra": 1},
                 {"type": "other", "function": {"name": "f"}}):
        with pytest.raises(EnvelopeError) as refused:
            _prepare({"messages": _chat_messages(), "tools": [tool]})
        assert refused.value.code == "contract_violation"


def test_a03_function_optional_fields_may_be_omitted_but_never_null() -> None:
    _prepare({"messages": _chat_messages(), "tools": [_tool()]})  # name only
    for function in ({"name": "f", "description": None}, {"name": "f", "parameters": None}):
        with pytest.raises(EnvelopeError) as refused:
            _prepare({"messages": _chat_messages(), "tools": [{"type": "function", "function": function}]})
        assert refused.value.param.endswith(("description", "parameters"))
    _prepare({"messages": _chat_messages(), "tools": [_tool(description="", parameters={})]})


def test_a03_strict_defaults_to_false_and_true_is_refused() -> None:
    _prepare({"messages": _chat_messages(), "tools": [_tool()]})
    _prepare({"messages": _chat_messages(), "tools": [_tool(strict=False)]})
    for value in (True, None, "false", 1):
        tool = {"type": "function", "function": {"name": "f", "strict": value}}
        with pytest.raises(EnvelopeError) as refused:
            _prepare({"messages": _chat_messages(), "tools": [tool]})
        assert refused.value.param == "tools[0].function.strict"


def test_a03_duplicate_tool_names_are_case_sensitively_refused() -> None:
    with pytest.raises(EnvelopeError) as refused:
        _prepare({"messages": _chat_messages(), "tools": [_tool("get_weather"), _tool("get_weather")]})
    assert refused.value.param == "tools"
    _prepare({"messages": _chat_messages(), "tools": [_tool("get_weather"), _tool("Get_weather")]})


def test_a03_tool_count_object_bytes_and_array_bytes_are_bounded() -> None:
    _prepare({"messages": _chat_messages(), "tools": [_tool(f"t{index}") for index in range(32)]})
    with pytest.raises(EnvelopeError) as refused:
        _prepare({"messages": _chat_messages(), "tools": [_tool(f"t{index}") for index in range(33)]})
    assert refused.value.code == "envelope_exceeded" and refused.value.param == "tools"

    _prepare({"messages": _chat_messages(), "tools": [_sized_tool("max", 8192)]})
    with pytest.raises(EnvelopeError) as refused:
        _prepare({"messages": _chat_messages(), "tools": [_sized_tool("over", 8193)]})
    assert refused.value.code == "envelope_exceeded" and refused.value.param == "tools[0]"

    _prepare({"messages": _chat_messages(), "tools": _tools_of_canonical_bytes(65536)})
    with pytest.raises(EnvelopeError) as refused:
        _prepare({"messages": _chat_messages(), "tools": _tools_of_canonical_bytes(65537)})
    assert refused.value.code == "envelope_exceeded" and refused.value.param == "tools"


@pytest.mark.parametrize("choice", ["auto", "none", "required"])
def test_a03_string_tool_choice_values_pass(choice) -> None:
    _prepare({"messages": _chat_messages(), "tools": [_tool()], "tool_choice": choice})


def test_a03_tool_choice_shape_matrix() -> None:
    for choice in ("", "any", 1, None, True):
        with pytest.raises(EnvelopeError) as refused:
            _prepare({"messages": _chat_messages(), "tools": [_tool()], "tool_choice": choice})
        assert refused.value.code == "contract_violation"
        assert refused.value.param.startswith("tool_choice")

    _prepare({"messages": _chat_messages(), "tools": [_tool()],
              "tool_choice": {"type": "function", "function": {"name": "get_weather"}}})
    for bad in ({"type": "function"}, {"type": "function", "function": {}},
                {"type": "function", "function": {"name": "get_weather", "extra": 1}},
                {"type": "function", "function": {"name": ""}},
                {"type": "other", "function": {"name": "get_weather"}}):
        with pytest.raises(EnvelopeError) as refused:
            _prepare({"messages": _chat_messages(), "tools": [_tool()], "tool_choice": bad})
        assert refused.value.code == "contract_violation"


def test_a03_parallel_tool_calls_only_false_is_accepted() -> None:
    _prepare({"messages": _chat_messages()})
    _prepare({"messages": _chat_messages(), "tools": [_tool()], "parallel_tool_calls": False})
    for value in (True, None, 0, "false"):
        with pytest.raises(EnvelopeError) as refused:
            _prepare({"messages": _chat_messages(), "tools": [_tool()], "parallel_tool_calls": value})
        assert refused.value.param == "parallel_tool_calls"


def test_a03_reasoning_effort_must_be_a_non_empty_string() -> None:
    _prepare({"messages": _chat_messages(), "reasoning_effort": "low"})
    for value in ("", None, 1, True):
        with pytest.raises(EnvelopeError) as refused:
            _prepare({"messages": _chat_messages(), "reasoning_effort": value})
        assert refused.value.param == "reasoning_effort"


@pytest.mark.parametrize("assistant", [{}, {"content": None}, {"content": ""}, {"content": "42"},
                                       {"content": [{"type": "text", "text": "hi"}]}])
def test_a04_assistant_content_is_free_when_it_carries_calls(assistant) -> None:
    prepared = _prepare({"messages": _exchange(_call(), assistant=assistant)})
    assert prepared.requires_tools


@pytest.mark.parametrize("value", [None, []])
def test_a04_tool_calls_null_or_empty_are_refused(value) -> None:
    with pytest.raises(EnvelopeError) as refused:
        _prepare({"messages": [{"role": "assistant", "content": "hi", "tool_calls": value}]})
    assert refused.value.code == "contract_violation"
    assert refused.value.param == "messages[0].tool_calls"


def test_a04_call_items_are_closed_and_typed() -> None:
    for call in ({"id": "c", "type": "function"},
                 {"id": "c", "type": "function", "function": {"name": "f"}},
                 {"id": "c", "type": "other", "function": {"name": "f", "arguments": "{}"}},
                 {"id": "", "type": "function", "function": {"name": "f", "arguments": "{}"}},
                 {"id": "c", "type": "function", "function": {"name": "f", "arguments": "{}"}, "extra": 1},
                 {"type": "function", "function": {"name": "f", "arguments": "{}"}}):
        with pytest.raises(EnvelopeError) as refused:
            _prepare({"messages": _exchange(call)})
        assert refused.value.code == "contract_violation"


@pytest.mark.parametrize("arguments", [None, {}, 1, ["{}"]])
def test_a04_arguments_must_be_a_string(arguments) -> None:
    with pytest.raises(EnvelopeError) as refused:
        _prepare({"messages": _exchange(_call(arguments=arguments))})
    assert refused.value.param == "messages[1].tool_calls[0].function.arguments"


def test_a04_id_and_tool_call_id_byte_limits() -> None:
    _prepare({"messages": _exchange(_call("a" * 64))})
    with pytest.raises(EnvelopeError) as refused:
        _prepare({"messages": _exchange(_call("a" * 65))})
    assert refused.value.code == "envelope_exceeded"
    assert refused.value.param == "messages[1].tool_calls[0].id"

    messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "tool_calls": [_call("c-1")]},
                {"role": "tool", "tool_call_id": "b" * 65, "content": "{}"}]
    with pytest.raises(EnvelopeError) as refused:
        _prepare({"messages": messages})
    assert refused.value.code == "envelope_exceeded" and refused.value.param == "messages[2].tool_call_id"


@pytest.mark.parametrize("bad", ["a\x00b", "a\ud800b"])
def test_a04_ids_must_be_symbolic_text(bad) -> None:
    with pytest.raises(EnvelopeError) as refused:
        _prepare({"messages": _exchange(_call(bad))})
    assert refused.value.code == "contract_violation"
    assert refused.value.param == "messages[1].tool_calls[0].id"


def test_a04_history_arguments_total_is_bounded() -> None:
    _prepare({"messages": _exchange(_call(arguments="x" * 262_144))})
    with pytest.raises(EnvelopeError) as refused:
        _prepare({"messages": _exchange(_call(arguments="x" * 262_145))})
    assert refused.value.code == "envelope_exceeded" and refused.value.param == "messages"


def test_a04_calls_per_assistant_are_bounded() -> None:
    _prepare({"messages": _exchange(*[_call(f"call-{index}") for index in range(32)])})
    with pytest.raises(EnvelopeError) as refused:
        _prepare({"messages": _exchange(*[_call(f"call-{index}") for index in range(33)])})
    assert refused.value.code == "envelope_exceeded" and refused.value.param == "messages[1].tool_calls"


def test_a04_only_assistant_carries_calls_and_only_tool_carries_results() -> None:
    with pytest.raises(EnvelopeError) as refused:
        _prepare({"messages": [{"role": "user", "content": "hi", "tool_calls": [_call()]}]})
    assert refused.value.param == "messages[0].tool_calls"
    with pytest.raises(EnvelopeError) as refused:
        _prepare({"messages": [{"role": "user", "content": "hi", "tool_call_id": "c"}]})
    assert refused.value.param == "messages[0].tool_call_id"


def test_a04_tool_content_must_be_a_string() -> None:
    _prepare({"messages": _exchange(_call())})
    messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "tool_calls": [_call()]},
                {"role": "tool", "tool_call_id": "call-1"}]
    with pytest.raises(EnvelopeError) as refused:
        _prepare({"messages": messages})
    assert refused.value.param == "messages[2].content"


@pytest.mark.parametrize("value", [None, "", "why"])
def test_a04_null_placeholder_and_empty_string_reasoning_are_kept(value) -> None:
    prepared = _prepare({"messages": [{"role": "assistant", "content": "hi", "reasoning_content": value}]})
    decoded = prepared.decoded_body()["messages"][0]["reasoning_content"]
    assert decoded == value and (decoded is None) == (value is None)


def test_a04_reasoning_content_is_assistant_only() -> None:
    for message in ({"role": "user", "content": "hi", "reasoning_content": None},
                    {"role": "user", "content": "hi", "reasoning_content": "why"}):
        with pytest.raises(EnvelopeError) as refused:
            _prepare({"messages": [message]})
        assert refused.value.param == "messages[0].reasoning_content"


def test_a04_plain_null_assistant_content_is_still_refused() -> None:
    with pytest.raises(EnvelopeError):
        _prepare({"messages": [{"role": "assistant", "content": None}]})


def test_a04_out_of_order_results_and_serial_batches_pass() -> None:
    messages = [{"role": "user", "content": "hi"},
                {"role": "assistant", "tool_calls": [_call("c-1"), _call("c-2", name="other")]},
                {"role": "tool", "tool_call_id": "c-2", "content": "{}"},
                {"role": "tool", "tool_call_id": "c-1", "content": "{}"},
                {"role": "assistant", "content": "done"}]
    prepared = _prepare({"messages": messages})
    assert prepared.requires_tools  # the history still demands the capability

    serial = [{"role": "user", "content": "hi"},
              {"role": "assistant", "tool_calls": [_call("c-1")]},
              {"role": "tool", "tool_call_id": "c-1", "content": "{}"},
              {"role": "assistant", "tool_calls": [_call("c-2")]},
              {"role": "tool", "tool_call_id": "c-2", "content": "{}"}]
    _prepare({"messages": serial})  # a new batch after a closed one is legal


def test_a04_orphan_duplicate_and_incomplete_results_are_refused() -> None:
    orphan = [{"role": "user", "content": "hi"},
              {"role": "tool", "tool_call_id": "c-1", "content": "{}"}]
    with pytest.raises(EnvelopeError) as refused:
        _prepare({"messages": orphan})
    assert refused.value.param == "messages[1].tool_call_id"

    duplicate = [{"role": "user", "content": "hi"},
                 {"role": "assistant", "tool_calls": [_call("c-1")]},
                 {"role": "tool", "tool_call_id": "c-1", "content": "{}"},
                 {"role": "tool", "tool_call_id": "c-1", "content": "{}"}]
    with pytest.raises(EnvelopeError) as refused:
        _prepare({"messages": duplicate})
    assert refused.value.param == "messages[3].tool_call_id"

    incomplete = [{"role": "user", "content": "hi"},
                  {"role": "assistant", "tool_calls": [_call("c-1"), _call("c-2")]},
                  {"role": "tool", "tool_call_id": "c-1", "content": "{}"},
                  {"role": "user", "content": "again"}]
    with pytest.raises(EnvelopeError) as refused:
        _prepare({"messages": incomplete})
    assert refused.value.param == "messages[3].role"

    trailing = [{"role": "user", "content": "hi"}, {"role": "assistant", "tool_calls": [_call("c-1")]}]
    with pytest.raises(EnvelopeError) as refused:
        _prepare({"messages": trailing})
    assert refused.value.param == "messages"

    duplicated_call_id = [{"role": "user", "content": "hi"},
                          {"role": "assistant", "tool_calls": [_call("c-1")]},
                          {"role": "tool", "tool_call_id": "c-1", "content": "{}"},
                          {"role": "assistant", "tool_calls": [_call("c-1")]}]
    with pytest.raises(EnvelopeError) as refused:
        _prepare({"messages": duplicated_call_id})
    assert refused.value.param == "messages[3].tool_calls"


def test_a04_history_without_chat_capability_is_refused() -> None:
    with pytest.raises(EnvelopeError) as refused:
        _prepare({"messages": _exchange(_call())}, capabilities=("vision",))
    assert refused.value.code == "capability_mismatch" and refused.value.param == "tools"


def test_a04_history_calls_are_checked_without_current_tools() -> None:
    prepared = _prepare({"messages": _exchange(_call())})  # no top-level tools at all
    assert prepared.requires_tools


def test_a06_local_explicit_effort_and_reasoning_require_thinking() -> None:
    with pytest.raises(EnvelopeError) as refused:
        _prepare({"messages": _chat_messages(), "reasoning_effort": "low"}, capabilities=("chat",))
    assert refused.value.code == "capability_mismatch" and refused.value.param == "reasoning_effort"

    history = [{"role": "assistant", "content": "hi", "reasoning_content": "why"}]
    with pytest.raises(EnvelopeError) as refused:
        _prepare({"messages": history}, capabilities=("chat",))
    assert refused.value.code == "capability_mismatch" and refused.value.param == "messages"

    placeholder = [{"role": "assistant", "content": "hi", "reasoning_content": None}]
    assert not _prepare({"messages": placeholder}, capabilities=("chat",)).requires_thinking

    empty = [{"role": "assistant", "content": "hi", "reasoning_content": ""}]
    assert _prepare({"messages": empty}).requires_thinking


def test_a06_local_an_unlisted_effort_value_is_refused() -> None:
    with pytest.raises(EnvelopeError) as refused:
        _prepare({"messages": _chat_messages(), "reasoning_effort": "high"})
    assert refused.value.code == "contract_violation" and refused.value.param == "reasoning_effort"
    _prepare({"messages": _chat_messages(), "reasoning_effort": "none"})


def test_a06_local_new_capability_models_refuse_template_overrides() -> None:
    for field in sorted(SYNTHETIC_POLICY.denied_template_fields):
        with pytest.raises(EnvelopeError) as refused:
            _prepare({"messages": _chat_messages(), field: None})
        assert refused.value.code == "contract_violation" and refused.value.param == field

    # A model without tools/thinking keeps the historical passthrough.
    for field in sorted(SYNTHETIC_POLICY.denied_template_fields):
        _prepare({"messages": _chat_messages(), field: "value"}, capabilities=("chat", "vision"))


def test_a06_local_tool_choice_association_matrix() -> None:
    tools = [_tool()]
    _prepare({"messages": _chat_messages(), "tools": tools})
    _prepare({"messages": _chat_messages(), "tools": tools, "tool_choice": "auto"})
    _prepare({"messages": _chat_messages(), "tools": tools, "tool_choice": "required"})
    _prepare({"messages": _chat_messages(), "tools": tools,
              "tool_choice": {"type": "function", "function": {"name": "get_weather"}}})
    _prepare({"messages": _chat_messages(), "tools": [], "tool_choice": "none"})
    _prepare({"messages": _chat_messages(), "parallel_tool_calls": False})

    for body in ({"tools": [], "tool_choice": "auto"},
                 {"tools": [], "tool_choice": "required"},
                 {"tools": [], "tool_choice": {"type": "function", "function": {"name": "get_weather"}}},
                 {"tools": tools, "tool_choice": {"type": "function", "function": {"name": "missing"}}}):
        with pytest.raises(EnvelopeError) as refused:
            _prepare({"messages": _chat_messages(), **body})
        assert refused.value.code == "contract_violation"
        assert refused.value.param.startswith("tool_choice")
