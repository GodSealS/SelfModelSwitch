from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from model_scheduler.config import AppConfigV2, ConfigError, config_digest, load_config, model_specs


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


VALID = """
schema_version: 1
server:
  host: 127.0.0.1
  port: 8090
  workers: 1
  max_request_body_bytes: 4194304
  body_timeout_seconds: 10
  shutdown_grace_seconds: 30
llama_swap:
  base_url: http://127.0.0.1:8080
  connect_timeout_seconds: 5
  control_timeout_seconds: 30
  load_timeout_seconds: 900
  unload_timeout_seconds: 45
scheduler:
  poll_interval_seconds: 2
  request_queue_timeout_seconds: 1800
  queue_capacity: 128
  priority_aging_seconds: 30
  switch_drain_timeout_seconds: 30
  switch_retry_seconds: 30
  resource_safety_margin: 0.15
  min_free_memory_bytes: 2147483648
  max_evictions_per_request: 8
  memory_reclaim_timeout_seconds: 10
  heat: {half_life_seconds: 1800, request_weight: 1.0, token_weight: 0.0001}
  thrash: {switch_window_seconds: 10, max_switches_in_window: 3, cooldown_seconds: 15}
resources:
  provider: psutil
  system_reserve_bytes: 8589934592
  sample_interval_seconds: 1
  sample_max_age_seconds: 2
storage:
  mount_path: /mnt/model-ssd
  model_directory: /mnt/model-ssd/models
  expected_uuid: 11111111-2222-3333-4444-555555555555
  filesystem: ext4
gateway:
  connect_timeout_seconds: 5
  pool_timeout_seconds: 5
  read_idle_timeout_seconds: 60
  write_idle_timeout_seconds: 60
  inference_timeout_seconds: 900
  close_timeout_seconds: 5
  max_response_body_bytes: 16777216
  max_sse_event_bytes: 1048576
models:
  embedding:
    upstream_url: http://127.0.0.1:10001
    capabilities: [embeddings]
    file: embedding.gguf
    sha256: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    container_name: sms-thor-local-embedding
    memory: {reserved_bytes: 4294967296}
    scheduling: {priority: 100, max_concurrency: 1, evictable: false, pinned: true}
    lifecycle: {preload: true, ttl_seconds: 0}
  reranker:
    upstream_url: http://127.0.0.1:10002
    capabilities: [rerank]
    file: reranker.gguf
    sha256: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    container_name: sms-thor-local-reranker
    memory: {reserved_bytes: 6442450944}
    scheduling: {priority: 80, max_concurrency: 1, evictable: true, pinned: false}
    lifecycle: {preload: false, ttl_seconds: 900}
  qwen-small:
    upstream_url: http://127.0.0.1:10003
    capabilities: [chat]
    file: qwen-small.gguf
    sha256: "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    container_name: sms-thor-local-qwen-small
    memory: {reserved_bytes: 10737418240}
    scheduling: {priority: 50, max_concurrency: 1, evictable: true, pinned: false}
    lifecycle: {preload: false, ttl_seconds: 900}
  qwen-large:
    upstream_url: http://127.0.0.1:10004
    capabilities: [chat]
    file: qwen-large.gguf
    sha256: "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
    container_name: sms-thor-local-qwen-large
    memory: {reserved_bytes: 32212254720}
    scheduling: {priority: 40, max_concurrency: 1, evictable: true, pinned: false}
    lifecycle: {preload: false, ttl_seconds: 1800}
"""


def test_loads_strict_v1_lab_config(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, VALID))

    assert config.schema_version == 1
    assert config.server.host == "127.0.0.1"
    assert config.models["embedding"].scheduling.pinned is True
    assert config.models["embedding"].capabilities == frozenset({"embeddings"})


def test_v1_capability_set_stays_closed_to_the_new_model_capabilities(tmp_path: Path) -> None:
    # TC01: schema-v1 has no envelope and keeps its own closed set, so the new
    # model capabilities (and vision) are not registrable there.
    for capability in ("tools", "thinking", "vision"):
        text = VALID.replace("capabilities: [chat]", f"capabilities: [chat, {capability}]")
        assert f"chat, {capability}" in text
        with pytest.raises(ConfigError, match="capabilities"):
            load_config(write_config(tmp_path, text))


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ("schema_version: 1\n", "schema_version"),
        ("workers: 1", "workers"),
        ("host: 127.0.0.1", "host"),
        ("pinned: true", "pinned"),
        ("provider: psutil", "provider"),
    ],
)
def test_rejects_missing_or_invalid_legacy_shape(
    tmp_path: Path, replacement: str, message: str
) -> None:
    if replacement.startswith("schema_version"):
        text = VALID.replace(replacement, "")
    elif replacement.startswith("workers"):
        text = VALID.replace(replacement, "workers: 2")
    elif replacement.startswith("host"):
        text = VALID.replace(replacement, "host: 0.0.0.0")
    elif replacement.startswith("pinned"):
        text = VALID.replace(replacement, "pinned: 'false'")
    else:
        text = VALID.replace(replacement, "provider: auto")

    with pytest.raises(ConfigError, match=message):
        load_config(write_config(tmp_path, text))


def test_rejects_duplicate_or_unknown_yaml_keys(tmp_path: Path) -> None:
    duplicate = VALID.replace("  port: 8090", "  port: 8090\n  port: 9090")
    unknown = VALID.replace("  port: 8090", "  port: 8090\n  surprise: true")

    with pytest.raises(ConfigError, match="duplicate"):
        load_config(write_config(tmp_path, duplicate))
    with pytest.raises(ConfigError, match="unknown"):
        load_config(write_config(tmp_path, unknown))


def test_rejects_pinned_model_with_evictable_or_nonpreload(tmp_path: Path) -> None:
    invalid = VALID.replace("evictable: false", "evictable: true").replace(
        "preload: true", "preload: false"
    )

    with pytest.raises(ConfigError, match="pinned"):
        load_config(write_config(tmp_path, invalid))


def test_model_specs_preserve_capabilities_and_preload_intent(tmp_path: Path) -> None:
    specs = model_specs(load_config(write_config(tmp_path, VALID)))
    assert specs["embedding"].preload is True
    assert specs["embedding"].pinned is True
    assert specs["qwen-small"].capabilities == frozenset({"chat"})


# The tool-calling extension's public software fixture (plan/tool-calling-and-reasoning
# acceptance §4): the same v2 shape, with the envelope the A-cases pin —
# ctx 8192, input 4096, output 1024, one image. Existing behaviour is untouched:
# this constant is additive, `V2` keeps the envelope its own tests were written for.
V2_CHAT_FIXTURE = """
schema_version: 2
registration:
  runtimes:
    - runtime_id: llama-cpp-1
      profile_id: llama-cpp-gguf-v1
      image_digest: registry.example/sms-runtime@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
      adapter_sha256: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
      lock_sha256: cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
      startup_args: ["--no-webui", "--host", "--port", "--ctx-size"]
  models:
    - model_id: qwen-small
      runtime_id: llama-cpp-1
      capabilities: [chat, vision]
      assets:
        - {role: model, path: qwen-small.gguf, sha256: eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee, size_bytes: 2000000000}
        - {role: projector, path: mmproj.gguf, sha256: ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff, size_bytes: 1000000}
      port: 10002
      envelope: {ctx_size: 8192, max_input_tokens: 4096, max_output_tokens: 1024, max_parallel: 1, max_image_tokens: 1280, max_image_edge_pixels: 1024, max_images: 1}
      timeout_seconds: 900
      reserved_bytes: 6106148045
      measured: false
      measurement_ref: null
      physical_resident_peak_bytes: null
server:
  host: 127.0.0.1
  port: 8099
  workers: 1
  max_request_body_bytes: 4194304
  body_timeout_seconds: 10
  shutdown_grace_seconds: 5
blobs:
  root: /tmp/sms-blobs
  chunk_reserve_bytes: 1048576
  input_ttl_seconds: 60
  output_ttl_seconds: 60
  max_blob_bytes: 1048576
  min_free_disk_bytes: 1048576
  owner_quota_bytes: 1048576
  total_quota_bytes: 1048576
control:
  allowed_uids: [1000]
  peer_group: null
  socket_path: /tmp/sms-control/control.sock
gateway:
  connect_timeout_seconds: 5
  read_idle_timeout_seconds: 5
  write_idle_timeout_seconds: 5
  pool_timeout_seconds: 5
  inference_timeout_seconds: 60
  max_response_body_bytes: 1048576
  max_sse_event_bytes: 65536
  close_timeout_seconds: 5
resources:
  provider: psutil
  sample_interval_seconds: 1
  sample_max_age_seconds: 2
  system_reserve_bytes: 1048576
  model_budget_bytes: 1048576
scheduler:
  poll_interval_seconds: 1
  queue_capacity: 4
  request_queue_timeout_seconds: 30
  max_evictions_per_request: 1
  memory_reclaim_timeout_seconds: 5
  min_free_memory_bytes: 1048576
  resource_safety_margin: 0.1
  switch_drain_timeout_seconds: 5
  switch_retry_seconds: 5
  priority_aging_seconds: 30
  heat: {half_life_seconds: 60, request_weight: 1.0, token_weight: 0.0001}
  thrash: {switch_window_seconds: 10, max_switches_in_window: 3, cooldown_seconds: 5}
  sessions: {queue_capacity: 4, queue_timeout_seconds: 30, ttl_seconds: 30, heartbeat_seconds: 5, prepare_limit_seconds: 30, hard_timeout_seconds: 60, cleanup_limit_seconds: 10, stop_grace_seconds: 5}
storage:
  mount_path: /tmp/sms-models
  model_directory: /tmp/sms-models/models
  filesystem: ext4
  expected_uuid: 11111111-2222-3333-4444-555555555555
"""

V2 = """
schema_version: 2
registration:
  runtimes:
    - runtime_id: llama-cpp-1
      profile_id: llama-cpp-gguf-v1
      image_digest: registry.example/sms-runtime@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
      adapter_sha256: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
      lock_sha256: cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
      startup_args: ["--no-webui", "--host", "--port", "--ctx-size"]
  models:
    - model_id: embedding
      runtime_id: llama-cpp-1
      capabilities: [embeddings]
      assets:
        - {role: model, path: embedding.gguf, sha256: dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd, size_bytes: 1000000000}
      port: 10001
      envelope: {ctx_size: 8192, max_input_tokens: 4096, max_output_tokens: 1, max_parallel: 1, max_image_tokens: 0, max_image_edge_pixels: 0, max_images: 0}
      timeout_seconds: 900
      reserved_bytes: 6106148045
      measured: false
      measurement_ref: null
      physical_resident_peak_bytes: null
    - model_id: qwen-small
      runtime_id: llama-cpp-1
      capabilities: [chat]
      assets:
        - {role: model, path: qwen-small.gguf, sha256: eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee, size_bytes: 2000000000}
      port: 10002
      envelope: {ctx_size: 32768, max_input_tokens: 28672, max_output_tokens: 4096, max_parallel: 2, max_image_tokens: 0, max_image_edge_pixels: 0, max_images: 0}
      timeout_seconds: 900
      reserved_bytes: 6106148045
      measured: false
      measurement_ref: null
      physical_resident_peak_bytes: null
server:
  host: 127.0.0.1
  port: 8090
  workers: 1
  max_request_body_bytes: 4194304
  body_timeout_seconds: 10
  shutdown_grace_seconds: 30
scheduler:
  poll_interval_seconds: 2
  request_queue_timeout_seconds: 1800
  queue_capacity: 128
  priority_aging_seconds: 30
  switch_drain_timeout_seconds: 30
  switch_retry_seconds: 30
  resource_safety_margin: 0.15
  min_free_memory_bytes: 2147483648
  max_evictions_per_request: 8
  memory_reclaim_timeout_seconds: 10
  pinned_models: [embedding]
  preload_models: [embedding]
  heat: {half_life_seconds: 1800, request_weight: 1.0, token_weight: 0.0001}
  thrash: {switch_window_seconds: 10, max_switches_in_window: 3, cooldown_seconds: 15}
  sessions: {queue_capacity: 128, queue_timeout_seconds: 1800, prepare_limit_seconds: 900, heartbeat_seconds: 10, ttl_seconds: 30, stop_grace_seconds: 30, cleanup_limit_seconds: 60, hard_timeout_seconds: 3600}
resources:
  provider: psutil
  system_reserve_bytes: 8589934592
  sample_interval_seconds: 1
  sample_max_age_seconds: 2
  model_budget_bytes: 34359738368
storage:
  mount_path: /mnt/model-ssd
  model_directory: /mnt/model-ssd/models
  expected_uuid: 11111111-2222-3333-4444-555555555555
  filesystem: ext4
  verify_timeout_seconds: 900
gateway:
  connect_timeout_seconds: 5
  pool_timeout_seconds: 5
  read_idle_timeout_seconds: 60
  write_idle_timeout_seconds: 60
  inference_timeout_seconds: 900
  close_timeout_seconds: 5
  max_response_body_bytes: 16777216
  max_sse_event_bytes: 1048576
control:
  socket_path: /run/self-model-switch/control.sock
  allowed_uids: [1000, 1001]
  peer_group: sms-client
blobs:
  root: /var/lib/self-model-switch/blobs
  owner_quota_bytes: 4294967296
  total_quota_bytes: 17179869184
  chunk_reserve_bytes: 1073741824
  min_free_disk_bytes: 2147483648
  input_ttl_seconds: 86400
  output_ttl_seconds: 86400
  max_blob_bytes: 1073741824
candidate_sha256: null
"""


def v2_document() -> dict:
    return yaml.safe_load(V2)


def v2_text(document: dict) -> str:
    return yaml.safe_dump(document, sort_keys=False)


def test_loads_dynamic_v2_registration(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, V2))

    assert isinstance(config, AppConfigV2)
    assert config.schema_version == 2
    assert sorted(config.models) == ["embedding", "qwen-small"]
    assert sorted(config.runtimes) == ["llama-cpp-1"]
    assert config.models["embedding"].runtime_id == "llama-cpp-1"
    assert config.models["embedding"].envelope.max_input_tokens == 4096


@pytest.mark.parametrize(
    "section",
    ["registration", "server", "scheduler", "resources", "storage", "gateway", "control", "blobs"],
)
def test_v2_requires_every_top_level_section(tmp_path: Path, section: str) -> None:
    document = v2_document()
    del document[section]

    with pytest.raises(ConfigError, match=section):
        load_config(write_config(tmp_path, v2_text(document)))


def test_v2_rejects_unknown_top_level_keys(tmp_path: Path) -> None:
    document = v2_document()
    document["llama_swap"] = {}

    with pytest.raises(ConfigError, match="unknown"):
        load_config(write_config(tmp_path, v2_text(document)))


def test_v2_accepts_only_null_or_bound_candidate_sha256(tmp_path: Path) -> None:
    bound = v2_document()
    bound["candidate_sha256"] = "a" * 64
    assert load_config(write_config(tmp_path, v2_text(bound))).candidate_sha256 == "a" * 64

    draft = v2_document()
    del draft["candidate_sha256"]
    assert load_config(write_config(tmp_path, v2_text(draft))).candidate_sha256 is None

    malformed = v2_document()
    malformed["candidate_sha256"] = "not-a-digest"
    with pytest.raises(ConfigError, match="candidate_sha256"):
        load_config(write_config(tmp_path, v2_text(malformed)))


def test_config_digest_excludes_the_derived_candidate_sha256() -> None:
    draft = v2_document()
    bound = {**draft, "candidate_sha256": "b" * 64}

    assert config_digest(draft) == config_digest(bound)
    assert len(config_digest(draft)) == 64


def test_v2_keeps_legacy_scheduler_heat_thrash_and_free_floor(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, V2))

    assert config.scheduler.heat.half_life_seconds == 1800
    assert config.scheduler.thrash.max_switches_in_window == 3
    assert config.scheduler.min_free_memory_bytes == 2147483648
    assert config.scheduler.poll_interval_seconds == 2


def test_v2_session_and_storage_defaults_are_explicit(tmp_path: Path) -> None:
    document = v2_document()
    del document["scheduler"]["sessions"]
    del document["storage"]["verify_timeout_seconds"]
    config = load_config(write_config(tmp_path, v2_text(document)))

    assert config.scheduler.sessions.queue_capacity == 128
    assert config.scheduler.sessions.queue_timeout_seconds == 1800
    assert config.scheduler.sessions.prepare_limit_seconds == 900
    assert config.scheduler.sessions.hard_timeout_seconds == 3600
    assert config.storage.verify_timeout_seconds == 900


def test_v2_control_and_blob_defaults_are_explicit(tmp_path: Path) -> None:
    document = v2_document()
    del document["control"]["socket_path"]
    del document["control"]["peer_group"]
    for key in ("owner_quota_bytes", "total_quota_bytes", "chunk_reserve_bytes", "max_blob_bytes"):
        del document["blobs"][key]
    config = load_config(write_config(tmp_path, v2_text(document)))

    assert config.control.socket_path == Path("/run/self-model-switch/control.sock")
    assert config.control.peer_group is None
    assert config.blobs.owner_quota_bytes == 4294967296
    assert config.blobs.total_quota_bytes == 17179869184
    assert config.blobs.max_blob_bytes == 1073741824


def test_v2_requires_the_explicit_budget_and_blob_root(tmp_path: Path) -> None:
    without_budget = v2_document()
    del without_budget["resources"]["model_budget_bytes"]
    with pytest.raises(ConfigError, match="model_budget_bytes"):
        load_config(write_config(tmp_path, v2_text(without_budget)))

    without_root = v2_document()
    del without_root["blobs"]["root"]
    with pytest.raises(ConfigError, match="root"):
        load_config(write_config(tmp_path, v2_text(without_root)))


def test_v2_pinned_models_must_be_registered_and_preloaded(tmp_path: Path) -> None:
    not_preloaded = v2_document()
    not_preloaded["scheduler"]["pinned_models"] = ["embedding", "qwen-small"]
    with pytest.raises(ConfigError, match="preload"):
        load_config(write_config(tmp_path, v2_text(not_preloaded)))

    unknown = v2_document()
    unknown["scheduler"]["pinned_models"] = ["ghost"]
    unknown["scheduler"]["preload_models"] = ["ghost"]
    with pytest.raises(ConfigError, match="ghost"):
        load_config(write_config(tmp_path, v2_text(unknown)))


def test_v2_registration_violations_are_rejected(tmp_path: Path) -> None:
    unknown_runtime = v2_document()
    unknown_runtime["registration"]["models"][0]["runtime_id"] = "missing"
    with pytest.raises(ConfigError, match="unknown runtime"):
        load_config(write_config(tmp_path, v2_text(unknown_runtime)))

    duplicate_port = v2_document()
    duplicate_port["registration"]["models"][1]["port"] = 10001
    with pytest.raises(ConfigError, match="duplicate"):
        load_config(write_config(tmp_path, v2_text(duplicate_port)))


def test_v2_rejects_a_model_port_that_reuses_the_server_port(tmp_path: Path) -> None:
    document = v2_document()
    document["registration"]["models"][0]["port"] = 8090

    with pytest.raises(ConfigError, match="port"):
        load_config(write_config(tmp_path, v2_text(document)))
