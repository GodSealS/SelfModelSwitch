from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tarfile

from model_scheduler.deploy import render


def _input() -> dict:
    models = {}
    for name, pooling in (("embedding", "mean"), ("reranker", "rank"), ("qwen-small", None), ("qwen-large", None)):
        models[name] = {"file": f"{name}.gguf", "sha256": "a" * 64, "context_size": 2048, "parallel": 1, "pooling": pooling, "reserved_bytes": 1024, "measured": False}
    return {"deployment_id": "thor-local", "ssd_uuid": "uuid", "ssd_filesystem": "ext4", "jetpack_version": "7", "llama_swap_version": "v1", "llama_swap_sha256": "b" * 64, "image": "repo/image@sha256:" + "c" * 64, "validation_report": None, "models": models}


def test_release_archive_is_auditable_and_excludes_workspace_state(tmp_path: Path) -> None:
    source = tmp_path / "input.json"; source.write_text(json.dumps(_input()))
    deployment = tmp_path / "deployment"; render(source, "lab", deployment)
    output = tmp_path / "releases"
    result = subprocess.run([".venv/bin/python", "scripts/build-release.py", "--deployment", str(deployment), "--output", str(output), "--release-id", "test-1"], text=True, capture_output=True, check=True)
    archive = output / "self-model-switch-test-1.tar.gz"
    assert archive.exists() and (output / "SHA256SUMS").exists()
    assert "self-model-switch-test-1.tar.gz" in result.stdout
    with tarfile.open(archive) as bundle:
        names = bundle.getnames()
    assert "self-model-switch-test-1/deployment/manifest.json" in names
    assert "self-model-switch-test-1/requirements.lock" in names
    assert "self-model-switch-test-1/requirements-dev.lock" in names
    assert not any(".venv" in name or name.endswith("config.yaml") for name in names if "/deployment/" not in name)


def test_release_archive_retains_a_verified_thor_report_when_present(tmp_path: Path) -> None:
    source = tmp_path / "input.json"; source.write_text(json.dumps(_input()))
    deployment = tmp_path / "deployment"; render(source, "lab", deployment)
    (deployment / "thor-report.json").write_text('{"schema_version":1}\n')
    output = tmp_path / "releases"
    subprocess.run([".venv/bin/python", "scripts/build-release.py", "--deployment", str(deployment), "--output", str(output), "--release-id", "test-2"], text=True, capture_output=True, check=True)
    with tarfile.open(output / "self-model-switch-test-2.tar.gz") as bundle:
        assert "self-model-switch-test-2/deployment/thor-report.json" in bundle.getnames()
