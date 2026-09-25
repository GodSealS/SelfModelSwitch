"""CT06: the 27B independent runtime and its capability/flag gate (TC07, A07).

These cases pin what the chat-features profile may render and what stays
refused: an independent runtime identity for the one model that carries tools
and thinking, fixed values for the two new flags, no auto/none substitution, a
lab-only profile, a production refusal, and a 7B render that must not move.
Every negative case asserts the refusal itself, never a rendered fallback.
"""
from __future__ import annotations

import copy
from importlib import import_module

import pytest

from model_scheduler.contracts_v2 import (
    CHAT_FEATURES_PROFILE,
    GGUF_PROFILE,
    ContractError,
    ModelSpec,
    parse_deployment,
    parse_runtime_spec,
    require_production_openable,
)
from model_scheduler.runtime_profiles import (
    CHAT_FEATURE_MODEL_IDS,
    CHAT_FEATURES_LAUNCH_RULES,
    CAPABILITY_FLAGS,
    FlagSource,
    LaunchRenderError,
    render_container_launch,
)

IMAGE = "sms-llama-cpp@sha256:" + "8" * 64
MODEL_DIRECTORY = "/media/jtzn/sandisk-ext4/models"
GGUF_RUNTIME = "llama-cpp-cuda-sm87-4bc272f"
FEATURES_RUNTIME = "llama-cpp-chat-features-4bc272f"
SEVEN_B = "qwen25vl-7b-q4"
TWENTY_SEVEN_B = "qwen36-27b"
CONFIG_SHA256 = "d" * 64
LAB_BUDGET = 16_000_000_000

BASE_FLAGS = [
    "--load-mode",
    "--parallel",
    "--kv-unified-per-slot",
    "--n-gpu-layers",
    "--flash-attn",
    "--no-warmup",
    "--no-webui",
    "--host",
    "--port",
]
VISION_FLAGS = ["--image-max-tokens"]
GGUF_FLAGS = [*BASE_FLAGS, *VISION_FLAGS]


def _runtime(*, runtime_id: str, profile_id: str, startup_args: list[str]) -> dict:
    return {
        "runtime_id": runtime_id,
        "profile_id": profile_id,
        "image_digest": IMAGE,
        "adapter_sha256": "b" * 64,
        "lock_sha256": "c" * 64,
        "startup_args": list(startup_args),
    }


def _model(*, model_id: str, runtime_id: str, capabilities: tuple[str, ...], measured: bool = True) -> dict:
    vision = "vision" in capabilities
    assets = [{"role": "model", "path": f"{model_id}/model.gguf", "sha256": "3f" + "0" * 62, "size_bytes": 4683072320}]
    if vision:
        assets.append({"role": "projector", "path": f"{model_id}/mmproj.gguf", "sha256": "d1" + "0" * 62, "size_bytes": 1354162912})
    return {
        "model_id": model_id,
        "runtime_id": runtime_id,
        "capabilities": list(capabilities),
        "assets": assets,
        "port": 10002,
        "envelope": {
            "ctx_size": 32768,
            "max_input_tokens": 28672,
            "max_output_tokens": 4096,
            "max_parallel": 2,
            "max_image_tokens": 1280 if vision else 0,
            "max_image_edge_pixels": 1024 if vision else 0,
            "max_images": 1 if vision else 0,
        },
        "timeout_seconds": 3600,
        "reserved_bytes": 6106148045,
        "measured": measured,
        "measurement_ref": "e" * 64 if measured else None,
        "physical_resident_peak_bytes": 5_000_000_000 if measured else None,
    }


def _registration(
    model_id: str = TWENTY_SEVEN_B,
    *,
    capabilities: tuple[str, ...] = ("chat", "tools"),
    profile_id: str = CHAT_FEATURES_PROFILE,
    runtime_id: str = FEATURES_RUNTIME,
    startup_args: list[str] | None = None,
    measured: bool = True,
    extra_models: list[dict] | None = None,
    extra_runtimes: list[dict] | None = None,
    model_port: int = 10002,
):
    flags = [*BASE_FLAGS, "--jinja"] if startup_args is None else startup_args
    runtimes = [_runtime(runtime_id=runtime_id, profile_id=profile_id, startup_args=flags)]
    if extra_runtimes is not None:
        runtimes.extend(extra_runtimes)
    model = _model(model_id=model_id, runtime_id=runtime_id, capabilities=capabilities, measured=measured)
    model["port"] = model_port
    models = [model, *(extra_models or [])]
    return parse_deployment({"schema_version": 2, "runtimes": runtimes, "models": models})


def _render(registration, model_id: str = TWENTY_SEVEN_B, *, mode: str = "lab", config_sha256: str = CONFIG_SHA256, **kwargs):
    return render_container_launch(
        registration,
        model_id,
        deployment_id="lab-orin",
        model_directory=MODEL_DIRECTORY,
        config_sha256=config_sha256,
        mode=mode,
        container_runtime="nvidia",
        temporary_budget_bytes=None if mode == "production" else LAB_BUDGET,
        **kwargs,
    )


def _count(argv: tuple[str, ...], flag: str) -> int:
    return sum(1 for token in argv if token == flag)


def test_a07_tools_capability_renders_jinja_once_and_no_reasoning_format() -> None:
    launch = _render(_registration(capabilities=("chat", "tools")))

    assert _count(launch.argv, "--jinja") == 1
    assert "--reasoning-format" not in launch.argv
    assert launch.profile_id == CHAT_FEATURES_PROFILE
    assert launch.server_args[-1] == "--jinja"
    assert launch.server_args[:-1] == (
        "--model", f"/models/{TWENTY_SEVEN_B}/model.gguf",
        "--parallel", "2",
        "--kv-unified-per-slot", "32768",
        "--n-gpu-layers", "99",
        "--flash-attn", "auto",
        "--no-warmup",
        "--no-webui",
        "--load-mode", "auto",
        "--host", "0.0.0.0",
        "--port", "8080",
    )


def test_a07_thinking_capability_renders_jinja_and_the_fixed_deepseek_value() -> None:
    launch = _render(_registration(capabilities=("chat", "thinking"), startup_args=[*BASE_FLAGS, "--jinja", "--reasoning-format"]))

    assert _count(launch.argv, "--jinja") == 1
    assert _count(launch.argv, "--reasoning-format") == 1
    assert tuple(launch.argv[-2:]) == ("--reasoning-format", "deepseek")
    # no unregistered substitution: the shipped server never sees auto/none here
    assert {"auto", "none"} & set(launch.argv[launch.argv.index("--reasoning-format"):]) == set()


def test_a07_the_capability_union_renders_each_required_flag_exactly_once() -> None:
    launch = _render(_registration(capabilities=("chat", "tools", "thinking"),
                                  startup_args=[*BASE_FLAGS, "--jinja", "--reasoning-format"]))

    assert _count(launch.argv, "--jinja") == 1
    assert _count(launch.argv, "--reasoning-format") == 1
    assert tuple(launch.argv[-3:]) == ("--jinja", "--reasoning-format", "deepseek")

    with pytest.raises(ContractError, match="duplicate startup arg"):
        _registration(startup_args=[*BASE_FLAGS, "--jinja", "--jinja", "--reasoning-format"])


def test_a07_a_chat_feature_capability_without_chat_is_refused() -> None:
    with pytest.raises(ContractError, match="requires chat"):
        _registration(capabilities=("tools",))
    with pytest.raises(ContractError, match="requires chat"):
        _registration(capabilities=("thinking",))


def test_a07_a_missing_capability_flag_is_refused_instead_of_substituted() -> None:
    tools_jinja_missing = _registration(capabilities=("chat", "tools"), startup_args=BASE_FLAGS)
    with pytest.raises(LaunchRenderError, match="must enable --jinja"):
        _render(tools_jinja_missing)

    thinking_flags_missing = _registration(capabilities=("chat", "thinking"), startup_args=[*BASE_FLAGS, "--jinja"])
    with pytest.raises(LaunchRenderError, match="must enable --reasoning-format"):
        _render(thinking_flags_missing)


def test_a07_a_capability_foreign_flag_is_refused() -> None:
    tools_with_reasoning = _registration(capabilities=("chat", "tools"), startup_args=[*BASE_FLAGS, "--jinja", "--reasoning-format"])
    with pytest.raises(LaunchRenderError, match="--reasoning-format"):
        _render(tools_with_reasoning)

    plain_chat_with_jinja = _registration(capabilities=("chat",), startup_args=[*BASE_FLAGS, "--jinja"])
    with pytest.raises(LaunchRenderError, match="no tools or thinking capability"):
        _render(plain_chat_with_jinja)


def test_a07_the_value_sources_pin_the_any_of_capability_gate() -> None:
    jinja = CHAT_FEATURES_LAUNCH_RULES.flag_sources["--jinja"]
    reasoning = CHAT_FEATURES_LAUNCH_RULES.flag_sources["--reasoning-format"]

    assert jinja == FlagSource("fixed", requires_any_capability=frozenset({"tools", "thinking"}))
    assert reasoning == FlagSource("fixed", value="deepseek", requires_any_capability=frozenset({"thinking"}))
    assert CAPABILITY_FLAGS["tools"] == ("--jinja",)
    assert CAPABILITY_FLAGS["thinking"] == ("--jinja", "--reasoning-format")


def test_a07_a_value_source_with_both_conditions_requires_both_not_either(monkeypatch) -> None:
    """`requires_capability` AND `requires_any_capability`: never silently OR."""
    runtime = _runtime(runtime_id=FEATURES_RUNTIME, profile_id=CHAT_FEATURES_PROFILE,
                       startup_args=[*BASE_FLAGS, "--jinja"])
    model = _model(model_id=TWENTY_SEVEN_B, runtime_id=FEATURES_RUNTIME, capabilities=("chat", "tools"))
    registration = parse_deployment({"schema_version": 2, "runtimes": [runtime], "models": [model]})
    rules = type(CHAT_FEATURES_LAUNCH_RULES)(
        flag_sources={**CHAT_FEATURES_LAUNCH_RULES.flag_sources,
                      "--jinja": FlagSource("fixed", requires_capability="vision",
                                            requires_any_capability=frozenset({"tools", "thinking"}))},
        required_flags=CHAT_FEATURES_LAUNCH_RULES.required_flags,
    )
    monkeypatch.setitem(import_module("model_scheduler.runtime_profiles").LAUNCH_RULES, CHAT_FEATURES_PROFILE, rules)

    with pytest.raises(LaunchRenderError, match="vision"):
        _render(registration)


def test_a07_the_old_profile_refuses_the_chat_feature_capabilities_and_flags() -> None:
    old_flags = _runtime(runtime_id=GGUF_RUNTIME, profile_id=GGUF_PROFILE, startup_args=[*GGUF_FLAGS, "--jinja"])
    with pytest.raises(ContractError, match="not allowed by profile"):
        parse_runtime_spec(old_flags)
    old_reasoning = _runtime(runtime_id=GGUF_RUNTIME, profile_id=GGUF_PROFILE,
                             startup_args=[*GGUF_FLAGS, "--reasoning-format"])
    with pytest.raises(ContractError, match="not allowed by profile"):
        parse_runtime_spec(old_reasoning)

    registration = _registration(profile_id=GGUF_PROFILE, runtime_id=GGUF_RUNTIME, startup_args=GGUF_FLAGS)
    with pytest.raises(LaunchRenderError, match="require profile 'llama-cpp-chat-features-v1'"):
        _render(registration)


def test_a07_the_chat_features_profile_requires_a_chat_feature_capability() -> None:
    """Dropping the capability but keeping the new profile/flag is not a rollback."""
    registration = _registration(capabilities=("chat",))
    with pytest.raises(LaunchRenderError, match="declares no tools or thinking capability"):
        _render(registration)


def test_a07_production_refuses_the_chat_features_profile_and_its_capabilities() -> None:
    registration = _registration(capabilities=("chat", "tools"))
    with pytest.raises(LaunchRenderError, match="lab launches"):
        _render(registration, mode="production")

    model = _model(model_id=TWENTY_SEVEN_B, runtime_id=FEATURES_RUNTIME, capabilities=("chat", "thinking"))
    runtime = _runtime(runtime_id=FEATURES_RUNTIME, profile_id=CHAT_FEATURES_PROFILE, startup_args=[*BASE_FLAGS, "--jinja"])
    parsed = parse_deployment({"schema_version": 2, "runtimes": [runtime], "models": [model]})
    with pytest.raises(ContractError, match="lab-only"):
        require_production_openable(parsed.models[0], parsed.runtimes[0])


def test_a07_the_first_release_limits_chat_features_to_the_registered_27b_model() -> None:
    assert CHAT_FEATURE_MODEL_IDS == frozenset({TWENTY_SEVEN_B})
    registration = _registration("some-other-model", capabilities=("chat", "tools"))
    with pytest.raises(LaunchRenderError, match=TWENTY_SEVEN_B):
        _render(registration, "some-other-model")


def test_a07_the_independent_runtime_binds_one_profile_per_model() -> None:
    seven_b_runtime = _runtime(runtime_id=GGUF_RUNTIME, profile_id=GGUF_PROFILE, startup_args=GGUF_FLAGS)
    twenty_seven_b = _model(model_id=TWENTY_SEVEN_B, runtime_id=FEATURES_RUNTIME, capabilities=("chat", "tools", "thinking"))
    twenty_seven_b["port"] = 10003
    seven_b = _model(model_id=SEVEN_B, runtime_id=GGUF_RUNTIME, capabilities=("chat", "vision"))
    seven_b["port"] = 10002
    registration = parse_deployment({
        "schema_version": 2,
        "runtimes": [seven_b_runtime, _runtime(runtime_id=FEATURES_RUNTIME, profile_id=CHAT_FEATURES_PROFILE,
                                               startup_args=[*BASE_FLAGS, "--jinja", "--reasoning-format"])],
        "models": [twenty_seven_b, seven_b],
    })

    features = _render(registration, TWENTY_SEVEN_B)
    shared = _render(registration, SEVEN_B)

    assert features.runtime_id == FEATURES_RUNTIME and shared.runtime_id == GGUF_RUNTIME
    assert features.profile_id == CHAT_FEATURES_PROFILE and shared.profile_id == GGUF_PROFILE
    assert features.image_digest == shared.image_digest == IMAGE
    assert features.labels["io.self-model-switch.runtime"] == FEATURES_RUNTIME
    assert shared.labels["io.self-model-switch.runtime"] == GGUF_RUNTIME
    assert "--jinja" in features.argv and "--jinja" not in shared.argv
    assert "--image-max-tokens" in shared.argv and "--image-max-tokens" not in features.argv
    assert "--mmproj" in shared.argv and "--mmproj" not in features.argv


def test_a07_the_seven_b_render_only_moves_for_the_whitelisted_metadata() -> None:
    """The 7B launch is byte-identical except the deployment-derived label."""
    runtime = _runtime(runtime_id=GGUF_RUNTIME, profile_id=GGUF_PROFILE, startup_args=GGUF_FLAGS)
    seven_b = _model(model_id=SEVEN_B, runtime_id=GGUF_RUNTIME, capabilities=("chat", "vision"))
    registration = parse_deployment({"schema_version": 2, "runtimes": [runtime], "models": [seven_b]})

    before = _render(registration, SEVEN_B, config_sha256=CONFIG_SHA256)
    after = _render(registration, SEVEN_B, config_sha256="e" * 64)

    assert len(before.argv) == len(after.argv)
    moved = [index for index, (old, new) in enumerate(zip(before.argv, after.argv)) if old != new]
    assert len(moved) == 1
    index = moved[0]
    assert before.argv[index - 1] == after.argv[index - 1] == "--label"
    assert before.argv[index].startswith("io.self-model-switch.config-sha256=")
    assert after.argv[index].startswith("io.self-model-switch.config-sha256=")
    assert before.server_args == after.server_args

    envelope_moved = copy.deepcopy(registration.models[0])
    moved_spec = ModelSpec(**{**vars(envelope_moved), "envelope": type(envelope_moved.envelope)(
        **{**vars(envelope_moved.envelope), "ctx_size": 16384, "max_input_tokens": 12288})})
    changed_after = _render(
        type(registration)(runtimes=registration.runtimes, models=(moved_spec,)),
        SEVEN_B,
        config_sha256=CONFIG_SHA256,
    )
    assert changed_after.server_args != before.server_args  # the comparison above is not vacuous
