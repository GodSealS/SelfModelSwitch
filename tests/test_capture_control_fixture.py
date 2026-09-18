"""Tests for the controlled llama-swap capture tool (P06a)."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "capture_control_fixture.py"


def _capture_module():
    spec = importlib.util.spec_from_file_location("capture_control_fixture", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _v2_config(port: int = 18081) -> dict:
    return {
        "schema_version": 2,
        "registration": {
            "runtimes": [
                {
                    "runtime_id": "llama-cpp-cuda-sm87-4bc272f",
                    "profile_id": "llama-cpp-gguf-v1",
                    "image_digest": "sms-llama-cpp@sha256:" + "8" * 64,
                    "adapter_sha256": "b" * 64,
                    "lock_sha256": "c" * 64,
                    "startup_args": [
                        "--load-mode",
                        "--parallel",
                        "--kv-unified-per-slot",
                        "--image-max-tokens",
                        "--n-gpu-layers",
                        "--flash-attn",
                        "--no-warmup",
                        "--no-webui",
                        "--host",
                        "--port",
                    ],
                }
            ],
            "models": [
                {
                    "model_id": "qwen25vl-7b-q4",
                    "runtime_id": "llama-cpp-cuda-sm87-4bc272f",
                    "capabilities": ["chat", "vision"],
                    "assets": [
                        {
                            "role": "model",
                            "path": "qwen25vl-7b-q4/model.gguf",
                            "sha256": "3f" + "0" * 62,
                            "size_bytes": 4683072320,
                        },
                        {
                            "role": "projector",
                            "path": "qwen25vl-7b-q4/mmproj.gguf",
                            "sha256": "d1" + "0" * 62,
                            "size_bytes": 1354162912,
                        },
                    ],
                    "port": port,
                    "envelope": {
                        "ctx_size": 32768,
                        "max_input_tokens": 28672,
                        "max_output_tokens": 4096,
                        "max_parallel": 2,
                        "max_image_tokens": 1280,
                        "max_image_edge_pixels": 1024,
                        "max_images": 1,
                    },
                    "timeout_seconds": 3600,
                    "reserved_bytes": 6106148045,
                    "measured": False,
                    "measurement_ref": None,
                    "physical_resident_peak_bytes": None,
                }
            ],
        },
        "server": {
            "host": "127.0.0.1",
            "port": 8000,
            "workers": 1,
            "max_request_body_bytes": 1048576,
            "body_timeout_seconds": 30,
            "shutdown_grace_seconds": 10,
        },
        "scheduler": {
            "poll_interval_seconds": 1,
            "request_queue_timeout_seconds": 30,
            "queue_capacity": 16,
            "priority_aging_seconds": 30,
            "switch_drain_timeout_seconds": 30,
            "switch_retry_seconds": 5,
            "resource_safety_margin": 0.15,
            "min_free_memory_bytes": 2147483648,
            "max_evictions_per_request": 1,
            "memory_reclaim_timeout_seconds": 10,
            "heat": {"half_life_seconds": 60, "request_weight": 1, "token_weight": 0},
            "thrash": {"switch_window_seconds": 60, "max_switches_in_window": 4, "cooldown_seconds": 30},
        },
        "resources": {
            "provider": "psutil",
            "system_reserve_bytes": 8589934592,
            "sample_interval_seconds": 1,
            "sample_max_age_seconds": 2,
            "model_budget_bytes": 16000000000,
        },
        "storage": {
            "mount_path": "/media/jtzn/sandisk-ext4",
            "model_directory": "/media/jtzn/sandisk-ext4/models",
            "expected_uuid": "0e0a0f2e-1111-2222-3333-444455556666",
            "filesystem": "ext4",
        },
        "gateway": {
            "connect_timeout_seconds": 5,
            "pool_timeout_seconds": 5,
            "read_idle_timeout_seconds": 60,
            "write_idle_timeout_seconds": 60,
            "inference_timeout_seconds": 900,
            "close_timeout_seconds": 5,
            "max_response_body_bytes": 1048576,
            "max_sse_event_bytes": 65536,
        },
        "control": {"allowed_uids": [1000]},
        "blobs": {"root": "/home/jtzn/self-model-switch-blobs"},
    }


def _capture_kwargs(tmp_path: Path, **overrides):
    kwargs = dict(
        config=_v2_config(),
        output_directory=tmp_path / "evidence",
        llama_swap_path="/opt/self-model-switch/bin/llama-swap",
        llama_swap_port=18080,
        deployment_id="lab-orin",
        model_directory="/media/jtzn/sandisk-ext4/models",
        config_sha256="d" * 64,
        container_runtime="nvidia",
        http=lambda *_args, **_kwargs: None,
        start_process=lambda argv, cwd: None,
        stop_process=lambda: 0,
        port_in_use=lambda host, port: False,
        sleep=lambda _seconds: None,
        clock=lambda: 0.0,
    )
    kwargs.update(overrides)
    return kwargs


def test_probe_files_bind_the_registered_model_to_the_rendered_argv() -> None:
    module = _capture_module()
    files = module.build_probe_files(
        _v2_config(),
        llama_swap_port=18080,
        deployment_id="lab-orin",
        model_directory="/media/jtzn/sandisk-ext4/models",
        config_sha256="d" * 64,
        container_runtime="nvidia",
    )

    assert files["model_id"] == "qwen25vl-7b-q4"
    assert files["llama_swap_config"]["port"] == 18080
    cmd = files["llama_swap_config"]["models"]["qwen25vl-7b-q4"]["cmd"]
    assert "--runtime=nvidia" in cmd
    assert "--model /models/qwen25vl-7b-q4/model.gguf" in cmd
    assert "--mmproj /models/qwen25vl-7b-q4/mmproj.gguf" in cmd
    # llama-swap assigns the published port through ${PORT}; the registered port
    # stays in the manifest as the deployment input.
    assert "${PORT}" in cmd
    assert "--publish" in cmd
    entry = files["manifest"]["models"]["qwen25vl-7b-q4"]
    assert "127.0.0.1:18081:8080" in entry["argv"]
    assert entry["probe_port_variable"] == "${PORT}"
    assert entry["registered_port"] == 18081
    assert files["manifest"]["image"].startswith("sms-llama-cpp@sha256:")
    assert entry["argv"][0] == "docker"
    assert files["manifest"]["lab_only"] is True


def test_probe_files_refuse_non_loopback_endpoints() -> None:
    module = _capture_module()
    with pytest.raises(module.CaptureError, match="loopback"):
        module.build_probe_files(
            _v2_config(),
            llama_swap_port=18080,
            deployment_id="lab-orin",
            model_directory="/models",
            config_sha256="d" * 64,
            container_runtime="nvidia",
            listen_host="0.0.0.0",
        )
    with pytest.raises(module.CaptureError, match="loopback"):
        module.build_probe_files(
            _v2_config(),
            llama_swap_port=18080,
            deployment_id="lab-orin",
            model_directory="/models",
            config_sha256="d" * 64,
            container_runtime="nvidia",
            http_base_url="http://192.168.1.10:18080",
        )


def test_capture_records_requests_answers_and_negative_samples(tmp_path: Path) -> None:
    module = _capture_module()
    seen: list[tuple[str, str]] = []
    running_answers = [b'{"running": []}', b'{"running": ["qwen25vl-7b-q4"]}', b'{"running": []}']

    def fake_http(base_url: str, method: str, path: str, *, timeout: float) -> dict:
        seen.append((method, path))
        assert base_url == "http://127.0.0.1:18080"
        if method == "GET" and path == "/health":
            return module.probe_http_result(method, path, 200, "text/plain", b"OK")
        if method == "GET" and path == "/running":
            return module.probe_http_result(method, path, 200, "application/json", running_answers.pop(0))
        if method == "GET" and path == "/upstream/qwen25vl-7b-q4/health":
            return module.probe_http_result(method, path, 200, "text/plain", b"OK")
        if method == "POST" and path == "/api/models/unload/qwen25vl-7b-q4":
            return module.probe_http_result(method, path, 200, "text/plain", b"OK")
        if method == "GET" and path == "/props?model=qwen25vl-7b-q4":
            return module.probe_http_result(method, path, 404, "application/json", b'{"error":"not found"}')
        raise AssertionError(f"unexpected probe {method} {path}")

    record = module.capture(**_capture_kwargs(tmp_path, http=fake_http))

    assert ("GET", "/running") in seen
    assert ("POST", "/api/models/unload/qwen25vl-7b-q4") in seen
    assert record["result"] == "captured"
    steps = [exchange["step"] for exchange in record["exchanges"]]
    assert steps[0] == "health"
    assert "running_empty" in steps
    assert "load_trigger" in steps
    assert "running_loaded" in steps
    assert "unload" in steps
    assert record["negative_samples"][0]["counts_as_success"] is False
    assert record["negative_samples"][0]["status"] == 404
    assert (tmp_path / "evidence" / "capture.json").is_file()
    assert (tmp_path / "evidence" / "llama-swap.probe.yaml").is_file()


def test_capture_refuses_a_non_empty_output_directory(tmp_path: Path) -> None:
    module = _capture_module()
    output = tmp_path / "evidence"
    output.mkdir()
    (output / "old.json").write_text("{}", encoding="utf-8")

    with pytest.raises(module.CaptureError, match="non-empty"):
        module.capture(**_capture_kwargs(tmp_path, output_directory=output))


def test_capture_keeps_material_when_the_probe_fails(tmp_path: Path) -> None:
    module = _capture_module()

    def failing_http(base_url: str, method: str, path: str, *, timeout: float) -> dict:
        raise module.CaptureError(f"connection refused: {path}")

    output = tmp_path / "evidence"
    with pytest.raises(module.CaptureError):
        module.capture(**_capture_kwargs(tmp_path, output_directory=output, http=failing_http))

    record = json.loads((output / "capture.json").read_text(encoding="utf-8"))
    assert record["result"] == "failed"
    assert record["error"]
    assert record["exchanges"] == []


def test_probe_http_result_marks_redirects_and_binary_bodies() -> None:
    module = _capture_module()
    redirect = module.probe_http_result("GET", "/running", 302, "text/html", b"<html>")
    assert redirect["status"] == 302
    assert redirect["counts_as_control_response"] is False

    binary = module.probe_http_result("GET", "/running", 200, "application/json", b"\xff\xfe")
    assert binary["body_utf8"] is None
    assert binary["body_sha256"] == hashlib.sha256(b"\xff\xfe").hexdigest()
    assert binary["counts_as_control_response"] is True
