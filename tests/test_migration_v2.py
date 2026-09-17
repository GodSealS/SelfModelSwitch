"""migrate-v2 must be explicit: every runtime, asset, envelope, measurement and
budget the v1 configuration cannot already state comes from the inventory, and a
partial inventory never produces a startable schema-v2 file."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from model_scheduler.config import AppConfigV2, load_config
from model_scheduler.contracts_v2 import effective_reserved_bytes
from model_scheduler.deploy import main
from model_scheduler.migration_v2 import MigrationError, migrate_v2
from run import main as check_config_main

V1_SOURCE = Path(__file__).resolve().parent.parent / "config.yaml"


def _envelope(parallel: int = 1) -> dict:
    return {
        "ctx_size": 32768,
        "max_input_tokens": 28672,
        "max_output_tokens": 256,
        "max_parallel": parallel,
        "max_image_tokens": 0,
        "max_image_edge_pixels": 0,
        "max_images": 0,
    }


def _asset(filename: str, size_bytes: int) -> dict:
    return {"role": "model", "path": filename, "sha256": "0" * 64, "size_bytes": size_bytes}


def _inventory() -> dict:
    return {
        "resources": {"model_budget_bytes": 34359738368},
        "control": {"socket_path": "/run/self-model-switch/control.sock", "allowed_uids": [1000]},
        "blobs": {"root": "/var/lib/self-model-switch/blobs"},
        "runtimes": {
            "llama-cpp-1": {
                "profile_id": "llama-cpp-gguf-v1",
                "image_digest": "registry.example/sms-runtime@sha256:" + "a" * 64,
                "adapter_sha256": "b" * 64,
                "lock_sha256": "c" * 64,
                "startup_args": ["--no-webui", "--host", "--port", "--ctx-size"],
            }
        },
        "models": {
            "embedding": {
                "runtime_id": "llama-cpp-1",
                "timeout_seconds": 900,
                "envelope": _envelope(),
                "assets": [_asset("embedding.gguf", 1000000000)],
                "measured": False,
                "measurement_ref": None,
                "physical_resident_peak_bytes": None,
            },
            "reranker": {
                "runtime_id": "llama-cpp-1",
                "timeout_seconds": 900,
                "envelope": _envelope(),
                "assets": [_asset("reranker.gguf", 2000000000)],
                "measured": False,
                "measurement_ref": None,
                "physical_resident_peak_bytes": None,
            },
            "qwen-small": {
                "runtime_id": "llama-cpp-1",
                "timeout_seconds": 900,
                "envelope": _envelope(),
                "assets": [_asset("qwen-small.gguf", 3000000000)],
                "measured": False,
                "measurement_ref": None,
                "physical_resident_peak_bytes": None,
            },
            "qwen-large": {
                "runtime_id": "llama-cpp-1",
                "timeout_seconds": 900,
                "envelope": _envelope(),
                "assets": [_asset("qwen-large.gguf", 4000000000)],
                "measured": False,
                "measurement_ref": None,
                "physical_resident_peak_bytes": None,
            },
        },
    }


def _write_json(tmp_path: Path, name: str, document: dict) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _v1_source(tmp_path: Path, mutate=None) -> Path:
    document = yaml.safe_load(V1_SOURCE.read_text(encoding="utf-8"))
    if mutate is not None:
        mutate(document)
    path = tmp_path / "config-v1.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def _pin_qwen_small(document: dict) -> None:
    model = document["models"]["qwen-small"]
    model["scheduling"].update(pinned=True, evictable=False)
    model["lifecycle"].update(preload=True, ttl_seconds=0)


def test_complete_inventory_migrates_the_legacy_four_ids(tmp_path: Path) -> None:
    source = _v1_source(tmp_path)
    before = source.read_bytes()
    output = tmp_path / "scheduler-v2.yaml"

    document = migrate_v2(source, _write_json(tmp_path, "inventory.json", _inventory()), output)

    assert document["schema_version"] == 2
    assert source.read_bytes() == before
    config = load_config(output)
    assert isinstance(config, AppConfigV2)
    assert sorted(config.models) == ["embedding", "qwen-large", "qwen-small", "reranker"]
    assert config.models["embedding"].reserved_bytes == effective_reserved_bytes(4294967296, legacy_v1_margin=0.15)
    assert config.models["qwen-large"].capabilities == ("chat",)
    assert config.models["embedding"].capabilities == ("embeddings",)
    assert config.resources.model_budget_bytes == 34359738368
    assert config.blobs.root == Path("/var/lib/self-model-switch/blobs")
    assert config.control.allowed_uids == (1000,)
    assert config.candidate_sha256 is None


def test_migration_keeps_pinned_and_preload_decisions(tmp_path: Path) -> None:
    source = _v1_source(tmp_path, _pin_qwen_small)
    output = tmp_path / "scheduler-v2.yaml"

    migrate_v2(source, _write_json(tmp_path, "inventory.json", _inventory()), output)
    config = load_config(output)

    assert config.scheduler.pinned_models == ("embedding", "qwen-small")
    assert config.scheduler.preload_models == ("embedding", "qwen-small")


def test_migration_carries_the_legacy_scheduler_and_storage_policy(tmp_path: Path) -> None:
    output = tmp_path / "scheduler-v2.yaml"

    migrate_v2(_v1_source(tmp_path), _write_json(tmp_path, "inventory.json", _inventory()), output)
    config = load_config(output)

    assert config.scheduler.heat.half_life_seconds == 1800
    assert config.scheduler.thrash.max_switches_in_window == 3
    assert config.scheduler.resource_safety_margin == 0.15
    assert config.storage.expected_uuid == "REQUIRED_REAL_UUID"
    assert config.gateway.inference_timeout_seconds == 900


def test_migrated_config_passes_the_check_config_entrypoint(tmp_path: Path, capsys) -> None:
    output = tmp_path / "scheduler-v2.yaml"
    migrate_v2(_v1_source(tmp_path), _write_json(tmp_path, "inventory.json", _inventory()), output)

    assert check_config_main(["--config", str(output), "--check-config"]) == 0
    printed = capsys.readouterr().out
    assert "schema_version=2" in printed
    assert "embedding,qwen-large,qwen-small,reranker" in printed


def test_the_v1_source_still_passes_check_config(tmp_path: Path, capsys) -> None:
    source = _v1_source(tmp_path)

    assert check_config_main(["--config", str(source), "--check-config"]) == 0
    assert "schema_version=1" in capsys.readouterr().out


def test_refuses_to_overwrite_an_existing_output(tmp_path: Path) -> None:
    output = tmp_path / "scheduler-v2.yaml"
    output.write_text("schema_version: 2\n", encoding="utf-8")

    with pytest.raises(MigrationError, match="already exists"):
        migrate_v2(_v1_source(tmp_path), _write_json(tmp_path, "inventory.json", _inventory()), output)
    assert output.read_text(encoding="utf-8") == "schema_version: 2\n"


def test_rejects_an_input_that_is_already_schema_v2(tmp_path: Path) -> None:
    first = tmp_path / "scheduler-v2.yaml"
    migrate_v2(_v1_source(tmp_path), _write_json(tmp_path, "inventory.json", _inventory()), first)

    with pytest.raises(MigrationError, match="schema_version 2"):
        migrate_v2(first, _write_json(tmp_path, "inventory.json", _inventory()), tmp_path / "second.yaml")


def test_missing_inventory_exits_two_and_writes_nothing(tmp_path: Path, capsys) -> None:
    inventory = _inventory()
    del inventory["models"]["qwen-small"]["timeout_seconds"]
    del inventory["models"]["qwen-large"]
    del inventory["resources"]["model_budget_bytes"]
    del inventory["models"]["embedding"]["assets"][0]["size_bytes"]
    output = tmp_path / "scheduler-v2.yaml"
    inventory_path = _write_json(tmp_path, "inventory.json", inventory)

    assert main(["migrate-v2", "--input", str(_v1_source(tmp_path)), "--inventory", str(inventory_path), "--output", str(output)]) == 2
    report = json.loads(capsys.readouterr().out)

    assert "models.qwen-small.timeout_seconds" in report["missing"]
    assert "models.qwen-large" in report["missing"]
    assert "resources.model_budget_bytes" in report["missing"]
    assert "models.embedding.assets[0].size_bytes" in report["missing"]
    assert not output.exists()


def test_missing_runtime_details_are_reported(tmp_path: Path, capsys) -> None:
    inventory = _inventory()
    del inventory["runtimes"]["llama-cpp-1"]["image_digest"]
    del inventory["runtimes"]["llama-cpp-1"]["profile_id"]
    del inventory["models"]["reranker"]["runtime_id"]
    source = _v1_source(tmp_path)
    inventory_path = _write_json(tmp_path, "inventory.json", inventory)

    assert main(["migrate-v2", "--input", str(source), "--inventory", str(inventory_path), "--output", str(tmp_path / "out.yaml")]) == 2
    report = json.loads(capsys.readouterr().out)

    assert "runtimes.llama-cpp-1.image_digest" in report["missing"]
    assert "runtimes.llama-cpp-1.profile_id" in report["missing"]
    assert "models.reranker.runtime_id" in report["missing"]
    assert not (tmp_path / "out.yaml").exists()


def test_an_absent_runtime_table_reports_every_referenced_runtime(tmp_path: Path, capsys) -> None:
    inventory = _inventory()
    del inventory["runtimes"]
    inventory_path = _write_json(tmp_path, "inventory.json", inventory)

    assert main(["migrate-v2", "--input", str(_v1_source(tmp_path)), "--inventory", str(inventory_path), "--output", str(tmp_path / "out.yaml")]) == 2
    report = json.loads(capsys.readouterr().out)

    assert "runtimes.llama-cpp-1" in report["missing"]


def test_inventory_conflicting_with_the_v1_file_exits_two(tmp_path: Path, capsys) -> None:
    inventory = _inventory()
    inventory["models"]["embedding"]["assets"][0]["sha256"] = "f" * 64
    inventory["models"]["embedding"]["assets"][0]["path"] = "renamed.gguf"
    inventory["models"]["qwen-small"]["envelope"]["max_parallel"] = 4
    inventory["models"]["ghost"] = inventory["models"]["embedding"]
    inventory_path = _write_json(tmp_path, "inventory.json", inventory)
    output = tmp_path / "scheduler-v2.yaml"

    assert main(["migrate-v2", "--input", str(_v1_source(tmp_path)), "--inventory", str(inventory_path), "--output", str(output)]) == 2
    report = json.loads(capsys.readouterr().out)

    assert "models.embedding.assets[0].sha256" in report["conflicts"]
    assert "models.embedding.assets[0].path" in report["conflicts"]
    assert "models.qwen-small.envelope.max_parallel" in report["conflicts"]
    assert "models.ghost" in report["conflicts"]
    assert not output.exists()


def test_measured_inventory_requires_the_measurement_material(tmp_path: Path, capsys) -> None:
    inventory = _inventory()
    inventory["models"]["qwen-small"]["measured"] = True
    inventory["models"]["qwen-small"]["measurement_ref"] = "d" * 64
    inventory["models"]["reranker"]["measured"] = True
    inventory_path = _write_json(tmp_path, "inventory.json", inventory)
    output = tmp_path / "scheduler-v2.yaml"

    assert main(["migrate-v2", "--input", str(_v1_source(tmp_path)), "--inventory", str(inventory_path), "--output", str(output)]) == 2
    report = json.loads(capsys.readouterr().out)

    assert "models.qwen-small.physical_resident_peak_bytes" in report["missing"]
    assert "models.reranker.measurement_ref" in report["missing"]
    assert not output.exists()


def test_a_measured_inventory_is_carried_into_the_registration(tmp_path: Path) -> None:
    inventory = _inventory()
    inventory["models"]["qwen-small"].update(
        measured=True, measurement_ref="d" * 64, physical_resident_peak_bytes=8000000000
    )
    output = tmp_path / "scheduler-v2.yaml"

    migrate_v2(_v1_source(tmp_path), _write_json(tmp_path, "inventory.json", inventory), output)
    model = load_config(output).models["qwen-small"]

    assert model.measured is True
    assert model.measurement_ref == "d" * 64
    assert model.physical_resident_peak_bytes == 8000000000
