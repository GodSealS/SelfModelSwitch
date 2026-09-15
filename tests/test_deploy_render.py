from __future__ import annotations

import json

import pytest

from model_scheduler.deploy import DeployError, render


def input_data(measured: bool = False) -> dict:
    models = {}
    for name, file, pooling in (("embedding", "embedding.gguf", "mean"), ("reranker", "reranker.gguf", "rank"), ("qwen-small", "qwen-small.gguf", None), ("qwen-large", "qwen-large.gguf", None)):
        models[name] = {"file": file, "sha256": "a" * 64, "context_size": 2048, "parallel": 1, "pooling": pooling, "reserved_bytes": 1024, "measured": measured}
    return {"deployment_id": "thor-local", "ssd_uuid": "uuid", "ssd_filesystem": "ext4", "jetpack_version": "7", "llama_swap_version": "v1", "llama_swap_sha256": "b" * 64, "image": "repo/image@sha256:" + "c" * 64, "validation_report": None, "models": models}


def test_lab_render_writes_one_manifest(tmp_path) -> None:
    source = tmp_path / "input.json"; source.write_text(json.dumps(input_data()))
    output = tmp_path / "out"
    manifest = render(source, "lab", output)
    assert manifest["deployment_id"] == "thor-local"
    assert json.loads((output / "manifest.json").read_text())["models"]["embedding"]["file"] == "embedding.gguf"


def test_production_rejects_unmeasured_or_placeholder_input(tmp_path) -> None:
    source = tmp_path / "input.json"; source.write_text(json.dumps(input_data(False)))
    with pytest.raises(DeployError, match="measured"):
        render(source, "production", tmp_path / "out")
