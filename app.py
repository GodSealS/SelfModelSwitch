"""HTTP application factory.

The factory deliberately has no module-level connection or model initialization:
that would make imports perform control-plane I/O and hide startup failures.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
import inspect
import math
import os
from pathlib import Path
from time import monotonic
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from model_scheduler.api_models import ChatRequest, EmbeddingRequest, RerankRequest
from model_scheduler.config import ConfigError, load_config
from model_scheduler.contracts import Capability, GatewayError, Outcome
from model_scheduler.model_registry import Conflict


def _config_path(explicit: str | Path | None) -> Path:
    if explicit is not None:
        return Path(explicit)
    return Path(os.environ.get("MODEL_SCHEDULER_CONFIG", "config.yaml"))


def _error(status: int, code: str, message: str, request_id: str, param: str | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"message": message, "type": "invalid_request_error" if status < 500 else "upstream_error", "code": code, "param": param}, "request_id": request_id}, headers={"X-Request-ID": request_id})


def create_app(config_path: str | Path | None = None, *, scheduler=None, gateway=None, health_checks=None) -> FastAPI:
    """Create a listener that remains diagnostically live while dependencies recover."""
    config = load_config(_config_path(config_path))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.shutting_down = False
        # T05 installs the scheduler recovery task here.  Do not block lifespan on
        # model loading: /live must remain available when a dependency is down.
        yield
        app.state.shutting_down = True

    app = FastAPI(title="AGX Thor Model Scheduler", version="1.0", lifespan=lifespan)
    app.state.config = config
    app.state.ready = False
    app.state.scheduler = scheduler
    app.state.gateway = gateway
    app.state.health_checks = health_checks

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
        ready = all(checks.values()) and not app.state.shutting_down
        return JSONResponse(status_code=200 if ready else 503, content={"ok": ready, "checks": checks, "reason": None if ready else "dependencies_unready"})

    @app.get("/v1/models")
    async def list_models() -> dict[str, object]:
        return {"object": "list", "data": [{"id": model_id, "object": "model", "created": 0, "owned_by": "self-model-switch"} for model_id in sorted(config.models)]}

    @app.get("/api/status")
    async def status():
        if app.state.scheduler is None:
            return {"ready": False, "resources": None, "queue_size": 0, "models": {}}
        return await app.state.scheduler.status()

    @app.get("/api/models")
    async def models():
        if app.state.scheduler is None:
            return {model_id: {"state": "unknown", "in_flight": 0} for model_id in sorted(config.models)}
        return (await app.state.scheduler.status())["models"]

    @app.post("/api/models/{model_id}/unload")
    async def unload(model_id: str):
        request_id = str(uuid4())
        if model_id not in config.models:
            return _error(404, "model_not_found", "Unknown model", request_id, "model")
        if app.state.scheduler is None:
            return _error(503, "service_unavailable", "Service is not ready", request_id)
        try:
            await app.state.scheduler.unload(model_id, monotonic() + config.llama_swap.unload_timeout_seconds)
            return JSONResponse(content={"ok": True, "model": model_id}, headers={"X-Request-ID": request_id})
        except Conflict as exc:
            code = str(exc)
            return _error(409, code if code in {"model_pinned", "model_busy"} else "model_busy", "Model cannot be unloaded", request_id)

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        request_id = str(uuid4())
        try:
            payload = await request.json()
            body = ChatRequest.model_validate(payload)
        except (ValidationError, ValueError):
            return _error(400, "invalid_request", "Invalid chat request", request_id)
        model = config.models.get(body.model)
        if model is None:
            return _error(404, "model_not_found", "Unknown model", request_id, "model")
        if "chat" not in model.capabilities:
            return _error(400, "unsupported_capability", "Model does not support chat", request_id, "model")
        if app.state.scheduler is None or app.state.gateway is None:
            return _error(503, "service_unavailable", "Service is not ready", request_id)
        lease = None
        try:
            deadline = monotonic() + config.gateway.inference_timeout_seconds
            lease = await app.state.scheduler.acquire(body.model, request_id, deadline)
            opened = await app.state.gateway.open(lease, Capability.CHAT, payload, deadline)
            if body.stream:
                async def stream_body():
                    outcome = Outcome.ABORTED
                    tail = b""
                    try:
                        async for chunk in opened.iter_bytes():
                            tail = (tail + chunk)[-1024:]
                            yield chunk
                            if b"data: [DONE]" in tail:
                                outcome = Outcome.SUCCESS
                    finally:
                        await opened.aclose()
                        await app.state.scheduler.release(lease, outcome)
                return StreamingResponse(stream_body(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Request-ID": request_id})
            response = await opened.json()
            await opened.aclose()
            await app.state.scheduler.release(lease, Outcome.SUCCESS)
            return JSONResponse(content=response, headers={"X-Request-ID": request_id})
        except GatewayError as exc:
            if lease is not None:
                await app.state.scheduler.release(lease, exc.outcome)
            return _error(exc.http_status, exc.code, "Upstream request failed", request_id)
        except (TimeoutError, RuntimeError):
            if lease is not None:
                await app.state.scheduler.release(lease, Outcome.ABORTED)
            return _error(503, "service_unavailable", "Service is not ready", request_id)

    async def acquire_json(model_id: str, capability: Capability, payload: dict, request_id: str):
        model = config.models.get(model_id)
        if model is None:
            return None, _error(404, "model_not_found", "Unknown model", request_id, "model")
        if capability.value not in model.capabilities:
            return None, _error(400, "unsupported_capability", "Model does not support this operation", request_id, "model")
        if app.state.scheduler is None or app.state.gateway is None:
            return None, _error(503, "service_unavailable", "Service is not ready", request_id)
        deadline = monotonic() + config.gateway.inference_timeout_seconds
        try:
            lease = await app.state.scheduler.acquire(model_id, request_id, deadline)
            opened = await app.state.gateway.open(lease, capability, payload, deadline)
            result = await opened.json()
            return (lease, opened, result), None
        except GatewayError as exc:
            if "lease" in locals(): await app.state.scheduler.release(lease, exc.outcome)
            return None, _error(exc.http_status, exc.code, "Upstream request failed", request_id)
        except (TimeoutError, RuntimeError):
            if "lease" in locals(): await app.state.scheduler.release(lease, Outcome.ABORTED)
            return None, _error(503, "service_unavailable", "Service is not ready", request_id)

    @app.post("/v1/embeddings")
    async def embeddings(request: Request):
        request_id = str(uuid4())
        try:
            payload = await request.json(); body = EmbeddingRequest.model_validate(payload)
        except (ValidationError, ValueError):
            return _error(400, "invalid_request", "Invalid embedding request", request_id)
        inputs = [body.input] if isinstance(body.input, str) else body.input
        if not inputs or len(inputs) > 256 or any(not value.strip() for value in inputs):
            return _error(400, "invalid_request", "Embedding input is invalid", request_id, "input")
        context, error = await acquire_json(body.model, Capability.EMBEDDINGS, payload, request_id)
        if error: return error
        lease, opened, result = context
        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, list) or len(data) != len(inputs) or {item.get("index") for item in data if isinstance(item, dict)} != set(range(len(inputs))):
            await opened.aclose(); await app.state.scheduler.release(lease, Outcome.ABORTED)
            return _error(502, "upstream_protocol_error", "Invalid embedding response", request_id)
        for item in data:
            vector = item.get("embedding")
            if not isinstance(vector, list) or not vector or any(type(x) not in (int, float) or not math.isfinite(x) for x in vector):
                await opened.aclose(); await app.state.scheduler.release(lease, Outcome.ABORTED)
                return _error(502, "upstream_protocol_error", "Invalid embedding response", request_id)
        await opened.aclose(); await app.state.scheduler.release(lease, Outcome.SUCCESS)
        return JSONResponse(content={"object": "list", "data": sorted(data, key=lambda item: item["index"]), "model": body.model}, headers={"X-Request-ID": request_id})

    @app.post("/v1/rerank")
    async def rerank(request: Request):
        request_id = str(uuid4())
        try:
            payload = await request.json(); body = RerankRequest.model_validate(payload)
        except (ValidationError, ValueError):
            return _error(400, "invalid_request", "Invalid rerank request", request_id)
        if not body.query.strip() or not 1 <= len(body.documents) <= 256 or any(not item.strip() for item in body.documents):
            return _error(400, "invalid_request", "Rerank input is invalid", request_id)
        count = len(body.documents) if body.top_n is None else body.top_n
        if not 1 <= count <= len(body.documents): return _error(400, "invalid_request", "top_n is out of range", request_id, "top_n")
        context, error = await acquire_json(body.model, Capability.RERANK, {"model": body.model, "query": body.query, "documents": body.documents, "top_n": len(body.documents)}, request_id)
        if error: return error
        lease, opened, result = context
        items = result.get("results") if isinstance(result, dict) else None
        if not isinstance(items, list) or {item.get("index") for item in items if isinstance(item, dict)} != set(range(len(body.documents))) or any(type(item.get("relevance_score")) not in (int, float) or not math.isfinite(item["relevance_score"]) for item in items):
            await opened.aclose(); await app.state.scheduler.release(lease, Outcome.ABORTED)
            return _error(502, "upstream_protocol_error", "Invalid rerank response", request_id)
        ordered = sorted(items, key=lambda item: (-item["relevance_score"], item["index"]))[:count]
        public = [{"index": item["index"], "relevance_score": item["relevance_score"], **({"document": {"text": body.documents[item["index"]]}} if body.return_documents else {})} for item in ordered]
        await opened.aclose(); await app.state.scheduler.release(lease, Outcome.SUCCESS)
        return JSONResponse(content={"model": body.model, "results": public}, headers={"X-Request-ID": request_id})

    return app


try:
    app = create_app()
except ConfigError:
    # run.py reports configuration errors with exit 78. Keeping import errors
    # explicit here avoids binding a half-configured production listener.
    raise
