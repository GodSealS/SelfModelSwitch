"""CT07: the OpenAI-compatible two-round fixtures and their driver (TC08, A08).

The compat path exists because tools and thinking are not internal execution
operations: they travel as ordinary chat bodies over the public route. These
cases pin what the driver may do with them — build round 2 only from what the
service really answered, keep every raw byte, and refuse instead of inventing a
tool result, a call id or a static first-round call.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from model_scheduler.acceptance import backend_cases as bc
from model_scheduler.acceptance import chat_compat as cc
from model_scheduler.acceptance import fixtures as fx
from model_scheduler.acceptance import materials as ms
from model_scheduler.contracts_v2 import Envelope
from model_scheduler.evidence_contracts import ContractError

MODEL_ID = "qwen36-27b"
ENVELOPE = Envelope(ctx_size=32768, max_input_tokens=28672, max_output_tokens=1024, max_parallel=2,
                    max_image_tokens=0, max_image_edge_pixels=0, max_images=0)
DEADLINE = 120.0

TOOLS_ROUND_ONE = json.dumps({
    "id": "chatcmpl-round-1",
    "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None, "reasoning_content": "需要一个天气工具",
        "tool_calls": [{"id": "call_real_upstream_1", "type": "function",
                        "function": {"name": "get_weather", "arguments": "{\"city\":\"Beijing\"}"}}]}}],
    "usage": {"prompt_tokens": 210, "completion_tokens": 33},
}, ensure_ascii=False).encode("utf-8")

TOOLS_ROUND_TWO = json.dumps({
    "id": "chatcmpl-round-2",
    "choices": [{"index": 0, "finish_reason": "stop", "message": {
        "role": "assistant", "content": "SMS_WEATHER_OK_27", "reasoning_content": "已取到标记"}}],
    "usage": {"prompt_tokens": 260, "completion_tokens": 6},
}, ensure_ascii=False).encode("utf-8")

THINKING_ROUND_ONE = json.dumps({
    "choices": [{"index": 0, "finish_reason": "stop", "message": {
        "role": "assistant", "content": " 19 * 23 = 437 RESULT=437 ", "reasoning_content": "先算乘法"}}],
    "usage": {"prompt_tokens": 41, "completion_tokens": 29},
}, ensure_ascii=False).encode("utf-8")

THINKING_ROUND_TWO = json.dumps({
    "choices": [{"index": 0, "finish_reason": "stop", "message": {
        "role": "assistant", "content": "RESULT=438", "reasoning_content": "再加一"}}],
    "usage": {"prompt_tokens": 88, "completion_tokens": 14},
}, ensure_ascii=False).encode("utf-8")


class FakeTransport:
    """A scripted upstream: the driver may only use what this hands back."""

    def __init__(self, *bodies: bytes, status: int = 200, request_id: bool = True) -> None:
        self.bodies = list(bodies)
        self.status = status
        self.request_id = request_id
        self.sent: list[bytes] = []
        self.streamed: list[bool] = []

    def chat(self, request_json: bytes, *, stream: bool, deadline: float) -> cc.CompatRound:
        self.sent.append(request_json)
        self.streamed.append(stream)
        return cc.CompatRound(status=self.status, raw=self.bodies[len(self.sent) - 1],
                              request_id=f"req-{len(self.sent)}" if self.request_id else None)


class ExplodingAggregator:
    """A streaming round without a real aggregator is refused, never guessed."""

    def aggregate(self, raw: bytes) -> dict:
        raise AssertionError("the aggregator must not run before it is injected")


def _driver(*bodies: bytes, aggregator=None) -> cc.CompatDriver:
    return cc.CompatDriver(FakeTransport(*bodies), aggregator=aggregator)


def _tools_scenario(stream: bool = False) -> cc.CompatScenario:
    return cc.tools_scenario(MODEL_ID, envelope=ENVELOPE, stream=stream, deadline_seconds=DEADLINE)


def _thinking_scenario(stream: bool = False) -> cc.CompatScenario:
    return cc.thinking_scenario(MODEL_ID, envelope=ENVELOPE, stream=stream, deadline_seconds=DEADLINE)


def _replace(scenario: cc.CompatScenario, **changes) -> cc.CompatScenario:
    return cc.CompatScenario(**{**vars(scenario), **changes})


def test_a08_the_scenario_only_accepts_the_compat_transport_and_two_rounds() -> None:
    scenario = _tools_scenario()

    assert scenario.transport == cc.TRANSPORT_COMPAT
    assert scenario.rounds == 2 and scenario.capability == "tools" and scenario.stream is False
    assert scenario.model_id == MODEL_ID and scenario.tool_result_json == cc.TOOL_RESULT_JSON.encode("utf-8")

    body = json.loads(scenario.first_request_json)
    assert [message["role"] for message in body["messages"]] == ["user"]  # no static first-round call
    assert any(tool["function"]["name"] == cc.TOOL_NAME for tool in body["tools"])
    assert body["tool_choice"] == {"type": "function", "function": {"name": cc.TOOL_NAME}}
    assert body["parallel_tool_calls"] is False
    assert body["stream"] is False
    assert body["max_tokens"] == min(1024, ENVELOPE.max_output_tokens)

    with pytest.raises(cc.CompatError, match="transport"):
        _replace(scenario, transport="internal")
    with pytest.raises(cc.CompatError, match="rounds"):
        _replace(scenario, rounds=1)
    with pytest.raises(cc.CompatError, match="deadline"):
        _replace(scenario, deadline_seconds=0)


def test_a08_a_tools_scenario_without_the_fixed_result_is_refused_at_the_factory() -> None:
    """The driver never invents a tool result: without one the fixture does not exist."""
    with pytest.raises(cc.CompatError, match="tool_result_json"):
        cc.tools_scenario(MODEL_ID, envelope=ENVELOPE, stream=False, deadline_seconds=DEADLINE,
                          tool_result_json=None)

    with pytest.raises(cc.CompatError, match="capability"):
        _replace(_tools_scenario(), capability="chat")

    thinking = _thinking_scenario()
    assert thinking.tool_result_json is None
    assert json.loads(thinking.first_request_json)["messages"][0]["content"] == cc.THINKING_FIRST_PROMPT


def test_a08_the_fixture_hash_covers_every_field_and_the_fixed_expectation() -> None:
    scenario = _tools_scenario()
    digest = scenario.digest()

    assert scenario.first_request_json and scenario.tool_result_json  # no empty authoring fields
    assert _replace(scenario, fixture_id="other").digest() != digest
    assert _replace(scenario, tool_result_json=cc.THINKING_FOLLOWUP_PROMPT.encode("utf-8")).digest() != digest
    assert _tools_scenario(stream=True).digest() != digest
    assert _replace(scenario, deadline_seconds=DEADLINE + 1).digest() != digest
    changed_expectation = dict(scenario.expected)
    changed_expectation["final_content"] = "SMS_WEATHER_OK_99"
    assert _replace(scenario, expected=changed_expectation).digest() != digest
    assert _tools_scenario().digest() == digest  # reproducible


def test_a08_missing_transport_scenario_or_aggregator_is_a_refusal(tmp_path: Path) -> None:
    spec = cc.CaseSpec(case_id=f"B:{MODEL_ID}:cap:tools", variant="json-hot", scenario=_tools_scenario())

    with pytest.raises(cc.CompatError, match="transport"):
        cc.CompatDriver(None).run(spec, tmp_path / "no-transport")
    with pytest.raises(cc.CompatError, match="aggregator"):
        _driver(TOOLS_ROUND_ONE, TOOLS_ROUND_TWO).run(
            cc.CaseSpec(case_id=spec.case_id, variant="sse-hot", scenario=_tools_scenario(stream=True)),
            tmp_path / "no-aggregator",
        )
    with pytest.raises(cc.CompatError, match="scenario"):
        cc.CompatDriver(FakeTransport(TOOLS_ROUND_ONE)).run(None, tmp_path / "no-scenario")

    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "keep.txt").write_text("older material", encoding="utf-8")
    with pytest.raises(cc.CompatError, match="empty"):
        _driver(TOOLS_ROUND_ONE, TOOLS_ROUND_TWO).run(spec, occupied)


def test_a08_the_second_round_is_built_from_the_real_first_response(tmp_path: Path) -> None:
    transport = FakeTransport(TOOLS_ROUND_ONE, TOOLS_ROUND_TWO)
    run = cc.CompatDriver(transport).run(
        cc.CaseSpec(case_id=f"B:{MODEL_ID}:cap:tools", variant="json-hot", scenario=_tools_scenario()),
        tmp_path / "tools-json-hot",
    )

    assert transport.streamed == [False, False]
    first = json.loads(transport.sent[0])
    second = json.loads(transport.sent[1])
    call_id = json.loads(TOOLS_ROUND_ONE)["choices"][0]["message"]["tool_calls"][0]["id"]

    assert second["messages"][:1] == first["messages"]  # the original round-1 user turn
    assert second["messages"][1]["tool_calls"][0]["id"] == call_id  # the id the service really gave
    assert second["messages"][1]["reasoning_content"] == "需要一个天气工具"  # reasoning is not dropped
    assert second["messages"][-1] == {"role": "tool", "tool_call_id": call_id,
                                     "content": "{\"city\":\"Beijing\",\"marker\":\"SMS_WEATHER_OK_27\",\"temperature_c\":23}"}
    assert "tools" not in second and "tool_choice" not in second and "parallel_tool_calls" not in second
    assert second["max_tokens"] == first["max_tokens"] and second["stream"] is False
    assert run.tool_call_id == call_id and run.final_content.strip() == cc.TOOL_MARKER
    assert cc.link_problems(json.loads((tmp_path / "tools-json-hot" / cc.COMPAT_FILE).read_text("utf-8"))) == []


def test_a08_a_different_upstream_id_is_copied_not_statically_filled(tmp_path: Path) -> None:
    bodies = (TOOLS_ROUND_ONE.replace(b"call_real_upstream_1", b"call_another_real_id"), TOOLS_ROUND_TWO)
    transport = FakeTransport(*bodies)
    cc.CompatDriver(transport).run(
        cc.CaseSpec(case_id=f"B:{MODEL_ID}:cap:tools", variant="json-hot", scenario=_tools_scenario()),
        tmp_path / "tools-other-id",
    )

    second = json.loads(transport.sent[1])
    assert second["messages"][1]["tool_calls"][0]["id"] == "call_another_real_id"
    assert second["messages"][-1]["tool_call_id"] == "call_another_real_id"


def test_a08_the_thinking_rounds_keep_reasoning_and_the_fixed_followup(tmp_path: Path) -> None:
    transport = FakeTransport(THINKING_ROUND_ONE, THINKING_ROUND_TWO)
    run = cc.CompatDriver(transport).run(
        cc.CaseSpec(case_id=f"B:{MODEL_ID}:cap:thinking", variant="json-hot", scenario=_thinking_scenario()),
        tmp_path / "thinking-json-hot",
    )

    second = json.loads(transport.sent[1])
    carried = second["messages"][1]
    assert carried["content"] == " 19 * 23 = 437 RESULT=437 "  # exactly what came back
    assert carried["reasoning_content"] == "先算乘法"
    assert second["messages"][-1] == {"role": "user", "content": cc.THINKING_FOLLOWUP_PROMPT}
    assert "tools" not in second
    assert run.first_content.strip().endswith("RESULT=437") and run.final_content.strip() == "RESULT=438"
    assert run.first_reasoning.strip() and run.final_reasoning.strip()


def test_a08_both_rounds_leave_their_raw_material(tmp_path: Path) -> None:
    target = tmp_path / "material"
    cc.CompatDriver(FakeTransport(TOOLS_ROUND_ONE, TOOLS_ROUND_TWO)).run(
        cc.CaseSpec(case_id=f"B:{MODEL_ID}:cap:tools", variant="json-hot", scenario=_tools_scenario()),
        target,
    )

    for directory, answered in ((target / "round-1", TOOLS_ROUND_ONE), (target / "round-2", TOOLS_ROUND_TWO)):
        request = (directory / cc.REQUEST_FILE).read_bytes()
        response = (directory / cc.RESPONSE_FILE).read_bytes()
        assert request  # the bytes that were really sent are kept, not re-rendered from memory
        assert response == answered  # untouched: the service's own bytes
        document = json.loads((directory / cc.BODY_FILE).read_text(encoding="utf-8"))
        assert document["id"] == json.loads(answered)["id"]

    facts = json.loads((target / cc.COMPAT_FILE).read_text(encoding="utf-8"))
    assert facts["case_id"] == f"B:{MODEL_ID}:cap:tools" and facts["variant"] == "json-hot"
    assert facts["scenario_digest"] == _tools_scenario().digest()
    assert [round_row["request_id"] for round_row in facts["rounds"]] == ["req-1", "req-2"]
    assert facts["rounds"][0]["finish_reason"] == "tool_calls"
    assert facts["rounds"][1]["usage"]["completion_tokens"] == 6

    refs = cc.compat_material_refs(target)
    assert (Path("round-1") / cc.RESPONSE_FILE).as_posix() in dict(refs)
    assert dict(refs)[(Path("round-2") / cc.RESPONSE_FILE).as_posix()] == hashlib.sha256(TOOLS_ROUND_TWO).hexdigest()


def test_a08_removing_the_second_round_or_rewriting_an_id_stays_visible(tmp_path: Path) -> None:
    target = tmp_path / "tampered"
    cc.CompatDriver(FakeTransport(TOOLS_ROUND_ONE, TOOLS_ROUND_TWO)).run(
        cc.CaseSpec(case_id=f"B:{MODEL_ID}:cap:tools", variant="json-hot", scenario=_tools_scenario()),
        target,
    )
    facts = json.loads((target / cc.COMPAT_FILE).read_text(encoding="utf-8"))
    before = dict(cc.compat_material_refs(target))

    deleted = target / "round-2"
    for path in sorted(deleted.rglob("*"), reverse=True):
        path.unlink()
    deleted.rmdir()
    assert dict(cc.compat_material_refs(target)) != before  # a recomputed outer hash cannot hide this
    with pytest.raises(cc.CompatError, match="round 2"):
        cc.verify_material(target, facts)

    # Rewriting the carried id *and* re-hashing the material the way an attacker
    # would still leaves round 2 unlinked to the call round 1 really produced.
    again = tmp_path / "rewritten"
    cc.CompatDriver(FakeTransport(TOOLS_ROUND_ONE, TOOLS_ROUND_TWO)).run(
        cc.CaseSpec(case_id=f"B:{MODEL_ID}:cap:tools", variant="json-hot", scenario=_tools_scenario()),
        again,
    )
    forged = json.loads((again / "round-2" / cc.REQUEST_FILE).read_text(encoding="utf-8"))
    forged["messages"][1]["tool_calls"][0]["id"] = "call_forged"
    forged["messages"][-1]["tool_call_id"] = "call_forged"
    (again / "round-2" / cc.REQUEST_FILE).write_text(cc.canonical_json_bytes(forged).decode("utf-8"),
                                                     encoding="utf-8")
    forged_facts = json.loads((again / cc.COMPAT_FILE).read_text(encoding="utf-8"))
    forged_facts["material"] = [{"relative_path": path, "sha256": digest}
                                for path, digest in cc.compat_material_refs(again)
                                if path != cc.COMPAT_FILE]
    reviewed = cc.verify_material(again, forged_facts)
    assert any("tool_call_id" in problem for problem in reviewed["problems"])
    assert cc.link_problems(forged_facts)


def test_a08_the_case_specs_are_derived_from_the_declared_capabilities() -> None:
    specs = cc.case_specs_for(MODEL_ID, ("chat", "tools", "thinking"), ENVELOPE, deadline_seconds=DEADLINE)

    assert [(spec.case_id, spec.variant) for spec in specs] == [
        (f"B:{MODEL_ID}:cap:tools", variant) for variant in cc.VARIANTS
    ] + [(f"B:{MODEL_ID}:cap:thinking", variant) for variant in cc.VARIANTS]
    assert [spec.scenario.stream for spec in specs[:4]] == [False, True, False, True]
    assert cc.case_specs_for(MODEL_ID, ("chat", "vision"), ENVELOPE, deadline_seconds=DEADLINE) == ()

    with pytest.raises(cc.CompatError, match="variant"):
        cc.CaseSpec(case_id=f"B:{MODEL_ID}:cap:tools", variant="json-warm", scenario=_tools_scenario())
    with pytest.raises(cc.CompatError, match="stream"):
        cc.CaseSpec(case_id=f"B:{MODEL_ID}:cap:tools", variant="json-hot", scenario=_tools_scenario(stream=True))
    with pytest.raises(cc.CompatError, match="case id"):
        cc.CaseSpec(case_id=f"B:{MODEL_ID}:cap:vision", variant="json-hot", scenario=_tools_scenario())
    with pytest.raises(cc.CompatError, match="case id"):
        cc.CaseSpec(case_id="L:alt:cap:tools", variant="json-hot", scenario=_tools_scenario())


def test_a08_the_derived_case_set_covers_every_declared_capability() -> None:
    """Coverage comes from the registration: a new capability adds its case id."""
    from model_scheduler.contracts_v2 import MODEL_CAPABILITIES
    from model_scheduler.evidence_contracts import CAPABILITIES, parse_case_id

    assert CAPABILITIES >= MODEL_CAPABILITIES
    for capability in cc.COMPAT_CAPABILITIES:
        assert parse_case_id(f"B:{MODEL_ID}:cap:{capability}") == f"B:{MODEL_ID}:cap:{capability}"
    with pytest.raises(ContractError):
        parse_case_id(f"B:{MODEL_ID}:cap:audio")

    specs = cc.case_specs_for(MODEL_ID, ("chat", "tools"), ENVELOPE, deadline_seconds=DEADLINE)
    assert {spec.case_id for spec in specs} == {f"B:{MODEL_ID}:cap:tools"}  # thinking is not derived when undeclared


def test_a08_the_internal_execution_surface_is_unchanged() -> None:
    """The compat route adds nothing to the internal execution DTO or its schema."""
    from model_scheduler.control_protocol_v1 import EXECUTION_OPERATIONS, render_schema_text, schema_document

    assert EXECUTION_OPERATIONS == frozenset({"chat", "vision", "embeddings", "rerank"})
    assert "tools" not in schema_document()["capabilities"]
    committed = (Path(__file__).resolve().parents[1] / "schemas" / "control-v1.json").read_text(encoding="utf-8")
    assert committed == render_schema_text()


# ---------------------------------------------------------------------------
# A08: the driver/fixture selection — old capabilities keep the legacy route.
# ---------------------------------------------------------------------------


class LegacyDriver:
    """The internal execution surface a chat feature must never be routed through."""

    def load(self, model_id: str, *, cold: bool):
        return {"provider": "llama-cpp-1", "instance": {"model_id": model_id}, "cold": cold}

    def start(self, model_id: str, request):
        return {"execution_id": "exec-1", "provider": "llama-cpp-1"}

    def execute(self, model_id: str, request):
        return {"provider": "llama-cpp-1", "device_activity": {"raw_samples": {"tegrastats": 4}},
                "output": {"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
                "observed": {"input_tokens": 1, "output_tokens": 1, "parallel": 1}}

    def cancel(self, model_id: str, execution_id: str):
        return {"provider": "llama-cpp-1", "cancelled": True}

    def stop(self, model_id: str):
        return {"provider": "llama-cpp-1", "stop_proven": True, "instance": None}

    def cleanup(self, model_id: str):
        return {"stopped": True}


class FakeCompatRunner:
    """The compat route, as the B layer sees it: what it wrote, and what it saw."""

    def __init__(self, status: str = "passed", problems: tuple[str, ...] = ()) -> None:
        self.status = status
        self.problems = problems
        self.calls: list[tuple[str, str, str, Path]] = []

    def run_case(self, *, model_id: str, capability: str, variant: str, directory: Path) -> dict:
        self.calls.append((model_id, capability, variant, directory))
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "round-1" / cc.RESPONSE_FILE).parent.mkdir(parents=True, exist_ok=True)
        (directory / "round-1" / cc.RESPONSE_FILE).write_bytes(b"{}")
        return {"status": self.status, "problems": list(self.problems),
                "facts": {"tool_call_id": "call_from_compat", "transport": cc.TRANSPORT_COMPAT}}


FILLER = fx.FillerSpec(unit="a", tokens_per_unit=1.0, template_overhead_tokens=19,
                       vision_template_overhead_tokens=50, instruction="Reply briefly.", instruction_tokens=3)


def _executor(tmp_path: Path, runner=None, **kwargs):
    store = ms.CaseMaterialStore(tmp_path, run_id="run-1", candidate_sha256="a" * 64, device_digest="b" * 64)
    executor = bc.CaseExecutor(LegacyDriver(), collector=store, filler_of={MODEL_ID: FILLER},
                               compat=runner, **kwargs)
    return executor


def test_a08_the_legacy_fixtures_never_stand_in_for_a_chat_feature() -> None:
    fixtures = fx.fixtures_for(MODEL_ID, ("chat", "tools", "thinking"), ENVELOPE, filler=FILLER)

    assert [fixture.capability for fixture in fixtures] == ["chat"]
    with pytest.raises(fx.FixtureError, match="no fixture is defined"):
        fx.fixtures_for(MODEL_ID, ("speech",), ENVELOPE)


def test_a08_a_chat_feature_without_the_compat_runner_stays_unproven(tmp_path: Path) -> None:
    attempts = _executor(tmp_path / "store").run_model(model_id=MODEL_ID,
                                                       capabilities=("chat", "tools", "thinking"),
                                                       envelope=ENVELOPE)
    by_case = {attempt.case_id: attempt for attempt in attempts}

    assert {f"B:{MODEL_ID}:load", f"B:{MODEL_ID}:infer", f"B:{MODEL_ID}:envelope", f"B:{MODEL_ID}:cancel",
            f"B:{MODEL_ID}:stop", f"B:{MODEL_ID}:reload", f"B:{MODEL_ID}:cap:chat"} <= set(by_case)
    assert by_case[f"B:{MODEL_ID}:cap:chat"].status == "passed"  # the legacy route is untouched
    for capability in ("tools", "thinking"):
        attempt = by_case[f"B:{MODEL_ID}:cap:{capability}"]
        assert attempt.status == "unknown" and attempt.problems
        assert attempt.facts["transport"] == cc.TRANSPORT_COMPAT


def test_a08_the_compat_runner_owns_the_feature_case_and_its_material(tmp_path: Path) -> None:
    runner = FakeCompatRunner()
    attempts = _executor(tmp_path / "store", runner).run_model(model_id=MODEL_ID, capabilities=("chat", "tools"),
                                                               envelope=ENVELOPE)
    by_case = {attempt.case_id: attempt for attempt in attempts}
    attempt = by_case[f"B:{MODEL_ID}:cap:tools"]

    assert attempt.status == "passed" and attempt.facts["tool_call_id"] == "call_from_compat"
    assert [(model, capability, variant) for model, capability, variant, _ in runner.calls] == [
        (MODEL_ID, "tools", "json-hot")]
    directory = runner.calls[0][3]
    assert (directory / "round-1" / cc.RESPONSE_FILE).is_file()
    assert (directory.parent / "case.json").is_file()  # the B layer still records the case

    with pytest.raises(bc.BackendCaseError, match="compat variant"):
        _executor(tmp_path / "rejected", runner, compat_variant="json-cold")


def test_a08_a_compat_case_the_runner_cannot_prove_is_not_a_pass(tmp_path: Path) -> None:
    runner = FakeCompatRunner(status="passed", problems=("round 2: no tool result was used",))
    attempts = _executor(tmp_path / "store", runner).run_model(model_id=MODEL_ID, capabilities=("chat", "thinking"),
                                                               envelope=ENVELOPE)

    attempt = {attempt.case_id: attempt for attempt in attempts}[f"B:{MODEL_ID}:cap:thinking"]
    assert attempt.status == "unknown" and "unresolved problems" in " ".join(attempt.problems)


# ---------------------------------------------------------------------------
# CT08: the SSE aggregator and the independent evaluator (TC08, A08/A09).
# ---------------------------------------------------------------------------


def _sse(stream_id: str = "call_fixture_1") -> bytes:
    """The raw SSE fixture of acceptance §6: fragments, usage-only, one DONE."""
    events = [
        '{"choices":[{"index":0,"delta":{"role":"assistant","reasoning_content":"查询天气"},"finish_reason":null}]}',
        '{"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"' + stream_id + '","type":"function",'
        '"function":{"name":"get_weather","arguments":"{\\"city\\":"}}]},"finish_reason":null}]}',
        '{"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"Beijing\\"}"}}]},'
        '"finish_reason":null}]}',
        '{"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}',
        '{"choices":[],"usage":{"prompt_tokens":123,"completion_tokens":45,"total_tokens":168}}',
    ]
    return ("\n\n".join(f"data: {event}" for event in events) + "\n\ndata: [DONE]\n\n").encode("utf-8")


def _per_byte(raw: bytes) -> cc.SseAggregator:
    aggregator = cc.SseAggregator()
    for index in range(len(raw)):
        aggregator.feed(raw[index : index + 1])
    return aggregator


def test_a09_the_aggregator_reassembles_a_stream_that_was_cut_at_every_byte() -> None:
    body = _per_byte(_sse()).finish()

    message = body["choices"][0]["message"]
    assert message["reasoning_content"] == "查询天气"
    assert message["tool_calls"][0]["id"] == "call_fixture_1"
    assert message["tool_calls"][0]["function"]["name"] == "get_weather"
    assert json.loads(message["tool_calls"][0]["function"]["arguments"]) == {"city": "Beijing"}
    assert body["choices"][0]["finish_reason"] == "tool_calls"
    assert body["usage"] == {"prompt_tokens": 123, "completion_tokens": 45, "total_tokens": 168}
    assert cc.SseAggregator().aggregate(_sse()) == body  # the whole-buffer path agrees


def test_a09_non_ascii_survives_a_boundary_inside_one_character() -> None:
    raw = ('data: {"choices":[{"index":0,"delta":{"reasoning_content":"查询天"},"finish_reason":null}]}\n\n'
           'data: {"choices":[{"index":0,"delta":{"reasoning_content":"气"},"finish_reason":"stop"}]}\n\n'
           'data: [DONE]\n\n').encode("utf-8")
    split = raw.index("天".encode("utf-8")) + 1  # cuts a multi-byte character in half

    aggregator = cc.SseAggregator()
    aggregator.feed(raw[:split])
    aggregator.feed(raw[split:])
    body = aggregator.finish()

    assert body["choices"][0]["message"]["reasoning_content"] == "查询天气"
    assert body["choices"][0]["finish_reason"] == "stop"


def test_a09_only_the_first_fragment_has_to_carry_the_id_and_name() -> None:
    raw = ('data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_a","type":"function",'
           '"function":{"name":"get_weather","arguments":"{\\"c"}}]},"finish_reason":null}]}\n\n'
           'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"ity\\":\\"B"}}]},'
           '"finish_reason":null}]}\n\n'
           'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"eijing\\"}"}}]},'
           '"finish_reason":"tool_calls"}]}\n\n'
           'data: [DONE]\n\n').encode("utf-8")

    call = cc.SseAggregator().aggregate(raw)["choices"][0]["message"]["tool_calls"][0]
    assert call["id"] == "call_a" and call["type"] == "function"
    assert call["function"]["name"] == "get_weather"
    assert json.loads(call["function"]["arguments"]) == {"city": "Beijing"}

    conflicting = raw.replace(b'{"index":0,"function":{"arguments":"eijing',
                              b'{"index":0,"id":"call_b","function":{"arguments":"eijing')
    with pytest.raises(cc.CompatError, match="id"):
        cc.SseAggregator().aggregate(conflicting)


def test_a09_usage_only_chunks_and_null_deltas_are_not_errors() -> None:
    raw = ('data: {"choices":[{"index":0,"delta":{"content":null,"reasoning_content":null},'
           '"finish_reason":null}]}\n\n'
           'data: {"choices":[{"index":0,"delta":{"content":"SMS_WEATHER_OK_27"},"finish_reason":"stop"}]}\n\n'
           'data: {"choices":[],"usage":{"prompt_tokens":9,"completion_tokens":2,"total_tokens":11}}\n\n'
           'data: [DONE]\n\n').encode("utf-8")

    body = cc.SseAggregator().aggregate(raw)
    assert body["choices"][0]["message"]["content"] == "SMS_WEATHER_OK_27"
    assert body["usage"]["total_tokens"] == 11


def test_a09_a_broken_stream_is_refused_not_approximated() -> None:
    broken = {
        "bad json": b'data: {"choices":[{"index":0,\n\ndata: [DONE]\n\n',
        "index type": b'data: {"choices":[{"index":"0","delta":{},"finish_reason":null}]}\n\ndata: [DONE]\n\n',
        "two indexes": b'data: {"choices":[{"index":0,"delta":{},"finish_reason":null},{"index":1,"delta":{},'
                       b'"finish_reason":null}]}\n\ndata: [DONE]\n\n',
        "missing DONE": b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n',
        "duplicate DONE": b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
                          b'data: [DONE]\n\ndata: [DONE]\n\n',
        "data after DONE": b'data: [DONE]\n\ndata: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n',
        "no finish reason": b'data: {"choices":[{"index":0,"delta":{"content":"x"},"finish_reason":null}]}\n\n'
                            b'data: [DONE]\n\n',
        "empty call id": b'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"","type":"function",'
                         b'"function":{"name":"get_weather","arguments":"{}"}}]},"finish_reason":"tool_calls"}]}\n\n'
                         b'data: [DONE]\n\n',
    }
    for label, raw in broken.items():
        with pytest.raises(cc.CompatError) as refused:
            cc.SseAggregator().aggregate(raw)
        assert str(refused.value), label

    with pytest.raises(cc.CompatError, match="event"):
        cc.SseAggregator(event_limit=2).aggregate(_sse())


def test_a08_a_streamed_scenario_runs_through_the_aggregator(tmp_path: Path) -> None:
    second = ('data: {"choices":[{"index":0,"delta":{"reasoning_content":"已取到",'
              '"content":"SMS_WEATHER"},"finish_reason":null}]}\n\n'
              'data: {"choices":[{"index":0,"delta":{"content":"_OK_27"},"finish_reason":"stop"}]}\n\n'
              'data: [DONE]\n\n').encode("utf-8")
    transport = FakeTransport(_sse("call_streamed_1"), second)
    driver = cc.CompatDriver(transport, aggregator=cc.SseAggregator)  # one fresh aggregator per round
    spec = cc.CaseSpec(case_id=f"B:{MODEL_ID}:cap:tools", variant="sse-hot", scenario=_tools_scenario(stream=True))

    run = driver.run(spec, tmp_path / "tools-sse-hot")

    assert run.tool_call_id == "call_streamed_1" and run.final_content == "SMS_WEATHER_OK_27"
    assert run.first_reasoning == "查询天气"
    assert json.loads(transport.sent[1])["messages"][-1]["tool_call_id"] == "call_streamed_1"
    assert (tmp_path / "tools-sse-hot" / "round-1" / cc.RESPONSE_FILE).read_bytes() == _sse("call_streamed_1")
    assert cc.evaluate_case(tmp_path / "tools-sse-hot")["problems"] == []


def _completed(tmp_path: Path, name: str, *, first: bytes = TOOLS_ROUND_ONE,
               second: bytes = TOOLS_ROUND_TWO, capability: str = "tools") -> Path:
    target = tmp_path / name
    scenario = _tools_scenario() if capability == "tools" else _thinking_scenario()
    bodies = (THINKING_ROUND_ONE, THINKING_ROUND_TWO) if capability == "thinking" else (first, second)
    cc.CompatDriver(FakeTransport(*bodies)).run(
        cc.CaseSpec(case_id=f"B:{MODEL_ID}:cap:{capability}", variant="json-hot", scenario=scenario), target)
    return target


def test_a08_the_evaluator_recomputes_and_refuses_the_tampered_material(tmp_path: Path) -> None:
    target = _completed(tmp_path, "clean")
    assert cc.evaluate_case(target)["problems"] == []

    facts = json.loads((target / cc.COMPAT_FILE).read_text(encoding="utf-8"))
    facts["status"] = "passed"  # a stored verdict is never an input

    for path in sorted((target / "round-2").rglob("*"), reverse=True):
        path.unlink()
    (target / "round-2").rmdir()
    with pytest.raises(cc.CompatError):
        cc.evaluate_case(target, facts)

    forged = _completed(tmp_path, "forged")
    request = json.loads((forged / "round-2" / cc.REQUEST_FILE).read_text(encoding="utf-8"))
    request["messages"][1]["tool_calls"][0]["id"] = "call_forged"
    request["messages"][-1]["tool_call_id"] = "call_forged"
    (forged / "round-2" / cc.REQUEST_FILE).write_bytes(cc.canonical_json_bytes(request))
    forged_facts = json.loads((forged / cc.COMPAT_FILE).read_text(encoding="utf-8"))
    forged_facts["material"] = [{"relative_path": path, "sha256": digest}
                                for path, digest in cc.compat_material_refs(forged) if path != cc.COMPAT_FILE]
    assert any("tool_call_id" in problem for problem in cc.evaluate_case(forged, forged_facts)["problems"])


def test_a08_the_evaluator_checks_the_markers_and_never_accepts_length(tmp_path: Path) -> None:
    truncated = json.dumps({"choices": [{"index": 0, "finish_reason": "length", "message": {
        "role": "assistant", "content": "SMS_WEATHER_OK_27"}}], "usage": {}}, ensure_ascii=False).encode("utf-8")
    target = _completed(tmp_path, "length", second=truncated)
    assert any("finish_reason" in problem for problem in cc.evaluate_case(target)["problems"])

    without_marker = json.dumps({"choices": [{"index": 0, "finish_reason": "stop", "message": {
        "role": "assistant", "content": "the weather is fine"}}], "usage": {}}, ensure_ascii=False).encode("utf-8")
    target = _completed(tmp_path, "no-marker", second=without_marker)
    assert any("marker" in problem for problem in cc.evaluate_case(target)["problems"])

    no_reasoning = THINKING_ROUND_ONE.replace('"reasoning_content": "先算乘法"'.encode("utf-8"),
                                              b'"reasoning_content": ""')
    target = tmp_path / "no-reasoning"
    cc.CompatDriver(FakeTransport(no_reasoning, THINKING_ROUND_TWO)).run(
        cc.CaseSpec(case_id=f"B:{MODEL_ID}:cap:thinking", variant="json-hot", scenario=_thinking_scenario()), target)
    assert any("reasoning" in problem for problem in cc.evaluate_case(target)["problems"])

    tagged = THINKING_ROUND_TWO.replace(b'"content": "RESULT=438"', b'"content": "<think>RESULT=438</think>"')
    target = tmp_path / "tagged"
    cc.CompatDriver(FakeTransport(THINKING_ROUND_ONE, tagged)).run(
        cc.CaseSpec(case_id=f"B:{MODEL_ID}:cap:thinking", variant="json-hot", scenario=_thinking_scenario()), target)
    assert any("thinking tag" in problem for problem in cc.evaluate_case(target)["problems"])


def test_a08_the_fixture_material_covers_every_declared_chat_feature(tmp_path: Path) -> None:
    scenarios = cc.compat_scenarios_for(MODEL_ID, ("chat", "tools", "thinking"), ENVELOPE, deadline_seconds=DEADLINE)
    entries = cc.write_fixture_material(tmp_path / "fixtures", scenarios)

    assert [entry["capabilities"] for entry in entries] == [["tools"], ["tools"], ["thinking"], ["thinking"]]
    assert all(entry["artifact"]["sha256"] for entry in entries)
    assert (tmp_path / "fixtures" / "qwen36-27b-tools.json").is_file()

    digest = cc.fixture_set_digest(scenarios)
    assert digest == cc.fixture_set_digest(
        cc.compat_scenarios_for(MODEL_ID, ("chat", "tools", "thinking"), ENVELOPE, deadline_seconds=DEADLINE))
    assert digest != cc.fixture_set_digest(
        cc.compat_scenarios_for(MODEL_ID, ("chat", "tools"), ENVELOPE, deadline_seconds=DEADLINE))
