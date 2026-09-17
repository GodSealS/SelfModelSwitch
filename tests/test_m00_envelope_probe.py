from __future__ import annotations

import functools
import importlib.util
import json
import math
from pathlib import Path
import sys
import zlib

import pytest


@functools.lru_cache(maxsize=1)
def _module():
    path = Path(__file__).resolve().parent.parent / "scripts" / "m00_envelope_probe.py"
    spec = importlib.util.spec_from_file_location("m00_envelope_probe", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _envelope(**overrides):
    module = _module()
    values = {
        "ctx_size": 32768,
        "max_input_tokens": 28672,
        "max_output_tokens": 4096,
        "parallel": 2,
        "image_max_tokens": 1280,
        "image_edge_pixels": 1024,
    }
    values.update(overrides)
    return module.Envelope(**values)


def _paths(**overrides):
    module = _module()
    values = {
        "model": "/media/jtzn/sandisk-ext4/models/qwen25vl-7b-q4/Qwen_Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf",
        "mmproj": "/media/jtzn/sandisk-ext4/models/qwen25vl-7b-q4/mmproj-Qwen_Qwen2.5-VL-7B-Instruct-bf16.gguf",
        "llama_server": "/opt/self-model-switch/probes/llama.cpp-4bc272fd729bd094c0422e4b8353da8d2fec91f8/llama-server",
        "evidence_root": "/home/jtzn/self-model-switch-evidence",
        "port": 18081,
        "host": "127.0.0.1",
    }
    values.update(overrides)
    return module.Paths(**values)


def _samples(entries):
    return [{"t": t, "available_bytes": available, "swap_free_bytes": 30 * 1024**3} for t, available in entries]


def test_default_candidate_envelope_is_valid():
    envelope = _envelope()
    envelope.validate()


def test_envelope_rejects_output_not_reserved_inside_context():
    with pytest.raises(ValueError):
        _envelope(max_input_tokens=30000).validate()


def test_envelope_rejects_invalid_parallel_and_image_budget():
    with pytest.raises(ValueError):
        _envelope(parallel=0).validate()
    with pytest.raises(ValueError):
        _envelope(image_max_tokens=0).validate()
    with pytest.raises(ValueError):
        _envelope(image_max_tokens=28672).validate()


def test_server_command_pins_slot_context_and_image_limit():
    command = _module().build_server_command(_envelope(), _paths())
    assert command[0].endswith("llama-server")
    assert "--ctx-size" not in command
    joined = list(zip(command, command[1:]))
    assert ("--parallel", "2") in joined
    assert ("--kv-unified-per-slot", "32768") in joined
    assert ("--image-max-tokens", "1280") in joined
    assert ("--n-gpu-layers", "99") in joined
    assert ("--flash-attn", "auto") in joined
    assert ("--host", "127.0.0.1") in joined
    assert ("--port", "18081") in joined
    assert "--no-warmup" in command
    assert _paths().model in command
    assert _paths().mmproj in command


def test_render_test_png_is_valid_and_deterministic():
    module = _module()
    data = module.render_test_png(1024, 1024, seed=7)
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert data[-8:-4] == b"IEND"
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    assert (width, height) == (1024, 1024)
    idat = b""
    offset = 8
    while offset < len(data):
        length = int.from_bytes(data[offset : offset + 4], "big")
        chunk_type = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        if chunk_type == b"IDAT":
            idat += payload
        offset += 12 + length
    raw = zlib.decompress(idat)
    assert len(raw) == 1024 * (1 + 1024 * 3)
    assert data == module.render_test_png(1024, 1024, seed=7)


def test_filler_text_is_space_separated_repeats():
    assert _module().filler_text(3) == " word word word"
    assert _module().filler_text(0) == ""


def test_completion_payload_carries_exact_token_array():
    module = _module()
    tokens = [1234] * 28672
    payload = module.build_completion_payload(tokens, n_predict=4096)
    assert payload["prompt"] == tokens
    assert len(payload["prompt"]) == 28672
    assert payload["n_predict"] == 4096
    assert payload["cache_prompt"] is False
    assert payload["stream"] is False
    assert payload["temperature"] == 0


def test_chat_payload_embeds_image_and_bounds_output():
    module = _module()
    payload = module.build_chat_payload(" word word", "data:image/png;base64,AAAA", max_tokens=4096)
    content = payload["messages"][0]["content"]
    assert [part["type"] for part in content] == ["text", "image_url"]
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert payload["max_tokens"] == 4096
    assert payload["stream"] is False


def test_memory_metrics_compute_baseline_and_delta():
    module = _module()
    gib = 1024**3
    samples = _samples(
        [
            (0.0, 50 * gib),
            (0.5, 50 * gib),
            (1.0, 50 * gib),
            (1.5, 50 * gib),
            (2.0, 49 * gib),
            (2.5, 40 * gib),
            (3.0, 30 * gib),
            (3.5, 30 * gib),
            (4.0, 31 * gib),
            (4.5, 45 * gib),
            (5.0, 50 * gib),
            (5.5, 50 * gib),
            (6.0, 50 * gib),
        ]
    )
    metrics = module.compute_memory_metrics(samples, launch_ts=2.0, end_ts=4.0)
    assert metrics["baseline_bytes"] == 50 * gib
    assert metrics["min_bytes"] == 30 * gib
    assert metrics["delta_bytes"] == 20 * gib
    assert metrics["gaps_over_500ms"] == 0
    assert metrics["measurement_valid"] is True


def test_memory_metrics_flag_gaps_shift_and_zero_delta():
    module = _module()
    gib = 1024**3
    gapped = _samples([(0.0, 50 * gib), (1.2, 40 * gib), (2.0, 40 * gib)])
    metrics = module.compute_memory_metrics(gapped, launch_ts=1.0, end_ts=1.9)
    assert metrics["gaps_over_500ms"] >= 1
    assert metrics["measurement_valid"] is False

    shifted = _samples(
        [
            (0.0, 50 * gib),
            (0.5, 50 * gib),
            (1.0, 50 * gib),
            (1.5, 40 * gib),
            (2.0, 20 * gib),
            (2.5, 20 * gib),
            (3.0, 20 * gib),
        ]
    )
    metrics = module.compute_memory_metrics(shifted, launch_ts=1.2, end_ts=2.6)
    assert metrics["baseline_shift_bytes"] > 256 * 1024**2
    assert metrics["measurement_valid"] is False

    flat = _samples([(0.0, 50 * gib), (0.5, 50 * gib), (1.0, 50 * gib), (1.5, 50 * gib)])
    metrics = module.compute_memory_metrics(flat, launch_ts=0.5, end_ts=1.2)
    assert metrics["delta_bytes"] == 0
    assert metrics["measurement_valid"] is False


def test_summarize_runs_reports_peak_and_reserved_budget():
    module = _module()
    gib = 1024**3
    runs = [
        {"cases": {}, "memory": {"delta_bytes": 12 * gib}, "stop": {"quiescent": True}},
        {"cases": {}, "memory": {"delta_bytes": 13 * gib}, "stop": {"quiescent": True}},
        {"cases": {}, "memory": {"delta_bytes": 10 * gib}, "stop": {"quiescent": False}},
    ]
    summary = module.summarize_runs(runs, concurrency=2)
    assert summary["runs_completed"] == 3
    assert summary["quiescent_runs"] == 2
    assert summary["all_quiescent"] is False
    assert summary["measured_peak_bytes"] == 13 * gib
    assert summary["reserved_r_bytes"] == math.ceil(13 * gib * 1.15)
    assert summary["concurrency"] == 2


def test_tegrastats_line_parser_extracts_ram_swap_and_gpu():
    module = _module()
    line = "09-17-2026 10:14:32 RAM 1587/61005MB lfb 281MB SWAP 0/30480MB CPU [5%@-] GR3D_FREQ 99% GR3D_EMC_FREQ 0%"
    parsed = module.parse_tegrastats_line(line)
    assert parsed == {"ram_used_mb": 1587, "ram_total_mb": 61005, "swap_used_mb": 0, "gr3d_freq_pct": 99}
    assert module.parse_tegrastats_line("not a tegrastats line") is None


def test_verify_sha256_accepts_match_and_rejects_mismatch(tmp_path: Path):
    module = _module()
    target = tmp_path / "asset.bin"
    target.write_bytes(b"probe")
    digest = module.sha256_file(target)
    assert module.verify_sha256(target, digest) == digest
    with pytest.raises(ValueError):
        module.verify_sha256(target, "0" * 64)


def test_classify_stop_requires_exit_free_port_and_reclaimed_memory():
    module = _module()
    good = module.classify_stop(exit_code=0, port_free=True, reclaimed=True, kill_used=False)
    assert good["quiescent"] is True
    assert good["graceful_stop"] is True
    killed = module.classify_stop(exit_code=-9, port_free=True, reclaimed=False, kill_used=True)
    assert killed["quiescent"] is False
    assert killed["graceful_stop"] is False


def test_evidence_directory_name_is_unique_and_utc():
    module = _module()
    name = module.evidence_dir_name(now_utc="2026-09-17T04:05:06Z")
    assert name == "m00-qwen25vl-envelope-20260917T040506Z"


def test_harness_module_imports_without_side_effects():
    module = _module()
    assert json.dumps(module.PROBE_SCHEMA) == "1"
