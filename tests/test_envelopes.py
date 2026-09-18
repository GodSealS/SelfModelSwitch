"""P20: the C06 pre-dispatch input envelope, exercised through real fixtures.

Every capability the matrix turns on has an input fixture here, and the checks
are proven to CONSUME that fixture: the injected counter receives the exact
messages and image count, the image bytes are decoded to their real pixels, and
an over-limit request never reaches a lease or a gateway call.
"""
from __future__ import annotations

import base64
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
    check_chat_budget,
    check_chat_input,
    check_embeddings_input,
    check_images,
    check_rerank_input,
    collect_image_sizes,
    effective_max_tokens,
    fixture_coverage,
)

# The first candidate's measured envelope (plan/m00-envelope.md §3).
ENVELOPE = Envelope(ctx_size=32768, max_input_tokens=28672, max_output_tokens=4096, max_parallel=2,
                    max_image_tokens=1280, max_image_edge_pixels=1024, max_images=1)


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


CAPABILITY_FIXTURES: dict[str, dict] = {
    "chat": {"messages": _chat_messages()},
    "vision": {"messages": _vision_messages(_png(64, 64))},
    "embeddings": {"input": ["one", "two"]},
    "rerank": {"query": "q", "documents": ["one", "two"]},
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
    def __init__(self) -> None:
        self.leases: list[str] = []
        self.outcomes: list[Outcome] = []

    async def acquire(self, model_id, request_id, deadline):
        self.leases.append(model_id)
        return Lease("lease", request_id, model_id, 1)

    async def release(self, lease, outcome, tokens=None):
        self.outcomes.append(outcome)


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


def test_no_audio_or_video_route_exists_on_the_compatibility_surface() -> None:
    app = create_app(scheduler=None)
    paths = {getattr(route, "path", "") for route in app.routes}

    assert not any("audio" in path or "video" in path for path in paths)
