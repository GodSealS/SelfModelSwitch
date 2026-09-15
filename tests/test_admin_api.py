from fastapi.testclient import TestClient

from app import create_app
from model_scheduler.scheduler import ModelUnavailable


class Scheduler:
    async def status(self):
        return {"resources": {"source": "psutil"}, "queue_size": 2, "models": {"qwen-small": {"state": "ready", "in_flight": 0}}}


def test_status_and_models_are_scheduler_backed() -> None:
    with TestClient(create_app(scheduler=Scheduler())) as client:
        status = client.get("/api/status")
        models = client.get("/api/models")
    assert status.status_code == 200
    assert status.json()["queue_size"] == 2
    assert models.json()["qwen-small"]["state"] == "ready"


def test_health_reflects_actual_injected_dependency_checks() -> None:
    with TestClient(create_app(health_checks=lambda: {"llama_swap": True, "storage": True, "resources": True, "preload": True, "control": True})) as client:
        healthy = client.get("/health")
    with TestClient(create_app(health_checks=lambda: {"llama_swap": True, "storage": False, "resources": True, "preload": True, "control": True})) as client:
        unhealthy = client.get("/health")
    assert healthy.status_code == 200 and healthy.json()["ok"] is True
    assert unhealthy.status_code == 503 and unhealthy.json()["checks"]["storage"] is False


def test_manual_unload_does_not_return_success_without_stop_evidence() -> None:
    class UnverifiedScheduler:
        async def unload(self, model_id, deadline): raise ModelUnavailable("stop_unverified")
    with TestClient(create_app(scheduler=UnverifiedScheduler())) as client:
        response = client.post("/api/models/qwen-small/unload")
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "stop_unverified"


def test_lifespan_starts_injected_preload_without_blocking_live_endpoint() -> None:
    class PreloadingScheduler:
        called = False
        async def preload(self, deadline): self.called = True
    scheduler = PreloadingScheduler()
    with TestClient(create_app(scheduler=scheduler)) as client:
        assert client.get("/live").status_code == 200
        assert scheduler.called is True


def test_preload_failure_keeps_health_unready() -> None:
    class BrokenPreloadScheduler:
        async def preload(self, deadline): raise RuntimeError("pinned preload failed")
    checks = lambda: {"llama_swap": True, "storage": True, "resources": True, "preload": True, "control": True}
    with TestClient(create_app(scheduler=BrokenPreloadScheduler(), health_checks=checks)) as client:
        response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["checks"]["preload"] is False


def test_framework_not_found_errors_use_the_standard_request_id_shape() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/does-not-exist")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert response.json()["request_id"] == response.headers["x-request-id"]
