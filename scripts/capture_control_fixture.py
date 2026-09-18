#!/usr/bin/env python3
"""Controlled llama-swap control-protocol probe (M02/P06a).

The tool exists because llama-swap does not publish a stable control contract:
`CONTROL_CONTRACT` must be derived from what the pinned release actually
answers. It therefore

* reads a schema-v2 deployment, renders each registered model's lab argv with
  the P06 profile renderer, and writes a probe llama-swap configuration plus a
  manifest into one exclusive output directory before anything is started;
* only ever talks to loopback endpoints built from that configuration and
  refuses a non-loopback listen address, proxy URL or model port;
* probes the real control surface of the pinned release (`/health`, `/running`,
  the `/upstream/<model>/...` load trigger, `POST /api/models/unload/<model>`),
  records every request with status, content type, body and body hash, and keeps
  the raw material even when the probe fails;
* records a negative sample for the previously guessed load path instead of
  counting a 404 as success;
* never downloads assets, never writes a system install directory and never
  starts the production scheduler.

Exit codes follow the plan's CLI contract: 0 captured, 2 input/material error,
3 probe executed but failed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from model_scheduler.contracts_v2 import ContractError, DeploymentSpec, parse_deployment  # noqa: E402
from model_scheduler.control_protocol_v1 import Fence  # noqa: E402
from model_scheduler.model_runner import RunnerError, SupervisedLaunch  # noqa: E402
from model_scheduler.runtime_profiles import LaunchRenderError, render_container_launch  # noqa: E402

CAPTURE_SCHEMA = 1
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
# Pinned release, verified by tests/test_llama_swap_fixture.py against the shipped asset.
PINNED_RELEASE = {
    "version": "v217",
    "asset": "llama-swap_217_linux_arm64.tar.gz",
    "sha256": "36c58cf69f1422e999acba0b7bff0d47d5b955cb95e8d3875c461d814a74cc29",
}
# The load path the project previously guessed; captured as a negative sample only.
REJECTED_LOAD_PROBE = "/props?model={model_id}"
LOAD_TRIGGER = "/upstream/{model_id}/health"
UNLOAD_PATH = "/api/models/unload/{model_id}"
RUNNING_PATH = "/running"
HEALTH_PATH = "/health"
DEFAULT_LLAMA_SWAP = "/opt/self-model-switch/bin/llama-swap"
PROBE_CONFIG_NAME = "llama-swap.probe.yaml"
CAPTURE_NAME = "capture.json"
MANIFEST_NAME = "manifest.json"
SERVER_LOG_NAME = "llama-swap.log"


class CaptureError(RuntimeError):
    """The probe ran but could not produce trustworthy material (exit 3)."""


class CaptureInputError(CaptureError):
    """The inputs or the output directory are unusable (exit 2)."""


def probe_http_result(method: str, path: str, status: int, content_type: str | None, body: bytes) -> dict:
    """Record one raw answer; never rewrite it into a verdict."""
    try:
        body_utf8: str | None = body.decode("utf-8")
    except UnicodeDecodeError:
        body_utf8 = None
    return {
        "request": {"method": method, "path": path},
        "status": status,
        "content_type": content_type,
        "body_utf8": body_utf8,
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "counts_as_control_response": 200 <= status < 300,
    }


def probe_http(base_url: str, method: str, path: str, *, timeout: float) -> dict:
    """One loopback request that never follows a redirect and never retries."""
    request = urllib.request.Request(f"{base_url}{path}", method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - loopback only
            body = response.read()
            return probe_http_result(method, path, response.status, response.headers.get("content-type"), body)
    except urllib.error.HTTPError as exc:
        body = exc.read()
        return probe_http_result(method, path, exc.code, exc.headers.get("content-type") if exc.headers else None, body)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise CaptureError(f"control request {method} {path} failed: {exc}") from exc


def load_config_document(path: Path) -> dict:
    """Read the schema-v2 configuration: JSON always, YAML when PyYAML is present."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CaptureInputError(f"cannot read configuration {path}: {exc}") from exc
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - exercised only without PyYAML
            raise CaptureError(
                "the probe needs a JSON schema-v2 configuration, or PyYAML for a YAML one"
            ) from exc
        try:
            document = yaml.safe_load(text)
        except Exception as exc:  # noqa: BLE001 - PyYAML raises several unrelated types
            raise CaptureInputError(f"invalid YAML configuration: {exc}") from exc
    if not isinstance(document, dict):
        raise CaptureInputError("the configuration root must be an object")
    return document


def probe_deployment(config: Mapping[str, Any]) -> DeploymentSpec:
    version = config.get("schema_version")
    registration = config.get("registration")
    if version != 2 or not isinstance(registration, Mapping):
        raise CaptureInputError("the probe requires a schema_version=2 configuration with a registration")
    try:
        return parse_deployment({"schema_version": version, **registration}, "registration")
    except ContractError as exc:
        raise CaptureInputError(f"invalid registration: {exc}") from exc


def build_probe_files(
    config: Mapping[str, Any],
    *,
    llama_swap_port: int,
    deployment_id: str,
    model_directory: str,
    config_sha256: str,
    container_runtime: str,
    listen_host: str = "127.0.0.1",
    http_base_url: str | None = None,
    model_ports: Mapping[str, int] | None = None,
) -> dict:
    """Render the probe llama-swap configuration and its manifest, or refuse."""
    if listen_host not in LOOPBACK_HOSTS:
        raise CaptureInputError(f"the probe must listen on loopback, not {listen_host!r}")
    if not isinstance(llama_swap_port, int) or isinstance(llama_swap_port, bool) or not 1 <= llama_swap_port <= 65535:
        raise CaptureInputError("the probe port must be a valid TCP port")
    base_url = http_base_url or f"http://{listen_host}:{llama_swap_port}"
    base_host = urllib.parse.urlsplit(base_url).hostname
    if base_host not in LOOPBACK_HOSTS:
        raise CaptureInputError(f"the control base URL must be loopback, not {base_url!r}")
    deployment = probe_deployment(config)
    if not deployment.models:
        raise CaptureInputError("the registration declares no model to probe")
    model = deployment.models[0]
    overrides = dict(model_ports or {})
    for model_id, port in overrides.items():
        if model_id not in {item.model_id for item in deployment.models}:
            raise CaptureInputError(f"model_ports references unknown model {model_id!r}")
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise CaptureInputError(f"model port for {model_id!r} must be a valid TCP port")
    registered_port = overrides.get(model.model_id, model.port)
    try:
        launch = render_container_launch(
            deployment,
            model.model_id,
            deployment_id=deployment_id,
            model_directory=model_directory,
            config_sha256=config_sha256,
            mode="lab",
            container_runtime=container_runtime,
            temporary_budget_bytes=model.reserved_bytes,
        )
    except (LaunchRenderError, ContractError) as exc:
        raise CaptureInputError(f"cannot render the probe launch: {exc}") from exc
    # llama-swap assigns the published port itself through ${PORT}; the lab
    # manifest keeps the registered port as the deployment input.
    rendered_argv = list(launch.argv)
    probe_argv = [token.replace(f"127.0.0.1:{registered_port}:", "127.0.0.1:${PORT}:") for token in rendered_argv]
    if not any("${PORT}" in token for token in probe_argv):
        raise CaptureInputError("the rendered argv must publish the model port llama-swap assigns")
    command = " ".join(shlex.quote(token) for token in probe_argv)
    # v217 takes its listen address from the command line, not from a config key,
    # and rejects the deprecated logRequests setting (measured 2026-09-18).
    llama_swap_config = {
        "healthCheckTimeout": 900,
        "logLevel": "info",
        "models": {model.model_id: {"cmd": command}},
    }
    manifest = {
        "schema": CAPTURE_SCHEMA,
        "lab_only": True,
        "deployment_id": deployment_id,
        "image": launch.image_digest,
        "runtime_id": launch.runtime_id,
        "profile_id": launch.profile_id,
        "container_runtime": launch.container_runtime,
        "model_directory": model_directory,
        "config_sha256": config_sha256,
        "models": {
            model.model_id: {
                "registered_port": registered_port,
                "argv": rendered_argv,
                "argv_sha256": hashlib.sha256("\x00".join(rendered_argv).encode("utf-8")).hexdigest(),
                "probe_argv": probe_argv,
                "probe_port_variable": "${PORT}",
                "assets": [{"role": asset.role, "path": asset.path, "sha256": asset.sha256} for asset in model.assets],
                "capabilities": list(model.capabilities),
                "envelope": {
                    "ctx_size": model.envelope.ctx_size,
                    "max_parallel": model.envelope.max_parallel,
                    "max_image_tokens": model.envelope.max_image_tokens,
                },
            }
        },
    }
    return {
        "model_id": model.model_id,
        "base_url": base_url,
        "listen": f"{listen_host}:{llama_swap_port}",
        "llama_swap_config": llama_swap_config,
        "manifest": manifest,
    }


class ProbeProcess:
    """One supervised llama-swap launch whose state stays observable (P06)."""

    def __init__(self, argv: list[str], cwd: Path, log_path: Path, fence: Fence) -> None:
        self._argv = argv
        self._cwd = cwd
        self._log_path = log_path
        self._fence = fence
        self._launch: SupervisedLaunch | None = None
        self._log = None

    def start(self) -> Any:
        self._log = self._log_path.open("wb")
        self._launch = SupervisedLaunch(self._argv, self._fence, popen=self._popen)
        return self._launch.start()

    def _popen(self, argv: list[str], *, start_new_session: bool) -> Any:
        import subprocess

        return subprocess.Popen(argv, cwd=str(self._cwd), stdout=self._log, stderr=subprocess.STDOUT, start_new_session=start_new_session)

    @property
    def operation(self) -> Any:
        return self._launch.operation if self._launch is not None else None

    def stop(self, timeout: float) -> int | None:
        if self._launch is None:
            return None
        try:
            self._launch.terminate()
        except RunnerError:
            pass
        exit_code = self._launch.wait(timeout=timeout)
        if exit_code is None:
            self._launch.signal_group(9)
            exit_code = self._launch.wait(timeout=timeout)
        if self._log is not None:
            self._log.close()
            self._log = None
        return exit_code


def capture(
    *,
    config: Mapping[str, Any],
    output_directory: Path,
    llama_swap_path: str,
    llama_swap_port: int,
    deployment_id: str,
    model_directory: str,
    config_sha256: str,
    container_runtime: str,
    http: Callable[..., dict],
    start_process: Callable[[list[str], Path], Any],
    stop_process: Callable[[], Any],
    port_in_use: Callable[[str, int], bool],
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    running_wait_seconds: float = 300.0,
    poll_seconds: float = 1.0,
) -> dict:
    """Run the probe sequence and keep every raw answer, success or failure."""
    output = Path(output_directory)
    if output.exists() and any(output.iterdir()):
        raise CaptureInputError(f"refusing to overwrite the non-empty output directory {output}")
    output.mkdir(parents=True, exist_ok=True)

    files = build_probe_files(
        config,
        llama_swap_port=llama_swap_port,
        deployment_id=deployment_id,
        model_directory=model_directory,
        config_sha256=config_sha256,
        container_runtime=container_runtime,
    )
    model_id = files["model_id"]
    probe_config_path = output / PROBE_CONFIG_NAME
    probe_config_path.write_text(json.dumps(files["llama_swap_config"], indent=2) + "\n", encoding="utf-8")
    (output / MANIFEST_NAME).write_text(json.dumps(files["manifest"], indent=2, sort_keys=True) + "\n", encoding="utf-8")

    record: dict = {
        "schema": CAPTURE_SCHEMA,
        "release": PINNED_RELEASE,
        "started_utc": _utc_now(),
        "model_id": model_id,
        "probe_config": files["llama_swap_config"],
        "manifest": files["manifest"],
        "exchanges": [],
        "negative_samples": [],
        "result": "failed",
        "error": None,
    }

    def write_record() -> None:
        (output / CAPTURE_NAME).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def exchange(step: str, method: str, path: str, *, counts: bool = True) -> dict:
        answer = http(files["base_url"], method, path, timeout=30.0)
        entry = {"step": step, **answer}
        if counts:
            record["exchanges"].append(entry)
        else:
            record["negative_samples"].append({**entry, "counts_as_success": False})
        write_record()
        return answer

    def running_models() -> list[str]:
        answer = http(files["base_url"], "GET", RUNNING_PATH, timeout=30.0)
        if not answer["counts_as_control_response"] or answer["body_utf8"] is None:
            raise CaptureError(f"unusable /running answer: status={answer['status']} body={answer['body_sha256']}")
        try:
            payload = json.loads(answer["body_utf8"])
        except json.JSONDecodeError as exc:
            raise CaptureError(f"/running did not return JSON: {exc}") from exc
        models = payload.get("running") if isinstance(payload, dict) else None
        if not isinstance(models, list) or any(not isinstance(item, str) for item in models):
            raise CaptureError(f"/running did not return a running list: {payload!r}")
        return models

    try:
        start_process(
            [llama_swap_path, "--listen", files["listen"], "--config", str(probe_config_path)], output
        )
        health = exchange("health", "GET", HEALTH_PATH)
        if not health["counts_as_control_response"]:
            raise CaptureError(f"llama-swap is not healthy on its loopback port: status={health['status']}")
        answer = exchange("running_empty", "GET", RUNNING_PATH)
        if "running" not in (answer["body_utf8"] or ""):
            raise CaptureError(f"unusable empty /running answer: {answer['status']}")

        exchange("load_trigger", "GET", LOAD_TRIGGER.format(model_id=model_id))
        load_deadline = clock() + running_wait_seconds
        loaded: list[str] = []
        while True:
            loaded = running_models()
            if model_id in loaded:
                break
            if clock() >= load_deadline:
                raise CaptureError(f"model {model_id!r} did not become running; /running={loaded}")
            sleep(poll_seconds)
        record["exchanges"].append({"step": "running_loaded", "running": loaded})
        write_record()

        exchange("unload", "POST", UNLOAD_PATH.format(model_id=model_id))
        unloaded_deadline = clock() + running_wait_seconds
        remaining: list[str] = [model_id]
        while True:
            remaining = running_models()
            if model_id not in remaining:
                break
            if clock() >= unloaded_deadline:
                raise CaptureError(f"model {model_id!r} stayed running after unload; /running={remaining}")
            sleep(poll_seconds)
        record["exchanges"].append({"step": "running_empty_after_unload", "running": remaining})
        write_record()

        exchange("rejected_load_probe", "GET", REJECTED_LOAD_PROBE.format(model_id=model_id), counts=False)
        record["result"] = "captured"
    except CaptureError as exc:
        record["error"] = str(exc)
        write_record()
        raise
    finally:
        exit_code = stop_process()
        host = urllib.parse.urlsplit(files["base_url"]).hostname or "127.0.0.1"
        record["stop"] = {
            "exit_code": exit_code,
            "port_in_use": port_in_use(host, llama_swap_port),
            "stopped_utc": _utc_now(),
        }
        record["finished_utc"] = _utc_now()
        write_record()

    return record


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", required=True, type=Path, help="schema-v2 deployment configuration (JSON, or YAML with PyYAML)")
    parser.add_argument("--output", required=True, type=Path, help="exclusive evidence directory for this probe")
    parser.add_argument("--llama-swap", default=DEFAULT_LLAMA_SWAP, dest="llama_swap_path")
    parser.add_argument("--llama-swap-port", type=int, default=18080, dest="llama_swap_port")
    parser.add_argument("--deployment-id", default="lab-orin", dest="deployment_id")
    parser.add_argument("--model-directory", required=True, dest="model_directory")
    parser.add_argument("--container-runtime", default="nvidia", dest="container_runtime")
    parser.add_argument("--load-timeout-seconds", type=float, default=900.0, dest="load_timeout_seconds")
    parser.add_argument("--stop-timeout-seconds", type=float, default=60.0, dest="stop_timeout_seconds")
    args = parser.parse_args(argv)

    try:
        document = load_config_document(args.config)
        config_sha256 = hashlib.sha256(args.config.read_bytes()).hexdigest()
        holder: dict[str, ProbeProcess] = {}

        def start_process(argv_list: list[str], cwd: Path) -> Any:
            process = ProbeProcess(argv_list, cwd, cwd / SERVER_LOG_NAME, Fence("probe-boot", "probe", 1, "probe-op-1", None, None))
            holder["process"] = process
            return process.start()

        def stop_process() -> Any:
            process = holder.get("process")
            return process.stop(args.stop_timeout_seconds) if process is not None else None

        record = capture(
            config=document,
            output_directory=args.output,
            llama_swap_path=args.llama_swap_path,
            llama_swap_port=args.llama_swap_port,
            deployment_id=args.deployment_id,
            model_directory=args.model_directory,
            config_sha256=config_sha256,
            container_runtime=args.container_runtime,
            http=probe_http,
            start_process=start_process,
            stop_process=stop_process,
            port_in_use=_port_in_use,
            sleep=time.sleep,
            clock=time.monotonic,
            running_wait_seconds=args.load_timeout_seconds,
        )
    except CaptureInputError as exc:
        print(f"capture input error: {exc}", file=sys.stderr)
        return 2
    except CaptureError as exc:
        print(f"capture error: {exc}", file=sys.stderr)
        return 3
    print(json.dumps({"result": record["result"], "output": str(args.output)}, sort_keys=True))
    return 0 if record["result"] == "captured" else 3


def _port_in_use(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
