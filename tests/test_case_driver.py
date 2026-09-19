"""The real case driver: exact API mapping, honest refusals, and the transport."""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import threading
import time

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

    def request(self, method: str, path: str, body=None) -> ApiResponse:
        self.requests.append((method, path, body))
        if self.fail_on is not None and self.fail_on in path:
            return ApiResponse(409, {"error": {"code": "conflict", "message": "already open"}})
        if method == "GET" and path == "/internal/peer":
            return ApiResponse(200, {"owner": "uid:1000", "boot_id": "boot-x", "via": "sms-control"})
        if path == "/internal/sessions" and method == "POST":
            self._sessions += 1
            return ApiResponse(201, {"session_id": f"session-{self._sessions}", "state": "preparing",
                                     "phase": "loading", "boot_id": "boot-x", "model_id": body["model_id"],
                                     "expires_in_ms": 30000, "hard_remaining_ms": 60000,
                                     "owner_token": f"tok-{self._sessions}", "error": None})
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

    assert loaded["session_id"] == "session-1" and loaded["cold"] is True and loaded["boot_id"] == "boot-x"
    assert [(method, path) for method, path, _b in transport.requests] == [
        ("POST", "/internal/sessions"), ("POST", "/internal/executions"),
        ("GET", "/internal/executions/execution-2")]  # create said running, one read said succeeded
    create = transport.requests[0][2]
    assert create == {"model_id": "qwen-small", "idempotency_key": "session-1", "correlation_id": "acceptance"}
    execution = transport.requests[1][2]
    assert execution["session_token"] == "tok-1" and execution["operation"] == "inference"
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


def test_the_driver_refuses_execute_before_a_load() -> None:
    driver = _driver(_Transport())

    with pytest.raises(DriverError, match="must load it first"):
        driver.execute("qwen-small", {"messages": []})
    with pytest.raises(DriverError, match="must load it first"):
        driver.stop("qwen-small")


def test_an_api_refusal_is_reported_not_guessed() -> None:
    transport = _Transport(fail_on="/internal/sessions")
    driver = _driver(transport)

    with pytest.raises(DriverError, match="409"):
        driver.load("qwen-small", cold=True)


def test_a_second_load_is_refused_and_stop_closes_the_session() -> None:
    transport = _Transport()
    driver = _driver(transport)
    driver.load("qwen-small", cold=True)

    with pytest.raises(DriverError, match="must not stack"):
        driver.load("qwen-small", cold=True)

    stopped = driver.stop("qwen-small")
    assert stopped["state"] == "closed"
    assert transport.requests[-1][1].endswith("/internal/sessions/session-1/close")
    assert driver.cleanup("qwen-small")["state"] == "closed"  # nothing left open is not an error


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

    assert transport.requests[-2] == ("POST", "/internal/executions/execution-2/cancel", {})
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
