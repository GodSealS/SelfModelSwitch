from __future__ import annotations

import json
import math

import pytest

from model_scheduler import contracts_v2 as cv2

GGUF_PROFILE = "llama-cpp-gguf-v1"
HF_PROFILE = "hf-sharded-v1"


def _valid_deployment() -> dict:
    """Schema-v2 deployment used by most tests.

    Asset digests and the reserved figure come from the M00 measurement. The
    physical resident peak is a synthetic test value because M00 has no measured
    physical upper bound yet (plan/08-execution-plan.md C02).
    """
    return {
        "schema_version": 2,
        "runtimes": [
            {
                "runtime_id": "llama-cpp-cuda-sm87-4bc272f",
                "profile_id": GGUF_PROFILE,
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
                    "max_images": 1,
                },
                "timeout_seconds": 3600,
                "reserved_bytes": 6106148045,
                "measured": True,
                "measurement_ref": "e" * 64,
                "physical_resident_peak_bytes": 5_000_000_000,
            }
        ],
    }


def _sharded_deployment() -> dict:
    """A sharded HF registration: three model files plus tokenizer and config."""
    data = _valid_deployment()
    data["runtimes"].append(
        {
            "runtime_id": "hf-transformers-cpu",
            "profile_id": HF_PROFILE,
            "image_digest": "ghcr.io/example/hf-serve@sha256:" + "f" * 64,
            "adapter_sha256": "b" * 64,
            "lock_sha256": "c" * 64,
            "startup_args": ["--host", "--port"],
        }
    )
    data["models"].append(
        {
            "model_id": "hf-sharded-model",
            "runtime_id": "hf-transformers-cpu",
            "capabilities": ["chat"],
            "assets": [
                {"role": "model", "path": "hf/shard-00001.safetensors", "sha256": "1" * 64, "size_bytes": 1000},
                {"role": "model", "path": "hf/shard-00002.safetensors", "sha256": "2" * 64, "size_bytes": 1000},
                {"role": "model", "path": "hf/shard-00003.safetensors", "sha256": "3" * 64, "size_bytes": 1000},
                {"role": "tokenizer", "path": "hf/tokenizer.json", "sha256": "4" * 64, "size_bytes": 10},
                {"role": "config", "path": "hf/config.json", "sha256": "5" * 64, "size_bytes": 10},
            ],
            "port": 18082,
            "envelope": {
                "ctx_size": 4096,
                "max_input_tokens": 3072,
                "max_output_tokens": 1024,
                "max_parallel": 1,
                "max_image_tokens": 0,
                "max_image_edge_pixels": 0,
                "max_images": 0,
            },
            "timeout_seconds": 600,
            "reserved_bytes": 1150000000,
            "measured": False,
            "measurement_ref": None,
            "physical_resident_peak_bytes": None,
        }
    )
    return data


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
        "profile_id": GGUF_PROFILE,
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
        "profile_id": GGUF_PROFILE,
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
        "max_images": 1,
    }
    cv2.parse_envelope(base)
    for key, value in (
        ("max_input_tokens", 30000),
        ("max_parallel", 0),
        ("max_image_tokens", 28672),
        ("max_image_edge_pixels", 0),
        ("max_output_tokens", 0),
        ("max_images", 0),
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


def test_model_capabilities_cover_tools_and_thinking_over_chat():
    # TC01: the model capability set is the matrix; tools/thinking are chat
    # features with a model asset and the openai-chat protocol, nothing more.
    assert cv2.MODEL_CAPABILITIES == frozenset(cv2.CAPABILITY_MATRIX)
    assert {"chat", "vision", "embeddings", "rerank", "tools", "thinking"} == cv2.MODEL_CAPABILITIES
    for capability in ("tools", "thinking"):
        spec = cv2.CAPABILITY_MATRIX[capability]
        assert spec.required_asset_roles == ("model",)
        assert spec.protocol == "openai-chat"


def test_tools_and_thinking_register_against_the_same_model_asset():
    data = _valid_deployment()
    data["models"][0]["capabilities"] = ["chat", "vision", "tools", "thinking"]
    deployment = cv2.parse_deployment(data)
    assert deployment.models[0].capabilities == ("chat", "vision", "tools", "thinking")


def test_tools_or_thinking_without_chat_is_refused():
    for capability in ("tools", "thinking"):
        data = _valid_deployment()
        # vision keeps the projector asset valid, so the only refusal left is the
        # missing chat dependency of the new capability.
        data["models"][0]["capabilities"] = ["vision", capability]
        with pytest.raises(cv2.ContractError, match="requires chat"):
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


def test_asset_paths_are_unique_and_roles_may_repeat():
    # A second model file is registration-legal in general, but the first GGUF
    # profile allows exactly one model asset, so it is rejected here.
    data = _valid_deployment()
    second_model = dict(data["models"][0]["assets"][0])
    second_model["path"] = "qwen25vl-7b-q4/model-00002.gguf"
    data["models"][0]["assets"].append(second_model)
    with pytest.raises(cv2.ContractError):
        cv2.parse_deployment(data)

    # Two roles must not name the same path.
    duplicate_path = _valid_deployment()
    duplicate_path["models"][0]["assets"][0]["path"] = "qwen25vl-7b-q4/mmproj.gguf"
    with pytest.raises(cv2.ContractError):
        cv2.parse_deployment(duplicate_path)


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


# ---------------------------------------------------------------------------
# M01 P01: runtime profiles, sharding, measurement fields, exact reservations


def test_profiles_are_a_closed_registered_set():
    assert cv2.PROFILES[GGUF_PROFILE].executable is True
    assert cv2.PROFILES[HF_PROFILE].executable is False
    unknown = _valid_deployment()
    unknown["runtimes"][0]["profile_id"] = "onnx-runtime-v9"
    with pytest.raises(cv2.ContractError):
        cv2.parse_deployment(unknown)


def test_startable_profile_gate_rejects_registration_only_profiles():
    deployment = cv2.parse_deployment(_sharded_deployment())
    by_id = {runtime.runtime_id: runtime for runtime in deployment.runtimes}
    assert cv2.require_startable_profile(by_id["llama-cpp-cuda-sm87-4bc272f"]).profile_id == GGUF_PROFILE
    with pytest.raises(cv2.ContractError):
        cv2.require_startable_profile(by_id["hf-transformers-cpu"])


def test_startup_args_are_bounded_by_the_profile():
    gguf = _valid_deployment()
    gguf["runtimes"][0]["startup_args"] = ["--load-mode", "--port"]
    cv2.parse_deployment(gguf)

    # --load-mode and --image-max-tokens are llama.cpp flags, not HF serve flags.
    sharded = _sharded_deployment()
    sharded["runtimes"][1]["startup_args"] = ["--load-mode"]
    with pytest.raises(cv2.ContractError):
        cv2.parse_deployment(sharded)


def test_gguf_profile_enforces_asset_cardinality_and_allowed_roles():
    two_projectors = _valid_deployment()
    two_projectors["models"][0]["assets"].append(
        {"role": "projector", "path": "qwen25vl-7b-q4/mmproj-2.gguf", "sha256": "d2" + "0" * 62, "size_bytes": 10}
    )
    with pytest.raises(cv2.ContractError):
        cv2.parse_deployment(two_projectors)

    # A projector without the vision capability is an inconsistent registration.
    projector_without_vision = _valid_deployment()
    projector_without_vision["models"][0]["capabilities"] = ["chat"]
    with pytest.raises(cv2.ContractError):
        cv2.parse_deployment(projector_without_vision)

    # Roles outside the profile are not registrable at all.
    extra_role = _valid_deployment()
    extra_role["models"][0]["assets"].append(
        {"role": "tokenizer", "path": "qwen25vl-7b-q4/tokenizer.json", "sha256": "9" * 64, "size_bytes": 10}
    )
    with pytest.raises(cv2.ContractError):
        cv2.parse_deployment(extra_role)


def test_sharded_profile_accepts_repeated_roles_keyed_by_path():
    deployment = cv2.parse_deployment(_sharded_deployment())
    sharded = deployment.models[1]
    assert [asset.role for asset in sharded.assets].count("model") == 3
    assert sorted(asset.path for asset in sharded.assets) == [
        "hf/config.json",
        "hf/shard-00001.safetensors",
        "hf/shard-00002.safetensors",
        "hf/shard-00003.safetensors",
        "hf/tokenizer.json",
    ]


def test_ids_flags_and_digests_must_match_the_whole_string():
    for bad_id in ("model-1\n", "model-1\x00", "model-1\x00x"):
        data = _valid_deployment()
        data["models"][0]["model_id"] = bad_id
        with pytest.raises(cv2.ContractError):
            cv2.parse_deployment(data)
    runtime_id = _valid_deployment()
    runtime_id["runtimes"][0]["runtime_id"] = "llama-cpp-cuda\n"
    with pytest.raises(cv2.ContractError):
        cv2.parse_deployment(runtime_id)
    digest = _valid_deployment()
    digest["runtimes"][0]["image_digest"] += "\n"
    with pytest.raises(cv2.ContractError):
        cv2.parse_deployment(digest)
    flag = _valid_deployment()
    flag["runtimes"][0]["startup_args"] = ["--parallel\n"]
    with pytest.raises(cv2.ContractError):
        cv2.parse_deployment(flag)
    checksum = _valid_deployment()
    checksum["models"][0]["assets"][0]["sha256"] += "\n"
    with pytest.raises(cv2.ContractError):
        cv2.parse_deployment(checksum)


def test_asset_paths_reject_control_bytes():
    for bad_path in ("a\x00b.gguf", "model.gguf\n", "a\tb.gguf", "model.gguf\x7f"):
        with pytest.raises(cv2.ContractError):
            cv2.parse_asset_ref({"role": "model", "path": bad_path, "sha256": "a" * 64, "size_bytes": 1})


def test_max_images_must_agree_with_the_vision_capability():
    missing = _valid_deployment()
    del missing["models"][0]["envelope"]["max_images"]
    with pytest.raises(cv2.ContractError):
        cv2.parse_deployment(missing)

    non_vision = _valid_deployment()
    non_vision["models"][0]["capabilities"] = ["chat"]
    non_vision["models"][0]["assets"] = [non_vision["models"][0]["assets"][0]]
    non_vision["models"][0]["envelope"].update({"max_image_tokens": 0, "max_image_edge_pixels": 0, "max_images": 0})
    parsed = cv2.parse_deployment(non_vision)
    assert parsed.models[0].envelope.max_images == 0

    vision_without_images = _valid_deployment()
    vision_without_images["models"][0]["envelope"]["max_images"] = 0
    with pytest.raises(cv2.ContractError):
        cv2.parse_deployment(vision_without_images)

    images_without_token_budget = _valid_deployment()
    images_without_token_budget["models"][0]["envelope"].update({"max_image_tokens": 0, "max_image_edge_pixels": 0})
    with pytest.raises(cv2.ContractError):
        cv2.parse_deployment(images_without_token_budget)

    boolean_images = _valid_deployment()
    boolean_images["models"][0]["envelope"]["max_images"] = True
    with pytest.raises(cv2.ContractError):
        cv2.parse_deployment(boolean_images)


def test_measured_models_must_bind_measurement_material():
    for key in ("measurement_ref", "physical_resident_peak_bytes"):
        missing = _valid_deployment()
        del missing["models"][0][key]
        with pytest.raises(cv2.ContractError):
            cv2.parse_deployment(missing)

    bad_ref = _valid_deployment()
    bad_ref["models"][0]["measurement_ref"] = "not-a-digest"
    with pytest.raises(cv2.ContractError):
        cv2.parse_deployment(bad_ref)

    for bad_peak in (0, -1, True, 1.5):
        bad_peak_data = _valid_deployment()
        bad_peak_data["models"][0]["physical_resident_peak_bytes"] = bad_peak
        with pytest.raises(cv2.ContractError):
            cv2.parse_deployment(bad_peak_data)

    inconsistent = _valid_deployment()
    inconsistent["models"][0]["measured"] = False
    with pytest.raises(cv2.ContractError):
        cv2.parse_deployment(inconsistent)

    unmeasured = _valid_deployment()
    unmeasured["models"][0].update(
        {"measured": False, "measurement_ref": None, "physical_resident_peak_bytes": None}
    )
    assert cv2.parse_deployment(unmeasured).models[0].measured is False


def test_production_open_requires_measurement_and_an_executable_profile():
    unmeasured = _valid_deployment()
    unmeasured["models"][0].update(
        {"measured": False, "measurement_ref": None, "physical_resident_peak_bytes": None}
    )
    parsed = cv2.parse_deployment(unmeasured)
    with pytest.raises(cv2.ContractError):
        cv2.require_production_openable(parsed.models[0], parsed.runtimes[0])

    measured = cv2.parse_deployment(_valid_deployment())
    assert cv2.require_production_openable(measured.models[0], measured.runtimes[0]) is None

    sharded = cv2.parse_deployment(_sharded_deployment())
    with pytest.raises(cv2.ContractError):
        cv2.require_production_openable(sharded.models[1], sharded.runtimes[1])


def test_reserved_bytes_use_the_exact_integer_margin_exactly_once():
    # M00: measured_peak 5309693952 B is registered as R = 6106148045 B.
    assert cv2.reserved_bytes_from_peak(5309693952) == 6106148045
    # One byte above an exact multiple of the margin still rounds up.
    assert cv2.reserved_bytes_from_peak(20) == 23
    assert cv2.reserved_bytes_from_peak(21) == 25
    assert cv2.reserved_bytes_from_peak(1) == 2
    for peak in (1, 7, 20, 21, 999_999, 5309693952):
        reserved = cv2.reserved_bytes_from_peak(peak)
        assert reserved * 100 >= peak * 115
        assert (reserved - 1) * 100 < peak * 115
    for bad_peak in (0, -1, True, 1.5):
        with pytest.raises(cv2.ContractError):
            cv2.reserved_bytes_from_peak(bad_peak)

    # v2 reserved_bytes already is R: the internal figure must not margin it again.
    assert cv2.effective_reserved_bytes(6106148045) == 6106148045
    assert cv2.effective_reserved_bytes(23) == 23


def test_v1_compatibility_boundary_matches_the_legacy_book():
    from model_scheduler.contracts import Capability, ModelSpec as LegacyModelSpec
    from model_scheduler.model_registry import Book

    peak = 5309693952
    legacy_spec = LegacyModelSpec(
        model_id="qwen25vl-7b-q4",
        upstream_url="http://127.0.0.1:18081",
        capabilities=frozenset({Capability.CHAT}),
        reserved_bytes=peak,
    )
    book = Book({"qwen25vl-7b-q4": legacy_spec}, model_budget=64 * 2**30, free_floor=2 * 2**30)
    assert book.margin == 0.15
    for value in (peak, 20, 21, 999_999):
        v1 = cv2.effective_reserved_bytes(value, legacy_v1_margin=book.margin)
        assert v1 == math.ceil(value * (1 + book.margin))
    assert cv2.effective_reserved_bytes(peak, legacy_v1_margin=0.15) == 6106148045


def test_physical_threshold_uses_the_same_margin_once():
    assert cv2.physical_reserved_bytes_from_peak(5_000_000_000) == 5_750_000_000
    assert cv2.physical_reserved_bytes_from_peak(5_000_000_001) == 5_750_000_002
    for bad_peak in (0, -1, True):
        with pytest.raises(cv2.ContractError):
            cv2.physical_reserved_bytes_from_peak(bad_peak)


def test_json_rejects_non_finite_numbers_anywhere():
    for text in ('{"a": 1e999}', '{"a": -1e999}', '{"a": [1, {"b": 1e999}]}', '{"a": {"b": [1E999]}}'):
        with pytest.raises(cv2.ContractError):
            cv2.parse_json_document(text)
    injected = json.dumps(_valid_deployment()).replace('"max_parallel": 2', '"max_parallel": 1e999')
    with pytest.raises(cv2.ContractError):
        cv2.parse_deployment_json(injected)
