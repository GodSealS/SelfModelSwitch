"""CT10: run one derived candidate suite over the real loopback service (acceptance §7).

The runner consumes the case list `lab_suite` derives and the ports `lab_http` provides;
it never picks a case, a variant or a payload of its own beyond the frozen fixture set and
the documented legacy operations. Every generation request is spent from the site's frozen
`request_limit` *before* it leaves, so an exhausted budget stops the run instead of
sending one more request. All material stays under the output directory, the failed
attempts are kept, and the report is written even when cases fail — a run that failed is
still a run with evidence.

Budget exhaustion (`not_run` + `request_limit`) is not a pass; the caller turns any
non-passed final attempt into exit 3.
"""
from __future__ import annotations

import base64
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
from typing import Any, Mapping

from ..contracts_v2 import canonical_json_bytes
from . import chat_compat as cc
from . import fixtures as fx
from . import lab_report as lr
from . import lab_suite as ls
from .chat_probe import ShellPort

CHAT_PROMPT = "Reply with exactly SMS_CHAT_OK."
#: The legacy structural regression bounds its own answer; the budget-exhaustion evidence
#: (acceptance §4/A01) belongs to the probe's D02, which uses its own "until the limit" prompt.
LEGACY_MAX_TOKENS = 64
FACTS_FILE = "facts.json"
CLEANUP_FILE = "cleanup.json"


class LabRunError(RuntimeError):
    """The run cannot start or cannot continue; nothing is improvised around it."""


class RequestLimitReached(RuntimeError):
    """The frozen request budget is exhausted; the run stops instead of sending one more."""


class RequestBudget:
    """The site's frozen generation-request budget, spent before every send."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0
        self._lock = threading.Lock()

    def spend(self) -> None:
        with self._lock:
            if self.used >= self.limit:
                raise RequestLimitReached(f"the frozen request_limit of {self.limit} is exhausted")
            self.used += 1


class BudgetedTransport:
    """`CompatTransport` that spends the frozen budget before it posts."""

    def __init__(self, inner: cc.CompatTransport, budget: RequestBudget) -> None:
        self._inner = inner
        self._budget = budget

    def chat(self, request_json: bytes, *, stream: bool, deadline: float) -> cc.CompatRound:
        self._budget.spend()
        return self._inner.chat(request_json, stream=stream, deadline=deadline)


def checkout_state(shell: ShellPort, checkout: str) -> dict:
    """The deployed commit and tree state, read from the checkout the site names."""
    head = shell.run(["git", "-C", checkout, "rev-parse", "HEAD"], timeout=30.0)
    status = shell.run(["git", "-C", checkout, "status", "--porcelain", "--untracked-files=all"], timeout=30.0)
    return {"head": head.stdout.strip(), "clean": status.stdout.strip() == "",
            "exit": {"head": head.returncode, "status": status.returncode}}


def precheck_problems(*, site, suite: ls.LabSuite, git_state: Mapping[str, Any]) -> list[str]:
    """The input reasons this run must not start: identities, budget and the checkout."""
    problems = ls.identity_problems(suite=suite, policy_source_sha256=site.policy_source_sha256 or "",
                                    fixture_set_sha256=site.fixture_set_sha256 or "")
    problems.extend(ls.budget_problems(suite=suite, request_limit=site.request_limit))
    if git_state.get("head") != site.expected_sha:
        problems.append(f"the checkout is at {git_state.get('head')!r}, not the frozen {site.expected_sha}")
    if not git_state.get("clean"):
        problems.append("the checkout is not clean; a run never starts on uncommitted code")
    return problems


def _slug(case_id: str) -> str:
    slug = case_id.lower().replace(":", "_")
    if not slug or any(not (character.isalnum() or character in "_-") for character in slug):
        raise LabRunError(f"case id {case_id!r} has no directory-safe slug")
    return slug


def _image_data_url(edge_pixels: int) -> str:
    png = fx.render_test_png(edge_pixels, edge_pixels)
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def _legacy_payload(model_id: str, *, stream: bool, max_tokens: int, image_url: str | None = None) -> dict:
    if image_url is None:
        content: Any = CHAT_PROMPT
    else:
        content = [{"type": "text", "text": CHAT_PROMPT}, {"type": "image_url", "image_url": {"url": image_url}}]
    return {"model": model_id, "messages": [{"role": "user", "content": content}],
            "max_tokens": max_tokens, "stream": stream}


def _message_of(body: Any) -> Mapping[str, Any] | None:
    if not isinstance(body, Mapping):
        return None
    choices = body.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], Mapping) else None
    message = choice.get("message") if choice else None
    return message if isinstance(message, Mapping) else None


def _legacy_problems(variant: str, body: Any, *, max_output_tokens: int) -> list[str]:
    """The structural assertions of one legacy round; a short 200 is not budget evidence."""
    problems: list[str] = []
    choices = body.get("choices") if isinstance(body, Mapping) else None
    choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], Mapping) else None
    if choice is None:
        return ["the answer carries no choice"]
    if choice.get("finish_reason") != "stop":
        problems.append(f"finish_reason {choice.get('finish_reason')!r} is not stop")
    content = (_message_of(body) or {}).get("content")
    if not isinstance(content, str) or not content.strip():
        problems.append("the answer carries no content")
    elif any(tag in content for tag in cc.THINKING_TAGS):
        problems.append("the answer carries a thinking tag")
    usage = body.get("usage") if isinstance(body, Mapping) else None
    completion = usage.get("completion_tokens") if isinstance(usage, Mapping) else None
    if variant == "budget-boundary":
        if not isinstance(completion, int):
            problems.append("the boundary round reported no completion_tokens")
        elif completion > max_output_tokens:
            problems.append(f"completion_tokens {completion} exceeds the registered budget {max_output_tokens}")
    return problems


@dataclass
class LabRunner:
    """One suite run: the frozen case list, the real ports and the material under `output`."""

    suite: ls.LabSuite
    site: Any
    output: Path
    transport: cc.CompatTransport
    service: Any
    shell: ShellPort | None = None
    clock: Any = None
    git_state: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        self.output = Path(self.output)
        self.output.mkdir(parents=True, exist_ok=True)
        if self.clock is None:
            from .chat_probe import SystemClock

            self.clock = SystemClock()
        self.budget = RequestBudget(self.site.request_limit)
        self.attempts: list[dict] = []
        self.final: list[dict] = []
        self.registration_problems = ls.candidate_registration_problems(
            {model_id: model.capabilities for model_id, model in self.site.models.items()})
        self.exhausted = False

    # -- report-visible helpers -------------------------------------------

    def _stamp(self) -> str:
        return self.clock.utc_now().astimezone(timezone.utc).isoformat()

    def _write(self, relative: str, document: Any) -> str:
        target = self.output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(canonical_json_bytes(document) + b"\n")
        return relative

    def _attempt_directory(self, case: ls.LabCase, attempt: int) -> Path:
        # One directory per case *and* variant: a compat case runs four times, and material
        # is never written into a directory that already holds a round.
        directory = self.output / "cases" / f"{_slug(case.case_id)}__{case.variant}" / f"attempt-{attempt}"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _material_files(self, directory: Path) -> dict[str, list[str]]:
        files = {"request_files": [], "response_files": [], "observation_files": [], "cleanup_files": []}
        for path in sorted(candidate for candidate in directory.rglob("*") if candidate.is_file()):
            relative = path.relative_to(self.output).as_posix()
            if path.name == cc.REQUEST_FILE:
                files["request_files"].append(relative)
            elif path.name == cc.RESPONSE_FILE:
                files["response_files"].append(relative)
            elif path.name == CLEANUP_FILE:
                files["cleanup_files"].append(relative)
            else:
                files["observation_files"].append(relative)
        return files

    # -- one case ----------------------------------------------------------

    def _post(self, directory: Path, label: str, payload: Mapping[str, Any], *,
              stream: bool) -> tuple[cc.CompatRound, Any]:
        """One compat request with its raw material; the frozen budget is spent first."""
        request_json = canonical_json_bytes(payload)
        (directory / label).mkdir(parents=True, exist_ok=True)
        (directory / label / cc.REQUEST_FILE).write_bytes(request_json)
        round_record = BudgetedTransport(self.transport, self.budget).chat(
            request_json, stream=stream, deadline=float(self.site.timeouts.total_seconds))
        (directory / label / cc.RESPONSE_FILE).write_bytes(round_record.raw)
        body: Any = None
        try:
            body = cc.SseAggregator().aggregate(round_record.raw) if stream \
                else json.loads(round_record.raw.decode("utf-8"))
        except Exception:  # noqa: BLE001 - an unreadable answer is material, not a crash
            body = None
        (directory / label / cc.BODY_FILE).write_bytes(canonical_json_bytes(
            body if isinstance(body, (dict, list)) else {"unparsable": round_record.raw.decode("utf-8", "replace")}))
        return round_record, body

    def _unload(self, directory: Path, model_id: str) -> dict:
        evidence = self.service.unload(model_id)
        self._write((directory / CLEANUP_FILE).relative_to(self.output).as_posix(), evidence)
        return evidence

    def _run_compat(self, case: ls.LabCase, directory: Path) -> tuple[str, str | None]:
        driver = cc.CompatDriver(BudgetedTransport(self.transport, self.budget), aggregator=cc.SseAggregator)
        if case.reload:
            evidence = self._unload(directory, case.model_id)
            if not evidence.get("stopped"):
                return "failed", "the reload variant could not prove the model stopped before the cold round"
        spec = cc.CaseSpec(case_id=case.case_id, variant=case.variant, scenario=case.scenario)
        driver.run(spec, directory / "compat")
        problems = cc.evaluate_case(directory / "compat")["problems"]
        if problems:
            return "failed", "; ".join(problems)
        return "passed", None

    def _run_legacy(self, case: ls.LabCase, directory: Path) -> tuple[str, str | None]:
        model = self.site.models[case.model_id]
        envelope = model.envelope
        facts: dict[str, Any] = {"variant": case.variant, "model_id": case.model_id,
                                 "runtime_id": model.runtime_id, "profile_id": model.profile_id,
                                 "envelope": {"max_output_tokens": envelope.max_output_tokens,
                                              "max_parallel": envelope.max_parallel,
                                              "max_image_edge_pixels": envelope.max_image_edge_pixels}}
        if case.variant == "cold-count":
            evidence = self._unload(directory, case.model_id)
            if not evidence.get("stopped"):
                return "failed", "the cold round could not prove the model stopped before it was counted"
            facts["generation_before"] = (evidence.get("before") or {}).get("generation")
            facts["generation_after"] = (evidence.get("after") or {}).get("generation")
        if case.variant == "budget-boundary":
            return self._run_boundary(case, directory, facts)

        stream = case.variant == "chat-sse"
        max_tokens = min(LEGACY_MAX_TOKENS, envelope.max_output_tokens)
        image_url = _image_data_url(envelope.max_image_edge_pixels) if case.variant == "vision-json" else None
        payload = _legacy_payload(case.model_id, stream=stream, max_tokens=max_tokens, image_url=image_url)
        round_record, body = self._post(directory, "round-1", payload, stream=stream)
        facts.update({"stream": stream, "max_tokens": max_tokens, "http_status": round_record.status,
                      "finish_reason": ((body or {}).get("choices") or [{}])[0].get("finish_reason")
                      if isinstance(body, Mapping) else None,
                      "usage": (body or {}).get("usage") if isinstance(body, Mapping) else None,
                      "request_id": round_record.request_id})
        self._write((directory / FACTS_FILE).relative_to(self.output).as_posix(), facts)
        if round_record.status != 200:
            return "failed", f"the {case.variant} round answered {round_record.status}, not 200"
        problems = _legacy_problems(case.variant, body, max_output_tokens=envelope.max_output_tokens)
        if problems:
            return "failed", "; ".join(problems)
        return "passed", None

    def _run_boundary(self, case: ls.LabCase, directory: Path, facts: dict) -> tuple[str, str | None]:
        """The registered parallelism, fired at once, each request a single bounded choice."""
        model = self.site.models[case.model_id]
        parallel = int(model.envelope.max_parallel)
        tokens = int(model.envelope.max_output_tokens)
        payload = _legacy_payload(case.model_id, stream=False, max_tokens=tokens)
        facts.update({"parallel": parallel, "max_tokens": tokens, "concurrent": True})

        def one(index: int) -> tuple[int, cc.CompatRound, Any]:
            round_record, body = self._post(directory, f"request-{index}", payload, stream=False)
            return index, round_record, body

        problems: list[str] = []
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            outcomes = list(pool.map(one, range(1, parallel + 1)))
        rows = []
        for index, round_record, body in outcomes:
            rows.append({"index": index, "http_status": round_record.status, "request_id": round_record.request_id,
                         "usage": (body or {}).get("usage") if isinstance(body, Mapping) else None})
            if round_record.status != 200:
                problems.append(f"request {index} answered {round_record.status}, not 200")
                continue
            problems.extend(f"request {index}: {problem}"
                            for problem in _legacy_problems("budget-boundary", body, max_output_tokens=tokens))
        facts["requests"] = rows
        self._write((directory / FACTS_FILE).relative_to(self.output).as_posix(), facts)
        if problems:
            return "failed", "; ".join(problems)
        return "passed", None

    # -- the run -----------------------------------------------------------

    def run(self) -> dict:
        """Run every derived case once, in order, and write the report whatever happens."""
        started = self._stamp()
        if self.registration_problems:
            self._write("registration.json", {"problems": self.registration_problems,
                                              "note": "the first-release candidate registration is incomplete"})
        for case in self.suite.cases:
            attempt_started = self._stamp()
            directory = self._attempt_directory(case, 1)
            if self.exhausted:
                status, reason = "not_run", "the frozen request_limit was exhausted"
            else:
                try:
                    status, reason = (self._run_compat if case.kind == "compat" else self._run_legacy)(case, directory)
                except RequestLimitReached as exc:
                    self.exhausted = True
                    status, reason = "not_run", str(exc)
                except Exception as exc:  # noqa: BLE001 - a broken case is material, not a crash
                    status, reason = "failed", f"{type(exc).__name__}: {exc}"
            attempt_ended = self._stamp()
            self.attempts.append({"case_id": case.case_id, "variant": case.variant, "attempt": 1, "status": status,
                                  "reason": reason, "start": attempt_started, "end": attempt_ended,
                                  **self._material_files(directory)})
            self.final.append({"case_id": case.case_id, "variant": case.variant, "attempt": 1})
        lr.write_report(self.output, suite=self.suite.suite, site_sha256=self.site.sha256,
                        code_sha=self.site.expected_sha, policy_source_sha256=self.site.policy_source_sha256,
                        fixture_set_sha256=self.site.fixture_set_sha256, candidate_report_sha256=None,
                        started_at_utc=started, ended_at_utc=self._stamp(), attempts=self.attempts,
                        final=self.final)
        # The returned document is the one on disk: a caller never sees a memory-only verdict.
        return lr.load_report(self.output)

    # -- verdicts ----------------------------------------------------------

    def final_statuses(self) -> dict[tuple[str, str], str]:
        return {(row["case_id"], row["variant"]): row["status"] for row in self.attempts}

    def passed(self) -> bool:
        if self.registration_problems:
            return False
        if not self.attempts:
            return False
        return all(row["status"] == "passed" for row in self.attempts)


def required_pairs(suite: ls.LabSuite) -> tuple[tuple[str, str], ...]:
    """The (case, variant) pairs a report of this suite must cite."""
    return tuple((case.case_id, case.variant) for case in suite.cases)
