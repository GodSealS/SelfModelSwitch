"""HTTP application factory.

The factory deliberately has no module-level connection or model initialization:
that would make imports perform control-plane I/O and hide startup failures.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
import os
from pathlib import Path
from time import monotonic
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from model_scheduler.api_models import ChatRequest
from model_scheduler.config import ConfigError, load_config
from model_scheduler.contracts import Capability, GatewayError, Outcome


def _config_path(explicit: str | Path | None) -> Path:
    if explicit is not None:
        return Path(explicit)
    return Path(os.environ.get("MODEL_SCHEDULER_CONFIG", "config.yaml"))


def _error(status: int, code: str, message: str, request_id: str, param: str | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"message": message, "type": "invalid_request_error" if status < 500 else "upstream_error", "code": code, "param": param}, "request_id": request_id}, headers={"X-Request-ID": request_id})


def create_app(config_path: str | Path | None = None, *, scheduler=None, gateway=None) -> FastAPI:
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

    @app.get("/live")
    async def live() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/health")
    async def health() -> JSONResponse:
        checks = {"llama_swap": False, "storage": False, "resources": False, "preload": False, "control": False}
        return JSONResponse(status_code=503, content={"ok": False, "checks": checks, "reason": "starting"})

    @app.get("/v1/models")
    async def list_models() -> dict[str, object]:
        return {"object": "list", "data": [{"id": model_id, "object": "model", "created": 0, "owned_by": "self-model-switch"} for model_id in sorted(config.models)]}

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
                # The following T08 slice replaces this with a response owner that
                # parses SSE completion and observes client disconnects.
                await opened.aclose()
                await app.state.scheduler.release(lease, Outcome.ABORTED)
                return _error(503, "streaming_not_ready", "Streaming is not ready", request_id)
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

    return app


try:
    app = create_app()
except ConfigError:
    # run.py reports configuration errors with exit 78. Keeping import errors
    # explicit here avoids binding a half-configured production listener.
    raise
