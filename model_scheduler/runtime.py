"""Explicit runtime composition for the single scheduler process."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlsplit

from .adapters.llama_cpp import LlamaCppAdapter
from .backend_control import LlamaSwapBackend, ManagedLifecycle, ManagedModel
from .deploy import DeployError
from .config import AppConfig, model_specs
from .contracts_v2 import DeploymentSpec
from .control_identity import TokenAuthority
from .control_recovery import ControlRecoveryClient, DeploymentRecovery, ReconcileOutcome
from .execution_service import ExecutionService
from .idempotency import IdempotencyStore
from .llama_swap_client import LlamaSwapClient, LlamaSwapControlContract
from .model_registry import Book
from .ports_v3 import STOPPED, ExpectedInstance, LifecyclePolicy, ObservationTarget
from .process_observer import ProcessObserver
from .resource_monitor import ResourceMonitor
from .scheduler import ModelScheduler
from .session_manager import SessionManager
from .storage_monitor import StorageAdmissionGuard, StorageMonitor


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE = re.compile(r"[^@]+@sha256:[0-9a-f]{64}\Z")
_DEPLOYMENT = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")


class RuntimeCompositionError(ValueError):
    """The rendered manifest cannot safely be joined to this scheduler config."""


def build_backend(config: AppConfig, manifest: Any, config_sha256: str, contract: LlamaSwapControlContract) -> LlamaSwapBackend:
    """Join one fixture-bound control contract to a matching rendered manifest."""
    if not isinstance(manifest, dict) or not _SHA256.fullmatch(config_sha256):
        raise RuntimeCompositionError("invalid runtime manifest identity")
    deployment_id, image, manifest_digest, models = (
        manifest.get("deployment_id"),
        manifest.get("image"),
        manifest.get("config_sha256"),
        manifest.get("models"),
    )
    if (
        not isinstance(deployment_id, str)
        or not _DEPLOYMENT.fullmatch(deployment_id)
        or not isinstance(image, str)
        or not _IMAGE.fullmatch(image)
        or manifest_digest != config_sha256
        or not isinstance(models, dict)
        or set(models) != set(config.models)
    ):
        raise RuntimeCompositionError("invalid runtime manifest identity")
    managed: dict[str, ManagedModel] = {}
    for model_id, model in config.models.items():
        rendered = models[model_id]
        if (
            not isinstance(rendered, dict)
            or rendered.get("container_name") != model.container_name
            or rendered.get("file") != model.file
            or rendered.get("sha256") != model.sha256
            or model.container_name != f"sms-{deployment_id}-{model_id}"
        ):
            raise RuntimeCompositionError("manifest/config model identity mismatch")
        port = urlsplit(model.upstream_url).port
        if port is None:
            raise RuntimeCompositionError("invalid model loopback port")
        managed[model_id] = ManagedModel(model.container_name, port)
    control = LlamaSwapClient(
        config.llama_swap.base_url,
        timeout=config.llama_swap.control_timeout_seconds,
        load_timeout=config.llama_swap.load_timeout_seconds,
        contract=contract,
    )
    return LlamaSwapBackend(control, ProcessObserver(deployment_id, image, config_sha256), managed)


@dataclass(frozen=True)
class StartupReconciliation:
    """What the C03 startup sequence proved for this deployment (P07)."""

    ok: bool
    error_code: str | None
    confirmed_stopped: frozenset[str]
    unproven: frozenset[str]
    remaining_container_ids: tuple[str, ...]


async def reconcile_startup(
    book: Book,
    recovery: DeploymentRecovery,
    observers: Mapping[str, Any],
    *,
    deadline: float,
) -> StartupReconciliation:
    """Close admission, clean this deployment's leftovers, then clear the book.

    Admission closes before any docker I/O. Only a model whose instance is
    independently observed STOPPED may be reconciled; any unproven or unremoved
    instance keeps `Book.recovering` set, so no load is admitted and no
    reservation is released (plan/08-execution-plan.md C03).
    """
    epoch = book.begin_recovery()
    outcome: ReconcileOutcome = recovery.reconcile(close_admission=lambda: book.begin_recovery(), deadline=deadline)
    confirmed: set[str] = set()
    unproven: set[str] = set(book.specs) - set(observers)
    for model_id, observer in observers.items():
        if model_id not in book.specs:
            unproven.add(model_id)
            continue
        observation = await observer.observe(ObservationTarget(deployment_id=recovery.deployment_id), deadline)
        (confirmed if observation.state == STOPPED else unproven).add(model_id)
    if not outcome.ok:
        return StartupReconciliation(False, outcome.error_code, frozenset(confirmed), frozenset(unproven), outcome.remaining_container_ids)
    if unproven:
        return StartupReconciliation(False, "unproven_stop", frozenset(confirmed), frozenset(unproven), ())
    book.finish_recovery(epoch, frozenset(confirmed))
    return StartupReconciliation(True, None, frozenset(confirmed), frozenset(), ())


def ledger_specs_from(*, config: AppConfig | None = None, registration: DeploymentSpec | None = None) -> dict[str, Any]:
    """Exactly one registration source becomes the book's ledger specs (C02).

    The v1 configuration crosses the compatibility boundary with its legacy
    percent margin; a v2 registration carries R (and its physical peak) already
    and is passed through unchanged, so no margin is ever applied twice.
    """
    if (config is None) == (registration is None):
        raise ValueError("exactly one of config or registration is required")
    if config is not None:
        return model_specs(config)
    models = {model.model_id: model for model in registration.models}
    if len(models) != len(registration.models):
        raise ValueError("the registration declares duplicate model ids")
    runtimes = {runtime.runtime_id for runtime in registration.runtimes}
    unknown = {model.runtime_id for model in registration.models} - runtimes
    if unknown:
        raise ValueError(f"models reference unknown runtimes: {sorted(unknown)}")
    return models


def build_scheduler(
    config: AppConfig,
    backend: Any,
    *,
    resources: Any | None = None,
    storage_guard: Any | None = None,
    recovery: Any | None = None,
    confirmed_stopped: Iterable[str] | None = None,
) -> ModelScheduler:
    """Build the one authoritative book from strict config and injected ports.

    `confirmed_stopped=None` keeps the legacy v1 bootstrap (every model starts
    UNLOADED). The v3 entry passes the observed set: only those models are
    bootstrapped, and every model without independent stop evidence stays
    UNKNOWN, which no load can ever admit.
    """
    resource_port = resources or ResourceMonitor()
    snapshot = resource_port.snapshot_now()
    budget = snapshot.total_bytes - config.resources.system_reserve_bytes - config.scheduler.min_free_memory_bytes
    if budget <= 0:
        raise ValueError("system memory cannot satisfy configured reserves")
    book = Book(
        ledger_specs_from(config=config),
        model_budget=budget,
        free_floor=config.scheduler.min_free_memory_bytes,
        margin=config.scheduler.resource_safety_margin,
        max_sample_age=config.resources.sample_max_age_seconds,
        half_life=config.scheduler.heat.half_life_seconds,
        request_weight=config.scheduler.heat.request_weight,
        token_weight=config.scheduler.heat.token_weight,
    )
    if confirmed_stopped is None:
        for model_id in book.specs:
            book.bootstrap_stopped(model_id)
    else:
        observed = frozenset(confirmed_stopped)
        unknown_ids = observed - set(book.specs)
        if unknown_ids:
            raise ValueError(f"stop evidence names unknown models: {sorted(unknown_ids)}")
        for model_id in observed:
            book.bootstrap_stopped(model_id)
    guard = storage_guard if storage_guard is not None else StorageAdmissionGuard(
        StorageMonitor(
            config.storage.mount_path,
            config.storage.model_directory,
            config.storage.expected_uuid,
            config.storage.filesystem,
        ),
        config.models,
        sample_interval_seconds=config.resources.sample_interval_seconds,
    )
    return ModelScheduler(
        book,
        resource_port,
        backend,
        queue_capacity=config.scheduler.queue_capacity,
        priority_aging_seconds=config.scheduler.priority_aging_seconds,
        poll_interval_seconds=config.scheduler.poll_interval_seconds,
        max_evictions=config.scheduler.max_evictions_per_request,
        switch_drain_timeout_seconds=config.scheduler.switch_drain_timeout_seconds,
        switch_retry_seconds=config.scheduler.switch_retry_seconds,
        switch_window_seconds=config.scheduler.thrash.switch_window_seconds,
        max_switches_in_window=config.scheduler.thrash.max_switches_in_window,
        cooldown_seconds=config.scheduler.thrash.cooldown_seconds,
        recovery=recovery if recovery is not None else ControlRecoveryClient(),
        admission_guard=guard,
    )

@dataclass(frozen=True)
class ManagedExecutionRuntime:
    """The P16 composition: scheduler books, lifecycle bridge and the execution service."""

    scheduler: ModelScheduler
    lifecycle: ManagedLifecycle
    adapters: Mapping[str, Any]
    service: ExecutionService


def build_managed_execution(
    *,
    boot_id: str,
    deployment: DeploymentSpec,
    deployment_id: str,
    book: Book,
    resources: Any,
    control: Any,
    clients: Mapping[str, Any],
    observers: Mapping[str, Any],
    inference_base_urls: Mapping[str, str],
    blobs: Any,
    sessions: SessionManager | None = None,
    clock: Callable[[], float] | None = None,
    tokens: TokenAuthority | None = None,
    idempotency: IdempotencyStore | None = None,
    event_sink: Callable[[str, Mapping[str, object]], None] | None = None,
    fixture_path: Path | None = None,
    scheduler_kwargs: Mapping[str, Any] | None = None,
    execution_kwargs: Mapping[str, Any] | None = None,
    lifecycle_policy: LifecyclePolicy | None = None,
    expected_instances: Mapping[str, ExpectedInstance],
) -> ManagedExecutionRuntime:
    """Join one v2 registration, llama-swap control, C03 observers and the queue.

    Every model gets its own adapter whose identity is RESOLVED THROUGH THE
    BOOK: the bridge only looks the accepted identity up, so execute/stop always
    carry the one the books committed at READY (P16 AC1, K2). The execution
    service is built in managed-termination mode: dispatched requests settle
    via the adapter's trusted protocol when it claims one, otherwise via a
    proven independent STOPPED of the shared instance (P16 AC2/AC3).
    """
    runtimes = {runtime.runtime_id: runtime for runtime in deployment.runtimes}
    models = {model.model_id: model for model in deployment.models}
    missing = set(models) - set(clients) - set(observers) - set(inference_base_urls)
    if missing:
        raise RuntimeCompositionError(f"managed composition lacks ports for: {sorted(missing)}")
    # K4: the expectation is assembled by the composition root from the raw config
    # bytes. There is no weak fallback — a missing or mismatched expectation is a
    # refused startup, because an observation can never be the source of the rule
    # that judges it.
    if not isinstance(expected_instances, Mapping) or set(expected_instances) != set(models):
        raise RuntimeCompositionError("managed composition needs exactly one expected instance per registered model")
    for model_id, expected in expected_instances.items():
        if (not isinstance(expected, ExpectedInstance) or expected.deployment_id != deployment_id
                or expected.model_id != model_id or expected.digest_kind != "config"):
            raise RuntimeCompositionError(f"the expected instance for {model_id!r} does not belong to this deployment")
    adapters: dict[str, Any] = {}
    lifecycle = ManagedLifecycle(
        boot_id=boot_id, deployment_id=deployment_id, specs=models,
        adapter_for=adapters.__getitem__, observers=observers,
        instance_lookup=book.instance, policy=lifecycle_policy, expected=expected_instances,
    )
    for model_id, model in models.items():
        adapters[model_id] = LlamaCppAdapter(
            runtime=runtimes[model.runtime_id], model=model,
            inference_base_url=inference_base_urls[model_id], client=clients[model_id],
            identity=lambda mid=model_id: lifecycle.instance(mid),
            fixture_path=fixture_path, control=control,
        )
    clock_kwargs = {} if clock is None else {"clock": clock}
    scheduler = ModelScheduler(book, resources, lifecycle, sessions=sessions,
                               require_instance_identity=True,
                               **clock_kwargs, **(scheduler_kwargs or {}))
    service = ExecutionService(
        scheduler, blobs=blobs, backend_for=adapters.__getitem__, boot_id=boot_id,
        tokens=tokens, idempotency=idempotency, event_sink=event_sink, managed_termination=True,
        **clock_kwargs, **(execution_kwargs or {}),
    )
    return ManagedExecutionRuntime(scheduler=scheduler, lifecycle=lifecycle, adapters=adapters, service=service)


def load_lab_manifest(path: str | Path, *, config_path: str | Path | None = None) -> dict[str, Any]:
    """Load a lab manifest rendered by `deploy render --mode lab` (P06b).

    A lab manifest is a test instance identity: it is marked lab-only and binds
    the exact configuration digest plus every model's rendered argv, image,
    runtime and profile. Given `config_path` the digest is re-checked, so a
    manifest can never be joined to a different configuration.
    """
    manifest_path = Path(path)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeployError(f"cannot read the lab manifest: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("mode") != "lab" or manifest.get("lab_only") is not True:
        raise DeployError("the lab manifest must be an explicit lab-only render")
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("models"), dict) or not manifest["models"]:
        raise DeployError("the lab manifest declares no model")
    for model_id, entry in manifest["models"].items():
        if not isinstance(entry, dict) or not isinstance(entry.get("argv"), list) or not entry["argv"]:
            raise DeployError(f"the lab manifest entry for {model_id!r} has no rendered argv")
        digest = hashlib.sha256(b"\x00".join(token.encode("utf-8") for token in entry["argv"])).hexdigest()
        if entry.get("argv_sha256") != digest:
            raise DeployError(f"the lab manifest entry for {model_id!r} does not match its argv digest")
    if config_path is not None:
        try:
            config_sha256 = hashlib.sha256(Path(config_path).read_bytes()).hexdigest()
        except OSError as exc:
            raise DeployError(f"cannot read the lab configuration: {exc}") from exc
        if manifest.get("config_sha256") != config_sha256:
            raise DeployError("the lab manifest config digest does not match this configuration")
    return manifest


def lab_launch_argv(manifest: Mapping[str, Any], model_id: str) -> list[str]:
    """The exact rendered argv of one lab model; unknown ids are refused."""
    models = manifest.get("models") if isinstance(manifest, Mapping) else None
    entry = models.get(model_id) if isinstance(models, Mapping) else None
    if not isinstance(entry, Mapping) or not isinstance(entry.get("argv"), list):
        raise DeployError(f"the lab manifest has no rendered entry for model {model_id!r}")
    return list(entry["argv"])
