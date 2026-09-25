"""CT10: the real loopback HTTP ports the candidate suite runs on (acceptance §2/§7).

The compat transport posts the *frozen* request bytes to the service under test and
returns the service's own bytes: a streamed answer is read incrementally, so a stalled
stream becomes a deadline failure instead of a hang, and the raw bytes are what the
evaluator later recomputes from. The service port reads the status document and asks the
management API to release a model before a cold or reloaded round; it never substitutes a
model upstream for the service, and every figure it cannot read stays an error.

Both ports carry the site's frozen timeouts: `connect_seconds` for the connection,
`read_idle_seconds` for one read, and the scenario's own deadline for the whole round.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

import httpx

from . import chat_compat as cc

SERVICE_STATUS_PATH = "/api/status"
UNLOAD_PATH = "/api/models/{model_id}/unload"
REQUEST_ID_HEADER = "x-request-id"
CHAT_PATH = "/v1/chat/completions"


class LabHttpError(RuntimeError):
    """A port that cannot answer is material for the case, never a pass."""


def _failure(stage: str, exc: BaseException) -> bytes:
    return json.dumps({"error": f"{stage}: {type(exc).__name__}: {exc}"}).encode("utf-8")


class HttpCompatTransport:
    """`CompatTransport`: post bytes, get bytes; a stream is read chunk by chunk."""

    def __init__(self, *, base_url: str, connect_seconds: float, read_idle_seconds: float, total_seconds: float,
                 auth: str | None = None, clock: Callable[[], float] = time.monotonic,
                 client_factory: Callable[..., Any] = httpx.Client) -> None:
        self._url = base_url.rstrip("/") + CHAT_PATH
        self._connect = connect_seconds
        self._read_idle = read_idle_seconds
        self._total = total_seconds
        self._auth = auth
        self._clock = clock
        self._client_factory = client_factory

    def headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self._auth:
            headers["authorization"] = f"Bearer {self._auth}"
        return headers

    def chat(self, request_json: bytes, *, stream: bool, deadline: float) -> cc.CompatRound:
        budget = min(float(deadline), float(self._total))
        started = self._clock()
        timeout = httpx.Timeout(connect=self._connect, read=min(self._read_idle, budget),
                                write=self._read_idle, pool=self._connect)
        try:
            with self._client_factory(timeout=timeout, follow_redirects=False) as client:
                with client.stream("POST", self._url, headers=self.headers(), content=request_json) as answer:
                    chunks: list[bytes] = []
                    for chunk in answer.iter_bytes():
                        chunks.append(chunk)
                        if self._clock() - started > budget:
                            return cc.CompatRound(status=0, raw=_failure("the round exceeded its deadline",
                                                                         TimeoutError(f"{budget}s")),
                                                  request_id=None, headers={})
                    return cc.CompatRound(status=answer.status_code, raw=b"".join(chunks),
                                          request_id=answer.headers.get(REQUEST_ID_HEADER),
                                          headers=dict(answer.headers))
        except Exception as exc:  # noqa: BLE001 - a transport failure is material, not a crash
            return cc.CompatRound(status=0, raw=_failure("the compat request failed", exc), request_id=None,
                                  headers={})


@dataclass
class LabServicePort:
    """The deployment's own view: the status document and the release of one model."""

    base_url: str
    timeout_seconds: float = 60.0
    poll_seconds: float = 2.0
    clock: Callable[[], float] = field(default=time.monotonic)
    wait: Callable[[float], None] = field(default=time.sleep)
    get: Callable[..., Any] = field(default=httpx.get)
    post: Callable[..., Any] = field(default=httpx.post)

    def status(self) -> dict:
        try:
            answer = self.get(self.base_url.rstrip("/") + SERVICE_STATUS_PATH, timeout=self.timeout_seconds)
        except Exception as exc:  # noqa: BLE001
            raise LabHttpError(f"cannot read {SERVICE_STATUS_PATH}: {type(exc).__name__}: {exc}") from exc
        try:
            document = answer.json()
        except ValueError as exc:
            raise LabHttpError(f"{SERVICE_STATUS_PATH} did not answer JSON") from exc
        if not isinstance(document, dict):
            raise LabHttpError(f"{SERVICE_STATUS_PATH} did not answer an object")
        return document

    def model_state(self, model_id: str) -> dict:
        """One model's row of the status document; a model the service does not know is an error."""
        models = self.status().get("models")
        row = models.get(model_id) if isinstance(models, Mapping) else None
        if not isinstance(row, Mapping):
            raise LabHttpError(f"the status document has no row for {model_id!r}")
        return dict(row)

    def unload(self, model_id: str, *, timeout: float | None = None) -> dict:
        """Ask the management API to release one model, and prove the state it reached."""
        limit = self.timeout_seconds if timeout is None else timeout
        started = self.clock()
        before = self._state_or_error(model_id)
        try:
            answer = self.post(self.base_url.rstrip("/") + UNLOAD_PATH.format(model_id=model_id), timeout=limit)
            status = int(getattr(answer, "status_code", 0))
            body = _json_or_none(getattr(answer, "content", b""))
        except Exception as exc:  # noqa: BLE001 - a refused release is material, not a crash
            return {"model_id": model_id, "unload_status": 0, "error": f"{type(exc).__name__}: {exc}",
                    "before": before, "after": None, "stopped": False,
                    "waited_seconds": round(self.clock() - started, 3)}
        after = before
        deadline = started + limit
        while self.clock() < deadline:
            after = self._state_or_error(model_id)
            if _is_unloaded(after):
                break
            self.wait(self.poll_seconds)
        return {"model_id": model_id, "unload_status": status, "unload_body": body, "before": before, "after": after,
                "stopped": _is_unloaded(after), "waited_seconds": round(self.clock() - started, 3)}

    def _state_or_error(self, model_id: str) -> dict:
        try:
            return self.model_state(model_id)
        except LabHttpError as exc:
            return {"error": str(exc)}


def _is_unloaded(state: Mapping[str, Any]) -> bool:
    return state.get("state") == "unloaded" and not state.get("in_flight") and not state.get("cancelling")


def _json_or_none(raw: bytes) -> dict | None:
    try:
        document = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return document if isinstance(document, dict) else None
