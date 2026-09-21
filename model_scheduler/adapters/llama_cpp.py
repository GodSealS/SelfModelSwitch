"""llama.cpp GGUF adapter: registered-protocol inference with trusted counting (M04/P15).

The adapter is the first real `BackendPort` for `llama-cpp-gguf-v1`. It talks to
a loopback llama-server using the paths pinned in `tests/fixtures/llama_cpp_v1.json`.
llama-swap, when injected, is used only as a load/unload lifecycle control; this
module never asks it to route inference.

C06 rules this module enforces before any dispatch (the shared input checks
live in `model_scheduler.envelope_validator`, P20):

* chat/vision consume OpenAI `messages`; embeddings consume `input`; rerank
  consumes `query`/`documents`;
* text tokens come from `/apply-template` then `/tokenize`, never from character
  length; an undetermined image is charged `envelope.max_image_tokens`;
* remote image URLs are refused; only `data:image/png` and `data:image/jpeg`
  data URLs are decoded, and decoded pixel edges are checked;
* a counting or envelope failure refuses the request, so the chat/embeddings
  path is never reached;
* HTTP 200 and an idle `/slots` row are request-idle facts, not device
  quiescence. `compute_quiescent=true` is not claimed here; the documented
  fallback is an independently proven STOPPED plus a reload.

Redirects are never followed. The scheduler process stays free of Torch/ORT:
this module only uses httpx and the stdlib.
"""
from __future__ import annotations

import asyncio
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import httpx

from ..contracts_v2 import (
    GGUF_PROFILE,
    ContractError,
    ModelSpec,
    RuntimeSpec,
    canonical_json_bytes,
    require_startable_profile,
)
from ..control_protocol_v1 import Fence, InstanceIdentity
from ..envelope_validator import (
    EnvelopeError,
    check_chat_budget,
    check_images,
    collect_image_sizes,
    effective_max_tokens,
)
from ..ports_v3 import CancelAck, ExecutionHandle, ExecutionRequest, Observation, StopAck

FIXTURE_PATH = Path(__file__).resolve().parent.parent.parent / "tests" / "fixtures" / "llama_cpp_v1.json"
UTC = timezone.utc


class AdapterError(ValueError):
    """A refused adapter action; `code` is a control-protocol error code."""

    def __init__(self, message: str, code: str = "contract_violation") -> None:
        super().__init__(message)
        self.code = code


def _reject_non_finite(_token: str) -> object:
    raise ValueError("non-finite JSON number")


def _json_object(value: Any, where: str) -> dict:
    if not isinstance(value, dict):
        raise AdapterError(f"{where}: expected an object", "contract_violation")
    return value


def _finite_numbers(values: Any, where: str) -> list[float]:
    if not isinstance(values, list) or not values:
        raise AdapterError(f"{where}: expected a non-empty number list", "backend_failed")
    out: list[float] = []
    for item in values:
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item):
            raise AdapterError(f"{where}: values must be finite numbers", "backend_failed")
        out.append(float(item))
    return out


def load_protocol_fixture(path: Path | None = None) -> dict:
    fixture_path = Path(path) if path is not None else FIXTURE_PATH
    try:
        document = json.loads(fixture_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdapterError(f"cannot read llama.cpp protocol fixture: {exc}", "backend_failed") from exc
    if document.get("schema_version") != 1 or document.get("profile_id") != GGUF_PROFILE:
        raise AdapterError("llama.cpp protocol fixture does not match llama-cpp-gguf-v1", "contract_violation")
    if document.get("http", {}).get("follow_redirects") is not False:
        raise AdapterError("fixture must pin follow_redirects=false", "contract_violation")
    slots = document.get("slot_protocol") or {}
    if slots.get("trusted_for_device_quiescence") is not False:
        raise AdapterError("fixture must not treat slots as device quiescence", "contract_violation")
    if slots.get("fallback_termination") != "independent_STOPPED":
        raise AdapterError("fixture must fall back to independent STOPPED", "contract_violation")
    return document


class LlamaCppAdapter:
    """One llama.cpp GGUF runtime identity plus one registered model envelope."""

    def __init__(
        self,
        *,
        runtime: RuntimeSpec,
        model: ModelSpec,
        inference_base_url: str,
        client: httpx.AsyncClient,
        identity: InstanceIdentity | None | Callable[[], InstanceIdentity | None] = None,
        fixture_path: Path | None = None,
        control: Any | None = None,
    ) -> None:
        try:
            profile = require_startable_profile(runtime)
        except ContractError as exc:
            raise AdapterError(str(exc), "contract_violation") from exc
        if profile.profile_id != GGUF_PROFILE or runtime.profile_id != GGUF_PROFILE:
            raise AdapterError(
                f"profile {runtime.profile_id!r} is not the llama.cpp GGUF profile",
                "contract_violation",
            )
        if model.runtime_id != runtime.runtime_id:
            raise AdapterError("model is not bound to this runtime", "contract_violation")
        self.runtime_id = runtime.runtime_id
        self.profile_id = runtime.profile_id
        self._runtime = runtime
        self._model = model
        self._base = inference_base_url.rstrip("/")
        self._client = client
        self._identity = identity
        self._control = control
        self._fixture = load_protocol_fixture(fixture_path)
        self.slot_protocol = dict(self._fixture["slot_protocol"])
        self.stop_reload_cost = dict(self.slot_protocol["stop_reload_cost"])
        self.last_http_complete = False
        self.last_slot_idle = False
        self._results: dict[str, bytes] = {}

    def _resolve_identity(self) -> InstanceIdentity | None:
        """The identity in force right now; a provider lets the lifecycle bridge reload it (P16)."""
        identity = self._identity() if callable(self._identity) else self._identity
        return identity

    @property
    def current_identity(self) -> InstanceIdentity | None:
        return self._resolve_identity()

    def take_result(self, execution_id: str) -> bytes | None:
        """The validated response bytes for one execution, delivered exactly once."""
        return self._results.pop(execution_id, None)

    def claims_device_quiescence(self) -> bool:
        """HTTP completion is never device-idle evidence."""
        return False

    def _endpoint(self, name: str) -> str:
        entry = self._fixture["endpoints"][name]
        return f"{self._base}{entry['path']}"

    async def _send(self, method: str, url: str, *, deadline: float, json_body: Mapping | None = None) -> httpx.Response:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise AdapterError("execution timed out before dispatch", "execution_timeout")
        # The execution deadline is the only authority over a request's lifetime:
        # an injected client's own default (httpx ships 5 s) must never cut a real
        # inference short, and a timeout that does happen means the deadline
        # elapsed — never "upstream unavailable".
        request = self._client.build_request(method, url, json=None if json_body is None else dict(json_body),
                                             timeout=remaining)
        try:
            async with asyncio.timeout(remaining):
                response = await self._client.send(request, follow_redirects=False)
        except (asyncio.TimeoutError, httpx.TimeoutException) as exc:
            raise AdapterError("upstream timed out", "execution_timeout") from exc
        except httpx.HTTPError as exc:
            raise AdapterError(f"upstream unavailable: {exc}", "backend_failed") from exc
        if 300 <= response.status_code < 400:
            await response.aclose()
            raise AdapterError("upstream redirect is not followed", "backend_failed")
        return response

    async def _json(self, method: str, url: str, *, deadline: float, json_body: Mapping | None = None) -> tuple[int, Any]:
        response = await self._send(method, url, deadline=deadline, json_body=json_body)
        status = response.status_code
        try:
            payload = json.loads(response.content, parse_constant=_reject_non_finite)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            await response.aclose()
            raise AdapterError("upstream protocol error: output is not finite JSON", "backend_failed") from exc
        await response.aclose()
        return status, payload

    async def _count_chat_tokens(self, messages: list[Any], image_count: int, deadline: float) -> int:
        status, templated = await self._json("POST", self._endpoint("apply_template"), deadline=deadline, json_body={"messages": messages})
        if status != 200 or not isinstance(templated, dict) or not isinstance(templated.get("prompt"), str):
            raise AdapterError("chat template counting failed", "backend_failed")
        status, tokenized = await self._json(
            "POST",
            self._endpoint("tokenize"),
            deadline=deadline,
            json_body={"content": templated["prompt"]},
        )
        tokens = tokenized.get("tokens") if isinstance(tokenized, dict) else None
        if status != 200 or not isinstance(tokens, list):
            raise AdapterError("tokenize counting failed", "backend_failed")
        text_tokens = len(tokens)
        image_tokens = image_count * self._model.envelope.max_image_tokens
        return text_tokens + image_tokens

    async def count_chat_input(self, messages: list[Any], image_count: int, deadline: float) -> int:
        """The runtime's own token count for one chat body (C06; the compatibility API injects this, P20)."""
        return await self._count_chat_tokens(messages, image_count, deadline)

    def _chat_payload(self, messages: list[Any], parameters: Mapping) -> dict[str, Any]:
        try:
            max_tokens = effective_max_tokens(parameters, self._model.envelope)
        except EnvelopeError as exc:
            raise AdapterError(str(exc), exc.code) from exc
        payload: dict[str, Any] = {
            "model": self._model.model_id,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": False,
        }
        for key in ("temperature", "top_p", "seed", "ignore_eos"):
            if key in parameters:
                payload[key] = parameters[key]
        return payload

    def _validate_chat(self, payload: Any) -> None:
        if not isinstance(payload, dict) or not isinstance(payload.get("model"), str) or not isinstance(payload.get("choices"), list):
            raise AdapterError("chat output shape is invalid", "backend_failed")
        usage = payload.get("usage")
        if usage is None:
            return
        if not isinstance(usage, dict):
            raise AdapterError("chat usage shape is invalid", "backend_failed")
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if key in usage and (isinstance(usage[key], bool) or not isinstance(usage[key], int) or usage[key] < 0):
                raise AdapterError("chat usage must be finite integers", "backend_failed")

    def _validate_embeddings(self, payload: Any) -> None:
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list) or not payload["data"]:
            raise AdapterError("embeddings output shape is invalid", "backend_failed")
        width = None
        for row in payload["data"]:
            item = _json_object(row, "embedding")
            vector = _finite_numbers(item.get("embedding"), "embedding")
            if width is None:
                width = len(vector)
            elif len(vector) != width:
                raise AdapterError("embedding dimensions are inconsistent", "backend_failed")

    def _validate_rerank(self, payload: Any) -> None:
        rows = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise AdapterError("rerank output shape is invalid", "backend_failed")
        for row in rows:
            item = _json_object(row, "rerank")
            score = item.get("relevance_score")
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
                raise AdapterError("rerank scores must be finite numbers", "backend_failed")

    async def _slots_idle(self, deadline: float) -> bool:
        status, payload = await self._json("GET", self._endpoint("slots"), deadline=deadline)
        if status != 200:
            return False
        slots = payload if isinstance(payload, list) else (payload.get("slots") if isinstance(payload, dict) else None)
        if not isinstance(slots, list) or not slots:
            return False
        return all(isinstance(slot, dict) and slot.get("is_processing") is False for slot in slots)

    async def _dispatch(self, name: str, body: Mapping, deadline: float, validator) -> Any:
        status, payload = await self._json("POST", self._endpoint(name), deadline=deadline, json_body=body)
        self.last_http_complete = 200 <= status < 300
        if not self.last_http_complete:
            raise AdapterError(f"{name} upstream status {status}", "backend_failed")
        validator(payload)
        try:
            self.last_slot_idle = await self._slots_idle(deadline)
        except AdapterError:
            self.last_slot_idle = False
        return payload

    def _require_identity(self) -> InstanceIdentity:
        identity = self._resolve_identity()
        if identity is None:
            raise AdapterError("instance identity is required after dispatch", "instance_unknown")
        return identity

    async def load(self, spec: ModelSpec, fence: Fence, deadline: float) -> Observation:
        if spec.model_id != self._model.model_id:
            raise AdapterError("load spec does not match this adapter", "contract_violation")
        if self._control is None:
            raise AdapterError("lifecycle control is required for load", "backend_failed")
        await self._control.load(spec.model_id)
        status, health = await self._json("GET", self._endpoint("health"), deadline=deadline)
        healthy = status == 200 and isinstance(health, dict) and health.get("status") == "ok"
        if healthy:
            await self._slots_idle(deadline)
        return Observation(
            state="running" if healthy else "unknown",
            sampled_at_monotonic=time.monotonic(),
            sampled_at_utc=datetime.now(UTC),
            port_state="listening" if healthy else "unknown",
            subprocess_state="running" if healthy else "unknown",
            instance=self._resolve_identity(),
            launch_operation=None,
        )

    async def execute(self, request: ExecutionRequest, fence: Fence, deadline: float) -> ExecutionHandle:
        self.last_http_complete = False
        self.last_slot_idle = False
        if request.operation not in self._model.capabilities:
            raise AdapterError(f"capability {request.operation!r} is not registered", "capability_mismatch")
        if request.inline_input is None:
            raise AdapterError("inline input is required", "contract_violation")
        parameters = dict(request.parameters)
        if request.operation in {"chat", "vision"}:
            messages = request.inline_input.get("messages")
            if not isinstance(messages, list):
                raise AdapterError("chat input requires messages", "contract_violation")
            try:
                images = collect_image_sizes(messages)
                check_images(images, capabilities=self._model.capabilities, operation=request.operation,
                             envelope=self._model.envelope)
                payload = self._chat_payload(messages, parameters)
            except EnvelopeError as exc:
                raise AdapterError(str(exc), exc.code) from exc
            tokens = await self._count_chat_tokens(messages, len(images), deadline)
            try:
                check_chat_budget(tokens, payload["max_tokens"], self._model.envelope)
            except EnvelopeError as exc:
                raise AdapterError(str(exc), exc.code) from exc
            output = await self._dispatch("chat", payload, deadline, self._validate_chat)
        elif request.operation == "embeddings":
            raw_input = request.inline_input.get("input")
            if not (isinstance(raw_input, str) and raw_input) and not (
                isinstance(raw_input, list) and raw_input and all(isinstance(item, str) and item for item in raw_input)
            ):
                raise AdapterError("embeddings input must be a non-empty string or string list", "contract_violation")
            output = await self._dispatch(
                "embeddings",
                {"model": self._model.model_id, "input": raw_input, "encoding_format": parameters.get("encoding_format", "float")},
                deadline,
                self._validate_embeddings,
            )
        elif request.operation == "rerank":
            query = request.inline_input.get("query")
            documents = request.inline_input.get("documents")
            if not isinstance(query, str) or not query or not isinstance(documents, list) or not documents:
                raise AdapterError("rerank requires query and documents", "contract_violation")
            output = await self._dispatch(
                "rerank",
                {
                    "query": query,
                    "documents": documents,
                    "top_n": parameters.get("top_n", len(documents)),
                    "return_documents": parameters.get("return_documents", False),
                },
                deadline,
                self._validate_rerank,
            )
        else:
            raise AdapterError(f"unsupported operation {request.operation!r}", "capability_mismatch")
        # The validated response is kept for exactly one publication (the service owns the blob).
        self._results[request.execution_id] = canonical_json_bytes(output)
        return ExecutionHandle(execution_id=request.execution_id, instance=self._require_identity())

    async def cancel(self, handle: ExecutionHandle, deadline: float) -> CancelAck:
        del deadline
        return CancelAck(execution_id=handle.execution_id, accepted=True)

    async def stop(self, identity: InstanceIdentity, fence: Fence, deadline: float) -> StopAck:
        del fence, deadline
        if self._control is None:
            return StopAck(accepted=False)
        await self._control.unload(identity.model_id)
        return StopAck(accepted=True)

    async def release(self, model_id: str) -> bool:
        """Unload a model by name when no verified instance was ever recorded.

        A load that fails verification can leave the control plane holding a container
        this boot never accepted. The unload endpoint is model-scoped, so the orphan can
        still be let go and the stop proven by observation; without this the model would
        keep a runtime it can neither use nor address.
        """
        if self._control is None:
            return False
        await self._control.unload(model_id)
        return True
