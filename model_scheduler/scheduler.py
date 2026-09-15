"""Single-owner async scheduler built around the atomic :class:`Book`."""
from __future__ import annotations

import asyncio
from time import monotonic

from .contracts import ControlRecoveryPort, Lease, MemorySample, Observation, Outcome, Presence
from .eviction_policy import EvictionPolicy
from .model_registry import Book, Conflict, StaleOperation
from .request_queue import RequestQueue, WaitState


class QueueFull(RuntimeError):
    pass


class ModelUnavailable(RuntimeError):
    pass


class ModelScheduler:
    """Coordinates shared loads and leases without ever awaiting under its lock."""

    def __init__(self, book: Book, resources, backend, *, queue_capacity: int = 128, priority_aging_seconds: float = 30, max_evictions: int = 8, recovery: ControlRecoveryPort | None = None):
        self.book = book
        self.resources = resources
        self.backend = backend
        self.recovery = recovery
        self.queue_capacity = queue_capacity
        self.eviction_policy = EvictionPolicy(book, max_evictions=max_evictions)
        self._condition = asyncio.Condition()
        self._queue = RequestQueue(capacity=queue_capacity, aging_seconds=priority_aging_seconds)
        self._loads: dict[str, asyncio.Task[None]] = {}
        self._eviction: asyncio.Task[tuple[str, ...]] | None = None

    async def acquire(self, model_id: str, request_id: str, deadline: float) -> Lease:
        if model_id not in self.book.specs:
            raise KeyError(model_id)
        if deadline <= monotonic():
            raise TimeoutError("queue deadline elapsed")
        async with self._condition:
            try:
                self._queue.enqueue(request_id, model_id, self.book.specs[model_id].priority, deadline, monotonic())
            except ValueError as exc:
                raise Conflict("duplicate waiter") from exc
            except OverflowError as exc:
                raise QueueFull("queue_full")
        try:
            while True:
                needs_sample = False
                async with self._condition:
                    now = monotonic()
                    if now >= deadline or not self._queue.contains(request_id):
                        self._queue.remove(request_id, WaitState.EXPIRED)
                        raise TimeoutError("queue deadline elapsed")
                    if self._queue.is_head(request_id, now):
                        try:
                            lease = self.book.acquire_ready(model_id, request_id, now)
                        except Conflict:
                            runtime = self.book.runtime[model_id]
                            if runtime.state.value == "error":
                                raise ModelUnavailable(runtime.last_error or "model_error")
                            needs_sample = (runtime.state.value == "unloaded" and model_id not in self._loads
                                            and self._eviction is None)
                        else:
                            self._queue.remove(request_id, WaitState.CLAIMED)
                            return lease
                if needs_sample:
                    # Sampling is external I/O; take it outside the condition and
                    # re-check state before committing the operation below.
                    snapshot = await self.resources.snapshot()
                    sample = MemorySample(snapshot.total_bytes, snapshot.available_bytes, snapshot.sampled_at)
                    async with self._condition:
                        runtime = self.book.runtime[model_id]
                        if runtime.state.value == "unloaded" and model_id not in self._loads:
                            now = monotonic()
                            if self.book.can_load(model_id, sample, now):
                                operation = self.book.begin_load(model_id, sample, now)
                                self._loads[model_id] = asyncio.create_task(self._finish_load(operation, deadline))
                            elif self._eviction is None:
                                candidates = self.eviction_policy.choose(self._load_deficit(model_id, sample), now=now)
                                if candidates:
                                    operations = self.book.begin_eviction([candidate.model_id for candidate in candidates])
                                    self._eviction = asyncio.create_task(self._run_eviction(operations, deadline))
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
                self._queue.remove(request_id)
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

    def _load_deficit(self, model_id: str, sample: MemorySample) -> int:
        required = self.book.required(model_id)
        return max(0, self.book.free_floor + required - sample.available_bytes,
                   self.book.committed + required - self.book.model_budget)

    async def _run_eviction(self, operations, deadline: float) -> tuple[str, ...]:
        try:
            return await self._finish_eviction(operations, deadline)
        finally:
            async with self._condition:
                self._eviction = None
                self._condition.notify_all()

    async def _finish_eviction(self, operations, deadline: float) -> tuple[str, ...]:
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

    async def release(self, lease: Lease, outcome: Outcome, tokens: int | None = None) -> None:
        async with self._condition:
            self.book.release(lease, outcome, monotonic(), tokens)
            self._condition.notify_all()

    async def preload(self, deadline: float) -> tuple[str, ...]:
        """Bring configured resident models to READY without granting user leases."""
        ready: list[str] = []
        for model_id, spec in self.book.specs.items():
            if spec.preload:
                await self._preload_one(model_id, deadline)
                ready.append(model_id)
        return tuple(ready)

    async def _preload_one(self, model_id: str, deadline: float) -> None:
        while True:
            needs_sample = False
            async with self._condition:
                runtime = self.book.runtime[model_id]
                if runtime.state.value == "ready":
                    return
                if runtime.state.value == "error":
                    raise ModelUnavailable(runtime.last_error or "preload_failed")
                needs_sample = (runtime.state.value == "unloaded" and not self._loads and self._eviction is None)
            if needs_sample:
                snapshot = await self.resources.snapshot()
                sample = MemorySample(snapshot.total_bytes, snapshot.available_bytes, snapshot.sampled_at)
                async with self._condition:
                    runtime = self.book.runtime[model_id]
                    now = monotonic()
                    if runtime.state.value == "unloaded" and not self._loads and self._eviction is None:
                        if self.book.can_load(model_id, sample, now):
                            operation = self.book.begin_load(model_id, sample, now)
                            self._loads[model_id] = asyncio.create_task(self._finish_load(operation, deadline))
                        else:
                            candidates = self.eviction_policy.choose(self._load_deficit(model_id, sample), now=now)
                            if candidates:
                                operations = self.book.begin_eviction([candidate.model_id for candidate in candidates])
                                self._eviction = asyncio.create_task(self._run_eviction(operations, deadline))
                    self._condition.notify_all()
                continue
            async with self._condition:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError("preload deadline elapsed")
                try:
                    await asyncio.wait_for(self._condition.wait(), remaining)
                except asyncio.TimeoutError as exc:
                    raise TimeoutError("preload deadline elapsed") from exc

    async def recover(self, deadline: float) -> None:
        """Reconcile all model accounting through the injected root-owned helper."""
        if self.recovery is None:
            raise ModelUnavailable("control_recovery_unavailable")
        async with self._condition:
            if any(runtime.leases for runtime in self.book.runtime.values()):
                raise Conflict("recovery_has_leases")
            epoch = self.book.begin_recovery()
            self._condition.notify_all()
        result = await self.recovery.recover(deadline)
        async with self._condition:
            if not result.ok:
                self._condition.notify_all()
                raise ModelUnavailable(result.error_code or "control_recovery_failed")
            self.book.finish_recovery(epoch, frozenset(result.stopped_models))
            self._condition.notify_all()

    async def unload(self, model_id: str, deadline: float) -> None:
        async with self._condition:
            if model_id not in self.book.specs: raise KeyError(model_id)
            spec, runtime = self.book.specs[model_id], self.book.runtime[model_id]
            if spec.pinned: raise Conflict("model_pinned")
            if runtime.state.value == "unloaded": return
            if self._eviction is not None or runtime.leases or runtime.operation_id or runtime.state.value != "ready": raise Conflict("model_busy")
            operation = self.book.begin_eviction([model_id], automatic=False)[0]
            task = asyncio.create_task(self._run_eviction([operation], deadline))
            self._eviction = task
            self._condition.notify_all()
        stopped = await task
        if model_id not in stopped:
            raise ModelUnavailable("stop_unverified")
            self._condition.notify_all()

    async def sweep_ttl(self, deadline: float) -> tuple[str, ...]:
        """Stop due idle models, without allowing TTL to outrun queued demand."""
        now = monotonic()
        async with self._condition:
            if self._eviction is not None:
                return ()
            waiting_models = self._queue.waiting_models()
            model_ids = [
                model_id for model_id in self.book.specs
                if model_id not in waiting_models and self.book.ttl_due(model_id, now)
            ]
            if not model_ids:
                return ()
            operations = self.book.begin_eviction(model_ids)
            task = asyncio.create_task(self._run_eviction(operations, deadline))
            self._eviction = task
            self._condition.notify_all()
        return await task

    def _rollback_pending(self, operations) -> None:
        for operation in operations:
            try:
                self.book.rollback_unsent(operation)
            except StaleOperation:
                pass

    async def status(self) -> dict[str, object]:
        snapshot = await self.resources.snapshot()
        async with self._condition:
            return {"resources": {"total_bytes": snapshot.total_bytes, "available_bytes": snapshot.available_bytes, "used_bytes": snapshot.used_bytes, "utilization": snapshot.utilization, "source": snapshot.source, "sample_age_seconds": snapshot.age_at(monotonic())}, "queue_size": self._queue.size, "models": {model_id: {"state": runtime.state.value, "generation": runtime.generation, "in_flight": len(runtime.leases), "last_error": runtime.last_error} for model_id, runtime in self.book.runtime.items()}}
