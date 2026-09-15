"""Single-owner async scheduler built around the atomic :class:`Book`."""
from __future__ import annotations

import asyncio
from time import monotonic

from .contracts import Lease, MemorySample, Observation, Outcome, Presence
from .model_registry import Book, Conflict, StaleOperation


class QueueFull(RuntimeError):
    pass


class ModelUnavailable(RuntimeError):
    pass


class ModelScheduler:
    """Coordinates shared loads and leases without ever awaiting under its lock."""

    def __init__(self, book: Book, resources, backend, *, queue_capacity: int = 128):
        self.book = book
        self.resources = resources
        self.backend = backend
        self.queue_capacity = queue_capacity
        self._condition = asyncio.Condition()
        self._waiters: set[str] = set()
        self._waiter_models: dict[str, str] = {}
        self._loads: dict[str, asyncio.Task[None]] = {}

    async def acquire(self, model_id: str, request_id: str, deadline: float) -> Lease:
        if model_id not in self.book.specs:
            raise KeyError(model_id)
        if deadline <= monotonic():
            raise TimeoutError("queue deadline elapsed")
        async with self._condition:
            if request_id in self._waiters:
                raise Conflict("duplicate waiter")
            if len(self._waiters) >= self.queue_capacity:
                raise QueueFull("queue_full")
            self._waiters.add(request_id)
            self._waiter_models[request_id] = model_id
        try:
            while True:
                needs_sample = False
                async with self._condition:
                    try:
                        lease = self.book.acquire_ready(model_id, request_id, monotonic())
                    except Conflict:
                        runtime = self.book.runtime[model_id]
                        if runtime.state.value == "error":
                            raise ModelUnavailable(runtime.last_error or "model_error")
                        needs_sample = runtime.state.value == "unloaded" and model_id not in self._loads
                    else:
                        return lease
                if needs_sample:
                    # Sampling is external I/O; take it outside the condition and
                    # re-check state before committing the operation below.
                    snapshot = await self.resources.snapshot()
                    sample = MemorySample(snapshot.total_bytes, snapshot.available_bytes, snapshot.sampled_at)
                    async with self._condition:
                        runtime = self.book.runtime[model_id]
                        if runtime.state.value == "unloaded" and model_id not in self._loads:
                            operation = self.book.begin_load(model_id, sample, monotonic())
                            self._loads[model_id] = asyncio.create_task(self._finish_load(operation, deadline))
                        self._condition.notify_all()
                    continue
                async with self._condition:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise TimeoutError("queue deadline elapsed")
                    try:
                        await asyncio.wait_for(self._condition.wait(), remaining)
                    except asyncio.TimeoutError as exc:
                        raise TimeoutError("queue deadline elapsed") from exc
        finally:
            async with self._condition:
                self._waiters.discard(request_id)
                self._waiter_models.pop(request_id, None)
                self._condition.notify_all()

    async def _finish_load(self, operation, deadline: float) -> None:
        try:
            observation: Observation = await self.backend.load(operation, deadline)
            async with self._condition:
                if observation.presence is Presence.RUNNING and observation.healthy:
                    self.book.loaded(operation, monotonic())
                else:
                    self.book.failed(operation, observation.detail_code or "load_unverified")
                self._condition.notify_all()
        except (Exception, asyncio.CancelledError) as exc:
            async with self._condition:
                try:
                    self.book.failed(operation, "load_failed")
                except StaleOperation:
                    pass
                self._condition.notify_all()
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            async with self._condition:
                self._loads.pop(operation.model_id, None)
                self._condition.notify_all()

    async def release(self, lease: Lease, outcome: Outcome, tokens: int | None = None) -> None:
        async with self._condition:
            self.book.release(lease, outcome, monotonic(), tokens)
            self._condition.notify_all()

    async def unload(self, model_id: str, deadline: float) -> None:
        async with self._condition:
            if model_id not in self.book.specs: raise KeyError(model_id)
            spec, runtime = self.book.specs[model_id], self.book.runtime[model_id]
            if spec.pinned: raise Conflict("model_pinned")
            if runtime.state.value == "unloaded": return
            if runtime.leases or runtime.operation_id or runtime.state.value != "ready": raise Conflict("model_busy")
            operation = self.book.begin_eviction([model_id], automatic=False)[0]
        try:
            observation = await self.backend.stop(operation, deadline)
        except asyncio.CancelledError:
            async with self._condition:
                try:
                    self.book.failed(operation, "stop_unverified")
                except StaleOperation:
                    pass
                self._condition.notify_all()
            raise
        except Exception:
            async with self._condition:
                try:
                    self.book.failed(operation, "stop_unverified")
                except StaleOperation:
                    pass
                self._condition.notify_all()
            raise
        async with self._condition:
            if observation.presence is Presence.STOPPED:
                self.book.stopped(operation)
            else:
                self.book.failed(operation, observation.detail_code or "stop_unverified")
            self._condition.notify_all()

    async def sweep_ttl(self, deadline: float) -> tuple[str, ...]:
        """Stop due idle models, without allowing TTL to outrun queued demand."""
        now = monotonic()
        async with self._condition:
            waiting_models = set(self._waiter_models.values())
            model_ids = [
                model_id for model_id in self.book.specs
                if model_id not in waiting_models and self.book.ttl_due(model_id, now)
            ]
            if not model_ids:
                return ()
            operations = self.book.begin_eviction(model_ids)
            self._condition.notify_all()

        stopped: list[str] = []
        for index, operation in enumerate(operations):
            try:
                observation = await self.backend.stop(operation, deadline)
            except asyncio.CancelledError:
                async with self._condition:
                    self.book.failed(operation, "stop_unverified")
                    self._rollback_pending(operations[index + 1:])
                    self._condition.notify_all()
                raise
            except Exception:
                observation = None
            async with self._condition:
                if observation is not None and observation.presence is Presence.STOPPED:
                    self.book.stopped(operation)
                    stopped.append(operation.model_id)
                else:
                    self.book.failed(operation, observation.detail_code if observation else "stop_unverified")
                    self._rollback_pending(operations[index + 1:])
                    self._condition.notify_all()
                    return tuple(stopped)
                self._condition.notify_all()
        return tuple(stopped)

    def _rollback_pending(self, operations) -> None:
        for operation in operations:
            try:
                self.book.rollback_unsent(operation)
            except StaleOperation:
                pass

    async def status(self) -> dict[str, object]:
        snapshot = await self.resources.snapshot()
        async with self._condition:
            return {"resources": {"total_bytes": snapshot.total_bytes, "available_bytes": snapshot.available_bytes, "used_bytes": snapshot.used_bytes, "utilization": snapshot.utilization, "source": snapshot.source, "sample_age_seconds": snapshot.age_at(monotonic())}, "queue_size": len(self._waiters), "models": {model_id: {"state": runtime.state.value, "generation": runtime.generation, "in_flight": len(runtime.leases), "last_error": runtime.last_error} for model_id, runtime in self.book.runtime.items()}}
