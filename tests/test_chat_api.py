from __future__ import annotations

import copy
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from app import BodyError, _read_json, create_app
from model_scheduler.chat_counting import ChatCountingError, CountReceipt
from model_scheduler.contracts import GatewayError, Lease, Outcome
from model_scheduler.control_protocol_v1 import InstanceIdentity
from model_scheduler.envelope_validator import FixedOutputBudgetPolicy
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
    app = create_app(config=config, scheduler=scheduler, gateway=Gateway(),
                     chat_counter=StubCounter(), chat_policy=SYNTHETIC_POLICY)

    with TestClient(app) as client:
        unknown = client.post("/v1/chat/completions", json={"model": "missing", "messages": []})
        mismatch = client.post("/v1/chat/completions", json={"model": "embedding", "messages": []})
        malformed = client.post("/v1/chat/completions", json={"model": "qwen-small", "messages": []})
        accepted = client.post("/v1/chat/completions",
                               json={"model": "qwen-small", "messages": [{"role": "user", "content": "hi"}]})

    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "model_not_found"
    assert mismatch.status_code == 422 and mismatch.json()["error"]["code"] == "unsupported_capability"
    assert malformed.status_code == 422  # the C06 format check refuses an empty message list before dispatch
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


# --------------------------------------------------------------------------- TC02: the budget that actually reaches the upstream


SYNTHETIC_POLICY = FixedOutputBudgetPolicy(
    policy_id="synthetic-unit-policy",
    recognized_output_fields=frozenset({"max_tokens", "max_completion_tokens", "n_predict"}),
    supported_output_fields=frozenset({"max_tokens", "max_completion_tokens"}),
)


class RecordingGateway:
    """Keeps the body that reaches the upstream, which is the only proof that matters."""

    def __init__(self) -> None:
        self.payloads: list[dict] = []

    async def open(self, lease, capability, payload, deadline):
        self.payloads.append(copy.deepcopy(payload))
        return StreamOpened() if payload.get("stream") else Opened()


def _instance(model_id: str) -> InstanceIdentity:
    return InstanceIdentity(container_id="container-1", started_at="2026-09-24T00:00:00Z", deployment_id="orin-lab",
                            model_id=model_id, runtime_id="llama-cpp-1", candidate_digest="c" * 64,
                            image_digest="sms-llama-cpp@sha256:" + "a" * 64)


class StubCounter:
    """A minimal ChatCountPort (TC05): one receipt for the prepared request."""

    def __init__(self, *, tokens: int = 8, failure: Exception | None = None) -> None:
        self.tokens, self.failure = tokens, failure
        self.counted: list[str] = []
        self.bindings: list[str] = []

    async def count(self, lease, request, *, deadline):
        self.counted.append(lease.model_id)
        if self.failure is not None:
            raise self.failure
        return CountReceipt(body_sha256=request.body_sha256, policy_id=request.policy_id,
                            generation=lease.generation, instance=_instance(lease.model_id),
                            template_sha256="a" * 64, template_tokens=self.tokens, image_charge=0,
                            charged_input_tokens=self.tokens)

    async def validate_binding(self, lease, receipt, *, deadline):
        self.bindings.append(lease.model_id)


def _post_budget(tmp_path: Path, payload: dict, gateway: RecordingGateway):
    """One request through the extension's public software fixture: envelope 8192 / 4096 / 1024."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("sms_v2_chat_fixture", Path(__file__).resolve().parent / "test_config.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = tmp_path / "budget-config.yaml"
    path.write_text(module.V2_CHAT_FIXTURE, encoding="utf-8")
    from model_scheduler.config import load_config

    scheduler = Scheduler()
    app = create_app(config=load_config(path), scheduler=scheduler, gateway=gateway,
                     chat_counter=StubCounter(), chat_policy=SYNTHETIC_POLICY)
    with TestClient(app) as client:
        response = client.post("/v1/chat/completions", json=payload)
    return response, scheduler, gateway


@pytest.mark.parametrize("extra,expected", [({}, 1024), ({"max_tokens": 4096}, 1024), ({"max_tokens": 64}, 64)])
def test_a01_budget_reaches_upstream(tmp_path, extra, expected) -> None:
    gateway = RecordingGateway()
    sent = {"model": "qwen-small", "messages": [{"role": "user", "content": "hi"}], **extra}
    response, scheduler, gateway = _post_budget(tmp_path, sent, gateway)

    assert response.status_code == 200, response.text
    assert gateway.payloads[0]["max_tokens"] == expected  # clipped, defaulted or kept
    assert scheduler.releases == [Outcome.SUCCESS]
    assert sent == {"model": "qwen-small", "messages": [{"role": "user", "content": "hi"}], **extra}


@pytest.mark.parametrize("value", [0, -1, None, True, 1.5, "64"])
def test_a01_a_budget_that_is_not_a_positive_integer_is_422(tmp_path, value) -> None:
    gateway = RecordingGateway()
    response, scheduler, gateway = _post_budget(
        tmp_path, {"model": "qwen-small", "messages": [{"role": "user", "content": "hi"}], "max_tokens": value},
        gateway)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "contract_violation"
    assert response.json()["error"]["param"] == "max_tokens"
    assert gateway.payloads == [] and scheduler.releases == []  # nothing was dispatched


def test_a01_both_stream_values_dispatch_the_same_budget(tmp_path) -> None:
    for stream in (False, True):
        gateway = RecordingGateway()
        response, scheduler, gateway = _post_budget(
            tmp_path, {"model": "qwen-small", "messages": [{"role": "user", "content": "hi"}],
                       "max_tokens": 64, "stream": stream}, gateway)
        assert response.status_code == 200, response.text
        assert gateway.payloads[0]["max_tokens"] == 64 and gateway.payloads[0]["stream"] is stream


def test_a02_max_completion_tokens_is_clipped_and_keeps_its_own_name(tmp_path) -> None:
    gateway = RecordingGateway()
    response, _, gateway = _post_budget(
        tmp_path, {"model": "qwen-small", "messages": [{"role": "user", "content": "hi"}],
                   "max_completion_tokens": 4096}, gateway)

    assert response.status_code == 200, response.text
    assert gateway.payloads[0]["max_completion_tokens"] == 1024
    assert "max_tokens" not in gateway.payloads[0]  # an explicit alias is not renamed


def test_a02_two_budget_fields_are_refused_even_when_equal(tmp_path) -> None:
    gateway = RecordingGateway()
    response, scheduler, gateway = _post_budget(
        tmp_path, {"model": "qwen-small", "messages": [{"role": "user", "content": "hi"}],
                   "max_tokens": 64, "max_completion_tokens": 64}, gateway)

    assert response.status_code == 422
    assert response.json()["error"]["param"] == "max_completion_tokens"
    assert gateway.payloads == [] and scheduler.releases == []


def test_a02_a_recognised_but_unsupported_alias_is_refused_before_dispatch(tmp_path) -> None:
    gateway = RecordingGateway()
    response, scheduler, gateway = _post_budget(
        tmp_path, {"model": "qwen-small", "messages": [{"role": "user", "content": "hi"}], "n_predict": 64},
        gateway)

    assert response.status_code == 422
    assert response.json()["error"]["param"] == "n_predict"
    assert gateway.payloads == [] and scheduler.releases == []


@pytest.mark.parametrize("value", [0, 2, True, None])
def test_a02_only_n_equals_one_is_accepted(tmp_path, value) -> None:
    gateway = RecordingGateway()
    response, scheduler, gateway = _post_budget(
        tmp_path, {"model": "qwen-small", "messages": [{"role": "user", "content": "hi"}], "n": value}, gateway)

    assert response.status_code == 422 and response.json()["error"]["param"] == "n"
    assert gateway.payloads == []


def test_a02_n_one_and_sampling_extras_survive_the_prepared_body(tmp_path) -> None:
    gateway = RecordingGateway()
    response, _, gateway = _post_budget(
        tmp_path, {"model": "qwen-small", "messages": [{"role": "user", "content": "hi"}], "n": 1,
                   "temperature": 0.25, "custom_extension": {"keep": [1, 2]}, "max_tokens": 64}, gateway)

    assert response.status_code == 200, response.text
    dispatched = gateway.payloads[0]
    assert dispatched["n"] == 1 and dispatched["temperature"] == 0.25
    assert dispatched["custom_extension"] == {"keep": [1, 2]}
    assert dispatched["max_tokens"] == 64


# --------------------------------------------------------------------------- TC03/TC04: local refusals make no runtime call


CONTRACT_POLICY = FixedOutputBudgetPolicy(
    policy_id="synthetic-contract-policy",
    recognized_output_fields=frozenset({"max_tokens", "max_completion_tokens", "n_predict"}),
    supported_output_fields=frozenset({"max_tokens", "max_completion_tokens"}),
    effort_values=frozenset({"none", "low"}),
    denied_template_fields=frozenset({"chat_template", "chat_template_kwargs", "reasoning_format",
                                      "parse_tool_calls", "generation_prompt"}),
)


class RecordingScheduler:
    """Counts the runtime calls a local refusal must never reach."""

    def __init__(self) -> None:
        self.acquired: list[str] = []
        self.warmed: list[str] = []
        self.releases: list[Outcome] = []

    async def acquire(self, model_id: str, request_id: str, deadline: float) -> Lease:
        self.acquired.append(model_id)
        return Lease("lease", request_id, model_id, 1)

    async def release(self, lease: Lease, outcome: Outcome, tokens=None) -> None:
        self.releases.append(outcome)

    async def warm(self, model_id: str, deadline: float) -> None:
        self.warmed.append(model_id)


def _contract_fixture(tmp_path: Path, *, new_capabilities: bool) -> Path:
    """The public software fixture, optionally as a chat+vision+tools+thinking model."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "sms_v2_chat_fixture", Path(__file__).resolve().parent / "test_config.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    text = module.V2_CHAT_FIXTURE
    if new_capabilities:
        text = text.replace("capabilities: [chat, vision]",
                            "capabilities: [chat, vision, tools, thinking]")
        assert "tools, thinking" in text
    path = tmp_path / ("contract-new-capabilities.yaml" if new_capabilities else "contract-chat-only.yaml")
    path.write_text(text, encoding="utf-8")
    return path


def _post_contract(tmp_path: Path, payload: dict, *, new_capabilities: bool = False, counter: StubCounter | None = None):
    """One request through the fixture, with every method call counted."""
    from model_scheduler.config import load_config

    gateway = RecordingGateway()
    scheduler = RecordingScheduler()
    counter = counter or StubCounter()

    app = create_app(config=load_config(_contract_fixture(tmp_path, new_capabilities=new_capabilities)),
                     scheduler=scheduler, gateway=gateway, chat_counter=counter,
                     chat_policy=CONTRACT_POLICY)
    with TestClient(app) as client:
        response = client.post("/v1/chat/completions", json=payload)
    return response, scheduler, gateway, counter


_USER = {"role": "user", "content": "hi"}


def _tool() -> dict:
    return {"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object"}}}


def _call(**overrides) -> dict:
    call = {"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}
    call.update(overrides)
    return call


LOCAL_REFUSALS = [
    ("tools-null", {"tools": None}, "contract_violation", "tools"),
    ("tool-name", {"tools": [{"type": "function", "function": {"name": "bad.name"}}]},
     "contract_violation", "tools[0].function.name"),
    ("tool-choice", {"tools": [_tool()], "tool_choice": "any"}, "contract_violation", "tool_choice"),
    ("parallel", {"tools": [_tool()], "parallel_tool_calls": True}, "contract_violation", "parallel_tool_calls"),
    ("effort-shape", {"reasoning_effort": ""}, "contract_violation", "reasoning_effort"),
    ("orphan-result", {"messages": [_USER, {"role": "tool", "tool_call_id": "call_1", "content": "{}"}]},
     "contract_violation", "messages[1].tool_call_id"),
]


@pytest.mark.parametrize("name,extra,code,param", LOCAL_REFUSALS, ids=[case[0] for case in LOCAL_REFUSALS])
def test_a03_a04_a06_local_refusals_are_422_and_make_no_runtime_call(tmp_path, name, extra, code, param) -> None:
    response, scheduler, gateway, counter = _post_contract(
        tmp_path, {"model": "qwen-small", "messages": [_USER], **extra}, new_capabilities=True)

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == code and error["param"] == param
    # the refusal is local: not a lease, a warm-up, a token count or a dispatch
    assert scheduler.acquired == [] and scheduler.warmed == [] and scheduler.releases == []
    assert counter.counted == [] and gateway.payloads == []


@pytest.mark.parametrize("extra,param", [
    ({"tools": [_tool()]}, "tools"),
    ({"reasoning_effort": "low"}, "reasoning_effort"),
    ({"messages": [_USER, {"role": "assistant", "content": "42", "reasoning_content": "why"}]}, "messages"),
])
def test_a04_a06_a_model_without_the_new_capabilities_is_refused_before_any_call(tmp_path, extra, param) -> None:
    response, scheduler, gateway, counter = _post_contract(
        tmp_path, {"model": "qwen-small", "messages": [_USER], **extra})

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "capability_mismatch" and error["param"] == param
    assert scheduler.acquired == [] and scheduler.warmed == []
    assert counter.counted == [] and gateway.payloads == []


@pytest.mark.parametrize("field", ["chat_template", "chat_template_kwargs", "reasoning_format",
                                   "parse_tool_calls", "generation_prompt"])
def test_a06_local_a_new_capability_model_refuses_template_overrides(tmp_path, field) -> None:
    response, scheduler, gateway, counter = _post_contract(
        tmp_path, {"model": "qwen-small", "messages": [_USER], field: None}, new_capabilities=True)

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "contract_violation" and error["param"] == field
    assert scheduler.acquired == [] and gateway.payloads == [] and counter.counted == []


def test_a03_the_accepted_tool_fields_reach_the_upstream_unchanged(tmp_path) -> None:
    tool_choice = {"type": "function", "function": {"name": "get_weather"}}
    sent = {"model": "qwen-small", "messages": [_USER], "tools": [_tool()], "tool_choice": tool_choice,
            "parallel_tool_calls": False, "max_tokens": 64, "reasoning_effort": "low"}
    response, scheduler, gateway, counter = _post_contract(tmp_path, sent, new_capabilities=True)

    assert response.status_code == 200, response.text
    dispatched = gateway.payloads[0]
    assert dispatched["tools"] == [_tool()] and dispatched["tool_choice"] == tool_choice
    assert dispatched["parallel_tool_calls"] is False and dispatched["reasoning_effort"] == "low"
    assert dispatched["max_tokens"] == 64 and "max_completion_tokens" not in dispatched
    assert scheduler.acquired == ["qwen-small"] and scheduler.releases == [Outcome.SUCCESS]
    assert counter.counted == ["qwen-small"] and counter.bindings == ["qwen-small"]


def test_a04_a_closed_history_reaches_the_upstream_with_its_ids(tmp_path) -> None:
    messages = [_USER, {"role": "assistant", "tool_calls": [_call()]},
                {"role": "tool", "tool_call_id": "call_1", "content": "{\"marker\":\"OK\"}"}]
    response, scheduler, gateway, counter = _post_contract(
        tmp_path, {"model": "qwen-small", "messages": messages, "max_tokens": 64}, new_capabilities=True)

    assert response.status_code == 200, response.text
    dispatched = gateway.payloads[0]["messages"]
    assert dispatched[1]["tool_calls"][0]["id"] == "call_1"
    assert dispatched[1]["tool_calls"][0]["function"]["arguments"] == "{}"
    assert dispatched[2]["tool_call_id"] == "call_1"
    assert scheduler.acquired == ["qwen-small"] and counter.counted == ["qwen-small"]


# --------------------------------------------------------------------------- TC05/TC06: the lease-bound count


def test_a05_a_count_that_cannot_be_proven_is_503_and_aborts_the_lease(tmp_path) -> None:
    counter = StubCounter(failure=ChatCountingError("service_unavailable", "the runtime is gone"))
    response, scheduler, gateway, counter = _post_contract(
        tmp_path, {"model": "qwen-small", "messages": [_USER]}, counter=counter)

    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "service_unavailable"
    assert scheduler.acquired == ["qwen-small"] and scheduler.releases == [Outcome.ABORTED]
    assert gateway.payloads == []


def test_a05_an_expired_count_is_504_and_aborts_the_lease(tmp_path) -> None:
    counter = StubCounter(failure=ChatCountingError("inference_timeout", "the deadline elapsed"))
    response, scheduler, gateway, counter = _post_contract(
        tmp_path, {"model": "qwen-small", "messages": [_USER]}, counter=counter)

    assert response.status_code == 504, response.text
    assert response.json()["error"]["code"] == "inference_timeout"
    assert scheduler.releases == [Outcome.ABORTED] and gateway.payloads == []


def test_a05_an_unbound_counter_is_refused_before_any_lease(tmp_path) -> None:
    # a v2 envelope without a counter must refuse (503), never dispatch with a skipped budget
    from model_scheduler.config import load_config

    scheduler, gateway = RecordingScheduler(), RecordingGateway()
    app = create_app(config=load_config(_contract_fixture(tmp_path, new_capabilities=False)),
                     scheduler=scheduler, gateway=gateway, chat_counter=None, chat_policy=CONTRACT_POLICY)

    with TestClient(app) as client:
        response = client.post("/v1/chat/completions", json={"model": "qwen-small", "messages": [_USER]})

    assert response.status_code == 503 and response.json()["error"]["code"] == "service_unavailable"
    assert scheduler.acquired == [] and gateway.payloads == []


def test_a05_an_unbound_policy_is_refused_before_any_lease(tmp_path) -> None:
    from model_scheduler.config import load_config

    scheduler, gateway = RecordingScheduler(), RecordingGateway()
    app = create_app(config=load_config(_contract_fixture(tmp_path, new_capabilities=False)),
                     scheduler=scheduler, gateway=gateway, chat_counter=StubCounter(), chat_policy={})

    with TestClient(app) as client:
        response = client.post("/v1/chat/completions", json={"model": "qwen-small", "messages": [_USER]})

    assert response.status_code == 503 and scheduler.acquired == [] and gateway.payloads == []
