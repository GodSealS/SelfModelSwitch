"""Supported single-worker process entry point.

Two explicit branches (P17 AC2), one order for both:

    parse -> the single instance lock -> ONE runtime context -> reconcile old
    instances -> Blob recovery -> open the entries

v1 keeps the shipped llama-swap path exactly as the existing tests pin it.
v2 is the managed composition: it never touches `build_backend` (the v1
four-model manifest join), it imports the pinned `CONTROL_CONTRACT` only when
a registered profile really needs llama-swap, and both listeners share ONE
scheduler/Book/BlobStore/boot_id (C08). The TCP entry (uvicorn) owns the
process lifespan — it starts and cleans up exactly once — while the control
entry is the peer-credential Unix listener from `control_server`, which
removes its socket before the shared cleanup completes.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
import hashlib
import inspect
import json
import os
from pathlib import Path
import sys
from time import monotonic
from uuid import uuid4

import uvicorn

from app import create_app
from model_scheduler.config import AppConfigV2, ConfigError, load_config
from model_scheduler.control_identity import TokenAuthority
from model_scheduler.idempotency import IdempotencyStore
from model_scheduler.instance_lock import InstanceLocked, acquire
from model_scheduler.runtime import (
    RuntimeCompositionError,
    build_backend,
    build_managed_execution,
    ledger_specs_from,
    reconcile_startup,
)

_MANIFEST_PATH = Path("/etc/self-model-switch/manifest.json")
_V1_LOCK_PATH = Path("/run/model-scheduler/scheduler.lock")
_V2_DEPLOYMENT_ENV = "SELFMODEL_SWITCH_DEPLOYMENT_ID"
_V2_SWAP_CONTROL_ENV = "SELFMODEL_SWITCH_SWAP_CONTROL_URL"


@dataclass(frozen=True)
class StartupPlan:
    """What the config alone decides before anything is built."""

    schema_version: int
    needs_swap_control: bool
    lock_path: Path


@dataclass
class RunContextV2:
    """The ONE managed runtime context shared by both listeners."""

    boot_id: str
    scheduler: object
    service: object
    lifecycle: object
    book: object
    blobs: object
    tokens: TokenAuthority
    recovery: object
    observers: dict
    config: object
    extras: dict = field(default_factory=dict)


def startup_plan(config) -> StartupPlan:
    from model_scheduler.contracts_v2 import GGUF_PROFILE
    if config.schema_version == 2:
        needs_swap = any(runtime.profile_id == GGUF_PROFILE for runtime in config.runtimes.values())
        return StartupPlan(2, needs_swap, Path(config.control.socket_path).parent / "scheduler.lock")
    return StartupPlan(1, True, _V1_LOCK_PATH)


def build_v2_context(config: AppConfigV2, *, config_sha256: str, env=None, ports: dict | None = None) -> RunContextV2:
    """Join one v2 registration to the managed composition with fail-closed site inputs.

    `ports` injects the external boundaries (resources/blobs/recovery/control/
    clients/observers) for tests and for the P20 production wiring; every
    default is the real implementation. A v2 process refuses to start without
    the site identity, and without a swap endpoint when a profile needs one —
    it never silently guesses.
    """
    import httpx

    from model_scheduler.blob_store import BlobStore
    from model_scheduler.contracts_v2 import GGUF_PROFILE, DeploymentSpec
    from model_scheduler.control_recovery import DeploymentRecovery, DeploymentRecoveryPort
    from model_scheduler.model_registry import Book
    from model_scheduler.process_observer import DockerProcessObserver
    from model_scheduler.resource_monitor import ResourceMonitor
    from model_scheduler.session_manager import SessionManager

    env = os.environ if env is None else env
    ports = {} if ports is None else ports
    deployment_id = env.get(_V2_DEPLOYMENT_ENV)
    if not isinstance(deployment_id, str) or not deployment_id:
        raise RuntimeCompositionError(f"v2 startup needs the site deployment identity in {_V2_DEPLOYMENT_ENV}")
    if not isinstance(config_sha256, str) or len(config_sha256) != 64 or any(ch not in "0123456789abcdef" for ch in config_sha256):
        raise RuntimeCompositionError("v2 startup needs the SHA-256 of the raw configuration bytes")
    from model_scheduler.ports_v3 import ExpectedInstance

    registration = DeploymentSpec(runtimes=tuple(config.runtimes.values()), models=tuple(config.models.values()))
    # K4: one expectation per registered model, assembled here from the raw bytes.
    # `identity_digest` is therefore the *configuration* digest, not a candidate one.
    runtimes_by_id = {runtime.runtime_id: runtime for runtime in config.runtimes.values()}
    expected_instances: dict[str, ExpectedInstance] = {}
    for model_id, model in config.models.items():
        runtime = runtimes_by_id.get(model.runtime_id)
        if runtime is None:
            raise RuntimeCompositionError(f"model {model_id!r} is registered without its runtime")
        expected_instances[model_id] = ExpectedInstance(
            deployment_id=deployment_id, model_id=model_id, runtime_id=model.runtime_id,
            image_digest=runtime.image_digest, identity_digest=config_sha256,
        )
    specs = ledger_specs_from(registration=registration)
    book = ports.get("book") or Book(
        specs, model_budget=config.resources.model_budget_bytes,
        free_floor=config.scheduler.min_free_memory_bytes, margin=0,
        max_sample_age=config.resources.sample_max_age_seconds,
        half_life=config.scheduler.heat.half_life_seconds,
        request_weight=config.scheduler.heat.request_weight,
        token_weight=config.scheduler.heat.token_weight,
    )
    for model_id in specs:
        book.bootstrap_stopped(model_id)  # P07 keeps UNKNOWN for unobserved models; reconcile confirms below
    boot_id = uuid4().hex
    tokens = TokenAuthority(boot_id=boot_id)
    blobs = ports.get("blobs") or BlobStore(
        config.blobs.root, owner_quota_bytes=config.blobs.owner_quota_bytes,
        global_quota_bytes=config.blobs.total_quota_bytes, min_free_bytes=config.blobs.min_free_disk_bytes,
        chunked_reserve_bytes=config.blobs.chunk_reserve_bytes,
    )
    needs_swap = any(runtime.profile_id == GGUF_PROFILE for runtime in config.runtimes.values())
    control = ports.get("control")
    if control is None and needs_swap:
        base_url = env.get(_V2_SWAP_CONTROL_ENV)
        if not isinstance(base_url, str) or not base_url:
            raise RuntimeCompositionError(
                f"a llama-swap profile is registered but {_V2_SWAP_CONTROL_ENV} is not set")
        from model_scheduler.llama_swap_contract import CONTROL_CONTRACT  # only when the profile needs it
        from model_scheduler.llama_swap_client import LlamaSwapClient
        control = LlamaSwapClient(base_url, contract=CONTROL_CONTRACT)
    observers = ports.get("observers") or {
        model_id: DockerProcessObserver(deployment_id, model_id, model.port,
                                        expected=expected_instances[model_id])
        for model_id, model in config.models.items()
    }
    inference_base_urls = {model_id: f"http://127.0.0.1:{model.port}" for model_id, model in config.models.items()}
    clients = ports.get("clients") or {
        model_id: httpx.AsyncClient(base_url=url, follow_redirects=False)
        for model_id, url in inference_base_urls.items()
    }
    resources = ports.get("resources") or ResourceMonitor()
    recovery = ports.get("recovery") or DeploymentRecovery(deployment_id)
    # K5/RP09: the scheduler's runtime recovery entry is the async adapter over this
    # same helper; the helper itself keeps its startup-reconcile role in the context.
    recovery_port = DeploymentRecoveryPort(
        deployment_id=deployment_id, recovery=recovery, observers=observers,
        models=tuple(config.models),
    )
    policy = config.scheduler.sessions
    sessions = SessionManager(
        max_sessions=policy.queue_capacity, wait_seconds=policy.queue_timeout_seconds,
        hard_deadline_seconds=policy.hard_timeout_seconds, heartbeat_seconds=policy.heartbeat_seconds,
        ttl_seconds=policy.ttl_seconds, prepare_seconds=policy.prepare_limit_seconds,
        stop_grace_seconds=policy.stop_grace_seconds, cleanup_seconds=policy.cleanup_limit_seconds,
    )
    idempotency = IdempotencyStore()
    runtime = build_managed_execution(
        boot_id=boot_id, deployment=registration, deployment_id=deployment_id, book=book,
        resources=resources, control=control, clients=clients, observers=observers,
        inference_base_urls=inference_base_urls, blobs=blobs, sessions=sessions,
        tokens=tokens, idempotency=idempotency,
        scheduler_kwargs={
            "queue_capacity": config.scheduler.queue_capacity,
            "priority_aging_seconds": config.scheduler.priority_aging_seconds,
            "poll_interval_seconds": config.scheduler.poll_interval_seconds,
            "switch_drain_timeout_seconds": config.scheduler.switch_drain_timeout_seconds,
            "switch_retry_seconds": config.scheduler.switch_retry_seconds,
            "max_evictions": config.scheduler.max_evictions_per_request,
            "recovery": recovery_port,
        },
        execution_kwargs={"queue_capacity": policy.queue_capacity, "wait_seconds": policy.queue_timeout_seconds},
        expected_instances=expected_instances,
    )
    return RunContextV2(boot_id=boot_id, scheduler=runtime.scheduler, service=runtime.service,
                         lifecycle=runtime.lifecycle, book=book, blobs=blobs, tokens=tokens,
                         recovery=recovery, observers=dict(observers), config=config,
                         extras={"runtime": runtime, "clients": clients, "idempotency": idempotency,
                                 "control": control})


def _execution_stats(context: RunContextV2):
    """What /api/status reports about executions: counts only, never a token."""

    def stats() -> dict[str, int]:
        service = context.service
        records = list(service.records().values()) if service is not None else []
        return {
            "total": len(records),
            "active": sum(1 for record in records if not record.settled),
            "pending_cleanup": len(service.pending_cleanup) if service is not None else 0,
        }

    return stats


def v2_health_checks(context: RunContextV2):
    """The conservative /health provider for the managed runtime (P19).

    A busy queue never makes health unhealthy (C04); a blocked admission
    (recovering/shutting down/storage fault) or an unreachable control plane
    does. It reads scheduler facts and the control client's own health probe.
    """

    async def checks() -> dict[str, bool]:
        config = context.config
        status = await context.scheduler.status()
        resources = status.get("resources") or {}
        admission = status.get("admission") or {}
        models = status.get("models") or {}
        age = resources.get("sample_age_seconds") if isinstance(resources, dict) else None
        storage_ready = isinstance(admission, dict) and admission.get("storage_unavailable") is False
        recovering = not isinstance(admission, dict) or admission.get("recovering") is not False
        shutting_down = not isinstance(admission, dict) or admission.get("shutting_down") is not False
        preload_ready = isinstance(models, dict) and all(
            isinstance(models.get(model_id), dict) and models[model_id].get("state") == "ready"
            for model_id in config.scheduler.preload_models
        )
        control_health = getattr(context.extras.get("control"), "health", None)
        control_ready = False
        if callable(control_health):
            result = control_health()
            if inspect.isawaitable(result):
                result = await result
            control_ready = result is True
        return {
            "storage": storage_ready,
            "resources": type(age) in (int, float) and 0 <= age <= config.resources.sample_max_age_seconds,
            "preload": preload_ready,
            "control": control_ready and not recovering and not shutting_down,
            "llama_swap": control_ready,
        }

    return checks


def _v2_token_counter(context: RunContextV2):
    """The compatibility API's token counter: the model adapter's own runtime count (C06/P20).

    C06 forbids estimating tokens from characters, so the compat route counts
    through the same `/apply-template` + `/tokenize` path the adapter uses. A
    model without a counter refuses (503) instead of being dispatched blind.
    """

    async def counter(model_id: str, messages: list, image_count: int) -> int:
        runtime = context.extras.get("runtime")
        adapters = getattr(runtime, "adapters", None)
        adapter = adapters.get(model_id) if isinstance(adapters, dict) else None
        count = getattr(adapter, "count_chat_input", None)
        if not callable(count):
            raise RuntimeCompositionError(f"no runtime token counter is available for {model_id!r}")
        return await count(messages, image_count,
                           monotonic() + context.config.gateway.inference_timeout_seconds)

    return counter


def build_v2_tcp_app(context: RunContextV2):
    """The TCP listener of the v2 process (P19): the legacy surface over the managed runtime.

    The control routes stay on the Unix socket: this app never registers
    `/internal/*`, so the TCP side answers 404 by construction (C08), and
    /api/status reports this process's own boot id. A v2 registration also
    brings its C06 envelope and the runtime token counter (P20).
    """
    from app import create_app

    return create_app(config=context.config, scheduler=context.scheduler, gateway=None,
                      boot_id=context.boot_id, execution_stats=_execution_stats(context),
                      health_checks=v2_health_checks(context), token_counter=_v2_token_counter(context))


def serve_v2(context: RunContextV2) -> None:
    """Reconcile, recover blobs, then run both listeners over ONE lifespan."""
    from model_scheduler.control_api import ControlAPI
    from model_scheduler.control_server import ControlServer, build_control_app

    config = context.config

    async def main() -> None:
        reconciliation = await reconcile_startup(
            context.book, context.recovery, context.observers,
            deadline=monotonic() + config.scheduler.memory_reclaim_timeout_seconds)
        if not reconciliation.ok:
            # Admission stays closed (Book.recovering) — the process reports instead of serving half-truths.
            raise RuntimeCompositionError(f"startup reconciliation failed: {reconciliation.error_code}")
        await context.blobs.recover(instances_running=False, boot_id=context.boot_id)
        app = build_v2_tcp_app(context)
        api = ControlAPI(boot_id=context.boot_id, blobs=context.blobs, scheduler=context.scheduler,
                         service=context.service, tokens=context.tokens,
                         idempotency=context.extras.get("idempotency"))
        control = ControlServer(build_control_app(boot_id=context.boot_id, api=api),
                                socket_path=config.control.socket_path,
                                allowed_uids=config.control.allowed_uids,
                                peer_group=config.control.peer_group)
        server = uvicorn.Server(uvicorn.Config(app, host=config.server.host, port=config.server.port,
                                               log_level="info"))
        await control.start()
        try:
            await server.serve()  # the TCP app owns the one shared lifespan
        finally:
            await control.stop()

    asyncio.run(main())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path)
    parser.add_argument("--check-config", action="store_true")
    args = parser.parse_args(argv)
    path = args.config or Path(os.environ.get("MODEL_SCHEDULER_CONFIG", "config.yaml"))
    try:
        config_bytes = path.read_bytes()
        config = load_config(path)
        if path.read_bytes() != config_bytes:
            raise ConfigError("configuration changed while loading")
        # K4: the digest is taken from the exact bytes the process will run on, before
        # any early return, so v1 and v2 can never disagree about what was configured.
        config_sha256 = hashlib.sha256(config_bytes).hexdigest()
    except (ConfigError, OSError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 78
    if args.check_config:
        print(f"schema_version={config.schema_version} models={','.join(sorted(config.models))}")
        return 0
    plan = startup_plan(config)
    if plan.schema_version == 2:
        try:
            with acquire(plan.lock_path):
                serve_v2(build_v2_context(config, config_sha256=config_sha256))
        except RuntimeCompositionError as exc:
            print(f"configuration error: {exc}", file=sys.stderr)
            return 78
        except InstanceLocked:
            print("scheduler instance already running", file=sys.stderr)
            return 73
        return 0
    try:
        try:
            from model_scheduler.llama_swap_contract import CONTROL_CONTRACT
        except ImportError:
            print("configuration error: fixed llama-swap control contract is not installed", file=sys.stderr)
            return 78
        config_digest = config_sha256
        manifest = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
        backend = build_backend(config, manifest, config_digest, CONTROL_CONTRACT)
        with acquire(plan.lock_path):
            uvicorn.run(create_app(path, config=config, backend=backend), host=config.server.host, port=config.server.port, workers=1, reload=False)
    except (OSError, ValueError, json.JSONDecodeError, RuntimeCompositionError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 78
    except InstanceLocked:
        print("scheduler instance already running", file=sys.stderr)
        return 73
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
