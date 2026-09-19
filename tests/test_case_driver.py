"""The real case driver: exact API mapping, honest refusals, and the transport."""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import threading
import time
from typing import Any, Mapping

import pytest

from model_scheduler.acceptance.driver import (
    ApiResponse,
    ControlApiCaseDriver,
    DriverError,
    UnixControlTransport,
)
from model_scheduler.control_protocol_v1 import PROTOCOL_VERSION_HEADER


class _Transport:
    """Records every request and answers with canned control-API documents."""

    def __init__(self, *, fail_on: str | None = None, terminal: str = "succeeded",
                 execution_states: list[str] | None = None) -> None:
        self.requests: list[tuple[str, str, dict | None]] = []
        self.fail_on = fail_on
        self.terminal = terminal
        self.execution_states = execution_states or []
        self.reads = 0
        self._sessions = 0
        self._executions = 0
        self.session_settled = "active"

    def request(self, method: str, path: str, body=None) -> ApiResponse:
        self.requests.append((method, path, body))
        if self.fail_on is not None and self.fail_on in path:
            return ApiResponse(409, {"error": {"code": "conflict", "message": "already open"}})
        if method == "GET" and path == "/internal/peer":
            return ApiResponse(200, {"owner": "uid:1000", "boot_id": "boot-x", "via": "sms-control"})
        if path == "/internal/sessions" and method == "POST":
            self._sessions += 1
            return ApiResponse(201, {"session_id": f"session-{self._sessions}", "state": "preparing",
                                     "phase": "queued", "boot_id": "boot-x", "model_id": body["model_id"],
                                     "expires_in_ms": 30000, "hard_remaining_ms": 60000,
                                     "owner_token": f"tok-{self._sessions}", "error": None})
        if path.startswith("/internal/sessions/") and method == "POST" and path.endswith("/heartbeat"):
            return ApiResponse(200, {"session_id": path.split("/")[3], "state": "active", "phase": None,
                                     "boot_id": "boot-x", "model_id": "qwen-small", "expires_in_ms": 30000,
                                     "hard_remaining_ms": 60000, "owner_token": None, "error": None})
        if path.startswith("/internal/sessions/") and method == "GET":
            return ApiResponse(200, {"session_id": path.split("/")[3], "state": self.session_settled,
                                     "phase": None, "boot_id": "boot-x", "model_id": "qwen-small",
                                     "expires_in_ms": 30000, "hard_remaining_ms": 60000,
                                     "owner_token": None, "error": None})
        if path.startswith("/internal/sessions/") and method == "POST" and path.endswith("/close"):
            return ApiResponse(200, {"session_id": path.split("/")[3], "state": "closed", "phase": None,
                                     "boot_id": "boot-x", "model_id": "qwen-small", "expires_in_ms": 0,
                                     "hard_remaining_ms": 0, "owner_token": None, "error": None})
        if path.startswith("/internal/executions/") and method == "POST" and path.endswith("/cancel"):
            self.terminal = "cancelled"
            return ApiResponse(200, {"execution_id": path.split("/")[3], "state": "cancelling",
                                     "dispatch_state": "dispatched", "compute_quiescent": False, "result": None,
                                     "error": None, "instance": None, "fence": None})
        if path == "/internal/executions" and method == "POST":
            self._executions += 1
            return ApiResponse(201, {"execution_id": f"execution-{self._executions + 1}", "state": "running",
                                     "dispatch_state": "dispatched", "compute_quiescent": False, "result": None,
                                     "error": None, "instance": {"container_id": "abc", "started_at": "t0",
                                                                 "deployment_id": "sms-orin-lab",
                                                                 "model_id": body["input"]["inline"].get("model_id",
                                                                                                         "qwen-small"),
                                                                 "runtime_id": "llama-cpp-1",
                                                                 "candidate_digest": "c" * 64,
                                                                 "image_digest": "sms-llama-cpp@sha256:" + "a" * 64},
                                     "fence": {"boot_id": "boot-x", "model_id": "qwen-small", "generation": 1,
                                               "operation_id": "op-1",
                                               "execution_id": f"execution-{self._executions + 1}",
                                               "attempt": 1}})
        if path.startswith("/internal/executions/") and method == "GET":
            self.reads += 1
            state = self.execution_states.pop(0) if self.execution_states else self.terminal
            return ApiResponse(200, {"execution_id": path.split("/")[3], "state": state,
                                     "dispatch_state": "dispatched", "compute_quiescent": state == self.terminal,
                                     "result": {"output": "ok"} if state == "succeeded" else None,
                                     "error": None,
                                     # the terminal view keeps its instance and fence: attribution survives
                                     "instance": {"container_id": "abc", "started_at": "t0", "deployment_id":
                                                  "sms-orin-lab", "model_id": "qwen-small",
                                                  "runtime_id": "llama-cpp-1", "candidate_digest": "c" * 64,
                                                  "image_digest": "sms-llama-cpp@sha256:" + "a" * 64},
                                     "fence": {"boot_id": "boot-x", "model_id": "qwen-small", "generation": 1,
                                               "operation_id": "op-1", "execution_id": path.split("/")[3],
                                               "attempt": 1}})
        raise AssertionError(f"unexpected request {method} {path}")


def _driver(transport: _Transport) -> ControlApiCaseDriver:
    return ControlApiCaseDriver(transport, sleep=lambda _seconds: None)


def test_a_load_opens_a_session_and_execute_polls_to_terminal() -> None:
    transport = _Transport(execution_states=["succeeded"])
    driver = _driver(transport)

    loaded = driver.load("qwen-small", cold=True)
    executed = driver.execute("qwen-small", {"messages": [{"role": "user", "content": "hi"}]})

    assert loaded["session_id"] == "session-1" and loaded["cold"] is True and loaded["state"] == "active"
    assert [(method, path) for method, path, _b in transport.requests] == [
        ("POST", "/internal/sessions"), ("GET", "/internal/sessions/session-1"),
        ("POST", "/internal/executions"), ("GET", "/internal/executions/execution-2")]
    create = transport.requests[0][2]
    assert create["model_id"] == "qwen-small" and create["correlation_id"] == "acceptance"
    assert create["idempotency_key"].startswith("session-") and len(create["idempotency_key"]) > len("session-")
    execution = next(body for method, path, body in transport.requests
                     if (method, path) == ("POST", "/internal/executions"))
    assert execution["session_token"] == "tok-1" and execution["operation"] == "chat"
    assert create["idempotency_key"] != execution["idempotency_key"]  # keys never collide
    assert execution["input"] == {"inline": {"messages": [{"role": "user", "content": "hi"}]}}
    assert executed["state"] == "succeeded" and executed["compute_quiescent"] is True
    assert executed["instance"]["container_id"] == "abc" and executed["fence"]["attempt"] == 1  # attribution


def test_the_driver_refuses_a_terminal_state_it_never_reached() -> None:
    transport = _Transport(execution_states=["running"] * 4)
    driver = _driver(transport)
    driver.deadline_seconds = 0.0
    driver.load("qwen-small", cold=True)

    with pytest.raises(DriverError, match="did not reach a terminal state"):
        driver.execute("qwen-small", {"messages": []})


def test_a_load_waits_until_the_session_is_active_and_refuses_a_stuck_one() -> None:
    transport = _Transport()
    driver = _driver(transport)
    transport.session_settled = "blocked"  # the session dies during preparation

    with pytest.raises(DriverError, match="before it became active"):
        driver.load("qwen-small", cold=True)

    stuck = _Transport()
    stuck_driver = _driver(stuck)
    stuck_driver.ready_timeout_seconds = 0.0
    with pytest.raises(DriverError, match="did not become active"):
        stuck_driver.load("qwen-small", cold=True)


def test_the_driver_refuses_execute_before_a_load() -> None:
    driver = _driver(_Transport())

    with pytest.raises(DriverError, match="must load it first"):
        driver.execute("qwen-small", {"messages": []})
    # a stop with nothing open is a no-op, not a failure (the matrix stops, then reloads)
    assert driver.stop("qwen-small") == {"session_id": None, "state": None, "stop_proven": False,
                                         "note": "no session was open"}


def test_an_api_refusal_is_reported_not_guessed() -> None:
    transport = _Transport(fail_on="/internal/sessions")
    driver = _driver(transport)

    with pytest.raises(DriverError, match="409"):
        driver.load("qwen-small", cold=True)


def test_stop_closes_the_session_and_cleanup_afterwards_is_a_no_op() -> None:
    transport = _Transport()
    driver = _driver(transport)
    driver.load("qwen-small", cold=True)

    stopped = driver.stop("qwen-small")
    assert stopped["state"] == "closed"
    assert transport.requests[-1][1].endswith("/internal/sessions/session-1/close")
    assert transport.requests[-1][2] == {"session_token": "tok-1"}  # the API refuses a close without it
    assert driver.cleanup("qwen-small")["state"] == "closed"  # nothing left open is not an error


def test_the_operation_is_derived_from_the_request_or_refused() -> None:
    from model_scheduler.acceptance.driver import DriverError, operation_of

    assert operation_of({"messages": [{"role": "user", "content": "hi"}]}) == "chat"
    assert operation_of({"messages": [{"role": "user", "content": [
        {"type": "text", "text": "describe"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}}]}]}) == "vision"
    assert operation_of({"input": ["a", "b"]}) == "embeddings"
    assert operation_of({"query": "q", "documents": ["d"]}) == "rerank"
    with pytest.raises(DriverError, match="cannot derive the operation"):
        operation_of({"unexpected": True})  # never guessed


def test_an_image_request_is_sent_as_the_vision_operation() -> None:
    transport = _Transport(execution_states=["succeeded"])
    driver = _driver(transport)
    driver.load("qwen-small", cold=True)

    driver.execute("qwen-small", {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "x"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}}]}]})

    creates = [body for method, path, body in transport.requests if path == "/internal/executions"]
    assert creates and creates[0]["operation"] == "vision"


def test_cancel_carries_the_session_token_and_a_stop_without_a_session_is_a_no_op() -> None:
    transport = _Transport(execution_states=["succeeded"])
    driver = _driver(transport)
    driver.load("qwen-small", cold=True)
    started = driver.start("qwen-small", {"messages": [{"role": "user", "content": "hi"}]})

    driver.cancel("qwen-small", started["execution_id"])
    cancels = [(method, path, body) for method, path, body in transport.requests if path.endswith("/cancel")]

    assert cancels and cancels[0][2] == {"session_token": "tok-1"}  # the API demands exactly this body
    stopped = driver.stop("qwen-small")
    assert stopped["stop_proven"] is True and stopped["state"] == "closed"  # the API's CLOSED view is the proof
    again = driver.stop("qwen-small")  # the matrix stops, then reloads: the proof survives
    assert again["stop_proven"] is True and again["session_id"] is None


def test_a_second_load_closes_the_previous_session_first() -> None:
    """The matrix wants independent cold starts: stop, then start from nothing."""
    transport = _Transport()
    driver = _driver(transport)
    driver.load("qwen-small", cold=True)

    driver.load("qwen-small", cold=True)

    closes = [path for method, path, _b in transport.requests if path.endswith("/close")]
    creates = [path for method, path, _b in transport.requests if path == "/internal/sessions"]
    assert closes and len(creates) == 2  # nothing stacks: two sessions, never two at once


def test_a_waiting_load_heartbeats_the_session() -> None:
    """The C04 policy expires an idle session in 30 s; a cold start takes longer."""
    transport = _Transport()
    driver = _driver(transport)
    driver.heartbeat_interval_seconds = 0.0  # every poll must beat, deterministically

    driver.load("qwen-small", cold=True)

    beats = [path for method, path, _b in transport.requests if path.endswith("/heartbeat")]
    assert beats, "the wait must heartbeat instead of letting the session idle out"
    beat = next(body for method, path, body in transport.requests if path.endswith("/heartbeat"))
    assert beat == {"session_token": "tok-1"}  # the token the API requires


def test_boot_id_comes_from_the_peer_endpoint() -> None:
    transport = _Transport()
    driver = _driver(transport)

    assert driver.boot_id() == "boot-x"
    assert transport.requests == [("GET", "/internal/peer", None)]


def test_cancel_uses_the_official_cancel_path() -> None:
    transport = _Transport()
    driver = _driver(transport)
    driver.load("qwen-small", cold=True)
    started = driver.start("qwen-small", {"messages": []})

    cancelled = driver.cancel("qwen-small", started["execution_id"])
    followed = transport.request("GET", f"/internal/executions/{started['execution_id']}")

    assert (transport.requests[-2][0], transport.requests[-2][1]) == ("POST", "/internal/executions/execution-2/cancel")
    assert transport.requests[-2][2] == {"session_token": "tok-1"}
    assert cancelled["execution_id"] == "execution-2" and followed.document["state"] == "cancelled"


def test_the_transport_speaks_http_over_a_unix_socket_and_sends_the_version_header() -> None:
    path = Path("/tmp") / f"sms-driver-{os.getpid()}.sock"
    path.unlink(missing_ok=True)
    seen: dict = {}

    def serve() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(path))
            listener.listen(1)
            connection, _ = listener.accept()
            with connection:
                raw = connection.recv(65536)
                seen["head"] = raw.decode("latin-1").split("\r\n\r\n")[0]
                seen["body"] = raw.decode("latin-1").split("\r\n\r\n")[1]
                payload = json.dumps({"owner_token": "tok-9", "state": "preparing"}).encode()
                connection.sendall(b"HTTP/1.1 201 Created\r\nContent-Length: " + str(len(payload)).encode()
                                   + b"\r\n\r\n" + payload)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    for _ in range(50):
        if path.exists():
            break
        time.sleep(0.01)
    try:
        response = UnixControlTransport(path).request("POST", "/internal/sessions", {"model_id": "qwen-small"})
    finally:
        path.unlink(missing_ok=True)

    assert response.status == 201 and response.document["owner_token"] == "tok-9"
    assert f"{PROTOCOL_VERSION_HEADER}: 1" in seen["head"]
    assert json.loads(seen["body"]) == {"model_id": "qwen-small"}


class _ResultBlobTransport(_Transport):
    """The real terminal view: the result *references* an output blob, it never ships it."""

    def __init__(self, blob: Mapping[str, Any] | None = None, *, publish_result: bool = True,
                 **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.blob_payload = blob
        self.publish_result = publish_result

    def request(self, method: str, path: str, body=None) -> ApiResponse:
        if method == "GET" and path.startswith("/internal/blobs/"):
            self.requests.append((method, path, body))  # the base transport only sees \/internal\/ routes
            if self.blob_payload is None:
                return ApiResponse(404, {"error": {"code": "not_found", "message": "no such blob"}})
            raw = json.dumps(self.blob_payload).encode("utf-8")
            return ApiResponse(200, {}, raw=raw)
        if path.startswith("/internal/executions/") and method == "GET":
            response = super().request(method, path, body)
            document = dict(response.document)
            if self.publish_result:
                document["result"] = {"blob_id": "blob-1", "owner": "uid:1000", "sha256": "b" * 64,
                                      "size_bytes": 21, "media_type": "application/json"}
            return ApiResponse(response.status, document, raw=json.dumps(document).encode("utf-8"))
        return super().request(method, path, body)


class _Sampler:
    """The injected boundary of the device: no subprocess, no tegrastats, pure rows."""

    def __init__(self, rows: tuple[dict, ...] = (), **kwargs: Any) -> None:
        self.rows = rows
        self.instance = kwargs.get("instance")
        self.started = False
        self.stopped = False

    def start(self, *, instance=None) -> None:
        self.started = True
        self.instance = instance

    def stop(self) -> tuple[dict, ...]:
        self.stopped = True
        return self.rows


def _chat_payload() -> dict:
    return {"messages": [{"role": "user", "content": "hi"}]}


def test_the_output_of_an_execution_is_the_published_blob_not_the_view() -> None:
    transport = _ResultBlobTransport({"message": {"content": "hello"}}, execution_states=["succeeded"])
    driver = ControlApiCaseDriver(transport, sleep=lambda _seconds: None)
    driver.load("qwen-small", cold=True)

    executed = driver.execute("qwen-small", _chat_payload())

    assert executed["output"] == {"message": {"content": "hello"}}
    assert ("GET", "/internal/blobs/blob-1") in [(method, path) for method, path, _b in transport.requests]


def test_an_execution_without_a_result_reference_has_no_output() -> None:
    transport = _ResultBlobTransport(None, publish_result=False, execution_states=["succeeded"])
    driver = ControlApiCaseDriver(transport, sleep=lambda _seconds: None)
    driver.load("qwen-small", cold=True)

    executed = driver.execute("qwen-small", _chat_payload())

    assert executed["output"] is None  # a claimed output without its blob is never invented
    assert not [path for _m, path, _b in transport.requests if path.startswith("/internal/blobs/")]


def test_a_missing_output_blob_is_reported_instead_of_dropped() -> None:
    transport = _ResultBlobTransport(None, execution_states=["succeeded"])
    driver = ControlApiCaseDriver(transport, sleep=lambda _seconds: None)
    driver.load("qwen-small", cold=True)

    with pytest.raises(DriverError, match="cannot be read"):
        driver.execute("qwen-small", _chat_payload())


def test_an_execution_is_attributed_to_the_instance_that_ran_it() -> None:
    transport = _ResultBlobTransport({"message": {"content": "hi"}}, execution_states=["succeeded"])
    driver = ControlApiCaseDriver(transport, sleep=lambda _seconds: None)
    driver.load("qwen-small", cold=True)

    executed = driver.execute("qwen-small", _chat_payload())

    assert executed["provider"] == "llama-cpp-1/sms-llama-cpp@sha256:" + "a" * 64
    assert executed["instance"]["container_id"] == "abc"


def test_device_activity_is_derived_from_raw_rows_never_from_a_boolean() -> None:
    transport = _ResultBlobTransport({"message": {"content": "hi"}}, execution_states=["succeeded"])
    rows = ({"kind": "tegrastats", "raw": "RAM 100/32000MB GR3D_FREQ 41%"},
            {"kind": "tegrastats", "raw": "RAM 200/32000MB GR3D_FREQ 87%"},
            {"kind": "proc_maps", "raw": "7f00 r-xp libcudart.so.12"})
    sampler = _Sampler(rows)
    driver = ControlApiCaseDriver(transport, sleep=lambda _seconds: None, sampler_factory=lambda: sampler)
    driver.load("qwen-small", cold=True)

    executed = driver.execute("qwen-small", _chat_payload())

    assert executed["device_activity"] == {"gr3d_peak_pct": 87, "cuda_library_mapped": True,
                                          "raw_samples": {"proc_maps": 1, "tegrastats": 2}}
    assert sampler.started and sampler.stopped
    assert sampler.instance is not None and sampler.instance["container_id"] == "abc"
    # the rows travel with the result so the executor can persist them as material
    assert executed["samples"] == rows


def test_a_fixtures_generation_controls_reach_the_protocol() -> None:
    """The boundary round must consume its output budget: it asks for that in the protocol's terms."""
    transport = _ResultBlobTransport({"usage": {"prompt_tokens": 1, "completion_tokens": 1}},
                                     execution_states=["succeeded"])
    driver = ControlApiCaseDriver(transport, sleep=lambda _seconds: None)
    driver.load("qwen-small", cold=True)

    driver.execute("qwen-small", {**_chat_payload(), "max_tokens": 4096, "ignore_eos": True, "n_parallel": 2,
                                  "unexpected": "dropped"})

    body = [payload for method, path, payload in transport.requests if path == "/internal/executions"][0]
    assert body["parameters"] == {"ignore_eos": True}  # only what the protocol defines


def test_the_round_reports_what_it_really_consumed() -> None:
    """The boundary is judged on this round's own usage, never on the declaration."""
    output = {"message": {"content": "hi"}, "usage": {"prompt_tokens": 1234, "completion_tokens": 56}}
    transport = _ResultBlobTransport(output, execution_states=["succeeded"])
    driver = ControlApiCaseDriver(transport, sleep=lambda _seconds: None)
    driver.load("qwen-small", cold=True)

    executed = driver.execute("qwen-small", {**_chat_payload(), "n_parallel": 2})

    assert executed["observed"] == {"input_tokens": 1234, "output_tokens": 56, "parallel": 2}


def test_the_observed_boundary_counts_the_images_it_really_sent() -> None:
    import base64

    from model_scheduler.acceptance.fixtures import render_test_png

    url = "data:image/png;base64," + base64.b64encode(render_test_png(8, 8)).decode("ascii")
    request = {"messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": url}},
        {"type": "image_url", "image_url": {"url": url}},
        {"type": "text", "text": "describe"}]}]}
    transport = _ResultBlobTransport({"usage": {"prompt_tokens": 900, "completion_tokens": 4}},
                                     execution_states=["succeeded"])
    driver = ControlApiCaseDriver(transport, sleep=lambda _seconds: None)
    driver.load("qwen-small", cold=True)

    executed = driver.execute("qwen-small", request)

    # the edge is read back from the image that was sent, not copied from the envelope
    assert executed["observed"]["images"] == 2 and executed["observed"]["image_edge_pixels"] == 8
    assert executed["observed"]["input_tokens"] == 900


def test_a_round_without_usage_reports_no_token_boundary() -> None:
    transport = _ResultBlobTransport({"message": {"content": "hi"}}, execution_states=["succeeded"])
    driver = ControlApiCaseDriver(transport, sleep=lambda _seconds: None)
    driver.load("qwen-small", cold=True)

    executed = driver.execute("qwen-small", _chat_payload())

    assert executed["observed"] == {"parallel": None}  # unreached is unreached, never declared reached


def test_a_case_without_samples_records_no_device_activity() -> None:
    transport = _ResultBlobTransport({"message": {"content": "hi"}}, execution_states=["succeeded"])
    driver = ControlApiCaseDriver(transport, sleep=lambda _seconds: None, sampler_factory=lambda: _Sampler(()))
    driver.load("qwen-small", cold=True)

    executed = driver.execute("qwen-small", _chat_payload())

    assert executed["device_activity"] is None  # unattributable, therefore never passed
    assert executed["samples"] == ()
