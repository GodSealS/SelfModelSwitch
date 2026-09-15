from fastapi.testclient import TestClient

from app import create_app


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
