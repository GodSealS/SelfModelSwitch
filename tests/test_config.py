from __future__ import annotations

from pathlib import Path

import pytest

from model_scheduler.config import ConfigError, load_config


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
