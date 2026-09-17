from __future__ import annotations

import json

import pytest

from model_scheduler import contracts_v2 as cv2


def _valid_deployment() -> dict:
    return {
        "schema_version": 2,
        "runtimes": [
            {
                "runtime_id": "llama-cpp-cuda-sm87-4bc272f",
                "image_digest": "ghcr.io/example/llama-cuda@sha256:" + "a" * 64,
                "adapter_sha256": "b" * 64,
                "lock_sha256": "c" * 64,
                "startup_args": ["--parallel", "--kv-unified-per-slot", "--image-max-tokens", "--no-warmup"],
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
                "port": 18081,
                "envelope": {
                    "ctx_size": 32768,
                    "max_input_tokens": 28672,
                    "max_output_tokens": 4096,
                    "max_parallel": 2,
                    "max_image_tokens": 1280,
                    "max_image_edge_pixels": 1024,
                },
                "timeout_seconds": 3600,
                "reserved_bytes": 6106148045,
                "measured": True,
            }
        ],
    }


def test_versions_and_protocol_limits_are_locked():
    assert cv2.SCHEMA_VERSION == 2
    assert cv2.CONTROL_PROTOCOL_VERSION == 1
    assert cv2.PORT_RANGE == (10001, 19999)
    assert cv2.IDEMPOTENCY_RETENTION_SECONDS == 86400
    assert cv2.CONTROL_SOCKET_MODE == 0o660
    assert cv2.SESSION_LIMITS["queue_capacity"] == 128
    assert cv2.SESSION_LIMITS["queue_timeout_seconds"] == 1800
    assert cv2.SESSION_LIMITS["prepare_limit_seconds"] == 900


def test_parse_valid_deployment_accepts_dynamic_ids():
    deployment = cv2.parse_deployment(_valid_deployment())
    assert [model.model_id for model in deployment.models] == ["qwen25vl-7b-q4"]
    assert deployment.models[0].envelope.max_parallel == 2
    assert deployment.models[0].capabilities == ("chat", "vision")
    assert deployment.runtimes[0].runtime_id == "llama-cpp-cuda-sm87-4bc272f"


def test_wrong_schema_version_is_rejected():
    data = _valid_deployment()
    data["schema_version"] = 1
    with pytest.raises(cv2.ContractError):
        cv2.parse_deployment(data)


def test_unknown_fields_are_rejected_at_every_level():
    cases = []
    top = _valid_deployment()
    top["candidate_sha256"] = "x"
    cases.append(top)
    runtime = _valid_deployment()
    runtime["runtimes"][0]["entrypoint"] = "/bin/sh"
    cases.append(runtime)
    asset = _valid_deployment()
    asset["models"][0]["assets"][0]["url"] = "https://example.com/model.gguf"
    cases.append(asset)
    envelope = _valid_deployment()
    envelope["models"][0]["envelope"]["max_audio_seconds"] = 60
    cases.append(envelope)
    for data in cases:
        with pytest.raises(cv2.ContractError):
            cv2.parse_deployment(data)


def test_job_stage_and_pipeline_fields_are_rejected():
    job = _valid_deployment()
    job["models"][0]["job_id"] = "movie-42"
    with pytest.raises(cv2.ContractError):
        cv2.parse_deployment(job)
    pipeline = _valid_deployment()
    pipeline["models"][0]["pipeline"] = {"stages": []}
    with pytest.raises(cv2.ContractError):
        cv2.parse_deployment(pipeline)
    stage = _valid_deployment()
    stage["stage"] = "transcribe"
    with pytest.raises(cv2.ContractError):
        cv2.parse_deployment(stage)


def test_booleans_are_not_integers():
    port = _valid_deployment()
    port["models"][0]["port"] = True
    with pytest.raises(cv2.ContractError):
        cv2.parse_deployment(port)
    size = _valid_deployment()
    size["models"][0]["assets"][0]["size_bytes"] = True
    with pytest.raises(cv2.ContractError):
        cv2.parse_deployment(size)


def test_non_finite_json_is_rejected():
    with pytest.raises(cv2.ContractError):
        cv2.parse_json_document('{"a": NaN}')
    with pytest.raises(cv2.ContractError):
        cv2.parse_json_document('{"a": Infinity}')
    with pytest.raises(cv2.ContractError):
        cv2.parse_json_document('{"a": -Infinity}')


def test_duplicate_json_keys_are_rejected():
    with pytest.raises(cv2.ContractError):
        cv2.parse_json_document('{"a": 1, "a": 2}')


def test_asset_paths_must_be_safe_relative_paths():
    for bad in ("/abs/model.gguf", "../escape.gguf", "a/../../b.gguf", "", "a//b.gguf", "a/./b.gguf", "a\\b.gguf"):
        asset = {"role": "model", "path": bad, "sha256": "a" * 64, "size_bytes": 1}
        with pytest.raises(cv2.ContractError):
            cv2.parse_asset_ref(asset)
    ok = cv2.parse_asset_ref({"role": "model", "path": "sub/dir/model.gguf", "sha256": "a" * 64, "size_bytes": 1})
    assert ok.path == "sub/dir/model.gguf"


def test_asset_hash_size_and_role_are_strict():
    base = {"role": "model", "path": "m.gguf", "sha256": "a" * 64, "size_bytes": 10}
    for key, value in (("sha256", "XYZ"), ("sha256", "a" * 63), ("size_bytes", 0), ("size_bytes", -1), ("role", "weights")):
        asset = dict(base)
        asset[key] = value
        with pytest.raises(cv2.ContractError):
            cv2.parse_asset_ref(asset)


def test_runtime_digest_must_be_pinned():
    base = {
        "runtime_id": "llama-cpp-cuda-sm87",
        "image_digest": "ghcr.io/example/llama-cuda@sha256:" + "a" * 64,
        "adapter_sha256": "b" * 64,
        "lock_sha256": "c" * 64,
        "startup_args": ["--parallel"],
    }
    cv2.parse_runtime_spec(base)
    for bad_digest in ("repo/image:latest", "repo/image@sha256:abc", "sha256:" + "a" * 64):
        runtime = dict(base)
        runtime["image_digest"] = bad_digest
        with pytest.raises(cv2.ContractError):
            cv2.parse_runtime_spec(runtime)


def test_runtime_startup_args_are_whitelisted_and_unique():
    base = {
        "runtime_id": "llama-cpp-cuda-sm87",
        "image_digest": "ghcr.io/example/llama-cuda@sha256:" + "a" * 64,
        "adapter_sha256": "b" * 64,
        "lock_sha256": "c" * 64,
        "startup_args": ["--parallel", "--load-mode"],
    }
    parsed = cv2.parse_runtime_spec(base)
    assert parsed.startup_args == ("--parallel", "--load-mode")
    for bad in (["--extra-args"], ["parallel"], ["--lora=foo"], ["--parallel", "--parallel"], ["--model"]):
        runtime = dict(base)
        runtime["startup_args"] = bad
        with pytest.raises(cv2.ContractError):
            cv2.parse_runtime_spec(runtime)


def test_envelope_consistency_rules():
    base = {
        "ctx_size": 32768,
        "max_input_tokens": 28672,
        "max_output_tokens": 4096,
        "max_parallel": 2,
        "max_image_tokens": 1280,
        "max_image_edge_pixels": 1024,
    }
    cv2.parse_envelope(base)
    for key, value in (
        ("max_input_tokens", 30000),
        ("max_parallel", 0),
        ("max_image_tokens", 28672),
        ("max_image_edge_pixels", 0),
        ("max_output_tokens", 0),
    ):
        envelope = dict(base)
        envelope[key] = value
        with pytest.raises(cv2.ContractError):
            cv2.parse_envelope(envelope)


def test_capability_matrix_requires_roles():
    data = _valid_deployment()
    data["models"][0]["assets"] = [data["models"][0]["assets"][0]]
    with pytest.raises(cv2.ContractError):
        cv2.parse_deployment(data)


def test_unknown_capability_is_rejected():
    data = _valid_deployment()
    data["models"][0]["capabilities"] = ["chat", "video"]
    with pytest.raises(cv2.ContractError):
        cv2.parse_deployment(data)


def test_duplicate_ports_models_and_ids_are_rejected():
    duplicate_port = _valid_deployment()
    second = json.loads(json.dumps(duplicate_port["models"][0]))
    second["model_id"] = "second-model"
    second["capabilities"] = ["chat"]
    second["assets"] = [second["assets"][0]]
    duplicate_port["models"].append(second)
    with pytest.raises(cv2.ContractError):
        cv2.parse_deployment(duplicate_port)

    duplicate_id = _valid_deployment()
    duplicate_id["models"].append(json.loads(json.dumps(duplicate_id["models"][0])))
    with pytest.raises(cv2.ContractError):
        cv2.parse_deployment(duplicate_id)

    duplicate_runtime = _valid_deployment()
    duplicate_runtime["runtimes"].append(json.loads(json.dumps(duplicate_runtime["runtimes"][0])))
    with pytest.raises(cv2.ContractError):
        cv2.parse_deployment(duplicate_runtime)


def test_model_id_grammar():
    for bad in ("UPPER", "has space", "x" * 80, "-leading", "trailing-", ""):
        data = _valid_deployment()
        data["models"][0]["model_id"] = bad
        with pytest.raises(cv2.ContractError):
            cv2.parse_deployment(data)
    for good in ("a", "model-1", "qwen25vl-7b-q4", "x" * 63):
        data = _valid_deployment()
        data["models"][0]["model_id"] = good
        cv2.parse_deployment(data)


def test_duplicate_asset_roles_are_rejected():
    data = _valid_deployment()
    duplicate = dict(data["models"][0]["assets"][0])
    duplicate["path"] = "other.gguf"
    data["models"][0]["assets"].append(duplicate)
    with pytest.raises(cv2.ContractError):
        cv2.parse_deployment(data)


def test_measured_and_reserved_bytes_are_strict():
    measured = _valid_deployment()
    measured["models"][0]["measured"] = "yes"
    with pytest.raises(cv2.ContractError):
        cv2.parse_deployment(measured)
    reserved = _valid_deployment()
    reserved["models"][0]["reserved_bytes"] = 0
    with pytest.raises(cv2.ContractError):
        cv2.parse_deployment(reserved)


def test_deployment_requires_existing_runtime_reference():
    data = _valid_deployment()
    data["models"][0]["runtime_id"] = "missing-runtime"
    with pytest.raises(cv2.ContractError):
        cv2.parse_deployment(data)


def test_digest_is_stable_and_key_order_independent():
    original = _valid_deployment()
    shuffled = {
        "models": json.loads(json.dumps(original["models"])),
        "runtimes": json.loads(json.dumps(original["runtimes"])),
        "schema_version": 2,
    }
    first = cv2.parse_deployment(_valid_deployment())
    second = cv2.parse_deployment(shuffled)
    assert cv2.deployment_digest(first) == cv2.deployment_digest(second)
    assert len(cv2.deployment_digest(first)) == 64
    assert cv2.canonical_json_bytes({"b": 1, "a": 2}) == b'{"a":2,"b":1}'
