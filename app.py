from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from model_scheduler.config import load_config
from model_scheduler.eviction_policy import EvictionPolicy
from model_scheduler.gateway import Gateway
from model_scheduler.heat_tracker import HeatTracker
from model_scheduler.llama_swap_client import LlamaSwapClient, LlamaSwapError
from model_scheduler.model_registry import ModelRegistry
from model_scheduler.resource_monitor import ResourceMonitor
from model_scheduler.scheduler import ModelScheduler, ResourceError

CFG = load_config("config.yaml")

registry = ModelRegistry(CFG.models)
heat = HeatTracker(CFG.scheduler.heat)
resources = ResourceMonitor(
    provider=CFG.resources.get("provider", "auto"),
    total_override=int(CFG.resources.get("total_memory_bytes", 0)),
)
llama = LlamaSwapClient(
    CFG.llama_swap["base_url"],
    timeout=float(CFG.llama_swap.get("timeout_seconds", 30)),
    load_timeout=float(CFG.llama_swap.get("load_timeout_seconds", 900)),
)
eviction = EvictionPolicy(registry, heat, CFG.scheduler)
scheduler = ModelScheduler(registry, resources, heat, eviction, llama, CFG.scheduler)
gateway = Gateway(
    CFG.llama_swap["base_url"],
    timeout=float(CFG.llama_swap.get("load_timeout_seconds", 900)),
)

app = FastAPI(title="AGX Thor Model Scheduler", version="0.1.0")


class ModelRequest(BaseModel):
    model: str
    messages: list[dict[str, Any]]
    stream: bool = False


def _token_count(response: dict[str, Any]) -> int:
    try:
        return int(response.get("usage", {}).get("completion_tokens", 0))
    except Exception:
        return 0


@app.on_event("startup")
async def startup():
    await scheduler.sync_running()


@app.get("/health")
async def health():
    try:
        ok = await llama.health()
        return {"ok": ok}
    except Exception as e:
        return JSONResponse(status_code=503, content={"ok": False, "error": str(e)})


@app.get("/api/status")
async def status():
    data = await scheduler.status()
    data["queue_size"] = 0
    return data


@app.get("/api/models")
async def models():
    return registry.snapshot()


@app.post("/api/models/{model_id}/unload")
async def unload(model_id: str):
    registry.require(model_id)
    r = registry.get_runtime(model_id)
    if r.in_flight:
        raise HTTPException(409, f"{model_id} has {r.in_flight} in-flight requests")
    try:
        await llama.unload(model_id)
        r.state = r.state.UNLOADED
        return {"ok": True, "model": model_id}
    except LlamaSwapError as e:
        raise HTTPException(502, str(e))


@app.post("/v1/chat/completions")
async def chat(request: Request):
    payload = await request.json()
    model_id = payload.get("model")
    if not model_id:
        raise HTTPException(400, "request.model is required")

    try:
        registry.require(model_id)
    except KeyError:
        raise HTTPException(404, f"unknown model: {model_id}")

    stream = bool(payload.get("stream", False))

    try:
        await scheduler.acquire(model_id)
    except (ResourceError, LlamaSwapError) as e:
        raise HTTPException(503, str(e))

    try:
        if stream:
            async def iterator():
                tokens = 0
                try:
                    async for chunk in gateway.stream_chat(payload):
                        yield chunk
                finally:
                    await scheduler.release(model_id, tokens)
            return StreamingResponse(
                iterator(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
            )

        response = await gateway.chat(payload)
        await scheduler.release(model_id, _token_count(response))
        return response

    except Exception:
        await scheduler.release(model_id)
        raise
