"""P30: the real ports behind the O cases.

O01 is the mixed workload against the deployment's compat surface plus an
end-state probe the zero tolerances are read from; O05 is the P26 preflight
primitive against the live site. Both are honest material: every number comes
from the live service, the container listing, `/proc/vmstat` or a file on this
machine.

The remaining ports (O02 storage faults, O03 Docker faults, O04 restart, O06
lab release) need the deployment's private mount namespace and its service
manager. Until they are wired their cases report `not_run` with the missing
condition — never a placeholder pass.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Callable, Mapping, Sequence

import httpx

from ..evidence_contracts import ContractError, candidate_digest, device_digest, parse_candidate
from ..preflight_v3 import PreflightError, manifest_identity, verify_environment
from ..process_observer import DEPLOYMENT_LABEL
from .workload import PlannedRequest

SITE_DEVICE_FIELDS = ("machine_id_sha256", "architecture", "device_tree_sha256", "mem_total_bytes",
                      "kernel_release", "model_disk_uuid", "scratch_disk_uuid")
DOCKER_TIMEOUT_SECONDS = 60


class PortError(RuntimeError):
    """A port refusal; the orchestrator turns it into a failed step, never a pass."""


# ---------------------------------------------------------------------------
# O01: the compat surface as the workload client, and the end-state probe


@dataclass
class HttpWorkloadDriver:
    """One request per arrival, measured end to end on the client side.

    The control view does not expose a phase split, so the whole latency is
    recorded as `execute_seconds` with `queue_seconds`/`load_seconds` at zero:
    the total a p95 is computed from still contains queueing, loading and
    execution — it is simply not split into parts the service never reports.
    """

    base_url: str
    timeout_seconds: float = 1800.0
    clock: Callable[[], float] = field(default=time.monotonic)
    post: Callable[..., Any] = field(default=httpx.post)

    def send(self, request: PlannedRequest) -> Mapping[str, Any]:
        if request.capability != "chat":
            raise PortError(f"the workload driver speaks chat; got {request.capability!r}")
        payload = {"model": request.model_id,
                   "messages": [{"role": "user", "content": "Reply with one word."}],
                   "max_tokens": 8}
        started = self.clock()
        try:
            response = self.post(self.base_url.rstrip("/") + "/v1/chat/completions", json=payload,
                                 timeout=self.timeout_seconds)
        except Exception as exc:  # noqa: BLE001 - a refused send is material, not a crash
            return {"error": f"{type(exc).__name__}: {exc}"}
        elapsed = self.clock() - started
        document: dict[str, Any] = {"status_code": response.status_code,
                                    "queue_seconds": 0.0, "load_seconds": 0.0,
                                    "execute_seconds": round(elapsed, 3),
                                    "latency_seconds": round(elapsed, 3),
                                    "latency_source": "client end-to-end"}
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, Mapping):
            document["usage"] = body.get("usage")
            document["model"] = body.get("model") or request.model_id
        return document


@dataclass
class FinalStateProbe:
    """The zero-tolerance end state, read from the live service and this machine.

    Every figure has a named source: `queue_depth`/`leases`/`sessions` come from
    `/api/status`, `instances_running`/`residual` from the managed container
    listing, `oom` from the `/proc/vmstat` delta and `unexpected_500` from the
    service log when one is supplied. A figure that cannot be read stays `None`,
    which fails the case instead of asserting a zero nobody observed.
    """

    base_url: str
    deployment_id: str
    log_path: Path | None = None
    timeout_seconds: float = 60.0
    _oom_baseline: int | None = field(default=None, init=False)

    def baseline(self) -> None:
        self._oom_baseline = _oom_kills()

    def read(self) -> dict:
        status = _get_json(self.base_url, "/api/status", timeout=self.timeout_seconds)
        models = status.get("models") if isinstance(status.get("models"), Mapping) else {}
        leases = None
        if isinstance(models, Mapping) and models:
            leases = sum(int(model.get("in_flight") or 0) + int(model.get("cancelling") or 0)
                         for model in models.values() if isinstance(model, Mapping))
        sessions = status.get("sessions") if isinstance(status.get("sessions"), Mapping) else None
        running = _managed_container_count(self.deployment_id)
        kills = _oom_kills()
        return {
            "oom": None if self._oom_baseline is None or kills is None else max(0, kills - self._oom_baseline),
            "unexpected_500": _count_500(self.log_path),
            "unsafe_evictions": _unsafe_evictions(models),
            "residual": running,
            "queue_depth": status.get("queue_size") if isinstance(status.get("queue_size"), int) else None,
            "leases": leases,
            "sessions": None if sessions is None else len(sessions.get("pending") or []),
            "instances_running": running,
        }

    # The orchestrator reads the end state through a mapping so the probe runs
    # *after* the workload, not when the call is assembled.
    def mapping(self) -> Mapping[str, Any]:
        return _LazyMapping(self.read)


class _LazyMapping(Mapping):
    """A read-only mapping that evaluates its reader on first access."""

    def __init__(self, reader: Callable[[], Mapping[str, Any]]) -> None:
        self._reader = reader
        self._value: Mapping[str, Any] | None = None

    def _resolve(self) -> Mapping[str, Any]:
        if self._value is None:
            self._value = dict(self._reader())
        return self._value

    def __getitem__(self, key: str) -> Any:
        return self._resolve()[key]

    def __iter__(self):
        return iter(self._resolve())

    def __len__(self) -> int:
        return len(self._resolve())


def _all_stopped(state: Mapping[str, Any]) -> bool:
    return state.get("instances_running") == 0 and state.get("residual") == 0


@dataclass
class DeploymentQuiescer:
    """The run's own stop evidence: ask the deployment to release what it served.

    The swap proxy keeps an upstream alive until it is told otherwise (the lab's
    `ttl` is 0), so a run can only *end* with every instance stopped if it asks
    for the release and then proves it stopped. Whatever the probe reads at the
    deadline is what the case records: no silent retry and no assumed zero.
    """

    base_url: str
    probe: Any
    models: Sequence[str] = ()
    timeout_seconds: float = 300.0
    poll_seconds: float = 2.0
    post: Callable[..., Any] = field(default=httpx.post)
    clock: Callable[[], float] = field(default=time.monotonic)
    wait: Callable[[float], None] = field(default=time.sleep)

    def __call__(self) -> Mapping[str, Any]:
        started = self.clock()
        released: list[str] = []
        refused: list[str] = []
        for model_id in self.models:
            try:
                response = self.post(self.base_url.rstrip("/") + f"/api/models/{model_id}/unload",
                                     timeout=self.timeout_seconds)
                refused.append(model_id) if getattr(response, "status_code", 0) != 200 else released.append(model_id)
            except Exception as exc:  # noqa: BLE001 - a refused release is material, not a crash
                refused.append(f"{model_id}: {type(exc).__name__}: {exc}")
        deadline = started + self.timeout_seconds
        while True:
            try:
                state = dict(self.probe.read())
            except Exception as exc:  # noqa: BLE001 - an unreadable end state must not look stopped
                state = {"error": f"{type(exc).__name__}: {exc}"}
            if _all_stopped(state) or self.clock() >= deadline:
                break
            self.wait(self.poll_seconds)
        return {"released": released, "refused": refused, "waited_seconds": round(self.clock() - started, 3),
                "stopped": _all_stopped(state), "final_state": state}


def _get_json(base_url: str, path: str, *, timeout: float) -> dict:
    try:
        response = httpx.get(base_url.rstrip("/") + path, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        raise PortError(f"cannot read {path}: {type(exc).__name__}: {exc}") from exc
    try:
        document = response.json()
    except ValueError as exc:
        raise PortError(f"{path} did not answer JSON") from exc
    if not isinstance(document, dict):
        raise PortError(f"{path} did not answer an object")
    return document


def _run(argv: Sequence[str], *, timeout: float = DOCKER_TIMEOUT_SECONDS) -> tuple[int, str]:
    try:
        result = subprocess.run(list(argv), capture_output=True, text=True, check=False, timeout=timeout)  # noqa: S603
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PortError(f"{argv[0]} is not usable: {exc}") from exc
    return result.returncode, result.stdout


def _managed_container_count(deployment_id: str) -> int | None:
    """How many containers of this deployment exist at all (running or not)."""
    try:
        code, stdout = _run(["docker", "ps", "--all", "--quiet", "--no-trunc",
                             "--filter", f"label={DEPLOYMENT_LABEL}={deployment_id}"])
    except PortError:
        return None
    if code != 0:
        return None
    return len([line for line in stdout.splitlines() if line.strip()])


def _oom_kills() -> int | None:
    try:
        text = Path("/proc/vmstat").read_text(encoding="utf-8")
    except OSError:
        return None
    found = re.search(r"^oom_kill\s+(\d+)$", text, re.MULTILINE)
    return int(found.group(1)) if found else None


def _count_500(log_path: Path | None) -> int | None:
    """Unexpected 500s in the service's own access log; no log, no claim."""
    if log_path is None:
        return None
    try:
        text = Path(log_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return sum(1 for line in text.splitlines() if re.search(r'"\s*500\s', line))


def _unsafe_evictions(models: Any) -> int | None:
    """Models left in an error state by a stop/eviction: an unsafe eviction would show here."""
    if not isinstance(models, Mapping):
        return None
    markers = ("stop_unverified", "evict", "cleanup")
    return sum(1 for model in models.values() if isinstance(model, Mapping)
               and isinstance(model.get("last_error"), str)
               and any(marker in model["last_error"] for marker in markers))


# ---------------------------------------------------------------------------
# O02: the storage faults stay inside a private mount namespace


class SubprocessProbe:
    """`unshare --user --map-root-user --mount` running `o02_probe` over pipes."""

    def __init__(self, *, config_path: Path, filesystem: str) -> None:
        self._process = subprocess.Popen(  # noqa: S603 - a fixed argv, no shell
            ["unshare", "--user", "--map-root-user", "--mount", "--propagation", "private",
             sys.executable, "-m", "model_scheduler.acceptance.o02_probe",
             "--config", str(config_path), "--filesystem", filesystem, "--parent-pid", str(os.getpid())],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def send(self, command: Mapping[str, Any]) -> Mapping[str, Any]:
        stdin, stdout = self._process.stdin, self._process.stdout
        if self._process.poll() is not None:
            raise PortError(f"the namespace probe exited with {self._process.returncode}: {self._stderr()}")
        if stdin is None or stdout is None:
            raise PortError("the namespace probe pipes are not open")
        stdin.write(json.dumps(dict(command)) + "\n")
        stdin.flush()
        line = stdout.readline()
        if not line.strip():
            raise PortError(f"the namespace probe answered nothing: {self._stderr()}")
        document = json.loads(line)
        if not isinstance(document, dict):
            raise PortError("the namespace probe answered a non-object")
        return document

    def close(self) -> None:
        try:
            if self._process.poll() is None:
                self.send({"action": "teardown"})
                self.send({"action": "quit"})
        except (PortError, ValueError):
            pass
        finally:
            self._process.terminate()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()

    def _stderr(self) -> str:
        stream = self._process.stderr
        text = stream.read() if stream is not None else ""
        return (text or "").strip()[:200]


@dataclass
class NamespaceDiskFaultPort:
    """O02's storage faults, executed inside a private mount namespace.

    The namespace comes from the launcher (`unshare --user --map-root-user
    --mount`), so the model disk is *shadowed* rather than unmounted and every
    scratch write stays on a dedicated tmpfs. When the namespace cannot be
    created at all, the first step reports itself unavailable and the case
    becomes `not_run` — never a pass.
    """

    config_path: Path
    filesystem: str
    probe_factory: Callable[[], Any] | None = None
    _probe: Any = field(default=None, init=False, repr=False)

    def _handle(self) -> Any:
        if self._probe is None:
            if self.probe_factory is not None:
                self._probe = self.probe_factory()
            else:
                self._probe = SubprocessProbe(config_path=Path(self.config_path),
                                              filesystem=self.filesystem)
        return self._probe

    def isolate_mount_namespace(self, deployment_id: str) -> Mapping[str, Any]:
        answer = self._answer("isolate", deployment_id=deployment_id)
        if "error" in answer:
            return {"available": False, "reason": answer["error"], "action": "isolate_mount_namespace"}
        return answer

    def fail_model_disk(self) -> Mapping[str, Any]:
        return self._step("fail_model_disk")

    def fill_scratch(self, quota_bytes: int) -> Mapping[str, Any]:
        return self._step("fill_scratch", quota_bytes=int(quota_bytes))

    def recover_model_disk(self) -> Mapping[str, Any]:
        return self._step("recover")

    def close(self) -> None:
        if self._probe is not None:
            self._probe.close()
            self._probe = None

    def _step(self, action: str, **payload: Any) -> Mapping[str, Any]:
        answer = self._answer(action, **payload)
        if "error" in answer:
            raise PortError(str(answer["error"]))
        return answer

    def _answer(self, action: str, **payload: Any) -> Mapping[str, Any]:
        answer = self._handle().send({"action": action, **payload})
        if not isinstance(answer, Mapping):
            raise PortError(f"the namespace probe answered a non-object for {action!r}: {answer!r}")
        return dict(answer)


# ---------------------------------------------------------------------------
# O03: the Docker channel fails, a stranger shows up, and a stop runs out of time


def _managed_prefix(deployment_id: str) -> str:
    return f"sms-{deployment_id}-"


def _foreign_containers(deployment_id: str, *, runner: Callable[..., tuple[int, str]] | None = None) -> list[str]:
    """Containers that are not this deployment's, so a fault can prove it left them alone."""
    execute = runner or _run
    code, stdout = execute(["docker", "ps", "--format", "{{.Names}}"], timeout=DOCKER_TIMEOUT_SECONDS)
    if code != 0:
        raise PortError("the container listing is not readable, so collateral damage cannot be ruled out")
    prefix = _managed_prefix(deployment_id)
    return sorted(name.strip() for name in stdout.splitlines()
                  if name.strip() and not name.strip().startswith(prefix))


def _http_status(base_url: str, path: str, *, timeout: float = 30.0) -> int:
    try:
        return httpx.get(base_url.rstrip("/") + path, timeout=timeout).status_code
    except Exception as exc:  # noqa: BLE001 - an unreachable surface is the fact, not a crash
        raise PortError(f"cannot read {path}: {type(exc).__name__}: {exc}") from exc


@dataclass
class DockerFaultPort:
    """O03's three faults against the live deployment, each undone in the same step.

    Every judgement is read from the machine or the service itself:

    * the channel goes down by stopping the Docker socket — containers already
      running are untouched, and the listing before and after is what proves it;
    * the stranger carries this deployment's label with a model nobody registered,
      which is exactly what the observer cannot attribute;
    * the stalled stop is a container under this deployment's own identity that
      ignores SIGTERM, so the deployment's stop has to run out of time.

    Nothing here assumes the outcome: if the service stays 200 or releases its
    budget, the case records that, and that is a finding and not a pass.
    """

    deployment_id: str
    base_url: str
    model_id: str
    image: str
    timeout_seconds: float = 180.0
    poll_seconds: float = 2.0
    runner: Callable[..., tuple[int, str]] | None = None
    post: Callable[..., Any] = field(default=httpx.post)
    clock: Callable[[], float] = field(default=time.monotonic)
    wait: Callable[[float], None] = field(default=time.sleep)

    def _run(self, argv: Sequence[str], *, timeout: float | None = None) -> tuple[int, str]:
        return (self.runner or _run)(list(argv), timeout=timeout or self.timeout_seconds)

    def _docker_works(self) -> bool:
        code, _stdout = self._run(["docker", "ps"], timeout=30.0)
        return code == 0

    def _listed(self) -> list[str]:
        code, stdout = self._run(["docker", "ps", "--format", "{{.Names}}"], timeout=30.0)
        if code != 0:
            return []
        return [line.strip() for line in stdout.splitlines() if line.strip()]

    def _status(self) -> dict:
        return _get_json(self.base_url, "/api/status", timeout=30.0)

    def _model(self) -> Mapping[str, Any]:
        models = self._status().get("models")
        if not isinstance(models, Mapping) or self.model_id not in models:
            raise PortError(f"the service does not report model {self.model_id!r}")
        entry = models[self.model_id]
        return entry if isinstance(entry, Mapping) else {}

    def _budget_kept(self, model: Mapping[str, Any]) -> bool:
        """A stop that never proved leaves the booking in place; only a proven stop clears it."""
        return model.get("state") != "unloaded"

    def _warm(self) -> bool:
        """Load the model through the deployment's own surface: with nothing loaded there is no budget to keep."""
        try:
            self.post(self.base_url.rstrip("/") + "/v1/chat/completions",
                      json={"model": self.model_id,
                            "messages": [{"role": "user", "content": "Reply with one word."}],
                            "max_tokens": 8},
                      timeout=self.timeout_seconds)
        except Exception:  # noqa: BLE001 - a refused request is the fact, not a crash
            return False
        deadline = self.clock() + self.timeout_seconds
        while self.clock() < deadline:
            try:
                if self._model().get("state") != "unloaded":
                    return True
            except PortError:
                return False
            self.wait(self.poll_seconds)
        return False

    def make_docker_unreachable(self) -> Mapping[str, Any]:
        before = _foreign_containers(self.deployment_id, runner=self.runner)
        baseline = _http_status(self.base_url, "/health")  # what the service said before anything was staged
        warmed = True
        if self._model().get("state") == "unloaded":
            warmed = self._warm()  # nothing loaded means nothing whose budget could be kept
        code, stdout = self._run(["sudo", "systemctl", "stop", "docker.socket"], timeout=60.0)
        if code != 0:
            raise PortError(f"the Docker socket could not be stopped: {stdout.strip()}")
        unreachable = not self._docker_works()
        health = _http_status(self.base_url, "/health")
        model = self._model()
        restore_code, restore_output = self._run(["sudo", "systemctl", "start", "docker.socket"], timeout=60.0)
        if restore_code != 0:
            raise PortError(f"the Docker socket could not be started again: {restore_output.strip()}")
        restored = self._docker_works()
        # Only with the channel back can the neighbours be compared: an unreadable
        # listing is not evidence that they survived.
        after = _foreign_containers(self.deployment_id, runner=self.runner)
        return {"available": True, "warmed": warmed, "docker_unreachable": unreachable,
                "docker_restored": restored, "health_baseline": baseline,
                "health_status": health, "budget_kept": self._budget_kept(model),
                "other_containers_untouched": before == after, "model_state": model.get("state"),
                "model_last_error": model.get("last_error"),
                "readiness_reason": self._status().get("readiness_reason"),
                "foreign_containers": after}

    def present_unknown_instance(self) -> Mapping[str, Any]:
        name = f"{_managed_prefix(self.deployment_id)}ghost"
        before = _foreign_containers(self.deployment_id, runner=self.runner)
        baseline = _http_status(self.base_url, "/health")
        code, stdout = self._run(["docker", "run", "-d", "--name", name,
                                  "--label", f"{DEPLOYMENT_LABEL}={self.deployment_id}",
                                  "--label", "io.self-model-switch.model=ghost",
                                  "--entrypoint", "sleep", self.image, "600"])
        if code != 0:
            raise PortError(f"the stranger container could not be started: {stdout.strip()}")
        try:
            health = _http_status(self.base_url, "/health")
            status = self._status()
            after = _foreign_containers(self.deployment_id, runner=self.runner)
            # It runs under this deployment's label, and the service still does not
            # serve it: an instance the deployment cannot own, not one it adopted.
            stranger_running = name in self._listed()
            unknown_recorded = stranger_running and "ghost" not in (status.get("models") or {})
            return {"available": True, "unknown_recorded": unknown_recorded, "health_status": health,
                    "health_baseline": baseline,
                    "other_containers_untouched": before == after, "stranger": name,
                    "stranger_running": stranger_running,
                    "readiness_reason": status.get("readiness_reason"), "foreign_containers": after}
        finally:
            self._run(["docker", "rm", "-f", name], timeout=60.0)

    def stall_stop(self, model_id: str) -> Mapping[str, Any]:
        name = f"{_managed_prefix(self.deployment_id)}{model_id}"
        self._run(["docker", "rm", "-f", name], timeout=60.0)
        stall_note = ""
        code, stdout = self._run(["docker", "run", "-d", "--name", name,
                                  "--label", f"{DEPLOYMENT_LABEL}={self.deployment_id}",
                                  "--label", f"io.self-model-switch.model={model_id}",
                                  "--entrypoint", "sh", self.image, "-c", "trap '' TERM; sleep 600"])
        if code != 0:
            raise PortError(f"the stalled container could not be started: {stdout.strip()}")
        try:
            try:
                answer = self.post(self.base_url.rstrip("/") + f"/api/models/{model_id}/unload", timeout=60.0)
                unload_code = int(getattr(answer, "status_code", 0))
            except Exception as exc:  # noqa: BLE001 - a refused unload is the fact being measured
                unload_code = -1
                stall_note = f"{type(exc).__name__}: {exc}"
            deadline = self.clock() + self.timeout_seconds
            model: Mapping[str, Any] = {}
            while True:
                model = self._model()
                if str(model.get("last_error") or "").startswith("stop_unverified"):
                    break
                if model.get("admission_blocked") is True or self.clock() >= deadline:
                    break
                self.wait(self.poll_seconds)
            health = _http_status(self.base_url, "/health")
            return {"available": True, "stop_timeout_recorded": model.get("last_error") == "stop_unverified",
                    "budget_kept": self._budget_kept(model), "health_status": health, "unload_status": unload_code,
                    "model_state": model.get("state"), "model_last_error": model.get("last_error"),
                    "stalled_container": name, "note": stall_note or None}
        finally:
            self._run(["docker", "rm", "-f", name], timeout=60.0)


# ---------------------------------------------------------------------------
# O05: the P26 preflight primitive plus the tamper scenarios


@dataclass
class PreflightPrimitivePort:
    """`verify_environment` over the live site; nothing is loaded, no gate is invoked."""

    site: Mapping[str, Any]
    tamper: Sequence[Mapping[str, Any]] = ()

    def preflight(self, candidate: Any, evidence: Any) -> Mapping[str, Any]:
        try:
            manifest = self._manifest(candidate, evidence)
        except (ContractError, PreflightError, KeyError, TypeError, ValueError) as exc:
            return {"accepted": False, "reason": f"{type(exc).__name__}: {exc}", "loaded": 0,
                    "production_gate": False}
        try:
            result = verify_environment(manifest, site=self.site)
        except PreflightError as exc:
            return {"accepted": False, "reason": str(exc), "loaded": 0, "production_gate": False}
        problems = list(result["problems"])
        return {"accepted": bool(result["ok"]), "reason": problems[0] if problems else "",
                "loaded": result["loaded_models"], "production_gate": False}

    def tamper_scenarios(self) -> Sequence[Mapping[str, Any]]:
        return list(self.tamper)

    def _manifest(self, candidate: Any, evidence: Any) -> dict:
        parsed = parse_candidate(candidate)  # a tampered candidate cannot even be parsed
        started = None
        if isinstance(evidence, Mapping):
            started = evidence.get("started_at")
        moment = started if isinstance(started, str) and started else _utc_now()
        device = asdict(parsed.device)
        manifest = {
            "schema_version": 3, "mode": "production", "deployment_id": parsed.deployment_id,
            "candidate_sha256": candidate_digest(parsed),
            "source_archive_sha256": parsed.source_archive_sha256,
            "config_sha256": parsed.config_sha256,
            "device_digest": device_digest(parsed.device),
            # The candidate's own device facts: layer 1 compares them with the live
            # site, so editing the registration must be refused, not papered over.
            "device": {field: device[field] for field in SITE_DEVICE_FIELDS},
            "model_filesystem": self.site["filesystem"],
            "models": {model.model_id: {
                "image_digest": _runtime_image(parsed, model.runtime_id),
                "container_name": f"sms-{parsed.deployment_id}-{model.model_id}",
                "assets": [{"path": asset.path, "size_bytes": asset.size_bytes, "sha256": asset.sha256}
                           for asset in model.assets]} for model in parsed.models},
            "evidence": {"started_at": moment},
        }
        manifest["identity_sha256"] = manifest_identity(manifest)
        return manifest


def _runtime_image(candidate: Any, runtime_id: str) -> str:
    for runtime in candidate.runtimes:
        if runtime.runtime_id == runtime_id:
            return runtime.image_digest
    raise PortError(f"model names an unregistered runtime {runtime_id!r}")


def read_site_facts(*, candidate: Any, config_path: Path, model_directory: Path,
                    source_archive_sha256: str, scratch_path: Path | None = None) -> dict:
    """The live site facts layer 1 compares against; every value is read on site."""
    device = read_device_facts(config_path=config_path, model_directory=model_directory, scratch_path=scratch_path)
    files: dict[str, dict] = {}
    for model in candidate.models:
        for asset in model.assets:
            path = Path(model_directory) / asset.path
            try:
                payload = path.read_bytes()
            except OSError as exc:
                raise PortError(f"the registered asset {asset.path} is not readable: {exc}") from exc
            files[asset.path] = {"size_bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
    images = {}
    for runtime in candidate.runtimes:
        images[runtime.image_digest] = image_is_present(runtime.image_digest)
    return {**device, "filesystem": _filesystem_of(model_directory),
            "images": images, "model_files": files,
            "config_sha256": _config_digest(Path(config_path)),
            "source_archive_sha256": source_archive_sha256}


def _config_digest(config_path: Path) -> str:
    """The deployment's own loader and digest, so the value matches the candidate's.

    `build_candidate` digests `config_module._yaml(path)` with
    `config_module.config_digest`; reading the file any other way would produce a
    second, differently-normalised value and refuse a correct deployment.
    """
    import model_scheduler.config as config_module

    return config_module.config_digest(config_module._yaml(Path(config_path)))


def read_device_facts(*, config_path: Path, model_directory: Path, scratch_path: Path | None = None) -> dict:
    """The C09 device fields, read with the same collector the candidate was frozen with.

    The preflight compares the registration's device digests byte for byte, so
    the live values have to come from the *same* collection code (`acceptance
    collect`) rather than a second implementation of the same idea.
    """
    from .collect import FactsError, collect_facts

    scratch = Path(scratch_path) if scratch_path is not None else Path(config_path).parent
    try:
        facts = collect_facts(model_disk=str(model_directory), scratch_disk=str(scratch))
    except FactsError as exc:
        raise PortError(f"the live site facts cannot be collected: {exc}") from exc
    return dict(facts.document()["device"])


def _filesystem_of(path: Path) -> str:
    code, stdout = _run(["findmnt", "-no", "FSTYPE", "--target", str(path)])
    value = stdout.strip().splitlines()[0].strip() if stdout.strip() else ""
    if code != 0 or not value:
        raise PortError(f"no filesystem type is readable for {path}")
    return value


def image_is_present(image_digest: str) -> bool:
    """Whether the pinned digest really exists locally.

    The lookup is by image id, not by the registered reference: a site image that
    was built locally carries no repository digest, so `name@sha256:…` cannot be
    inspected even though the bytes are right there.
    """
    recorded = image_digest.split("sha256:", 1)[1] if "sha256:" in image_digest else ""
    if not recorded:
        return False
    code, stdout = _run(["docker", "images", "--no-trunc", "--quiet"])
    if code != 0:
        return False
    wanted = f"sha256:{recorded}"
    return any(line.strip() == wanted for line in stdout.splitlines() if line.strip())


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_boot_id(base_url: str, *, timeout: float = 30.0) -> str | None:
    """The live deployment's boot id, when the compat surface answers."""
    try:
        document = _get_json(base_url, "/api/status", timeout=timeout)
    except PortError:
        return None
    boot = document.get("boot_id")
    return boot if isinstance(boot, str) and boot else None


def tamper_scenarios(candidate_document: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The O05 scenarios: each edit must be refused before any load."""
    scenarios: list[dict[str, Any]] = []

    def edited(mutate) -> dict:
        document = json.loads(json.dumps(candidate_document))
        mutate(document)
        return document

    def _asset(document: dict) -> None:
        document["models"][0]["assets"][0]["sha256"] = "0" * 64

    def _device(document: dict) -> None:
        document["device"]["mem_total_bytes"] = int(document["device"]["mem_total_bytes"]) + 1

    def _image(document: dict) -> None:
        document["runtimes"][0]["image_digest"] = "sms-llama-cpp@sha256:" + "1" * 64

    scenarios.append({"name": "asset hash", "candidate": edited(_asset), "evidence": {}})
    scenarios.append({"name": "device change", "candidate": edited(_device), "evidence": {}})
    scenarios.append({"name": "image digest", "candidate": edited(_image), "evidence": {}})
    scenarios.append({"name": "expired evidence", "candidate": json.loads(json.dumps(candidate_document)),
                      "evidence": {"started_at": "2020-01-01T00:00:00+00:00"}})
    return scenarios


# ---------------------------------------------------------------------------
# ports that are not wired yet


class NotWiredPort:
    """A port that is not wired: every action reports itself unavailable.

    The orchestrator maps `{"available": False}` to `not_run`, so an unwired
    port can never turn into a passed case.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def __getattr__(self, name: str) -> Callable[..., dict]:
        def unavailable(*_args: Any, **_kwargs: Any) -> dict:
            return {"available": False, "reason": self.reason, "action": name}

        return unavailable
