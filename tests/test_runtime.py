from __future__ import annotations

from pathlib import Path

from model_scheduler.config import load_config
from model_scheduler.control_recovery import ControlRecoveryClient
from model_scheduler.contracts import MemorySample
from model_scheduler.runtime import build_scheduler
from app import create_app



class Resources:
    def snapshot_now(self): return MemorySample(64 * 1024**3, 60 * 1024**3, 0)
    async def snapshot(self): return self.snapshot_now()


class Backend:
    async def load(self, operation, deadline): raise AssertionError
    async def stop(self, operation, deadline): raise AssertionError


def test_runtime_builder_wires_strict_config_to_single_scheduler_book(tmp_path) -> None:
    config = load_config(Path(__file__).resolve().parent.parent / "config.yaml")
    scheduler = build_scheduler(config, Backend(), resources=Resources(), storage_guard=lambda: True)
    assert scheduler.book.specs["embedding"].preload is True
    assert scheduler.queue_capacity == config.scheduler.queue_capacity
    assert scheduler.book.model_budget == 64 * 1024**3 - config.resources.system_reserve_bytes - config.scheduler.min_free_memory_bytes


def test_runtime_builder_defaults_to_a_fail_closed_storage_guard() -> None:
    config = load_config(Path(__file__).resolve().parent.parent / "config.yaml")
    scheduler = build_scheduler(config, Backend(), resources=Resources())
    assert scheduler.admission_guard is not None
    assert isinstance(scheduler.recovery, ControlRecoveryClient)


def test_app_factory_composes_runtime_when_control_backend_is_injected() -> None:
    app = create_app(backend=Backend(), resources=Resources(), storage_guard=lambda: True)
    assert app.state.scheduler.book.specs["embedding"].preload is True
    assert app.state.gateway is not None
