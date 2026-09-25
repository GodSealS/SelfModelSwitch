"""CT10: the candidate suite — its case list, its gate and its budget (acceptance §7).

The suite is never typed case by case. The registration the frozen site input declares
produces:

* `B:<model>:cap:{tools,thinking}` — one chat-feature case per declared capability, in
  the four variants (`json-hot`, `sse-hot`, `json-reload`, `sse-reload`);
* `L:<model>:tools-thinking` — the combination case, in the same four variants, when a
  model declares both capabilities;
* `L:<model>:legacy` — the shared chat/vision regression of every registered model in
  `chat-json`, `chat-sse`, `vision-json`, `cold-count`, `budget-boundary`, where the
  boundary variant fires the registered parallelism in one-choice requests.

The list, the request budget, the frozen fixture set and the first-release registration
are all derived from that declaration, so a missing capability, a wrong model or a
stale digest is a *refusal* (exit 2/3) instead of a quietly smaller suite. Nothing here
sends a request: the runner consumes this list.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from ..chat_counting import policy_source_digest
from ..runtime_profiles import CHAT_FEATURE_MODEL_IDS
from . import chat_compat as cc

SUITE_CANDIDATE = "candidate"

CASE_PREFIX_BACKEND = "B:"
CASE_PREFIX_LAB = "L:"
COMBINATION_CASE = "tools-thinking"
LEGACY_CASE = "legacy"
#: The shared regression variants, in the frozen order (acceptance §7).
LEGACY_VARIANTS = ("chat-json", "chat-sse", "vision-json", "cold-count", "budget-boundary")
BOUNDARY_VARIANT = "budget-boundary"


class SuiteError(RuntimeError):
    """A case that cannot be derived from the suite's own rules is refused, never renamed."""


@dataclass(frozen=True)
class LabCase:
    """One required suite case in one variant; every field is part of its identity."""

    case_id: str
    variant: str
    model_id: str
    kind: str
    capability: str | None = None
    scenario: cc.CompatScenario | None = None
    parallel: int = 1

    def __post_init__(self) -> None:
        if self.kind not in ("compat", "legacy"):
            raise SuiteError(f"kind {self.kind!r} is not a suite case kind")
        if not isinstance(self.case_id, str) or not self.case_id:
            raise SuiteError("a suite case needs its derived case id")
        if self.kind == "legacy":
            if self.variant not in LEGACY_VARIANTS:
                raise SuiteError(f"variant {self.variant!r} is not one of {', '.join(LEGACY_VARIANTS)}")
            if self.case_id != f"{CASE_PREFIX_LAB}{self.model_id}:{LEGACY_CASE}":
                raise SuiteError(f"case id {self.case_id!r} is not the derived legacy case for {self.model_id!r}")
            if self.scenario is not None:
                raise SuiteError("a legacy case carries no compat scenario")
            if isinstance(self.parallel, bool) or not isinstance(self.parallel, int) or self.parallel < 1:
                raise SuiteError("the boundary variant needs a positive registered parallelism")
            return
        if self.variant not in cc.VARIANTS:
            raise SuiteError(f"variant {self.variant!r} is not one of {', '.join(cc.VARIANTS)}")
        if self.capability not in cc.COMPAT_CAPABILITIES:
            raise SuiteError(f"capability {self.capability!r} has no compat case")
        if not isinstance(self.scenario, cc.CompatScenario):
            raise SuiteError("a compat case needs the scenario it runs; it is never improvised")
        if self.scenario.model_id != self.model_id or self.scenario.capability != self.capability:
            raise SuiteError("the scenario does not belong to the case's model and capability")
        backend = f"{CASE_PREFIX_BACKEND}{self.model_id}:cap:{self.capability}"
        combination = f"{CASE_PREFIX_LAB}{self.model_id}:{COMBINATION_CASE}"
        if self.case_id not in (backend, combination):
            raise SuiteError(f"case id {self.case_id!r} is not the derived compat case")
        if self.case_id == combination and self.capability != "tools":
            raise SuiteError("the combination case runs the tools dialogue")
        if bool(self.scenario.stream) is not self.variant.startswith("sse-"):
            raise SuiteError(f"variant {self.variant!r} disagrees with the scenario's stream flag")

    @property
    def reload(self) -> bool:
        return self.variant.endswith("-reload")

    def requests(self) -> int:
        """The generation requests this case needs; a chat-feature case is two rounds."""
        if self.kind == "compat":
            return cc.ROUNDS
        return self.parallel if self.variant == BOUNDARY_VARIANT else 1


@dataclass(frozen=True)
class LabSuite:
    """The derived case list of one suite: the runner consumes it, the report cites it."""

    suite: str
    cases: tuple[LabCase, ...]

    def requests(self) -> int:
        return sum(case.requests() for case in self.cases)

    def case_ids(self) -> tuple[str, ...]:
        ordered: dict[str, None] = {}
        for case in self.cases:
            ordered.setdefault(case.case_id, None)
        return tuple(ordered)

    def scenarios(self) -> tuple[cc.CompatScenario, ...]:
        """The distinct scenarios of the case list; the fixture digest covers exactly these."""
        distinct: dict[str, cc.CompatScenario] = {}
        for case in self.cases:
            if case.scenario is not None:
                distinct.setdefault(case.scenario.digest(), case.scenario)
        return tuple(distinct.values())


def _envelope_of(envelope_of: Mapping[str, Any] | Callable[[str], Any], model_id: str) -> Any:
    return envelope_of[model_id] if isinstance(envelope_of, Mapping) else envelope_of(model_id)


def candidate_suite(*, models: Mapping[str, Sequence[str]], envelope_of: Mapping[str, Any] | Callable[[str], Any],
                    deadline_seconds: float) -> LabSuite:
    """Derive the candidate case list from the registered capabilities (acceptance §7)."""
    cases: list[LabCase] = []
    for model_id in sorted(models):
        declared = set(models[model_id])
        envelope = _envelope_of(envelope_of, model_id)
        parallel = int(getattr(envelope, "max_parallel", 1))
        for capability in cc.COMPAT_CAPABILITIES:
            if capability not in declared:
                continue
            builder = cc.tools_scenario if capability == "tools" else cc.thinking_scenario
            for variant in cc.VARIANTS:
                cases.append(LabCase(
                    case_id=f"{CASE_PREFIX_BACKEND}{model_id}:cap:{capability}", variant=variant, model_id=model_id,
                    kind="compat", capability=capability,
                    scenario=builder(model_id, envelope=envelope, stream=variant.startswith("sse-"),
                                     deadline_seconds=deadline_seconds)))
        if set(cc.COMPAT_CAPABILITIES) <= declared:
            for variant in cc.VARIANTS:
                cases.append(LabCase(
                    case_id=f"{CASE_PREFIX_LAB}{model_id}:{COMBINATION_CASE}", variant=variant, model_id=model_id,
                    kind="compat", capability="tools",
                    scenario=cc.tools_thinking_scenario(model_id, envelope=envelope,
                                                        stream=variant.startswith("sse-"),
                                                        deadline_seconds=deadline_seconds)))
        for variant in LEGACY_VARIANTS:
            cases.append(LabCase(case_id=f"{CASE_PREFIX_LAB}{model_id}:{LEGACY_CASE}", variant=variant,
                                 model_id=model_id, kind="legacy", parallel=parallel))
    return LabSuite(suite=SUITE_CANDIDATE, cases=tuple(cases))


def candidate_registration_problems(models: Mapping[str, Sequence[str]]) -> list[str]:
    """The reasons this registration is not the first-release candidate (A07/§7)."""
    problems: list[str] = []
    feature_models = sorted(model_id for model_id, capabilities in models.items()
                            if set(capabilities) & set(cc.COMPAT_CAPABILITIES))
    if not feature_models:
        problems.append("no registered model declares the chat features the candidate must serve")
    for model_id in feature_models:
        missing = sorted(set(cc.COMPAT_CAPABILITIES) - set(models[model_id]))
        if missing:
            problems.append(f"{model_id} must register tools and thinking together; missing: {', '.join(missing)}")
        if model_id not in CHAT_FEATURE_MODEL_IDS:
            problems.append(f"{model_id} is outside the first-release chat-feature model set")
    return problems


def identity_problems(*, suite: LabSuite, policy_source_sha256: str, fixture_set_sha256: str) -> list[str]:
    """The frozen identities the run must carry; a stale digest is an input error, not a rerun."""
    problems: list[str] = []
    if policy_source_sha256 != policy_source_digest():
        problems.append("policy_source_sha256 does not match the registered policy source")
    if fixture_set_sha256 != cc.fixture_set_digest(suite.scenarios()):
        problems.append("fixture_set_sha256 does not match the suite's derived fixture set")
    return problems


def budget_problems(*, suite: LabSuite, request_limit: int) -> list[str]:
    """The frozen request budget must cover the derived list before anything is sent (§2)."""
    required = suite.requests()
    if request_limit < required:
        return [f"request_limit {request_limit} is below the {required} requests the candidate suite needs"]
    return []
