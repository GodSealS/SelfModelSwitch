"""HTTP application factory.

The factory deliberately has no module-level connection or model initialization:
that would make imports perform control-plane I/O and hide startup failures.
"""
from __future__ import annotations

import base64
from contextlib import asynccontextmanager, suppress
import asyncio
from dataclasses import dataclass
import inspect
import json
import math
import os
from pathlib import Path
import struct
from time import monotonic
from uuid import uuid4

from fastapi import FastAPI, Request
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ValidationError
import httpx

from model_scheduler.api_models import ChatRequest, EmbeddingRequest, RerankRequest
from model_scheduler.config import AppConfig, ConfigError, load_config
from model_scheduler.contracts import Capability, GatewayError, Outcome
from model_scheduler.envelope_validator import (
    EnvelopeError,
    check_chat_input,
    check_embeddings_input,
    check_rerank_input,
)
from model_scheduler.model_registry import Conflict
from model_scheduler.gateway import DirectInferenceGateway
from model_scheduler.runtime import build_scheduler
from model_scheduler.scheduler import ModelUnavailable, QueueFull

# C06 input refusals onto HTTP: an over-limit request is 422 (m00-envelope §3),
# an unsupported image media type is 415.
_ENVELOPE_STATUS = {"unsupported_media_type": 415}


class BodyError(ValueError):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code = status, code


class _StreamingLease:
    def __init__(self, opened, scheduler, lease) -> None:
        self.opened = opened
        self.scheduler = scheduler
        self.lease = lease
        self.completed = False
        self._cleaned = False

    async def cleanup(self, outcome: Outcome) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        try:
            await self.opened.aclose()
        finally:
            await self.scheduler.release(self.lease, outcome)


class _LeasedStreamingResponse(StreamingResponse):
    """Keep upstream-response ownership until ASGI has finished sending it."""
    def __init__(self, content, owner: _StreamingLease, **kwargs) -> None:
        super().__init__(content, **kwargs)
        self._owner = owner

    async def __call__(self, scope, receive, send) -> None:
        outcome = Outcome.ABORTED
        try:
            await super().__call__(scope, receive, send)
            outcome = Outcome.SUCCESS if self._owner.completed else Outcome.ABORTED
        finally:
            await asyncio.shield(self._owner.cleanup(outcome))


def _config_path(explicit: str | Path | None) -> Path:
    if explicit is not None:
        return Path(explicit)
    return Path(os.environ.get("MODEL_SCHEDULER_CONFIG", "config.yaml"))


@dataclass(frozen=True)
class _CatalogModel:
    """The legacy routes' view of one registered model (schema v1 or v2)."""

    model_id: str
    upstream_url: str
    capabilities: frozenset
    preload: bool
    envelope: object | None = None  # v2 registrations carry the measured C06 envelope


@dataclass(frozen=True)
class _LifecycleTimeouts:
    load_timeout_seconds: float
    unload_timeout_seconds: float


def _model_catalog(config) -> dict[str, _CatalogModel]:
    """One catalog over either schema (plan/08 P19, plan/03-api.md §1).

    A v2 registration drives the same compatibility surface: the upstream URL is
    derived from the registered port, `preload` comes from the scheduler policy
    and the capability tuple is the registration's own. v1 keeps its shape.
    """
    if getattr(config, "schema_version", 1) == 2:
        preload = set(config.scheduler.preload_models)
        return {
            model_id: _CatalogModel(model_id=model_id, upstream_url=f"http://127.0.0.1:{model.port}",
                                    capabilities=frozenset(model.capabilities), preload=model_id in preload,
                                    envelope=model.envelope)
            for model_id, model in config.models.items()
        }
    return {
        model_id: _CatalogModel(model_id=model_id, upstream_url=model.upstream_url,
                                capabilities=frozenset(model.capabilities), preload=model.lifecycle.preload)
        for model_id, model in config.models.items()
    }


def _lifecycle_timeouts(config) -> _LifecycleTimeouts:
    """Load/unload budgets for either schema; v2 has no llama-swap section."""
    if getattr(config, "schema_version", 1) == 2:
        reclaim = float(config.scheduler.memory_reclaim_timeout_seconds)
        return _LifecycleTimeouts(load_timeout_seconds=reclaim, unload_timeout_seconds=reclaim)
    return _LifecycleTimeouts(load_timeout_seconds=config.llama_swap.load_timeout_seconds,
                              unload_timeout_seconds=config.llama_swap.unload_timeout_seconds)


def _readiness_reason(document: dict) -> str | None:
    """Why /api/status is not ready, from its own admission facts only."""
    admission = document.get("admission")
    if isinstance(admission, dict):
        if admission.get("shutting_down"):
            return "shutting_down"
        if admission.get("storage_unavailable"):
            return "storage_unavailable"
        if admission.get("recovering") is not False:
            return "recovery_incomplete"
    return None


def _error(status: int, code: str, message: str, request_id: str, param: str | None = None, extra_headers: dict[str, str] | None = None) -> JSONResponse:
    headers = {"X-Request-ID": request_id, **(extra_headers or {})}
    error_type = "invalid_request_error" if status < 500 else "server_error" if status == 500 else "upstream_error"
    return JSONResponse(status_code=status, content={"error": {"message": message, "type": error_type, "code": code, "param": param}, "request_id": request_id}, headers=headers)


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _reject_non_finite_json_constant(_: str) -> object:
    raise ValueError("non-finite JSON number")


def _openapi_json_body(model: type[BaseModel]) -> dict[str, object]:
    """Document the same strict DTO used after the raw-body safety checks."""
    return {
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": model.model_json_schema()}},
        }
    }


def _media_type(headers) -> str:
    value = headers.get("content-type", "") if hasattr(headers, "get") else ""
    return value.split(";", 1)[0].strip().lower() if isinstance(value, str) else ""


def _valid_chat_response(response: object) -> bool:
    if not isinstance(response, dict) or not isinstance(response.get("model"), str) or not isinstance(response.get("choices"), list):
        return False
    usage = response.get("usage")
    return usage is None or (
        isinstance(usage, dict)
        and all(type(usage[key]) is int and usage[key] >= 0 for key in ("prompt_tokens", "completion_tokens", "total_tokens") if key in usage)
    )


async def _read_json(request: Request, *, max_bytes: int, timeout_seconds: float) -> dict[str, object]:
    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip().lower() != "application/json":
        raise BodyError(415, "unsupported_media_type", "Content-Type must be application/json")
    length = request.headers.get("content-length")
    if length is not None:
        try:
            declared_length = int(length)
        except ValueError as exc:
            raise BodyError(400, "invalid_content_length", "Content-Length is invalid") from exc
        if declared_length < 0:
            raise BodyError(400, "invalid_content_length", "Content-Length is invalid")
        if declared_length > max_bytes:
            raise BodyError(413, "request_too_large", "Request body exceeds the configured limit")
    try:
        async with asyncio.timeout(timeout_seconds):
            chunks: list[bytes] = []
            received = 0
            async for chunk in request.stream():
                received += len(chunk)
                if received > max_bytes:
                    raise BodyError(413, "request_too_large", "Request body exceeds the configured limit")
                chunks.append(chunk)
            raw = b"".join(chunks)
    except asyncio.TimeoutError as exc:
        raise BodyError(408, "request_body_timeout", "Request body timed out") from exc
    if len(raw) > max_bytes:
        raise BodyError(413, "request_too_large", "Request body exceeds the configured limit")
    try:
        payload = json.loads(raw, object_pairs_hook=_unique_json_object, parse_constant=_reject_non_finite_json_constant)
    except (UnicodeDecodeError, ValueError) as exc:
        raise BodyError(400, "invalid_json", "Request body is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise BodyError(400, "invalid_json", "Request body must be a JSON object")
    return payload


async def _require_empty_body(request: Request, *, timeout_seconds: float) -> None:
    length = request.headers.get("content-length")
    if length is not None:
        try:
            declared_length = int(length)
        except ValueError as exc:
            raise BodyError(400, "invalid_content_length", "Content-Length is invalid") from exc
        if declared_length != 0:
            raise BodyError(400, "invalid_request", "Request body must be empty")
    try:
        async with asyncio.timeout(timeout_seconds):
            async for chunk in request.stream():
                if chunk:
                    raise BodyError(400, "invalid_request", "Request body must be empty")
    except asyncio.TimeoutError as exc:
        raise BodyError(408, "request_body_timeout", "Request body timed out") from exc


def create_app(config_path: str | Path | None = None, *, config: AppConfig | None = None, scheduler=None, gateway=None, health_checks=None, backend=None, resources=None, storage_guard=None, recovery=None, boot_id: str | None = None, execution_stats=None, token_counter=None, preload_retry_delays: tuple[float, ...] = (5, 10, 20, 30)) -> FastAPI:
    """Create a listener that remains diagnostically live while dependencies recover.

    The same surface serves schema v2 (plan/08 P19): the catalog derives the
    upstream URL and the preload flag from the dynamic registration, `boot_id`
    and `execution_stats` enrich /api/status, and the load/unload budgets fall
    back to the memory-reclaim timeout because v2 has no llama-swap section.

    A v2 registration also carries the measured C06 envelope, so chat/vision,
    embeddings and rerank bodies are checked BEFORE any dispatch (P20):
    `token_counter(model_id, messages, image_count)` supplies the runtime's own
    token count (never a character estimate) when the deployment wires one.
    """
    if not preload_retry_delays or any(delay <= 0 for delay in preload_retry_delays):
        raise ValueError("preload_retry_delays must contain positive values")
    config = config or load_config(_config_path(config_path))
    catalog = _model_catalog(config)
    timeouts = _lifecycle_timeouts(config)
    owned_client = None
    if scheduler is None and backend is not None:
        scheduler = build_scheduler(config, backend, resources=resources, storage_guard=storage_guard, recovery=recovery)
    if gateway is None and scheduler is not None:
        timeout = httpx.Timeout(config.gateway.inference_timeout_seconds, connect=config.gateway.connect_timeout_seconds, read=config.gateway.read_idle_timeout_seconds, write=config.gateway.write_idle_timeout_seconds, pool=config.gateway.pool_timeout_seconds)
        owned_client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
        gateway = DirectInferenceGateway(
            {model_id: model.upstream_url for model_id, model in catalog.items()},
            owned_client,
            max_response_body_bytes=config.gateway.max_response_body_bytes,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.shutting_down = False
        app.state.preload_error = None
        app.state.preload_pending = False
        app.state.preload_task = None
        app.state.storage_watch_task = None
        if app.state.scheduler is not None and callable(getattr(app.state.scheduler, "monitor_storage_once", None)):
            async def watch_storage() -> None:
                while not app.state.shutting_down:
                    try:
                        await app.state.scheduler.monitor_storage_once(monotonic() + timeouts.unload_timeout_seconds)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        # The scheduler is fail-closed; a monitor fault must not
                        # take down /live or spin a retry loop.
                        pass
                    await asyncio.sleep(config.resources.sample_interval_seconds)
            app.state.storage_watch_task = asyncio.create_task(watch_storage())
        if app.state.scheduler is not None and callable(getattr(app.state.scheduler, "preload", None)):
            async def preload_with_backoff() -> None:
                attempt = 0
                while not app.state.shutting_down:
                    try:
                        await app.state.scheduler.preload(monotonic() + timeouts.load_timeout_seconds)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        app.state.preload_error = str(exc)
                        delay = preload_retry_delays[min(attempt, len(preload_retry_delays) - 1)]
                        attempt += 1
                        await asyncio.sleep(delay)
                    else:
                        app.state.preload_error = None
                        app.state.preload_pending = False
                        return
            app.state.preload_pending = True
            task = asyncio.create_task(preload_with_backoff())
            app.state.preload_task = task
        yield
        app.state.shutting_down = True
        storage_task = app.state.storage_watch_task
        if storage_task is not None:
            storage_task.cancel()
            with suppress(asyncio.CancelledError):
                await storage_task
        task = app.state.preload_task
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if app.state.scheduler is not None and callable(getattr(app.state.scheduler, "shutdown", None)):
            with suppress(Exception):
                await app.state.scheduler.shutdown(monotonic() + config.server.shutdown_grace_seconds)
        if app.state.owned_client is not None:
            await app.state.owned_client.aclose()

    app = FastAPI(title="AGX Model Scheduler", version="1.0", lifespan=lifespan)
    app.state.config = config
    app.state.catalog = catalog
    app.state.ready = False
    app.state.scheduler = scheduler
    app.state.gateway = gateway
    app.state.owned_client = owned_client
    app.state.health_checks = health_checks
    app.state.boot_id = boot_id
    app.state.execution_stats = execution_stats
    app.state.token_counter = token_counter

    async def close_and_release(opened, lease, outcome: Outcome, tokens: int | None = None) -> None:
        try:
            await opened.aclose()
        finally:
            await app.state.scheduler.release(lease, outcome, tokens)

    @app.exception_handler(StarletteHTTPException)
    async def framework_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        request_id = str(uuid4())
        code = "not_found" if exc.status_code == 404 else "method_not_allowed" if exc.status_code == 405 else "http_error"
        return _error(exc.status_code, code, str(exc.detail), request_id, extra_headers=dict(exc.headers or {}))

    @app.exception_handler(Exception)
    async def unexpected_error(_: Request, __: Exception) -> JSONResponse:
        request_id = str(uuid4())
        return _error(500, "internal_error", "Internal server error", request_id)

    @app.get("/live")
    async def live() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/health")
    async def health() -> JSONResponse:
        required = {"llama_swap", "storage", "resources", "preload", "control"}
        checks = {key: False for key in required}
        provider = app.state.health_checks
        if provider is not None:
            result = provider()
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, dict) and set(result) == required and all(type(value) is bool for value in result.values()):
                checks = result
        elif app.state.scheduler is not None and callable(getattr(app.state.scheduler, "status", None)):
            try:
                status = await app.state.scheduler.status()
                resources = status.get("resources", {}) if isinstance(status, dict) else {}
                admission = status.get("admission", {}) if isinstance(status, dict) else {}
                models = status.get("models", {}) if isinstance(status, dict) else {}
                age = resources.get("sample_age_seconds") if isinstance(resources, dict) else None
                storage_ready = isinstance(admission, dict) and admission.get("storage_unavailable") is False
                recovering = not isinstance(admission, dict) or admission.get("recovering") is not False
                shutting_down = not isinstance(admission, dict) or admission.get("shutting_down") is not False
                preload_models = [model_id for model_id, model in catalog.items() if model.preload]
                preload_ready = isinstance(models, dict) and all(
                    isinstance(models.get(model_id), dict) and models[model_id].get("state") == "ready"
                    for model_id in preload_models
                )
                control = getattr(getattr(app.state.scheduler, "backend", None), "control", None)
                control_health = getattr(control, "health", None)
                control_ready = False
                if callable(control_health):
                    result = control_health()
                    if inspect.isawaitable(result):
                        result = await result
                    control_ready = result is True
                checks = {
                    "storage": storage_ready,
                    "resources": type(age) in (int, float) and 0 <= age <= config.resources.sample_max_age_seconds,
                    "preload": preload_ready,
                    "control": control_ready and not recovering and not shutting_down,
                    "llama_swap": control_ready,
                }
            except Exception:
                checks = {key: False for key in required}
        if app.state.preload_pending or app.state.preload_error is not None:
            checks["preload"] = False
        ready = all(checks.values()) and not app.state.shutting_down
        return JSONResponse(status_code=200 if ready else 503, content={"ok": ready, "checks": checks, "reason": None if ready else "dependencies_unready"})

    @app.get("/v1/models")
    async def list_models() -> dict[str, object]:
        return {"object": "list", "data": [{"id": model_id, "object": "model", "created": 0, "owned_by": "self-model-switch"} for model_id in sorted(catalog)]}

    @app.get("/api/status")
    async def status():
        if app.state.scheduler is None:
            document: dict[str, object] = {"ready": False, "resources": None, "queue_size": 0, "models": {}}
        else:
            document = dict(await app.state.scheduler.status())
        stats = app.state.execution_stats
        if callable(stats):
            result = stats()
            if inspect.isawaitable(result):
                result = await result
            document["executions"] = result
        else:
            document["executions"] = None
        document["boot_id"] = app.state.boot_id
        document["readiness_reason"] = _readiness_reason(document)
        return document

    @app.get("/api/models")
    async def models():
        if app.state.scheduler is None:
            return {model_id: {"state": "unknown", "in_flight": 0} for model_id in sorted(catalog)}
        return (await app.state.scheduler.status())["models"]

    @app.post("/api/models/{model_id}/unload")
    async def unload(model_id: str, request: Request):
        request_id = str(uuid4())
        try:
            await _require_empty_body(request, timeout_seconds=config.server.body_timeout_seconds)
        except BodyError as exc:
            return _error(exc.status, exc.code, str(exc), request_id)
        if model_id not in catalog:
            return _error(404, "model_not_found", "Unknown model", request_id, "model")
        if app.state.scheduler is None:
            return _error(503, "service_unavailable", "Service is not ready", request_id)
        try:
            await app.state.scheduler.unload(model_id, monotonic() + timeouts.unload_timeout_seconds)
            return JSONResponse(content={"ok": True, "model": model_id}, headers={"X-Request-ID": request_id})
        except Conflict as exc:
            code = str(exc)
            return _error(409, code if code in {"model_pinned", "model_busy"} else "model_busy", "Model cannot be unloaded", request_id)
        except ModelUnavailable:
            return _error(502, "stop_unverified", "Model stop could not be verified", request_id)

    @app.post("/api/recover")
    async def recover_storage():
        request_id = str(uuid4())
        scheduler = app.state.scheduler
        recovery = getattr(scheduler, "storage_recovered", None) if scheduler is not None else None
        if not callable(recovery):
            return _error(503, "recovery_unavailable", "Storage recovery is unavailable", request_id)
        try:
            preloaded = await recovery(monotonic() + timeouts.load_timeout_seconds)
            return JSONResponse(
                content={"ok": True, "preloaded": list(preloaded)},
                headers={"X-Request-ID": request_id},
            )
        except Conflict as exc:
            return _error(409, str(exc), "Recovery cannot run while models are busy", request_id)
        except ModelUnavailable as exc:
            return _error(503, str(exc), "Storage recovery did not complete", request_id)

    @app.post("/v1/chat/completions", openapi_extra=_openapi_json_body(ChatRequest))
    async def chat(request: Request):
        request_id = str(uuid4())
        try:
            payload = await _read_json(request, max_bytes=config.server.max_request_body_bytes, timeout_seconds=config.server.body_timeout_seconds)
            body = ChatRequest.model_validate(payload)
        except BodyError as exc:
            return _error(exc.status, exc.code, str(exc), request_id)
        except (ValidationError, ValueError):
            return _error(400, "invalid_request", "Invalid chat request", request_id)
        model = catalog.get(body.model)
        if model is None:
            return _error(404, "model_not_found", "Unknown model", request_id, "model")
        if "chat" not in model.capabilities and "vision" not in model.capabilities:
            return _error(422, "unsupported_capability", "Model does not support chat", request_id, "model")
        if app.state.scheduler is None or app.state.gateway is None:
            return _error(503, "service_unavailable", "Service is not ready", request_id)
        if model.envelope is not None:
            counter = app.state.token_counter

            async def _count(messages, image_count):
                return await counter(body.model, messages, image_count)

            try:
                await check_chat_input(payload, capabilities=model.capabilities, envelope=model.envelope,
                                       token_counter=_count if counter is not None else None)
            except EnvelopeError as exc:
                return _error(_ENVELOPE_STATUS.get(exc.code, 422), exc.code, str(exc), request_id, "messages")
            except Exception:
                # Counting failed, so compliance cannot be proven: refuse instead of
                # dispatching a request whose envelope was never checked (C06).
                return _error(503, "service_unavailable", "The input could not be counted against the envelope",
                              request_id)
        lease = None
        try:
            deadline = monotonic() + config.gateway.inference_timeout_seconds
            lease = await app.state.scheduler.acquire(body.model, request_id, deadline)
            opened = await app.state.gateway.open(lease, Capability.CHAT, payload, deadline)
            if body.stream:
                if opened.status_code != 200 or _media_type(opened.headers) != "text/event-stream":
                    await close_and_release(opened, lease, Outcome.ABORTED)
                    return _error(502, "upstream_protocol_error", "Invalid chat streaming response", request_id)
                owner = _StreamingLease(opened, app.state.scheduler, lease)

                async def stream_body():
                    pending = b""
                    event_bytes = 0
                    async for chunk in opened.iter_bytes():
                        pending += chunk
                        lines = pending.splitlines(keepends=True)
                        pending = b""
                        if lines and not lines[-1].endswith((b"\n", b"\r")):
                            pending = lines.pop()
                        done_in_chunk = False
                        for line in lines:
                            event_bytes += len(line)
                            if event_bytes > config.gateway.max_sse_event_bytes:
                                return
                            if line.rstrip(b"\r\n") == b"data: [DONE]":
                                done_in_chunk = True
                            if not line.rstrip(b"\r\n"):
                                event_bytes = 0
                        if len(pending) > config.gateway.max_sse_event_bytes:
                            return
                        yield chunk
                        if done_in_chunk:
                            owner.completed = True
                            return
                return _LeasedStreamingResponse(stream_body(), owner, media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Request-ID": request_id})
            response = await opened.json()
            if _media_type(opened.headers) != "application/json" or not _valid_chat_response(response):
                await close_and_release(opened, lease, Outcome.ABORTED)
                return _error(502, "upstream_protocol_error", "Invalid chat response", request_id)
            await close_and_release(opened, lease, Outcome.SUCCESS)
            return JSONResponse(content=response, headers={"X-Request-ID": request_id})
        except asyncio.CancelledError:
            if lease is not None:
                with suppress(Exception):
                    await asyncio.shield(app.state.scheduler.release(lease, Outcome.ABORTED))
            raise
        except GatewayError as exc:
            if lease is not None:
                await app.state.scheduler.release(lease, exc.outcome)
            headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else None
            return _error(exc.http_status, exc.code, "Upstream request failed", request_id, extra_headers=headers)
        except QueueFull:
            return _error(429, "queue_full", "Request queue is full", request_id, extra_headers={"Retry-After": "1"})
        except TimeoutError:
            if lease is not None:
                await app.state.scheduler.release(lease, Outcome.ABORTED)
            return _error(504, "queue_timeout", "Request queue deadline elapsed", request_id)
        except (ModelUnavailable, RuntimeError):
            if lease is not None:
                await app.state.scheduler.release(lease, Outcome.ABORTED)
            return _error(503, "service_unavailable", "Service is not ready", request_id)

    async def acquire_json(model_id: str, capability: Capability, payload: dict, request_id: str):
        model = catalog.get(model_id)
        if model is None:
            return None, _error(404, "model_not_found", "Unknown model", request_id, "model")
        if capability.value not in model.capabilities:
            return None, _error(422, "unsupported_capability", "Model does not support this operation", request_id, "model")
        if app.state.scheduler is None or app.state.gateway is None:
            return None, _error(503, "service_unavailable", "Service is not ready", request_id)
        deadline = monotonic() + config.gateway.inference_timeout_seconds
        try:
            lease = await app.state.scheduler.acquire(model_id, request_id, deadline)
            opened = await app.state.gateway.open(lease, capability, payload, deadline)
            if opened.status_code != 200 or _media_type(opened.headers) != "application/json":
                await close_and_release(opened, lease, Outcome.ABORTED)
                return None, _error(502, "upstream_protocol_error", "Invalid upstream response", request_id)
            result = await opened.json()
            return (lease, opened, result), None
        except asyncio.CancelledError:
            if "lease" in locals():
                with suppress(Exception):
                    await asyncio.shield(app.state.scheduler.release(lease, Outcome.ABORTED))
            raise
        except GatewayError as exc:
            if "lease" in locals(): await app.state.scheduler.release(lease, exc.outcome)
            headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else None
            return None, _error(exc.http_status, exc.code, "Upstream request failed", request_id, extra_headers=headers)
        except QueueFull:
            return None, _error(429, "queue_full", "Request queue is full", request_id, extra_headers={"Retry-After": "1"})
        except TimeoutError:
            if "lease" in locals(): await app.state.scheduler.release(lease, Outcome.ABORTED)
            return None, _error(504, "queue_timeout", "Request queue deadline elapsed", request_id)
        except (ModelUnavailable, RuntimeError):
            if "lease" in locals(): await app.state.scheduler.release(lease, Outcome.ABORTED)
            return None, _error(503, "service_unavailable", "Service is not ready", request_id)

    @app.post("/v1/embeddings", openapi_extra=_openapi_json_body(EmbeddingRequest))
    async def embeddings(request: Request):
        request_id = str(uuid4())
        try:
            payload = await _read_json(request, max_bytes=config.server.max_request_body_bytes, timeout_seconds=config.server.body_timeout_seconds); body = EmbeddingRequest.model_validate(payload)
        except BodyError as exc:
            return _error(exc.status, exc.code, str(exc), request_id)
        except (ValidationError, ValueError):
            return _error(400, "invalid_request", "Invalid embedding request", request_id)
        try:
            inputs = check_embeddings_input(payload)
        except EnvelopeError as exc:
            return _error(_ENVELOPE_STATUS.get(exc.code, 422), exc.code, str(exc), request_id, "input")
        upstream_payload = dict(payload)
        upstream_payload["encoding_format"] = "float"
        context, error = await acquire_json(body.model, Capability.EMBEDDINGS, upstream_payload, request_id)
        if error: return error
        lease, opened, result = context
        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, list) or len(data) != len(inputs) or not all(isinstance(item, dict) for item in data):
            await close_and_release(opened, lease, Outcome.ABORTED)
            return _error(502, "upstream_protocol_error", "Invalid embedding response", request_id)
        vectors: list[tuple[int, list[int | float], bytes]] = []
        expected_dimensions: int | None = None
        for item in data:
            index = item.get("index")
            vector = item.get("embedding")
            if type(index) is not int or not isinstance(vector, list) or not vector or any(type(x) not in (int, float) or not math.isfinite(x) for x in vector):
                await close_and_release(opened, lease, Outcome.ABORTED)
                return _error(502, "upstream_protocol_error", "Invalid embedding response", request_id)
            if expected_dimensions is None:
                expected_dimensions = len(vector)
            if len(vector) != expected_dimensions:
                await close_and_release(opened, lease, Outcome.ABORTED)
                return _error(502, "upstream_protocol_error", "Invalid embedding response", request_id)
            try:
                packed = struct.pack(f"<{len(vector)}f", *vector)
            except (OverflowError, struct.error):
                await close_and_release(opened, lease, Outcome.ABORTED)
                return _error(502, "upstream_protocol_error", "Invalid embedding response", request_id)
            vectors.append((index, vector, packed))
        if {index for index, _, _ in vectors} != set(range(len(inputs))):
            await close_and_release(opened, lease, Outcome.ABORTED)
            return _error(502, "upstream_protocol_error", "Invalid embedding response", request_id)
        await close_and_release(opened, lease, Outcome.SUCCESS)
        encoded = [
            {
                "object": "embedding",
                "index": index,
                "embedding": base64.b64encode(packed).decode("ascii") if body.encoding_format == "base64" else vector,
            }
            for index, vector, packed in sorted(vectors)
        ]
        return JSONResponse(content={"object": "list", "data": encoded, "model": body.model}, headers={"X-Request-ID": request_id})

    @app.post("/v1/rerank", openapi_extra=_openapi_json_body(RerankRequest))
    async def rerank(request: Request):
        request_id = str(uuid4())
        try:
            payload = await _read_json(request, max_bytes=config.server.max_request_body_bytes, timeout_seconds=config.server.body_timeout_seconds); body = RerankRequest.model_validate(payload)
        except BodyError as exc:
            return _error(exc.status, exc.code, str(exc), request_id)
        except (ValidationError, ValueError):
            return _error(400, "invalid_request", "Invalid rerank request", request_id)
        try:
            check_rerank_input(payload)
        except EnvelopeError as exc:
            return _error(_ENVELOPE_STATUS.get(exc.code, 422), exc.code, str(exc), request_id)
        count = len(body.documents) if body.top_n is None else body.top_n
        if not 1 <= count <= len(body.documents): return _error(400, "invalid_request", "top_n is out of range", request_id, "top_n")
        context, error = await acquire_json(body.model, Capability.RERANK, {"model": body.model, "query": body.query, "documents": body.documents, "top_n": len(body.documents)}, request_id)
        if error: return error
        lease, opened, result = context
        items = result.get("results") if isinstance(result, dict) else None
        if (
            not isinstance(items, list)
            or len(items) != len(body.documents)
            or not all(isinstance(item, dict) for item in items)
            or any(type(item.get("index")) is not int for item in items)
            or {item["index"] for item in items} != set(range(len(body.documents)))
            or any(
                type(item.get("relevance_score")) not in (int, float)
                or not math.isfinite(item["relevance_score"])
                for item in items
            )
        ):
            await close_and_release(opened, lease, Outcome.ABORTED)
            return _error(502, "upstream_protocol_error", "Invalid rerank response", request_id)
        ordered = sorted(items, key=lambda item: (-item["relevance_score"], item["index"]))[:count]
        public = [{"index": item["index"], "relevance_score": item["relevance_score"], **({"document": {"text": body.documents[item["index"]]}} if body.return_documents else {})} for item in ordered]
        await close_and_release(opened, lease, Outcome.SUCCESS)
        return JSONResponse(content={"model": body.model, "results": public}, headers={"X-Request-ID": request_id})

    return app


try:
    app = create_app()
except ConfigError:
    # run.py reports configuration errors with exit 78. Keeping import errors
    # explicit here avoids binding a half-configured production listener.
    raise
