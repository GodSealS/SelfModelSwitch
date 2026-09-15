"""HTTP application factory.

The factory deliberately has no module-level connection or model initialization:
that would make imports perform control-plane I/O and hide startup failures.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from model_scheduler.config import AppConfig, ConfigError, load_config


def _config_path(explicit: str | Path | None) -> Path:
    if explicit is not None:
        return Path(explicit)
    return Path(os.environ.get("MODEL_SCHEDULER_CONFIG", "config.yaml"))


def create_app(config_path: str | Path | None = None) -> FastAPI:
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

    return app


try:
    app = create_app()
except ConfigError:
    # run.py reports configuration errors with exit 78. Keeping import errors
    # explicit here avoids binding a half-configured production listener.
    raise
