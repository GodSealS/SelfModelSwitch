"""CT10: the candidate suite runs end to end over a real HTTP service (A08/A10/A12).

The site input here is a real one: a clean temporary checkout at the frozen commit, real
model files, the frozen digests of this repository's policies and fixtures. A real HTTP
server answers the two-round compat dialogues, the thinking dialogue and the legacy
rounds, so the runner, the ports, the material and the report are exercised together —
including the reload/cold rounds and the registered-parallelism boundary.
"""
from __future__ import annotations

import dataclasses
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from model_scheduler import chat_counting
from model_scheduler.acceptance import chat_compat as cc
from model_scheduler.acceptance import lab_report as lr
from model_scheduler.acceptance import lab_runner as lrun
from model_scheduler.acceptance import lab_suite as ls
from model_scheduler.acceptance import chat_probe
from model_scheduler.contracts_v2 import Envelope

MODELS = {
    "qwen25vl-7b": ("chat", "vision"),
    "qwen36-27b": ("chat", "vision", "tools", "thinking"),
}
ENVELOPES = {
    "qwen25vl-7b": {"ctx_size": 32768, "max_input_tokens": 8192, "max_output_tokens": 4096, "max_parallel": 2,
                    "max_image_tokens": 128, "max_image_edge_pixels": 64, "max_images": 1},
    "qwen36-27b": {"ctx_size": 8192, "max_input_tokens": 4096, "max_output_tokens": 1024, "max_parallel": 1,
                   "max_image_tokens": 128, "max_image_edge_pixels": 64, "max_images": 1},
}
MODEL_SHA = "3f4513330aa7f109922bd701d773575484ae2b4a4090d6511260a2a4f8e3d069"
TEMPLATE_SHA = "a0bc6f6fc7a29a80017a433e8f03a1cc1236e838a944a2d034295a60c4f2fddb"
IMAGE = "sms-llama-cpp@sha256:" + "8" * 64
TIMEOUTS = {"connect_seconds": 5, "read_idle_seconds": 30, "total_seconds": 900}
THINKING_FIRST = "Compute 19 * 23. Finish with RESULT=437."
THINKING_FOLLOWUP = "Using the previous result, add 1. Finish with RESULT=438."


def _envelopes() -> dict:
    """The registered envelopes as objects; the site input carries the same values as JSON."""
    return {model_id: Envelope(**spec) for model_id, spec in ENVELOPES.items()}


def _checkout(tmp_path: Path) -> tuple[Path, str]:
    """A clean temporary checkout, so the runner's git guard is exercised for real."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    run = ["git", "-C", str(checkout)]
    subprocess.run([*run, "init", "-q", "-b", "main"], check=True, capture_output=True)
    subprocess.run([*run, "-c", "user.email=ct10@example.invalid", "-c", "user.name=CT10",
                    "commit", "-q", "--allow-empty", "-m", "candidate"], check=True, capture_output=True)
    head = subprocess.run([*run, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    return checkout, head


def _site(tmp_path: Path, checkout: Path, sha: str, service_base_url: str, **changes) -> dict:
    asset = tmp_path / "model.gguf"
    asset.write_bytes(b"gguf-fixture")
    projector = tmp_path / "mmproj.gguf"
    projector.write_bytes(b"mmproj-fixture")
    rollback = tmp_path / "rollback-manifest.json"
    rollback.write_text("{}\n", encoding="utf-8")
    document = {
        "schema_version": 1, "checkout": str(checkout),
        "remote_url": "https://github.com/GodSealS/SelfModelSwitch.git", "branch": "feature/ct10-suite",
        "expected_sha": sha, "deployment_id": "sms-orin-lab2", "mode": "lab", "phase": "candidate",
        "service_base_url": service_base_url, "gateway_base_url": None, "auth_env": None, "hardware_file": "hardware-raw.txt",
        "models": {
            model_id: {"capabilities": list(capabilities), "runtime_id": "llama-cpp-1",
                       "profile_id": "llama-cpp-gguf-v1", "image_digest": IMAGE,
                       "model_path": str(asset), "model_sha256": MODEL_SHA,
                       "projector_path": str(projector), "projector_sha256": MODEL_SHA,
                       "template_sha256": TEMPLATE_SHA, "upstream_base_url": "http://127.0.0.1:10002",
                       "envelope": dict(ENVELOPES[model_id]),
                       "launch_argv": ["docker", "run", "--rm", IMAGE, "--port", "8080"]}
            for model_id, capabilities in MODELS.items()},
        "timeouts": dict(TIMEOUTS), "request_limit": 64,
        "policy_source_sha256": chat_counting.policy_source_digest(),
        "fixture_set_sha256": cc.fixture_set_digest(
            ls.candidate_suite(models=MODELS, envelope_of=_envelopes(), deadline_seconds=900.0).scenarios()),
        "rollback_input": str(rollback), "rollback_sha": None, "client_evidence": [],
    }
    document.update(changes)
    (tmp_path / "hardware-raw.txt").write_text("hostname fixture\n", encoding="utf-8")
    (tmp_path / "site-input.json").write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return document


class _Service:
    """A real loopback service: the two-round dialogues, the legacy rounds and a release route."""

    def __init__(self) -> None:
        self.received: list[dict] = []
        self.finish_reason = "stop"  # a test can make every answer a truncated one
        self.status_document = {"models": {model_id: {"state": "active", "generation": 1, "in_flight": 0,
                                                      "cancelling": 0} for model_id in MODELS}}
        outer = self

        def _tool_call() -> dict:
            return {"choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "reasoning_content": "需要一个天气工具",
                "tool_calls": [{"id": "call_http_1", "type": "function",
                                "function": {"name": "get_weather", "arguments": "{\"city\":\"Beijing\"}"}}]}}],
                "usage": {"prompt_tokens": 210, "completion_tokens": 33}}

        def _stop(content: str, reasoning: str) -> dict:
            return {"choices": [{"index": 0, "finish_reason": outer.finish_reason, "message": {
                "role": "assistant", "content": content, "reasoning_content": reasoning}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 5}}

        def _answer(request: dict) -> dict:
            messages = request.get("messages", [])
            if any(message.get("role") == "tool" for message in messages):
                return _stop("SMS_WEATHER_OK_27", "已取到标记")
            text = json.dumps(messages, ensure_ascii=False)
            if THINKING_FOLLOWUP in text:
                return _stop("RESULT=438", "再加一")
            if THINKING_FIRST in text:
                return _stop(" 19 * 23 = 437 RESULT=437 ", "先算乘法")
            if "tools" in request:
                return _tool_call()
            return _stop("SMS_CHAT_OK", "普通回答")

        def _sse(body: dict) -> bytes:
            message = body["choices"][0]["message"]
            events = []
            if message.get("reasoning_content"):
                events.append({"choices": [{"index": 0, "delta": {"reasoning_content": message["reasoning_content"]},
                                            "finish_reason": None}]})
            if message.get("tool_calls"):
                call = message["tool_calls"][0]
                events.append({"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": call["id"],
                                                                                 "type": "function",
                                                                                 "function": call["function"]}]},
                                            "finish_reason": None}]})
            if message.get("content"):
                events.append({"choices": [{"index": 0, "delta": {"content": message["content"]},
                                            "finish_reason": None}]})
            events.append({"choices": [{"index": 0, "delta": {}, "finish_reason": body["choices"][0]["finish_reason"]}]})
            events.append({"choices": [], "usage": body["usage"]})
            return ("\n\n".join(f"data: {json.dumps(event, ensure_ascii=False)}" for event in events)
                    + "\n\ndata: [DONE]\n\n").encode("utf-8")

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:
                pass

            def _send(self, status: int, raw: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("content-type", content_type)
                self.send_header("x-request-id", f"req-{len(outer.received)}")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:  # noqa: N802 - the handler API
                if self.path == "/api/status":
                    self._send(200, json.dumps(outer.status_document).encode("utf-8"), "application/json")
                else:
                    self._send(404, b"{}", "application/json")

            def do_POST(self) -> None:  # noqa: N802 - the handler API
                body = self.rfile.read(int(self.headers.get("content-length", "0")))
                if self.path == "/v1/chat/completions":
                    request = json.loads(body.decode("utf-8"))
                    outer.received.append(request)
                    answer = _answer(request)
                    if request.get("stream"):
                        self._send(200, _sse(answer), "text/event-stream")
                    else:
                        self._send(200, json.dumps(answer).encode("utf-8"), "application/json")
                    return
                if self.path.endswith("/unload"):
                    model_id = self.path.split("/")[3]
                    row = outer.status_document["models"][model_id]
                    outer.status_document["models"][model_id] = {**row, "state": "unloaded",
                                                                 "generation": row["generation"] + 1}
                    self._send(200, b'{"ok": true}', "application/json")
                    return
                self._send(404, b"{}", "application/json")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self) -> _Service:
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _runner(tmp_path: Path, service: _Service, *, git_state: dict | None = None) -> tuple[object, object]:
    site = chat_probe.load_site_input(tmp_path / "site-input.json")
    suite = ls.candidate_suite(models={model_id: model.capabilities for model_id, model in site.models.items()},
                               envelope_of={model_id: model.envelope for model_id, model in site.models.items()},
                               deadline_seconds=site.timeouts.total_seconds)
    runner = lrun.LabRunner(suite=suite, site=site, output=tmp_path / "out",
                            git_state=git_state or {"head": site.expected_sha, "clean": True},
                            transport=_transport(service), service=_service_port(service))
    return site, runner


def _transport(service: _Service):
    from model_scheduler.acceptance import lab_http

    return lab_http.HttpCompatTransport(base_url=service.base_url, connect_seconds=5.0, read_idle_seconds=30.0,
                                        total_seconds=900.0)


def _service_port(service: _Service):
    from model_scheduler.acceptance import lab_http

    return lab_http.LabServicePort(base_url=service.base_url, timeout_seconds=5.0, poll_seconds=0.05)


def test_a10_the_whole_candidate_suite_runs_and_writes_its_report(tmp_path: Path) -> None:
    checkout, sha = _checkout(tmp_path)
    with _Service() as service:
        _site(tmp_path, checkout, sha, service.base_url)
        site, runner = _runner(tmp_path, service)
        document = runner.run()

    assert runner.registration_problems == []
    assert runner.passed() is True
    statuses = runner.final_statuses()
    assert len(statuses) == len(runner.suite.cases) == 3 * 4 + 2 * 5
    assert runner.budget.used == runner.suite.requests() <= site.request_limit
    assert lr.report_problems(document, required=lrun.required_pairs(runner.suite), root=tmp_path / "out") == []
    assert [row["status"] for row in document["attempts"]] == ["passed"] * len(runner.suite.cases)

    # The reload and cold rounds prove a real release, the boundary fires the registered parallelism.
    reload_rows = [row for row in document["attempts"] if row["variant"].endswith("-reload")]
    assert len(reload_rows) == 3 * 2 and all(row["cleanup_files"] for row in reload_rows)
    cold = [row for row in document["attempts"] if row["variant"] == "cold-count"]
    assert len(cold) == 2 and all(row["cleanup_files"] for row in cold)
    boundary = [row for row in document["attempts"] if row["variant"] == "budget-boundary"]
    assert [len(row["request_files"]) for row in boundary] == [2, 1]  # 7B parallel 2, 27B parallel 1
    facts = json.loads((tmp_path / "out" / boundary[0]["observation_files"][0]).read_text(encoding="utf-8"))
    assert facts["variant"] == "budget-boundary" and facts["parallel"] == 2
    vision = [row for row in document["attempts"] if row["variant"] == "vision-json"]
    assert len(vision) == 2 and all(row["request_files"] and row["response_files"] for row in vision)


def test_a10_the_runner_refuses_a_foreign_checkout_or_a_stale_freeze(tmp_path: Path) -> None:
    checkout, sha = _checkout(tmp_path)
    with _Service() as service:
        _site(tmp_path, checkout, sha, service.base_url)
        site, runner = _runner(tmp_path, service, git_state={"head": "b" * 40, "clean": True})
        assert any("frozen" in problem for problem in lrun.precheck_problems(
            site=site, suite=runner.suite, git_state=runner.git_state))

        runner = lrun.LabRunner(suite=runner.suite, site=site, output=tmp_path / "out2",
                                git_state={"head": site.expected_sha, "clean": False},
                                transport=_transport(service), service=_service_port(service))
        assert any("clean" in problem for problem in lrun.precheck_problems(
            site=site, suite=runner.suite, git_state=runner.git_state))

        stale = lrun.precheck_problems(site=site, suite=runner.suite,
                                       git_state={"head": site.expected_sha, "clean": True})
        assert stale == []
        smaller = dataclasses.replace(site, fixture_set_sha256="b" * 64)
        assert any("fixture_set_sha256" in problem for problem in lrun.precheck_problems(
            site=smaller, suite=runner.suite, git_state={"head": site.expected_sha, "clean": True}))
        expensive = dataclasses.replace(site, request_limit=1)
        assert any("request_limit" in problem for problem in lrun.precheck_problems(
            site=expensive, suite=runner.suite, git_state={"head": site.expected_sha, "clean": True}))


def test_a10_the_cli_runs_the_frozen_suite_against_the_named_service(tmp_path: Path, capsys) -> None:
    checkout, sha = _checkout(tmp_path)
    with _Service() as service:
        _site(tmp_path, checkout, sha, service.base_url)
        exit_code = cc.main(["run", "--suite", "candidate", "--site", str(tmp_path / "site-input.json"),
                             "--output", str(tmp_path / "cli-out")])

    assert exit_code == 0
    report = lr.load_report(tmp_path / "cli-out")
    assert report["suite"] == "candidate" and report["code_sha"] == sha
    assert all(row["status"] == "passed" for row in report["attempts"])
    assert "cli-out" in capsys.readouterr().out


def test_a10_the_cli_refuses_a_checkout_at_another_commit(tmp_path: Path, capsys) -> None:
    checkout, sha = _checkout(tmp_path)
    with _Service() as service:
        _site(tmp_path, checkout, "c" * 40, service.base_url)
        exit_code = cc.main(["run", "--suite", "candidate", "--site", str(tmp_path / "site-input.json"),
                             "--output", str(tmp_path / "cli-out")])

    assert exit_code == 2
    assert not (tmp_path / "cli-out").exists()  # nothing is written when the inputs do not hold
    assert "frozen" in capsys.readouterr().err


def test_a10_a_failed_case_still_writes_its_material_and_fails_the_run(tmp_path: Path, capsys) -> None:
    checkout, sha = _checkout(tmp_path)
    with _Service() as service:
        service.finish_reason = "length"  # a truncated answer is not a pass, and not a lost run
        _site(tmp_path, checkout, sha, service.base_url)
        exit_code = cc.main(["run", "--suite", "candidate", "--site", str(tmp_path / "site-input.json"),
                             "--output", str(tmp_path / "fail-out")])

    assert exit_code == 3
    report = lr.load_report(tmp_path / "fail-out")
    failed = [row for row in report["attempts"] if row["status"] == "failed"]
    assert failed and all(row["reason"] for row in failed)
    assert report["artifacts"]  # a failed run keeps the material it produced
    assert all(row["request_files"] or row["cleanup_files"] for row in failed)
    assert "fail-out" in capsys.readouterr().out


def test_a10_an_unknown_suite_is_an_argument_error() -> None:
    with pytest.raises(SystemExit) as failure:
        cc.main(["run", "--suite", "gateway", "--site", "site.json", "--output", "out"])

    assert failure.value.code == 2  # CT11's suites are not silently skipped
