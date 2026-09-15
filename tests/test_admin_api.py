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


def test_health_uses_conservative_scheduler_runtime_checks_without_an_injected_provider() -> None:
    class Control:
        async def health(self):
            return True

    class Backend:
        control = Control()

    class HealthyScheduler:
        backend = Backend()

        async def status(self):
            return {
                "resources": {"sample_age_seconds": 0.1},
                "admission": {"storage_unavailable": False, "recovering": False, "shutting_down": False},
                "models": {
                    "embedding": {"state": "ready"},
                    "reranker": {"state": "ready"},
                    "qwen-small": {"state": "ready"},
                    "qwen-large": {"state": "ready"},
                },
            }

    with TestClient(create_app(scheduler=HealthyScheduler())) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["checks"] == {"llama_swap": True, "storage": True, "resources": True, "preload": True, "control": True}


def test_manual_unload_does_not_return_success_without_stop_evidence() -> None:
    class UnverifiedScheduler:
        async def unload(self, model_id, deadline): raise ModelUnavailable("stop_unverified")
    with TestClient(create_app(scheduler=UnverifiedScheduler())) as client:
        response = client.post("/api/models/qwen-small/unload")
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "stop_unverified"


def test_explicit_recovery_uses_the_scheduler_storage_recovery_port() -> None:
    class RecoverableScheduler:
        called = False

        async def storage_recovered(self, deadline):
            self.called = True
            return ("embedding",)

    scheduler = RecoverableScheduler()
    with TestClient(create_app(scheduler=scheduler)) as client:
        response = client.post("/api/recover")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "preloaded": ["embedding"]}
    assert scheduler.called is True


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


def test_lifespan_invokes_injected_scheduler_shutdown() -> None:
    class ShutdownScheduler:
        called = False
        async def shutdown(self, deadline): self.called = True; return ()
    scheduler = ShutdownScheduler()
    with TestClient(create_app(scheduler=scheduler)):
        pass
    assert scheduler.called is True


def test_lifespan_starts_and_cancels_storage_monitoring() -> None:
    class StorageScheduler:
        calls = 0
        stopped = False

        async def monitor_storage_once(self, deadline):
            self.calls += 1
            return True

        async def shutdown(self, deadline):
            self.stopped = True
            return ()

    scheduler = StorageScheduler()
    with TestClient(create_app(scheduler=scheduler)) as client:
        assert client.get("/live").status_code == 200
    assert scheduler.calls >= 1
    assert scheduler.stopped is True
