from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from app import BodyError, _read_json, create_app
from model_scheduler.contracts import GatewayError, Lease, Outcome
from model_scheduler.scheduler import QueueFull


class Scheduler:
    def __init__(self):
        self.releases: list[Outcome] = []

    async def acquire(self, model_id: str, request_id: str, deadline: float) -> Lease:
        return Lease("lease", request_id, model_id, 1)

    async def release(self, lease: Lease, outcome: Outcome, tokens=None) -> None:
        self.releases.append(outcome)


class Opened:
    status_code = 200
    headers = {"content-type": "application/json"}

    async def json(self):
        return {"id": "chatcmpl-1", "model": "qwen-small", "choices": []}

    async def aclose(self):
        return None


class StreamOpened(Opened):
    headers = {"content-type": "text/event-stream"}

    def iter_bytes(self):
        async def iterator():
            yield b"data: first\n\n"
            yield b"data: [DONE]\n\n"
        return iterator()


class Gateway:
    async def open(self, lease, capability, payload, deadline):
        assert capability.value == "chat"
        assert payload["model"] == "qwen-small"
        return StreamOpened() if payload.get("stream") else Opened()


class MissingDoneGateway:
    async def open(self, lease, capability, payload, deadline):
        class MissingDone(StreamOpened):
            def iter_bytes(self):
                async def iterator():
                    yield b"data: partial\n\n"
                return iterator()
        return MissingDone()


class TextContainingDoneGateway:
    async def open(self, lease, capability, payload, deadline):
        class TextContainingDone(StreamOpened):
            def iter_bytes(self):
                async def iterator():
                    yield b'data: {"content":"data: [DONE]"}\n\n'
                return iterator()
        return TextContainingDone()


class BrokenCloseGateway:
    async def open(self, lease, capability, payload, deadline):
        class BrokenClose(StreamOpened):
            async def aclose(self):
                raise RuntimeError("close failed")
        return BrokenClose()


class InvalidChatSuccessGateway:
    async def open(self, lease, capability, payload, deadline):
        class Invalid(Opened):
            async def json(self):
                return {"model": "qwen-small", "choices": "not-a-list"}
        return Invalid()


class InvalidSSEGateway:
    async def open(self, lease, capability, payload, deadline):
        class Invalid(StreamOpened):
            headers = {"content-type": "application/json"}
        return Invalid()


class OversizedEventGateway:
    async def open(self, lease, capability, payload, deadline):
        class OversizedEvent(StreamOpened):
            def iter_bytes(self):
                async def iterator():
                    yield b"x" * (1024 * 1024 + 1)
                return iterator()
        return OversizedEvent()


class CombinedEventsGateway:
    async def open(self, lease, capability, payload, deadline):
        class CombinedEvents(StreamOpened):
            def iter_bytes(self):
                async def iterator():
                    event = b"data: " + b"x" * 600_000 + b"\n\n"
                    yield event + event + b"data: [DONE]\n\n"
                return iterator()
        return CombinedEvents()


def test_chat_acquires_and_releases_lease_after_valid_direct_response() -> None:
    scheduler = Scheduler()
    app = create_app(scheduler=scheduler, gateway=Gateway())
    with TestClient(app) as client:
        response = client.post("/v1/chat/completions", json={"model": "qwen-small", "messages": [{"role": "user", "content": "hello"}]})

    assert response.status_code == 200
    assert response.json()["id"] == "chatcmpl-1"
    assert scheduler.releases == [Outcome.SUCCESS]
    assert response.headers["x-request-id"]


def test_chat_rejects_capability_with_422_before_acquiring_lease() -> None:
    scheduler = Scheduler()
    app = create_app(scheduler=scheduler, gateway=Gateway())
    with TestClient(app) as client:
        response = client.post("/v1/chat/completions", json={"model": "embedding", "messages": []})

    assert response.status_code == 422  # plan/03-api.md §1: capability mismatch is 422
    assert response.json()["error"]["code"] == "unsupported_capability"
    assert scheduler.releases == []


def test_chat_rejects_invalid_upstream_success_shapes_before_returning_them() -> None:
    non_stream_scheduler = Scheduler()
    stream_scheduler = Scheduler()
    with TestClient(create_app(scheduler=non_stream_scheduler, gateway=InvalidChatSuccessGateway())) as client:
        non_stream = client.post("/v1/chat/completions", json={"model": "qwen-small", "messages": []})
    with TestClient(create_app(scheduler=stream_scheduler, gateway=InvalidSSEGateway())) as client:
        stream = client.post("/v1/chat/completions", json={"model": "qwen-small", "messages": [], "stream": True})
    assert non_stream.status_code == 502
    assert stream.status_code == 502
    assert non_stream.json()["error"]["code"] == stream.json()["error"]["code"] == "upstream_protocol_error"
    assert non_stream_scheduler.releases == [Outcome.ABORTED]
    assert stream_scheduler.releases == [Outcome.ABORTED]


def test_stream_response_owns_lease_until_done_event() -> None:
    scheduler = Scheduler()
    app = create_app(scheduler=scheduler, gateway=Gateway())
    with TestClient(app) as client:
        response = client.post("/v1/chat/completions", json={"model": "qwen-small", "messages": [], "stream": True})

    assert response.status_code == 200
    assert b"[DONE]" in response.content
    assert scheduler.releases == [Outcome.SUCCESS]


def test_stream_eof_without_done_aborts_lease() -> None:
    scheduler = Scheduler()
    with TestClient(create_app(scheduler=scheduler, gateway=MissingDoneGateway())) as client:
        response = client.post("/v1/chat/completions", json={"model": "qwen-small", "messages": [], "stream": True})
    assert response.status_code == 200
    assert scheduler.releases == [Outcome.ABORTED]


def test_done_text_inside_an_sse_payload_does_not_complete_lease() -> None:
    scheduler = Scheduler()
    with TestClient(create_app(scheduler=scheduler, gateway=TextContainingDoneGateway())) as client:
        response = client.post("/v1/chat/completions", json={"model": "qwen-small", "messages": [], "stream": True})
    assert response.status_code == 200
    assert scheduler.releases == [Outcome.ABORTED]


def test_stream_close_failure_still_releases_lease() -> None:
    scheduler = Scheduler()
    with TestClient(create_app(scheduler=scheduler, gateway=BrokenCloseGateway())) as client:
        with pytest.raises(RuntimeError, match="close failed"):
            client.post("/v1/chat/completions", json={"model": "qwen-small", "messages": [], "stream": True})
    assert scheduler.releases == [Outcome.SUCCESS]


def test_stream_aborts_an_oversized_unterminated_sse_event_without_forwarding_it() -> None:
    scheduler = Scheduler()
    with TestClient(create_app(scheduler=scheduler, gateway=OversizedEventGateway())) as client:
        response = client.post("/v1/chat/completions", json={"model": "qwen-small", "messages": [], "stream": True})
    assert response.status_code == 200
    assert response.content == b""
    assert scheduler.releases == [Outcome.ABORTED]


def test_stream_allows_multiple_valid_sse_events_combined_in_one_large_chunk() -> None:
    scheduler = Scheduler()
    with TestClient(create_app(scheduler=scheduler, gateway=CombinedEventsGateway())) as client:
        response = client.post("/v1/chat/completions", json={"model": "qwen-small", "messages": [], "stream": True})
    assert response.status_code == 200
    assert response.content.endswith(b"data: [DONE]\n\n")
    assert scheduler.releases == [Outcome.SUCCESS]


def test_chat_rejects_non_json_and_oversized_bodies_before_admission() -> None:
    scheduler = Scheduler()
    with TestClient(create_app(scheduler=scheduler, gateway=Gateway())) as client:
        wrong_type = client.post("/v1/chat/completions", content="not json", headers={"content-type": "text/plain"})
        oversized = client.post("/v1/chat/completions", content=b"x" * (4 * 1024 * 1024 + 1), headers={"content-type": "application/json"})
    assert wrong_type.status_code == 415
    assert oversized.status_code == 413
    assert scheduler.releases == []


@pytest.mark.parametrize(
    "payload",
    [
        b'{"model":"qwen-small","model":"qwen-large","messages":[]}',
        b'{"model":"qwen-small","messages":[],"temperature":NaN}',
        b'{"model":"qwen-small","messages":[],"temperature":Infinity}',
    ],
)
def test_chat_rejects_duplicate_and_non_finite_json_values_before_admission(payload: bytes) -> None:
    scheduler = Scheduler()
    with TestClient(create_app(scheduler=scheduler, gateway=Gateway())) as client:
        response = client.post("/v1/chat/completions", content=payload, headers={"content-type": "application/json"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_json"
    assert response.headers["x-request-id"] == response.json()["request_id"]
    assert scheduler.releases == []


def test_chat_maps_queue_full_and_queue_deadline_to_distinct_public_errors() -> None:
    class FullScheduler(Scheduler):
        async def acquire(self, model_id, request_id, deadline):
            raise QueueFull("queue_full")

    class TimedOutScheduler(Scheduler):
        async def acquire(self, model_id, request_id, deadline):
            raise TimeoutError("queue deadline elapsed")

    with TestClient(create_app(scheduler=FullScheduler(), gateway=Gateway())) as client:
        full = client.post("/v1/chat/completions", json={"model": "qwen-small", "messages": []})
    with TestClient(create_app(scheduler=TimedOutScheduler(), gateway=Gateway())) as client:
        timed_out = client.post("/v1/chat/completions", json={"model": "qwen-small", "messages": []})
    assert full.status_code == 429
    assert full.headers["retry-after"] == "1"
    assert full.json()["error"]["code"] == "queue_full"
    assert timed_out.status_code == 504
    assert timed_out.json()["error"]["code"] == "queue_timeout"


def test_chat_preserves_a_valid_upstream_retry_after_header() -> None:
    class RateLimitedGateway:
        async def open(self, *_):
            raise GatewayError(429, "upstream_rate_limited", Outcome.REJECTED, retry_after=7)

    with TestClient(create_app(scheduler=Scheduler(), gateway=RateLimitedGateway())) as client:
        response = client.post("/v1/chat/completions", json={"model": "qwen-small", "messages": []})
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "upstream_rate_limited"
    assert response.headers["retry-after"] == "7"


@pytest.mark.asyncio
async def test_json_body_limit_stops_reading_after_an_oversized_chunk_without_content_length() -> None:
    messages = [
        {"type": "http.request", "body": b'{"payload":"too-large"', "more_body": True},
        {"type": "http.request", "body": b"}", "more_body": False},
    ]
    calls = 0

    async def receive():
        nonlocal calls
        calls += 1
        return messages.pop(0)

    scope = {"type": "http", "method": "POST", "path": "/", "headers": [(b"content-type", b"application/json")]}
    from starlette.requests import Request

    with pytest.raises(BodyError) as error:
        await _read_json(Request(scope, receive), max_bytes=8, timeout_seconds=1)
    assert error.value.code == "request_too_large"
    assert calls == 1


def _load_v2_config(tmp_path: Path):
    """The shared v2 test registration, loaded through the same YAML the config tests pin."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("sms_v2_config", Path(__file__).resolve().parent / "test_config.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = tmp_path / "config.yaml"
    path.write_text(module.V2, encoding="utf-8")
    from model_scheduler.config import load_config

    return load_config(path)


def test_a_v2_registration_drives_the_same_chat_surface(tmp_path) -> None:
    config = _load_v2_config(tmp_path)
    scheduler = Scheduler()
    app = create_app(config=config, scheduler=scheduler, gateway=Gateway())

    with TestClient(app) as client:
        unknown = client.post("/v1/chat/completions", json={"model": "missing", "messages": []})
        mismatch = client.post("/v1/chat/completions", json={"model": "embedding", "messages": []})
        accepted = client.post("/v1/chat/completions", json={"model": "qwen-small", "messages": []})

    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "model_not_found"
    assert mismatch.status_code == 422 and mismatch.json()["error"]["code"] == "unsupported_capability"
    assert accepted.status_code == 200 and scheduler.releases == [Outcome.SUCCESS]


def test_the_legacy_chat_body_still_passes_unknown_fields_through() -> None:
    seen: dict = {}

    class RecordingGateway:
        async def open(self, lease, capability, payload, deadline):
            seen.update(payload)
            return Opened()

    app = create_app(scheduler=Scheduler(), gateway=RecordingGateway())
    payload = {"model": "qwen-small", "messages": [{"role": "user", "content": "hi"}],
               "temperature": 0.3, "custom_extension": {"nested": [1, 2]}}

    with TestClient(app) as client:
        response = client.post("/v1/chat/completions", json=payload)

    assert response.status_code == 200
    assert seen["temperature"] == 0.3  # C05's strict unknown-field rule belongs to /internal only
    assert seen["custom_extension"] == {"nested": [1, 2]}
