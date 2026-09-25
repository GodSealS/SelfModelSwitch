"""P23: the per-model B-case executor.

The executor only ever applies actions through the official API surface
(`CaseDriver`: load / start / execute / cancel / stop), never by editing a
registry or poking scheduler state. For every model it drives the six case
kinds of plan/06-acceptance.md §3 — `load`, `infer`, `envelope`, `cancel`,
`stop`, `reload` — plus one `cap:<capability>` case per declared capability,
and it owns the accounting that the acceptance document demands:

* at least three **independent cold starts** and three **full reload rounds**;
* the `envelope` round must reach every declared boundary dimension in the
  *same* request (text + vision + concurrency together, never split);
* a passing inference-shaped case needs the provider, attributable device
  activity from **raw samples** and a real output — all three;
* an exception leaves a failed attempt **and** a cleanup record; a case the
  executor cannot prove stays `unknown` and is never converted to `passed`.

Real execution happens at P29 (`acceptance run --layers B`); this module is the
logic, tested with injected drivers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import math
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from ..evidence_contracts import BACKEND_CASE_MIN_COLD_STARTS, BACKEND_CASE_MIN_RELOAD_ROUNDS
from . import chat_compat as cc
from .chat_compat import COMPAT_CAPABILITIES, TRANSPORT_COMPAT
from .fixtures import FillerSpec, Fixture, FixtureError, boundary_shortfalls, fixtures_for
from .materials import CaseMaterialSink

CASE_KINDS = ("load", "infer", "envelope", "cancel", "stop", "reload")
STATUSES = ("passed", "failed", "unknown", "not_run")
INFERENCE_KINDS = ("infer", "envelope", "capability")


class BackendCaseError(RuntimeError):
    """The executor refuses to run a case it cannot attribute."""


class CaseDriver(Protocol):
    """The official API surface the executor is allowed to use (P18 control API)."""

    def load(self, model_id: str, *, cold: bool) -> Mapping[str, Any]: ...

    def start(self, model_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def execute(self, model_id: str, request: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def cancel(self, model_id: str, execution_id: str) -> Mapping[str, Any]: ...

    def stop(self, model_id: str) -> Mapping[str, Any]: ...

    def cleanup(self, model_id: str) -> Mapping[str, Any]: ...


class CompatCaseRunner(Protocol):
    """What the two-round compat driver offers the B layer (CT07).

    Tools and thinking are chat features of the public route, so their case is
    not an `execute` of an internal execution operation: the driver runs its own
    two rounds and reports what it saw.
    """

    def run_case(self, *, model_id: str, capability: str, variant: str,
                 directory: Path) -> Mapping[str, Any]: ...


def _utc(clock) -> str:
    return clock.utc_now().astimezone(timezone.utc).isoformat()


@dataclass(frozen=True)
class Attempt:
    """One try at one case; failures are kept, never replaced."""

    case_id: str
    attempt: int
    status: str
    started_utc: str
    ended_utc: str
    duration_seconds: float
    facts: Mapping[str, Any] = field(default_factory=dict)
    failure: str | None = None
    cleanup: Mapping[str, Any] | None = None
    problems: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise BackendCaseError(f"unknown attempt status {self.status!r}")
        if self.status == "passed" and (self.failure or self.problems):
            raise BackendCaseError("a passed attempt cannot carry a failure or unsolved problems")

    def document(self) -> dict:
        return {"case_id": self.case_id, "attempt": self.attempt, "status": self.status,
                "started_utc": self.started_utc, "ended_utc": self.ended_utc,
                "duration_seconds": round(self.duration_seconds, 3), "facts": dict(self.facts),
                "failure": self.failure, "cleanup": dict(self.cleanup) if self.cleanup else None,
                "problems": list(self.problems)}


def capability_output_problems(capability: str, output: Mapping[str, Any]) -> list[str]:
    """Protocol/shape/finite/range of the body the deployment publishes.

    The published output *is* the runtime's own response: the chat body carries
    `choices[].message.content`, embeddings carry `data[].embedding` and rerank
    carries `results[].relevance_score`. Checking a normalized shape instead would
    report every healthy answer as empty.
    """
    problems: list[str] = []
    if capability == "chat" or capability == "vision":
        if not _chat_content(output).strip():
            problems.append(f"{capability}: the response carries no non-empty content")
    elif capability == "embeddings":
        vectors = _embedding_vectors(output)
        if not vectors:
            problems.append("embeddings: no vectors returned")
            return problems
        dimensions = {len(vector) for vector in vectors}
        if len(dimensions) != 1 or 0 in dimensions:
            problems.append("embeddings: vectors have inconsistent or empty dimensions")
            return problems
        for index, vector in enumerate(vectors):
            if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                   for value in vector):
                problems.append(f"embeddings: vector {index} has non-finite or non-numeric values")
                break
    elif capability == "rerank":
        results = output.get("results")
        if not isinstance(results, list) or not results:
            problems.append("rerank: no results returned")
            return problems
        for index, entry in enumerate(results):
            score = entry.get("relevance_score") if isinstance(entry, Mapping) else None
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
                problems.append(f"rerank: result {index} has a non-finite score")
                break
    else:
        problems.append(f"{capability!r} is not an acceptance capability")
    return problems


def _chat_content(output: Mapping[str, Any]) -> str:
    """The answer text of a chat completion body, whichever choice carried it."""
    choices = output.get("choices")
    if not isinstance(choices, list):
        return ""
    for entry in choices:
        if not isinstance(entry, Mapping):
            continue
        message = entry.get("message")
        content = message.get("content") if isinstance(message, Mapping) else entry.get("text")
        if isinstance(content, str) and content:
            return content
    return ""


def _embedding_vectors(output: Mapping[str, Any]) -> list[list]:
    """The vectors of an embeddings body, in the order the runtime returned them."""
    data = output.get("data")
    if not isinstance(data, list):
        return []
    vectors = []
    for row in data:
        vector = row.get("embedding") if isinstance(row, Mapping) else None
        if not isinstance(vector, list):
            return []
        vectors.append(vector)
    return vectors


def attribution_problems(kind: str, facts: Mapping[str, Any]) -> list[str]:
    """Provider + attributable device activity + real output (plan/06 §4)."""
    problems: list[str] = []
    if not facts.get("provider"):
        problems.append("no provider is recorded for this action")
    if kind in INFERENCE_KINDS:
        activity = facts.get("device_activity")
        if not isinstance(activity, Mapping) or not activity.get("raw_samples"):
            problems.append("no attributable device activity: raw samples are required")
        output = facts.get("output")
        if not isinstance(output, Mapping) or not output:
            problems.append("no real output was produced")
    if kind == "load" and not isinstance(facts.get("instance"), Mapping):
        problems.append("a load must report the instance it created")
    if kind == "stop" and facts.get("stop_proven") is not True:
        problems.append("a stop is only passed when the instance is proven stopped")
    if kind == "cancel" and facts.get("cancelled") is not True:
        problems.append("a cancel is only passed with evidence the execution stopped")
    return problems


class CaseExecutor:
    """Drive the B case matrix for the registered models, recording every attempt."""

    def __init__(self, driver: CaseDriver, *, clock=None, collector: CaseMaterialSink | None = None,
                 cold_starts: int = BACKEND_CASE_MIN_COLD_STARTS,
                 reload_rounds: int = BACKEND_CASE_MIN_RELOAD_ROUNDS,
                 filler_of: Mapping[str, FillerSpec] | None = None,
                 compat: CompatCaseRunner | None = None,
                 compat_variant: str = "json-hot") -> None:
        if cold_starts < BACKEND_CASE_MIN_COLD_STARTS:
            raise BackendCaseError("the acceptance minimum is three independent cold starts")
        if reload_rounds < BACKEND_CASE_MIN_RELOAD_ROUNDS:
            raise BackendCaseError("the acceptance minimum is three full reload rounds")
        if compat_variant not in cc.VARIANTS:
            raise BackendCaseError(f"the compat variant must be one of {', '.join(cc.VARIANTS)}")
        self.driver = driver
        self.compat = compat
        self.compat_variant = compat_variant
        self._clock = clock if clock is not None else _SystemClock()
        self.collector = collector
        self.cold_starts = cold_starts
        self.reload_rounds = reload_rounds
        self.filler_of = filler_of  # the measured token ratio per model, from the frozen material

    # -- the matrix --------------------------------------------------------

    def run_model(self, *, model_id: str, capabilities: Sequence[str], envelope) -> tuple[Attempt, ...]:
        """Every case of one model. Failures are recorded; nothing is retried silently."""
        try:
            filler = None if self.filler_of is None else self.filler_of.get(model_id)
            fixtures = fixtures_for(model_id, capabilities, envelope, filler=filler)
        except FixtureError as exc:
            raise BackendCaseError(str(exc)) from exc
        by_capability = {fixture.capability: fixture for fixture in fixtures}
        attempts: list[Attempt] = []

        first = fixtures[0]
        for attempt in range(1, self.cold_starts + 1):
            attempts.append(self._attempt(model_id, f"B:{model_id}:load", attempt, "load",
                                          lambda: self.driver.load(model_id, cold=True)))
        attempts.append(self._attempt(model_id, f"B:{model_id}:infer", 1, "infer",
                                      lambda: self.driver.execute(model_id, _basic_payload(first))))
        envelope_fixture = by_capability.get("chat") or first
        attempts.append(self._attempt(model_id, f"B:{model_id}:envelope", 1, "envelope",
                                      lambda: self.driver.execute(model_id, envelope_fixture.payload),
                                      fixture=envelope_fixture))
        attempts.append(self._attempt(model_id, f"B:{model_id}:cancel", 1, "cancel",
                                      lambda: self._cancel_once(model_id, first)))
        attempts.append(self._attempt(model_id, f"B:{model_id}:stop", 1, "stop",
                                      lambda: self.driver.stop(model_id)))
        for round_index in range(1, self.reload_rounds + 1):
            attempts.append(self._attempt(model_id, f"B:{model_id}:reload", round_index, "reload",
                                          lambda: self._reload_once(model_id)))
        for capability in capabilities:
            if capability in COMPAT_CAPABILITIES:
                # A chat feature never runs through the internal execution
                # surface: the compat route owns its two rounds (CT07).
                attempts.append(self._compat_capability(model_id, capability))
                continue
            capability_fixture = by_capability[capability]
            attempts.append(self._attempt(model_id, f"B:{model_id}:cap:{capability}", 1, "capability",
                                          lambda fixture=capability_fixture: self.driver.execute(
                                              model_id, fixture.payload),
                                          fixture=capability_fixture, capability=capability))
        return tuple(attempts)

    def _compat_capability(self, model_id: str, capability: str) -> Attempt:
        """One `cap:<feature>` case, handed to the compat route.

        Without the compat runner the case stays `unknown`: a chat feature is
        never served by the legacy driver, and an unproven case is never a pass.
        """
        case_id = f"B:{model_id}:cap:{capability}"
        started_utc = _utc(self._clock)
        started = self._clock.monotonic()
        directory = self.collector.begin_case(case_id, attempt=1) if self.collector is not None else None
        facts: dict[str, Any] = {"capability": capability, "transport": TRANSPORT_COMPAT}
        problems: list[str] = []
        failure: str | None = None
        status = "unknown"
        if self.compat is None:
            problems.append(f"no compat driver: {capability} is served by the two-round compat route")
        elif directory is None:
            problems.append("the compat route needs a material sink for its raw rounds")
        else:
            try:
                result = dict(self.compat.run_case(model_id=model_id, capability=capability,
                                                   variant=self.compat_variant,
                                                   directory=directory / "compat"))
            except Exception as exc:  # noqa: BLE001 - a refusal is material, not a crash
                failure = str(exc)
                status = "failed"
            else:
                status = str(result.get("status", "unknown"))
                problems = [str(item) for item in result.get("problems", ())]
                failure = None if result.get("failure") is None else str(result["failure"])
                facts.update({key: value for key, value in result.get("facts", {}).items()})
                if status == "passed" and problems:
                    status = "unknown"
                    problems.append("the compat runner reported passed with unresolved problems")
                elif status not in STATUSES:
                    problems.append(f"the compat runner reported the unknown status {status!r}")
                    status = "unknown"
        if self.collector is not None:
            if failure is not None:
                self.collector.record_failure(stage=case_id, error=failure,
                                              detail={"attempt": 1, "problems": problems})
            self.collector.end_case(status=status, facts=facts, failure=failure, problems=tuple(problems))
        return Attempt(case_id=case_id, attempt=1, status=status, started_utc=started_utc, ended_utc=_utc(self._clock),
                       duration_seconds=self._clock.monotonic() - started, facts=facts, failure=failure,
                       problems=tuple(problems))

    # -- the official-API sequences ---------------------------------------

    def _cancel_once(self, model_id: str, fixture: Fixture) -> Mapping[str, Any]:
        started = self.driver.start(model_id, fixture.payload)
        execution_id = started.get("execution_id")
        if not isinstance(execution_id, str) or not execution_id:
            raise BackendCaseError("start did not return an execution id: a cancel cannot be proven")
        acknowledgement = self.driver.cancel(model_id, execution_id)
        return {**acknowledgement, "execution_id": execution_id,
                "provider": acknowledgement.get("provider", started.get("provider"))}

    def _reload_once(self, model_id: str) -> Mapping[str, Any]:
        stopped = self.driver.stop(model_id)
        if stopped.get("stop_proven") is not True:
            raise BackendCaseError("a reload needs the previous instance proven stopped")
        loaded = self.driver.load(model_id, cold=True)
        # Both halves of the round sampled their own window: the case keeps both.
        samples = (*stopped.get("samples", ()), *loaded.get("samples", ()))
        return {"provider": loaded.get("provider", stopped.get("provider")), "reloaded": True,
                "stop_proven": True, "instance": loaded.get("instance"),
                **({"samples": samples} if samples else {})}

    # -- recording ---------------------------------------------------------

    def _record_samples(self, samples: Any) -> None:
        """Persist the raw rows the driver harvested; they are the case's material.

        Only the counts stay on the attempt: the rows themselves belong to the
        run's samples, which is exactly what the evaluator recomputes from.
        """
        if self.collector is None or not samples:
            return
        for row in samples:
            if not isinstance(row, Mapping):
                raise BackendCaseError("a device sample must be a mapping of kind and raw value")
            kind, raw = row.get("kind"), row.get("raw")
            if not isinstance(kind, str) or not kind:
                raise BackendCaseError("a device sample needs a kind, otherwise it cannot be recomputed")
            self.collector.record_sample(kind, raw)

    def _attempt(self, model_id: str, case_id: str, attempt: int, kind: str, action, *,
                 fixture: Fixture | None = None, capability: str | None = None) -> Attempt:
        started_utc = _utc(self._clock)
        started = self._clock.monotonic()
        if self.collector is not None:
            self.collector.begin_case(case_id, attempt=attempt)
        failure: str | None = None
        cleanup: Mapping[str, Any] | None = None
        problems: list[str] = []
        facts: dict[str, Any] = {}
        status = "unknown"
        try:
            result = dict(action() or {})
            facts = _without_samples(result)
            if fixture is not None:
                # The declared boundary belongs to the material: the evaluator
                # re-checks the round against it, so it cannot stay in memory only.
                facts["fixture"] = {"capability": fixture.capability, "fixture_id": fixture.fixture_id,
                                    "boundary": dict(fixture.boundary)}
            self._record_samples(result.get("samples"))
            if result.get("error"):
                failure = str(result["error"])
                status = "failed"
            else:
                problems = attribution_problems(kind, result)
                if kind == "envelope" and fixture is not None:
                    observed = result.get("observed")
                    shortfalls = boundary_shortfalls(fixture, observed if isinstance(observed, Mapping) else {})
                    if shortfalls:
                        problems.extend(f"the round did not reach the declared boundary: {item}"
                                        for item in shortfalls)
                if capability is not None:
                    output = result.get("output")
                    if isinstance(output, Mapping):
                        problems.extend(capability_output_problems(capability, output))
                if not problems:
                    status = "passed"
                else:
                    # an unattributable case is unknown; a case that fell short of its own
                    # contract (boundary, output shape, finiteness) is failed
                    status = "failed" if any("did not reach" in item or "no real output" in item
                                             or "non-finite" in item or "no non-empty content" in item
                                             or "inconsistent" in item or "no vectors" in item
                                             or "no results" in item for item in problems) else "unknown"
        except Exception as exc:  # noqa: BLE001 - the executor records any crash and cleans up
            failure = f"{type(exc).__name__}: {exc}"
            status = "failed"
            try:
                cleanup = dict(self.driver.cleanup(model_id))
            except Exception as cleanup_exc:  # noqa: BLE001 - a failed cleanup is itself material
                cleanup = {"error": f"{type(cleanup_exc).__name__}: {cleanup_exc}"}
        ended_utc = _utc(self._clock)
        attempt_record = Attempt(case_id=case_id, attempt=attempt, status=status, started_utc=started_utc,
                                 ended_utc=ended_utc, duration_seconds=self._clock.monotonic() - started,
                                 facts=facts, failure=failure, cleanup=cleanup, problems=tuple(problems))
        if self.collector is not None:
            if failure is not None:
                self.collector.record_failure(stage=case_id, error=failure,
                                              detail={"attempt": attempt, "cleanup": cleanup})
            self.collector.end_case(status=status, facts=facts, failure=failure, problems=tuple(problems))
        return attempt_record


class _SystemClock:
    def monotonic(self) -> float:
        import time

        return time.monotonic()

    def utc_now(self) -> datetime:
        return datetime.now(timezone.utc)


def _basic_payload(fixture: Fixture) -> Mapping[str, Any]:
    """A small version of the same capability's request: `infer` proves it runs."""
    payload = dict(fixture.payload)
    if fixture.capability == "chat":
        payload["messages"] = [{"role": "user", "content": "tok000001 tok000002 tok000003 tok000004"}]
        payload["max_tokens"] = min(8, int(fixture.boundary.get("output_tokens", 8)))
        payload["n_parallel"] = 1
    elif fixture.capability == "vision":
        message = payload["messages"][0]
        content = [item for item in message["content"] if item.get("type") == "image_url"][:1]
        content.append({"type": "text", "text": "tok000001 tok000002 tok000003 tok000004"})
        payload["messages"] = [{"role": "user", "content": content}]
        payload["max_tokens"] = min(8, int(fixture.boundary.get("output_tokens", 8)))
        payload["n_parallel"] = 1
    elif fixture.capability == "embeddings":
        payload["inputs"] = payload["inputs"][:2]
    else:
        payload["documents"] = payload["documents"][:2]
        payload["query"] = "tok000001 tok000002"
    return payload


def _without_samples(result: Mapping[str, Any]) -> dict:
    """The facts a case records: the attempt stays readable, the rows go to disk."""
    return {key: value for key, value in result.items() if key != "samples"}


def summarize(attempts: Sequence[Attempt]) -> dict:
    """Per-case final status: the last attempt wins, a missing case is `not_run`."""
    final: dict[str, str] = {}
    for attempt in attempts:
        final[attempt.case_id] = attempt.status
    counts = {status: sum(1 for value in final.values() if value == status) for status in STATUSES}
    return {"cases": dict(sorted(final.items())), "counts": counts,
            "passed": counts["passed"] > 0 and counts["failed"] == 0 and counts["unknown"] == 0
                      and counts["not_run"] == 0}
