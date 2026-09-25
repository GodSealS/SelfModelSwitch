"""CT07: the compat two-round scenarios, case specs and driver (TC08).

Tools and thinking are *chat features*, not internal execution operations: they
travel over the public OpenAI-compatible route, so their acceptance material is
built here instead of being smuggled into the internal DTOs. Nothing in this
module belongs to `control_protocol_v1`, and nothing here can be served by the
legacy driver: an old chat/vision case keeps that path, a new-capability case
needs this one.

What this module owns:

* `CompatScenario` — the frozen fixture of one two-round dialogue: the exact
  first-round bytes, the *fixed* tool result (never a subprocess, never a live
  query), the transport, the deadline and the fixed expectation, all covered by
  `digest()` so a changed fixture cannot keep an old verdict;
* `CaseSpec` — one required case plus its variant (`json-hot`, `sse-hot`,
  `json-reload`, `sse-reload`), derived from the declared capabilities only;
* `CompatDriver` — sends round 1, builds round 2 from what came back, and keeps
  every raw byte in a per-round directory.

What it refuses instead of guessing: a transport it did not receive, a second
round without a first round, a streaming round without the aggregator that owns
SSE recombination, a tools scenario without its fixed result, and an evidence
directory that already carries material.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import codecs
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol, Sequence

TRANSPORT_COMPAT = "compat"
COMPAT_CAPABILITIES = ("tools", "thinking")
VARIANTS = ("json-hot", "sse-hot", "json-reload", "sse-reload")
ROUNDS = 2
CASE_PREFIX = "B:"

REQUEST_FILE = "request.json"
RESPONSE_FILE = "response.raw"
BODY_FILE = "response.json"
COMPAT_FILE = "compat.json"

TOOL_NAME = "get_weather"
TOOL_ARGUMENTS = {"city": "Beijing"}
TOOL_RESULT_JSON = '{"city":"Beijing","marker":"SMS_WEATHER_OK_27","temperature_c":23}'
TOOL_MARKER = "SMS_WEATHER_OK_27"
TOOLS_USER_PROMPT = "Call get_weather with city Beijing. After the tool result, reply with exactly its marker."
THINKING_FIRST_PROMPT = "Compute 19 * 23. Finish with RESULT=437."
THINKING_FOLLOWUP_PROMPT = "Using the previous result, add 1. Finish with RESULT=438."
THINKING_FIRST_MARKER = "RESULT=437"
THINKING_FINAL_MARKER = "RESULT=438"
FIXTURE_BUDGET_CAP = 1024


class CompatError(RuntimeError):
    """A refusal to invent acceptance material: the driver reports, never guesses."""


def canonical_json_bytes(value: Any) -> bytes:
    """Deterministic bytes: sorted keys, compact separators, UTF-8, no NaN."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class CompatScenario:
    """One two-round compat fixture (TC08). Every field is part of the identity."""

    fixture_id: str
    capability: Literal["tools", "thinking"]
    transport: str
    model_id: str
    first_request_json: bytes
    tool_result_json: bytes | None
    stream: bool
    deadline_seconds: float
    rounds: int
    expected: Mapping[str, Any]

    def __post_init__(self) -> None:
        for name in ("fixture_id", "capability", "transport", "model_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise CompatError(f"the compat fixture needs a non-empty {name}")
        if self.capability not in COMPAT_CAPABILITIES:
            raise CompatError(f"capability {self.capability!r} has no compat scenario")
        if self.transport != TRANSPORT_COMPAT:
            raise CompatError(f"transport {self.transport!r} is not the compat route")
        if self.rounds != ROUNDS:
            raise CompatError(f"rounds {self.rounds!r}: a compat scenario is {ROUNDS} rounds")
        if not isinstance(self.first_request_json, bytes) or not self.first_request_json:
            raise CompatError("the first round needs the exact request bytes")
        if isinstance(self.deadline_seconds, bool) or not isinstance(self.deadline_seconds, (int, float)) \
                or not math.isfinite(self.deadline_seconds) or self.deadline_seconds <= 0:
            raise CompatError("the scenario needs one frozen positive deadline in seconds")
        if not isinstance(self.stream, bool):
            raise CompatError("stream must be a boolean")
        first_round = json.loads(self.first_request_json)
        if not isinstance(first_round, Mapping):
            raise CompatError("the first round must be a JSON object")
        if first_round.get("model") != self.model_id:
            raise CompatError(f"the first round is addressed to {first_round.get('model')!r}, not {self.model_id!r}")
        if bool(first_round.get("stream")) is not self.stream:
            raise CompatError("the first round's stream flag disagrees with the scenario")
        messages = first_round.get("messages")
        if not isinstance(messages, list) or not messages:
            raise CompatError("the first round needs its messages")
        if any(message.get("role") != "user" for message in messages if isinstance(message, Mapping)):
            # The first round must be a genuine user request: a fixture that already
            # answers itself cannot prove the service produced the call.
            raise CompatError("the first round must carry user messages only: no static tool call or answer")
        if self.capability == "tools":
            if not isinstance(self.tool_result_json, bytes) or not self.tool_result_json:
                raise CompatError("a tools scenario needs its fixed tool_result_json")
            tools = first_round.get("tools")
            if not isinstance(tools, list) or not any(
                tool.get("function", {}).get("name") == TOOL_NAME for tool in tools if isinstance(tool, Mapping)
            ):
                raise CompatError(f"a tools scenario must declare the {TOOL_NAME!r} tool")
        elif self.tool_result_json is not None:
            raise CompatError("a thinking scenario has no tool result")

    def digest(self) -> str:
        """The fixture identity: every field *and* the fixed expectation."""
        payload = {
            "fixture_id": self.fixture_id,
            "capability": self.capability,
            "transport": self.transport,
            "model_id": self.model_id,
            "first_request_json": self.first_request_json.decode("utf-8"),
            "tool_result_json": None if self.tool_result_json is None else self.tool_result_json.decode("utf-8"),
            "stream": self.stream,
            "deadline_seconds": self.deadline_seconds,
            "rounds": self.rounds,
            "expected": dict(self.expected),
        }
        return _sha256(canonical_json_bytes(payload))

    def document(self) -> dict:
        return {
            "fixture_id": self.fixture_id,
            "capability": self.capability,
            "transport": self.transport,
            "model_id": self.model_id,
            "stream": self.stream,
            "rounds": self.rounds,
            "deadline_seconds": self.deadline_seconds,
            "first_request_sha256": _sha256(self.first_request_json),
            "tool_result_sha256": None if self.tool_result_json is None else _sha256(self.tool_result_json),
            "expected": dict(self.expected),
            "scenario_sha256": self.digest(),
        }


@dataclass(frozen=True)
class CaseSpec:
    """One required compat case in one variant, bound to one scenario."""

    case_id: str
    variant: str
    scenario: CompatScenario

    def __post_init__(self) -> None:
        if self.variant not in VARIANTS:
            raise CompatError(f"variant {self.variant!r} is not one of {', '.join(VARIANTS)}")
        expected_case = f"{CASE_PREFIX}{self.scenario.model_id}:cap:{self.scenario.capability}"
        if self.case_id != expected_case:
            raise CompatError(f"case id {self.case_id!r} does not match the derived case {expected_case!r}")
        stream = self.variant.startswith("sse-")
        if stream is not self.scenario.stream:
            raise CompatError(f"variant {self.variant!r} and the scenario's stream={self.scenario.stream!r} disagree")

    @property
    def reload(self) -> bool:
        return self.variant.endswith("-reload")


@dataclass(frozen=True)
class CompatRound:
    """What the transport hands back: the service's own bytes and nothing else."""

    status: int
    raw: bytes
    request_id: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)


class CompatTransport(Protocol):
    """The one call the driver may make: post bytes, get bytes."""

    def chat(self, request_json: bytes, *, stream: bool, deadline: float) -> CompatRound: ...


class CompatAggregator(Protocol):
    """SSE recombination (CT08); without it a stream round cannot be read."""

    def aggregate(self, raw: bytes) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class CompatRun:
    """What a completed two-round scenario produced, before any verdict."""

    case_id: str
    variant: str
    scenario_digest: str
    tool_call_id: str | None
    tool_name: str | None
    first_content: str
    first_reasoning: str
    final_content: str
    final_reasoning: str
    finish_reasons: tuple[str, ...]
    location: Path

    def document(self) -> dict:
        field_names = ("case_id", "variant", "scenario_digest", "tool_call_id", "tool_name", "first_content",
                       "first_reasoning", "final_content", "final_reasoning")
        return {name: getattr(self, name) for name in field_names}


def tools_scenario(model_id: str, *, envelope, stream: bool, deadline_seconds: float,
                   tool_result_json: bytes | None = TOOL_RESULT_JSON.encode("utf-8")) -> CompatScenario:
    """The fixed two-round tool dialogue of plan/tool-calling-and-reasoning/acceptance.md §5."""
    budget = min(FIXTURE_BUDGET_CAP, int(envelope.max_output_tokens))
    body = {
        "model": model_id,
        "messages": [{"role": "user", "content": TOOLS_USER_PROMPT}],
        "tools": [{"type": "function", "function": {
            "name": TOOL_NAME,
            "description": "Return a fixed weather fixture for a city.",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                           "required": ["city"], "additionalProperties": False},
        }}],
        "tool_choice": {"type": "function", "function": {"name": TOOL_NAME}},
        "parallel_tool_calls": False,
        "max_tokens": budget,
        "stream": stream,
    }
    return CompatScenario(
        fixture_id=f"{model_id}-tools{"-sse" if stream else ""}",
        capability="tools",
        transport=TRANSPORT_COMPAT,
        model_id=model_id,
        first_request_json=canonical_json_bytes(body),
        tool_result_json=tool_result_json,
        stream=stream,
        deadline_seconds=float(deadline_seconds),
        rounds=ROUNDS,
        expected={"tool_name": TOOL_NAME, "tool_arguments": dict(TOOL_ARGUMENTS),
                  "final_content": TOOL_MARKER, "requires_reasoning": False},
    )


def thinking_scenario(model_id: str, *, envelope, stream: bool, deadline_seconds: float) -> CompatScenario:
    """The fixed two-question thinking dialogue: no tools, reasoning is carried back."""
    budget = min(FIXTURE_BUDGET_CAP, int(envelope.max_output_tokens))
    body = {
        "model": model_id,
        "messages": [{"role": "user", "content": THINKING_FIRST_PROMPT}],
        "max_tokens": budget,
        "stream": stream,
    }
    return CompatScenario(
        fixture_id=f"{model_id}-thinking{"-sse" if stream else ""}",
        capability="thinking",
        transport=TRANSPORT_COMPAT,
        model_id=model_id,
        first_request_json=canonical_json_bytes(body),
        tool_result_json=None,
        stream=stream,
        deadline_seconds=float(deadline_seconds),
        rounds=ROUNDS,
        expected={"first_content_contains": THINKING_FIRST_MARKER,
                  "final_content": THINKING_FINAL_MARKER, "requires_reasoning": True},
    )


def compat_scenarios_for(model_id: str, capabilities: Sequence[str], envelope, *,
                         deadline_seconds: float) -> tuple[CompatScenario, ...]:
    """One scenario per declared chat feature; old capabilities keep the old driver."""
    declared = set(capabilities)
    scenarios: list[CompatScenario] = []
    for capability in COMPAT_CAPABILITIES:
        if capability not in declared:
            continue
        for stream in (False, True):
            builder = tools_scenario if capability == "tools" else thinking_scenario
            scenarios.append(builder(model_id, envelope=envelope, stream=stream,
                                     deadline_seconds=deadline_seconds))
    return tuple(scenarios)


def case_specs_for(model_id: str, capabilities: Sequence[str], envelope, *, deadline_seconds: float,
                   variants: Sequence[str] = VARIANTS) -> tuple[CaseSpec, ...]:
    """The required compat cases with every variant, derived from the registration."""
    declared = set(capabilities)
    specs: list[CaseSpec] = []
    for capability in COMPAT_CAPABILITIES:
        if capability not in declared:
            continue
        for variant in variants:
            builder = tools_scenario if capability == "tools" else thinking_scenario
            scenario = builder(model_id, envelope=envelope, stream=variant.startswith("sse-"),
                               deadline_seconds=deadline_seconds)
            specs.append(CaseSpec(case_id=f"{CASE_PREFIX}{model_id}:cap:{capability}", variant=variant,
                                  scenario=scenario))
    return tuple(specs)


def assistant_message_of(body: Mapping[str, Any]) -> dict:
    """The assistant turn of one completion body, exactly as it arrived."""
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise CompatError("the completion carried no choice")
    message = choices[0].get("message") if isinstance(choices[0], Mapping) else None
    if not isinstance(message, Mapping):
        raise CompatError("the completion carried no assistant message")
    return dict(message)


def first_tool_call(body: Mapping[str, Any]) -> dict:
    """The tool call round 1 really produced; a scenario without one cannot continue."""
    calls = assistant_message_of(body).get("tool_calls")
    if not isinstance(calls, list) or not calls:
        raise CompatError("round 1 produced no tool call: round 2 cannot be built from it")
    call = calls[0]
    call_id, function = call.get("id"), call.get("function")
    if not isinstance(call_id, str) or not call_id:
        raise CompatError("the tool call of round 1 carries no id")
    if not isinstance(function, Mapping) or function.get("name") != TOOL_NAME:
        raise CompatError(f"round 1 called {function.get('name')!r}, not {TOOL_NAME!r}")
    return dict(call)


def build_second_round(scenario: CompatScenario, *, first_request: Mapping[str, Any],
                       first_body: Mapping[str, Any]) -> bytes:
    """Round 2: the original dialogue plus the real answer plus the fixed result.

    The assistant turn is copied byte-for-byte (`reasoning_content` included), so
    history really travels back; the tools scenario additionally drops
    `tools` / `tool_choice` / `parallel_tool_calls` to exercise the capability
    check from the *history* and not from the current request.
    """
    messages = [dict(message) for message in first_request.get("messages", [])]
    assistant = assistant_message_of(first_body)
    if scenario.capability == "tools":
        call_id = first_tool_call(first_body)["id"]
        messages.append(assistant)
        messages.append({"role": "tool", "tool_call_id": call_id,
                         "content": scenario.tool_result_json.decode("utf-8")})
    else:
        messages.append(assistant)
        messages.append({"role": "user", "content": THINKING_FOLLOWUP_PROMPT})

    body = dict(first_request)
    body["messages"] = messages
    for dropped in ("tools", "tool_choice", "parallel_tool_calls"):
        body.pop(dropped, None)
    return canonical_json_bytes(body)


class CompatDriver:
    """Run one compat scenario, keeping the raw material of both rounds."""

    def __init__(self, transport: CompatTransport | None, *, aggregator: CompatAggregator | None = None,
                 clock: Any | None = None) -> None:
        self.transport = transport
        self.aggregator = aggregator
        self._clock = clock if clock is not None else _SystemClock()

    def run(self, spec: CaseSpec | None, evidence_dir: str | Path) -> CompatRun:
        if self.transport is None:
            raise CompatError("the compat driver needs a transport: nothing is executed without the route")
        if not isinstance(spec, CaseSpec):
            raise CompatError("the compat driver needs the scenario it runs; refusing to pick one")
        scenario = spec.scenario
        if scenario.stream and self.aggregator is None:
            raise CompatError("a streaming round needs the SSE aggregator; it is never improvised here")
        target = self._prepare_directory(evidence_dir)

        first_request = json.loads(scenario.first_request_json)
        first_round = self._post(scenario, scenario.first_request_json)
        first_body = self._body(scenario, first_round)
        self._write_round(target / "round-1", scenario.first_request_json, first_round, first_body)

        call = first_tool_call(first_body) if scenario.capability == "tools" else None
        second_request_json = build_second_round(scenario, first_request=first_request, first_body=first_body)
        second_round = self._post(scenario, second_request_json)
        second_body = self._body(scenario, second_round)
        self._write_round(target / "round-2", second_request_json, second_round, second_body)

        first_message = assistant_message_of(first_body)
        final_message = assistant_message_of(second_body)
        run = CompatRun(
            case_id=spec.case_id,
            variant=spec.variant,
            scenario_digest=scenario.digest(),
            tool_call_id=call["id"] if call else None,
            tool_name=TOOL_NAME if call else None,
            first_content=first_message.get("content") or "",
            first_reasoning=first_message.get("reasoning_content") or "",
            final_content=final_message.get("content") or "",
            final_reasoning=final_message.get("reasoning_content") or "",
            finish_reasons=(_finish_reason(first_body), _finish_reason(second_body)),
            location=target,
        )
        self._write_document(target, scenario, spec, [first_round, second_round],
                             [first_body, second_body], run)
        return run

    # -- internals ---------------------------------------------------------

    def _post(self, scenario: CompatScenario, request_json: bytes) -> CompatRound:
        round_record = self.transport.chat(request_json, stream=scenario.stream,
                                           deadline=scenario.deadline_seconds)
        if not isinstance(round_record, CompatRound):
            raise CompatError("the transport must return the raw round it received")
        if round_record.status != 200:
            raise CompatError(f"the compat route answered {round_record.status}, not 200")
        return round_record

    def _body(self, scenario: CompatScenario, round_record: CompatRound) -> Mapping[str, Any]:
        if not scenario.stream:
            return json.loads(round_record.raw.decode("utf-8"))
        # A streamed round is recombined by a *fresh* aggregator that owns SSE:
        # recombination state belongs to one stream, never to the driver.
        aggregator = self.aggregator() if callable(self.aggregator) else self.aggregator
        if aggregator is None or not hasattr(aggregator, "aggregate"):
            raise CompatError("a streaming round needs the SSE aggregator; it is never improvised here")
        return aggregator.aggregate(round_record.raw)

    def _prepare_directory(self, evidence_dir: str | Path) -> Path:
        target = Path(evidence_dir)
        if target.exists() and any(target.iterdir()):
            raise CompatError(f"the evidence directory {target} must be empty: material is never overwritten")
        target.mkdir(parents=True, exist_ok=True)
        return target

    def _write_round(self, directory: Path, request_json: bytes, round_record: CompatRound,
                     body: Mapping[str, Any]) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / REQUEST_FILE).write_bytes(request_json)
        (directory / RESPONSE_FILE).write_bytes(round_record.raw)
        (directory / BODY_FILE).write_bytes(canonical_json_bytes(body))

    def _write_document(self, target: Path, scenario: CompatScenario, spec: CaseSpec,
                        rounds: Sequence[CompatRound], bodies: Sequence[Mapping[str, Any]],
                        run: CompatRun) -> None:
        records = []
        for index, (round_record, body) in enumerate(zip(rounds, bodies), start=1):
            usage = body.get("usage") if isinstance(body.get("usage"), Mapping) else {}
            records.append({
                "round": index,
                "request_sha256": _sha256((target / f"round-{index}" / REQUEST_FILE).read_bytes()),
                "response_sha256": _sha256(round_record.raw),
                "request_id": round_record.request_id,
                "status": round_record.status,
                "finish_reason": _finish_reason(body),
                "usage": {key: usage.get(key) for key in ("prompt_tokens", "completion_tokens", "total_tokens")},
                "started_utc": self._stamp(),
            })
        document = dict(run.document())
        document.update({
            "capability": scenario.capability,
            "location": str(target),
            "reload": spec.reload,
            "rounds": records,
            "tool_result_sha256": None if scenario.tool_result_json is None else _sha256(scenario.tool_result_json),
            "expected": dict(scenario.expected),
            "material": [{"relative_path": path, "sha256": digest} for path, digest in compat_material_refs(target)],
        })
        (target / COMPAT_FILE).write_bytes(canonical_json_bytes(document))

    def _stamp(self) -> str:
        return self._clock.utc_now().astimezone(timezone.utc).isoformat()


def _finish_reason(body: Mapping[str, Any]) -> str | None:
    choices = body.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], Mapping) else None
    return choice.get("finish_reason") if choice else None


class _SystemClock:
    def utc_now(self) -> datetime:
        return datetime.now(timezone.utc)


def compat_material_refs(root: str | Path) -> tuple[tuple[str, str], ...]:
    """Every file of the scenario and its digest: the evaluator re-reads these."""
    base = Path(root)
    if not base.is_dir():
        raise CompatError(f"no compat material at {base}")
    refs = []
    for path in sorted(candidate for candidate in base.rglob("*") if candidate.is_file()):
        refs.append((path.relative_to(base).as_posix(), _sha256(path.read_bytes())))
    return tuple(refs)


def link_problems(document: Mapping[str, Any]) -> list[str]:
    """Structural problems the recomputation can see without trusting a stored verdict."""
    problems: list[str] = []
    if not document.get("tool_call_id"):
        return problems  # a thinking scenario has no tool to link
    second_body = _recorded_body(document, round_index=2)
    second_request = _recorded_request(document, round_index=2)
    if second_request is None or second_body is None:
        problems.append("round 2: the material cannot be re-read")
        return problems
    first_call_ids = {call.get("id") for call in second_request["messages"][1].get("tool_calls", [])}
    if first_call_ids != {document["tool_call_id"]}:
        problems.append("round 2: the carried tool_call_id does not come from round 1")
    results = [message for message in second_request["messages"] if message.get("role") == "tool"]
    if len(results) != 1 or results[0].get("tool_call_id") != document["tool_call_id"]:
        problems.append("round 2: the tool result is not bound to the tool_call_id of round 1")
    if results and results[0].get("content") != TOOL_RESULT_JSON:
        problems.append("round 2: the tool result is not the fixed fixture result")
    return problems


def verify_material(root: str | Path, document: Mapping[str, Any] | None = None) -> dict:
    """Re-read the material of one scenario; a missing or rewritten round is refused."""
    base = Path(root)
    facts = dict(document) if document is not None else json.loads((base / COMPAT_FILE).read_text("utf-8"))
    facts = {**facts, "location": str(base)}
    rounds = list(facts.get("rounds") or [])
    if len(rounds) != ROUNDS:
        raise CompatError(f"the record carries {len(rounds)} rounds, a compat scenario has {ROUNDS}")
    for index, row in enumerate(rounds, start=1):
        directory = base / f"round-{index}"
        if not directory.is_dir():
            raise CompatError(f"round {index}: the material directory is missing")
        for name in (REQUEST_FILE, RESPONSE_FILE):
            if not (directory / name).is_file():
                raise CompatError(f"round {index}: {name} is missing")
        raw = (directory / RESPONSE_FILE).read_bytes()
        if row.get("response_sha256") and row["response_sha256"] != _sha256(raw):
            raise CompatError(f"round {index}: the raw response no longer matches the recorded digest")
    recorded = dict(compat_material_refs(base))
    for entry in facts.get("material", ()):
        if recorded.get(entry["relative_path"]) != entry["sha256"]:
            raise CompatError(f"{entry['relative_path']}: the material no longer matches the recorded digest")
    return {"problems": link_problems(facts), "rounds": len(rounds)}


def _recorded_request(document: Mapping[str, Any], *, round_index: int) -> dict | None:
    """The exact body of one round, re-read from the material instead of memory."""
    for entry in document.get("material", ()):
        if entry.get("relative_path") == f"round-{round_index}/{REQUEST_FILE}":
            target = _root_of(document) / entry["relative_path"]
            if not target.is_file():
                return None
            document_root = target
            return json.loads(document_root.read_text(encoding="utf-8"))
    return None


def _recorded_body(document: Mapping[str, Any], *, round_index: int) -> dict | None:
    """The parsed completion of one round, as it was persisted."""
    for entry in document.get("material", ()):
        if entry.get("relative_path") == f"round-{round_index}/{BODY_FILE}":
            target = _root_of(document) / entry["relative_path"]
            return json.loads(target.read_text(encoding="utf-8")) if target.is_file() else None
    return None


def _root_of(document: Mapping[str, Any]) -> Path:
    location = document.get("location")
    if isinstance(location, str) and location:
        return Path(location)
    raise CompatError("the record carries no material root to re-read from")


def relay_scenario(scenario: CompatScenario, **changes: Any) -> CompatScenario:
    """A new scenario with one change; nothing about a fixture is edited in place."""
    return replace(scenario, **changes)


# ---------------------------------------------------------------------------
# CT08: the SSE aggregator (TC08) — recombination is material, not a guess.
# ---------------------------------------------------------------------------


def _index_of(choice: Mapping[str, Any], where: str) -> int:
    index = choice.get("index")
    if isinstance(index, bool) or not isinstance(index, int):
        raise CompatError(f"{where}: index must be an integer, got {index!r}")
    if index != 0:
        raise CompatError(f"{where}: index {index} is not the one generated choice")
    return index


def _string_fragment(value: Any, where: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise CompatError(f"{where}: a text fragment must be a string, got {type(value).__name__}")
    return value


class SseAggregator:
    """Recombine one SSE completion: incremental UTF-8, per-index, once.

    The service passes the stream through untouched, so the acceptance side owns
    recombination. Every fragment counts: `id`/`type` may appear only in the first
    fragment (a later, different id is a contradiction), `name` and `arguments`
    are concatenated in arrival order, `content`/`reasoning_content` are separate
    and `null` adds nothing. `[DONE]` happens exactly once and ends the stream.
    """

    def __init__(self, *, event_limit: int = 100000) -> None:
        if isinstance(event_limit, bool) or not isinstance(event_limit, int) or event_limit < 1:
            raise CompatError("the event limit must be a positive integer")
        self.event_limit = event_limit
        self._decoder = codecs.getincrementaldecoder("utf-8")()
        self._buffer = ""
        self._events = 0
        self._done = False
        self._content = ""
        self._reasoning = ""
        self._finish_reason: str | None = None
        self._usage: dict[str, Any] = {}
        self._completion_id: str | None = None
        self._calls: dict[int, dict[str, Any]] = {}

    # -- feeding ----------------------------------------------------------

    def feed(self, chunk: bytes) -> None:
        if self._done:
            if chunk.strip():
                raise CompatError("the stream kept sending data after [DONE]")
            return
        if not isinstance(chunk, (bytes, bytearray)):
            raise CompatError("the stream feeds bytes, not text")
        text = self._decoder.decode(bytes(chunk))
        self._buffer += text
        while "\n\n" in self._buffer:
            raw_event, self._buffer = self._buffer.split("\n\n", 1)
            self._consume(raw_event)

    def aggregate(self, raw: bytes) -> Mapping[str, Any]:
        self.feed(raw)
        return self.finish()

    def finish(self) -> dict:
        """Close the stream: a truncation is a failure, never a partial answer."""
        if not self._done:
            if self._buffer.strip():
                self._consume(self._buffer)
                self._buffer = ""
            if not self._done:
                raise CompatError("the stream ended without [DONE]")
        if self._finish_reason is None:
            raise CompatError("the stream ended without a terminal finish_reason")
        message: dict[str, Any] = {"role": "assistant", "content": self._content}
        if self._reasoning:
            message["reasoning_content"] = self._reasoning
        if self._calls:
            message["tool_calls"] = self._ready_calls()
        choice: dict[str, Any] = {"index": 0, "finish_reason": self._finish_reason, "message": message}
        body: dict[str, Any] = {"choices": [choice]}
        if self._completion_id is not None:
            body["id"] = self._completion_id
        if self._usage:
            body["usage"] = dict(self._usage)
        return body

    # -- internals --------------------------------------------------------

    def _consume(self, raw_event: str) -> None:
        if self._done:
            raise CompatError("the stream kept sending data after [DONE]")
        payload = "\n".join(line[len("data:"):].lstrip(" ") for line in raw_event.splitlines()
                            if line.startswith("data:"))
        if not payload.strip():
            return
        self._events += 1
        if self._events > self.event_limit:
            raise CompatError(f"the stream exceeded {self.event_limit} events")
        if payload.strip() == "[DONE]":
            self._done = True
            return
        try:
            event = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise CompatError(f"the stream carried unparsable JSON: {exc}") from exc
        if not isinstance(event, Mapping):
            raise CompatError("an SSE event must be a JSON object")
        if isinstance(event.get("id"), str):
            self._completion_id = event["id"]
        usage = event.get("usage")
        if isinstance(usage, Mapping):
            self._usage.update(usage)
        choices = event.get("choices")
        if choices is None:
            return  # a usage-only event carries no choice
        if not isinstance(choices, list):
            raise CompatError("the stream event's choices must be a list")
        if len(choices) > 1:
            raise CompatError(f"the stream produced {len(choices)} generated choices; parallel=false allows one")
        for choice in choices:
            if not isinstance(choice, Mapping):
                raise CompatError("a streamed choice must be an object")
            _index_of(choice, "the streamed choice")
            self._apply(choice.get("delta"), choice.get("finish_reason"))

    def _apply(self, delta: Any, finish_reason: Any) -> None:
        if delta is None:
            delta = {}
        if not isinstance(delta, Mapping):
            raise CompatError("a streamed delta must be an object")
        self._content += _string_fragment(delta.get("content"), "the streamed content")
        self._reasoning += _string_fragment(delta.get("reasoning_content"), "the streamed reasoning_content")
        for call in delta.get("tool_calls") or ():
            if not isinstance(call, Mapping):
                raise CompatError("a streamed tool call must be an object")
            index = call.get("index")
            if isinstance(index, bool) or not isinstance(index, int):
                raise CompatError(f"a streamed tool call needs an integer index, got {index!r}")
            state = self._calls.setdefault(index, {"index": index, "id": None, "type": None,
                                                   "function": {"name": "", "arguments": ""}})
            for key in ("id", "type"):
                value = call.get(key)
                if value is None:
                    continue
                if not isinstance(value, str):
                    raise CompatError(f"a streamed tool call's {key} must be a string")
                if state[key] is not None and state[key] != value:
                    raise CompatError(f"the streamed tool call contradicts its own {key}")
                state[key] = value
            function = call.get("function")
            if function is None:
                continue
            if not isinstance(function, Mapping):
                raise CompatError("a streamed tool call's function must be an object")
            for key in ("name", "arguments"):
                value = function.get(key)
                if value is None:
                    continue
                if not isinstance(value, str):
                    raise CompatError(f"a streamed tool call's {key} must be a string")
                state["function"][key] = state["function"][key] + value
        if finish_reason is not None:
            if not isinstance(finish_reason, str):
                raise CompatError("a finish_reason must be a string")
            self._finish_reason = finish_reason

    def _ready_calls(self) -> list[dict[str, Any]]:
        calls = []
        for index in sorted(self._calls):
            call = self._calls[index]
            for key in ("id", "type"):
                if not call[key]:
                    raise CompatError(f"the streamed tool call {index} ended without a {key}")
            if not call["function"]["name"]:
                raise CompatError(f"the streamed tool call {index} ended without a name")
            calls.append(call)
        return calls


# ---------------------------------------------------------------------------
# CT08: the independent evaluator — verdicts come from raw material only.
# ---------------------------------------------------------------------------


#: Template markers the deployment must keep out of the answer: their presence
#: means thinking was not separated, so the answer is not the final one.
THINKING_TAGS = ("<think>", "</think>", "<thinking>", "</thinking>", "<|thinking|>")


def evaluate_case(root: str | Path, document: Mapping[str, Any] | None = None) -> dict:
    """Recompute one compat scenario from its raw material and report problems.

    Nothing here reads a stored status: a verdict is what the recomputation
    produces. A missing, rewritten or re-hashed round is refused outright; the
    returned `problems` are the semantic reasons this case is not a pass.
    """
    base = Path(root)
    facts = dict(document) if document is not None else json.loads((base / COMPAT_FILE).read_text("utf-8"))
    facts = {**facts, "location": str(base)}
    recorded = dict(compat_material_refs(base))
    for entry in facts.get("material", ()):
        if recorded.get(entry["relative_path"]) != entry["sha256"]:
            raise CompatError(f"{entry['relative_path']}: the material no longer matches the recorded digest")
    first_request = _read_json(base / "round-1" / REQUEST_FILE)
    first_body = _read_json(base / "round-1" / BODY_FILE)
    second_request = _read_json(base / "round-2" / REQUEST_FILE)
    second_body = _read_json(base / "round-2" / BODY_FILE)

    problems: list[str] = []
    problems.extend(link_problems(facts))
    problems.extend(_round_problems(facts, first_request, first_body, second_request, second_body))
    return {"problems": problems,
            "checks": {"rounds": len(facts.get("rounds", ())), "capability": facts.get("capability"),
                       "variant": facts.get("variant")}}


def _read_json(path: Path) -> dict:
    if not path.is_file():
        raise CompatError(f"{path.parent.name}: {path.name} is missing")
    return json.loads(path.read_text(encoding="utf-8"))


def _round_problems(facts: Mapping[str, Any], first_request: Mapping[str, Any], first_body: Mapping[str, Any],
                    second_request: Mapping[str, Any], second_body: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    capability = str(facts.get("capability", ""))
    expected = dict(facts.get("expected", {}))
    first_reason = _finish_reason(first_body)
    final_reason = _finish_reason(second_body)
    final_content = (assistant_message_of(second_body).get("content") or "").strip()

    if capability == "tools":
        if first_reason != "tool_calls":
            problems.append(f"round 1: finish_reason {first_reason!r} is not tool_calls")
        call = _call_of(first_body)
        if call is None:
            problems.append("round 1: no tool call was produced")
        else:
            if call.get("function", {}).get("name") != expected.get("tool_name"):
                problems.append("round 1: the tool call is not the fixed tool")
            arguments = call.get("function", {}).get("arguments")
            try:
                parsed = json.loads(arguments) if isinstance(arguments, str) else None
            except json.JSONDecodeError:
                parsed = None
            if parsed != expected.get("tool_arguments"):
                problems.append("round 1: the tool arguments are not the fixed fixture arguments")
        if final_reason != "stop":
            problems.append(f"round 2: finish_reason {final_reason!r} is not stop (length is not a pass)")
        if final_content != expected.get("final_content"):
            problems.append(f"round 2: the final answer is not the fixed marker {expected.get('final_content')!r}")
    elif capability == "thinking":
        first_content = (assistant_message_of(first_body).get("content") or "").strip()
        if not (assistant_message_of(first_body).get("reasoning_content") or "").strip():
            problems.append("round 1: the thinking round produced no reasoning")
        if expected.get("first_content_contains") and expected["first_content_contains"] not in first_content:
            problems.append(f"round 1: the answer does not contain {expected['first_content_contains']!r}")
        if final_reason != "stop":
            problems.append(f"round 2: finish_reason {final_reason!r} is not stop (length is not a pass)")
        if final_content != expected.get("final_content"):
            problems.append(f"round 2: the final answer is not {expected.get('final_content')!r}")
    else:
        problems.append(f"{capability!r} is not a compat capability")

    for marker in THINKING_TAGS:
        if marker in final_content:
            problems.append(f"round 2: the answer carries the thinking tag {marker!r}")
            break
    if final_reason in ("length", "content_filter"):
        problems.append(f"round 2: finish_reason {final_reason!r} is not a complete answer")
    if second_request.get("messages")[:1] != first_request.get("messages")[:1]:
        problems.append("round 2: the dialogue no longer starts with the first round's own messages")
    return problems


def _call_of(body: Mapping[str, Any]) -> dict | None:
    calls = assistant_message_of(body).get("tool_calls")
    return dict(calls[0]) if isinstance(calls, list) and calls and isinstance(calls[0], Mapping) else None


def write_fixture_material(directory: str | Path, scenarios: Sequence[CompatScenario]) -> list[dict]:
    """The candidate-shape fixture entries of these scenarios (size and hash)."""
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    entries: list[dict] = []
    for scenario in scenarios:
        payload = canonical_json_bytes(scenario.document())
        name = f"{scenario.fixture_id}.json"
        path = target / name
        if path.exists() and path.read_bytes() != payload:
            raise CompatError(f"the fixture material {name} already exists with different bytes")
        path.write_bytes(payload)
        entries.append({"fixture_id": scenario.fixture_id, "capabilities": [scenario.capability],
                        "artifact": {"relative_path": name, "size_bytes": len(payload),
                                     "sha256": _sha256(payload)}})
    return entries


def fixture_set_digest(scenarios: Sequence[CompatScenario]) -> str:
    """The fixture-set digest: one value for the whole set, order-independent."""
    return _sha256(canonical_json_bytes(sorted(scenario.digest() for scenario in scenarios)))
