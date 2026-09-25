"""CT10: the real loopback HTTP ports (A08/A09/A11), exercised against a real server.

The transport under test is not a fake: a real HTTP server answers here, so the exact
bytes that leave, the exact bytes that arrive, the streamed read, the request id and a
stalled stream are observed instead of simulated. The service port reads a status
document and proves a release the same way it will on the device.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from model_scheduler.acceptance import chat_compat as cc
from model_scheduler.acceptance import lab_http as lh
from model_scheduler.contracts_v2 import Envelope

MODEL_ID = "qwen36-27b"
SLOW_MODEL = "slow-model"
ENVELOPE = Envelope(ctx_size=8192, max_input_tokens=4096, max_output_tokens=1024, max_parallel=1,
                    max_image_tokens=1280, max_image_edge_pixels=1024, max_images=1)
DEADLINE = 60.0

TOOL_ROUND = {"id": "chatcmpl-http-1", "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
    "role": "assistant", "content": None, "reasoning_content": "需要一个天气工具",
    "tool_calls": [{"id": "call_http_1", "type": "function",
                    "function": {"name": "get_weather", "arguments": "{\"city\":\"Beijing\"}"}}]}}],
    "usage": {"prompt_tokens": 210, "completion_tokens": 33}}
STOP_ROUND = {"id": "chatcmpl-http-2", "choices": [{"index": 0, "finish_reason": "stop", "message": {
    "role": "assistant", "content": "SMS_WEATHER_OK_27", "reasoning_content": "已取到标记"}}],
    "usage": {"prompt_tokens": 260, "completion_tokens": 6}}
#: The exact SSE fixture of acceptance §6, as the service would send it.
SSE_TOOL_ROUND = (
    'data: {"choices":[{"index":0,"delta":{"role":"assistant","reasoning_content":"查询天气"},"finish_reason":null}]}\n\n'
    'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_http_1","type":"function",'
    '"function":{"name":"get_weather","arguments":"{\\"city\\":"}}]},"finish_reason":null}]}\n\n'
    'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"Beijing\\"}"}}]},'
    '"finish_reason":null}]}\n\n'
    'data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n\n'
    'data: {"choices":[],"usage":{"prompt_tokens":123,"completion_tokens":45,"total_tokens":168}}\n\n'
    'data: [DONE]\n\n').encode("utf-8")
SSE_STOP_ROUND = ('data: {"choices":[{"index":0,"delta":{"content":"SMS_WEATHER_OK_27"},"finish_reason":"stop"}]}\n\n'
                  'data: [DONE]\n\n').encode("utf-8")


class _Service:
    """A real loopback service: canned answers, a status document and a release route."""

    def __init__(self) -> None:
        self.received: list[tuple[str, bytes]] = []
        self.status_document: dict = {"models": {MODEL_ID: {"state": "active", "generation": 1, "in_flight": 0,
                                                            "cancelling": 0}}}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:  # a test server does not spam the log
                pass

            def _send(self, status: int, raw: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("content-type", content_type)
                self.send_header("x-request-id", "req-1")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:  # noqa: N802 - the handler API
                if self.path == lh.SERVICE_STATUS_PATH:
                    self._send(200, json.dumps(outer.status_document).encode("utf-8"), "application/json")
                else:
                    self._send(404, b"{}", "application/json")

            def do_POST(self) -> None:  # noqa: N802 - the handler API
                body = self.rfile.read(int(self.headers.get("content-length", "0")))
                outer.received.append((self.path, body))
                if self.path == lh.CHAT_PATH:
                    request = json.loads(body.decode("utf-8"))
                    if request.get("model") == SLOW_MODEL:
                        time.sleep(2.0)
                    second_round = any(message.get("role") == "tool" for message in request.get("messages", []))
                    if request.get("stream"):
                        self._send(200, SSE_STOP_ROUND if second_round else SSE_TOOL_ROUND, "text/event-stream")
                    else:
                        self._send(200, json.dumps(STOP_ROUND if second_round else TOOL_ROUND).encode("utf-8"),
                                   "application/json")
                    return
                if self.path.endswith("/unload"):
                    outer.status_document["models"][MODEL_ID] = {"state": "unloaded", "generation": 2,
                                                                 "in_flight": 0, "cancelling": 0}
                    self._send(200, json.dumps({"ok": True}).encode("utf-8"), "application/json")
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


def _transport(base_url: str, *, read_idle_seconds: float = 30.0) -> lh.HttpCompatTransport:
    return lh.HttpCompatTransport(base_url=base_url, connect_seconds=5.0, read_idle_seconds=read_idle_seconds,
                                  total_seconds=DEADLINE, auth="secret-token")


def test_a08_the_transport_carries_the_frozen_bytes_both_ways(tmp_path: Path) -> None:
    with _Service() as service:
        scenario = cc.tools_scenario(MODEL_ID, envelope=ENVELOPE, stream=True, deadline_seconds=DEADLINE)
        spec = cc.CaseSpec(case_id=f"B:{MODEL_ID}:cap:tools", variant="sse-hot", scenario=scenario)
        driver = cc.CompatDriver(_transport(service.base_url), aggregator=cc.SseAggregator)

        run = driver.run(spec, tmp_path / "http-case")

        assert run.tool_call_id == "call_http_1" and run.final_content == "SMS_WEATHER_OK_27"
        assert cc.evaluate_case(tmp_path / "http-case")["problems"] == []
        assert service.received[0][1] == scenario.first_request_json  # the frozen bytes really left
        assert json.loads(service.received[0][1])["stream"] is True
        assert (tmp_path / "http-case" / "round-1" / cc.RESPONSE_FILE).read_bytes() == SSE_TOOL_ROUND
        assert (tmp_path / "http-case" / "round-2" / cc.RESPONSE_FILE).read_bytes() == SSE_STOP_ROUND
        # The second round is built from the real streamed call, not from a fixture.
        assert json.loads(service.received[1][1])["messages"][-1]["tool_call_id"] == "call_http_1"


def test_a09_a_stalled_stream_is_a_deadline_failure_not_a_hang(tmp_path: Path) -> None:
    with _Service() as service:
        scenario = cc.tools_scenario(SLOW_MODEL, envelope=ENVELOPE, stream=False, deadline_seconds=DEADLINE)
        spec = cc.CaseSpec(case_id=f"B:{SLOW_MODEL}:cap:tools", variant="json-hot", scenario=scenario)
        driver = cc.CompatDriver(_transport(service.base_url, read_idle_seconds=0.3))
        started = time.monotonic()

        with pytest.raises(cc.CompatError, match="200"):
            driver.run(spec, tmp_path / "stalled")

        assert time.monotonic() - started < 2.0  # the idle bound cut it, the server's sleep did not
        assert not (tmp_path / "stalled" / "round-2").exists()  # no second round is improvised


def test_a11_the_service_port_reports_one_model_and_proves_its_release() -> None:
    with _Service() as service:
        port = lh.LabServicePort(base_url=service.base_url, poll_seconds=0.05, timeout_seconds=5.0)

        assert port.model_state(MODEL_ID)["generation"] == 1
        evidence = port.unload(MODEL_ID)

        assert evidence["unload_status"] == 200 and evidence["stopped"] is True
        assert evidence["before"]["state"] == "active" and evidence["after"]["state"] == "unloaded"
        assert evidence["after"]["generation"] == 2  # the reload case cites both generations
        assert port.model_state(MODEL_ID)["in_flight"] == 0

        with pytest.raises(lh.LabHttpError, match="no row"):
            port.model_state("qwen25vl-7b")
