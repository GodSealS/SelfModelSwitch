"""llama.cpp GGUF adapter: registered-protocol inference with trusted counting (M04/P15).

The adapter is the first real `BackendPort` for `llama-cpp-gguf-v1`. It talks to
a loopback llama-server using the paths pinned in `tests/fixtures/llama_cpp_v1.json`.
llama-swap, when injected, is used only as a load/unload lifecycle control; this
module never asks it to route inference.

C06 rules this module enforces before any dispatch:

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
import base64
import binascii
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
from ..ports_v3 import CancelAck, ExecutionHandle, ExecutionRequest, Observation, StopAck

FIXTURE_PATH = Path(__file__).resolve().parent.parent.parent / "tests" / "fixtures" / "llama_cpp_v1.json"
ALLOWED_IMAGE_MEDIA = frozenset({"image/png", "image/jpeg"})
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


def _png_size(data: bytes) -> tuple[int, int]:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise AdapterError("image is not a PNG", "envelope_exceeded")
    if data[12:16] != b"IHDR":
        raise AdapterError("PNG missing IHDR", "envelope_exceeded")
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    if width < 1 or height < 1:
        raise AdapterError("PNG has invalid dimensions", "envelope_exceeded")
    return width, height


def _jpeg_size(data: bytes) -> tuple[int, int]:
    if data[:2] != b"\xff\xd8":
        raise AdapterError("image is not a JPEG", "envelope_exceeded")
    index = 2
    while index + 9 <= len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in {0xC0, 0xC1, 0xC2}:
            height = int.from_bytes(data[index + 5 : index + 7], "big")
            width = int.from_bytes(data[index + 7 : index + 9], "big")
            if width < 1 or height < 1:
                raise AdapterError("JPEG has invalid dimensions", "envelope_exceeded")
            return width, height
        if marker in {0xD8, 0xD9} or marker < 0xC0:
            index += 2
            continue
        length = int.from_bytes(data[index + 2 : index + 4], "big")
        index += 2 + length
    raise AdapterError("JPEG missing size marker", "envelope_exceeded")


def _decode_data_url(url: str) -> tuple[str, bytes]:
    if not url.startswith("data:") or ";base64," not in url:
        raise AdapterError(f"unsupported image url {url[:48]!r}", "contract_violation")
    header, payload = url.split(";base64,", 1)
    media = header[len("data:") :].split(";", 1)[0].strip().lower()
    if media not in ALLOWED_IMAGE_MEDIA:
        raise AdapterError(f"unsupported image media type {media!r}", "unsupported_media_type")
    try:
        raw = base64.b64decode(payload, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise AdapterError("image data URL is not valid base64", "contract_violation") from exc
    return media, raw


def _image_size(media: str, raw: bytes) -> tuple[int, int]:
    return _png_size(raw) if media == "image/png" else _jpeg_size(raw)


def _message_parts(content: Any) -> list[Any]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return list(content)
    raise AdapterError("message content must be a string or part list", "contract_violation")


def _collect_images(messages: list[Any]) -> list[tuple[int, int]]:
    sizes: list[tuple[int, int]] = []
    if not isinstance(messages, list) or not messages:
        raise AdapterError("messages must be a non-empty list", "contract_violation")
    for message in messages:
        item = _json_object(message, "message")
        for part in _message_parts(item.get("content")):
            if not isinstance(part, dict):
                raise AdapterError("message part must be an object", "contract_violation")
            kind = part.get("type")
            if kind == "text":
                continue
            if kind != "image_url":
                raise AdapterError(f"unsupported message part {kind!r}", "contract_violation")
            image = _json_object(part.get("image_url"), "image_url")
            url = image.get("url")
            if not isinstance(url, str) or not url:
                raise AdapterError("image url is required", "contract_violation")
            if url.startswith("http://") or url.startswith("https://") or not url.startswith("data:"):
                raise AdapterError("remote image url is not allowed", "contract_violation")
            media, raw = _decode_data_url(url)
            sizes.append(_image_size(media, raw))
    return sizes


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
        request = self._client.build_request(method, url, json=None if json_body is None else dict(json_body))
        try:
            async with asyncio.timeout(remaining):
                response = await self._client.send(request, follow_redirects=False)
        except asyncio.TimeoutError as exc:
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

    def _check_images(self, sizes: list[tuple[int, int]], operation: str) -> None:
        envelope = self._model.envelope
        if sizes and "vision" not in self._model.capabilities:
            raise AdapterError("image input requires the vision capability", "capability_mismatch")
        if operation == "vision" and not sizes:
            raise AdapterError("vision requires an image", "contract_violation")
        if len(sizes) > envelope.max_images:
            raise AdapterError("image count exceeds envelope.max_images", "envelope_exceeded")
        for width, height in sizes:
            if max(width, height) > envelope.max_image_edge_pixels:
                raise AdapterError("image edge exceeds envelope.max_image_edge_pixels", "envelope_exceeded")

    def _check_budget(self, input_tokens: int, max_tokens: int) -> None:
        envelope = self._model.envelope
        if input_tokens > envelope.max_input_tokens:
            raise AdapterError("input tokens exceed envelope.max_input_tokens", "envelope_exceeded")
        if max_tokens > envelope.max_output_tokens:
            raise AdapterError("max_tokens exceeds envelope.max_output_tokens", "envelope_exceeded")
        if input_tokens + max_tokens > envelope.ctx_size:
            raise AdapterError("input plus output exceeds ctx_size", "envelope_exceeded")

    def _chat_payload(self, messages: list[Any], parameters: Mapping) -> dict[str, Any]:
        envelope = self._model.envelope
        requested = parameters.get("max_tokens", min(4096, envelope.max_output_tokens))
        if isinstance(requested, bool) or not isinstance(requested, int) or requested < 1:
            raise AdapterError("max_tokens must be a positive integer", "contract_violation")
        payload: dict[str, Any] = {
            "model": self._model.model_id,
            "messages": messages,
            "max_tokens": min(requested, envelope.max_output_tokens),
            "stream": False,
        }
        for key in ("temperature", "top_p", "seed"):
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
            images = _collect_images(messages)
            self._check_images(images, request.operation)
            payload = self._chat_payload(messages, parameters)
            tokens = await self._count_chat_tokens(messages, len(images), deadline)
            self._check_budget(tokens, payload["max_tokens"])
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
