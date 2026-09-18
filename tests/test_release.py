from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import subprocess
import tarfile

from model_scheduler.deploy import render


def _input() -> dict:
    models = {}
    for name, pooling in (("embedding", "mean"), ("reranker", "rank"), ("qwen-small", None), ("qwen-large", None)):
        models[name] = {"file": f"{name}.gguf", "sha256": "a" * 64, "context_size": 2048, "parallel": 1, "pooling": pooling, "reserved_bytes": 1024, "measured": False}
    return {"deployment_id": "orin-local", "hardware": {"model": "NVIDIA Jetson AGX Orin Developer Kit", "compatible": ["nvidia,p3737-0000+p3701-0005", "nvidia,p3701-0005", "nvidia,tegra234"]}, "ssd_uuid": "uuid", "ssd_filesystem": "ext4", "jetpack_version": "7", "llama_swap_version": "v1", "llama_swap_sha256": "b" * 64, "image": "repo/image@sha256:" + "c" * 64, "validation_report": None, "models": models}


def test_release_archive_is_auditable_and_excludes_workspace_state(tmp_path: Path) -> None:
    source = tmp_path / "input.json"; source.write_text(json.dumps(_input()))
    deployment = tmp_path / "deployment"; render(source, "lab", deployment)
    output = tmp_path / "releases"
    result = subprocess.run([sys.executable, "scripts/build-release.py", "--deployment", str(deployment), "--output", str(output), "--release-id", "test-1"], text=True, capture_output=True, check=True)
    archive = output / "self-model-switch-test-1.tar.gz"
    assert archive.exists() and (output / "SHA256SUMS").exists()
    assert "self-model-switch-test-1.tar.gz" in result.stdout
    with tarfile.open(archive) as bundle:
        names = bundle.getnames()
    assert "self-model-switch-test-1/deployment/manifest.json" in names
    assert "self-model-switch-test-1/requirements.lock" in names
    assert "self-model-switch-test-1/requirements-dev.lock" in names
    assert not any(".venv" in name or name.endswith("config.yaml") for name in names if "/deployment/" not in name)


def test_release_archive_retains_a_verified_hardware_report_when_present(tmp_path: Path) -> None:
    source = tmp_path / "input.json"; source.write_text(json.dumps(_input()))
    deployment = tmp_path / "deployment"; render(source, "lab", deployment)
    report = deployment / "hardware-report.json"; report.write_text('{"schema_version":2}\n')
    manifest_path = deployment / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["hardware_report_sha256"] = hashlib.sha256(report.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    output = tmp_path / "releases"
    subprocess.run([sys.executable, "scripts/build-release.py", "--deployment", str(deployment), "--output", str(output), "--release-id", "test-2"], text=True, capture_output=True, check=True)
    with tarfile.open(output / "self-model-switch-test-2.tar.gz") as bundle:
        assert "self-model-switch-test-2/deployment/hardware-report.json" in bundle.getnames()


def test_release_archive_rejects_a_report_that_no_longer_matches_manifest_digest(tmp_path: Path) -> None:
    source = tmp_path / "input.json"; source.write_text(json.dumps(_input()))
    deployment = tmp_path / "deployment"; render(source, "lab", deployment)
    report = deployment / "hardware-report.json"; report.write_text('{"schema_version":2}\n')
    manifest_path = deployment / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["hardware_report_sha256"] = "a" * 64
    manifest_path.write_text(json.dumps(manifest))
    result = subprocess.run([sys.executable, "scripts/build-release.py", "--deployment", str(deployment), "--output", str(tmp_path / "releases"), "--release-id", "test-3"], text=True, capture_output=True)
    assert result.returncode == 78
    assert "Hardware report" in result.stderr


# ---------------------------------------------------------------------------
# P28: v3 integrity, forbidden material and the bundle hashes
# ---------------------------------------------------------------------------


def _v3_deployment(tmp_path: Path) -> Path:
    import importlib.util

    from model_scheduler.preflight_v3 import manifest_identity

    deployment = tmp_path / "deployment"
    deployment.mkdir(parents=True, exist_ok=True)
    (deployment / "config.yaml").write_text("schema_version: 2\n", encoding="utf-8")
    (deployment / "llama-swap.yaml").write_text("{}\n", encoding="utf-8")
    (deployment / "fstab.fragment").write_text("# none\n", encoding="utf-8")
    for name in ("model-scheduler.service", "llama-swap.service"):
        (deployment / name).write_text("[Unit]\n", encoding="utf-8")
    manifest = {"schema_version": 3, "mode": "production", "production": True, "deployment_id": "sms-lab",
                "candidate_sha256": "a" * 64, "source_archive_sha256": "b" * 64, "config_sha256": "c" * 64,
                "device_digest": "d" * 64,
                "models": {"qwen-small": {"container_name": "sms-sms-lab-qwen-small",
                                          "image_digest": "sha256:" + "e" * 64}},
                "evidence": {"run_id": "run-1", "started_at": "2026-09-19T00:00:00Z",
                             "ended_at": "2026-09-19T01:00:00Z", "report_sha256": "f" * 64}}
    manifest["identity_sha256"] = manifest_identity(manifest)
    (deployment / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert importlib.util is not None
    return deployment


def _build_release(deployment: Path, output: Path, release_id: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "scripts/build-release.py", "--deployment", str(deployment),
                           "--output", str(output), "--release-id", release_id],
                          text=True, capture_output=True)


def test_a_v3_release_carries_the_manifest_and_evidence_hashes(tmp_path) -> None:
    deployment = _v3_deployment(tmp_path)
    output = tmp_path / "release"

    result = _build_release(deployment, output, "v3-1")

    assert result.returncode == 0, result.stderr
    bundle = json.loads((output / "bundle.json").read_text(encoding="utf-8"))
    assert bundle["release_id"] == "v3-1" and len(bundle["archive_sha256"]) == 64
    assert bundle["manifest_sha256"] == hashlib.sha256((deployment / "manifest.json").read_bytes()).hexdigest()
    assert bundle["candidate_sha256"] == "a" * 64
    assert bundle["evidence"]["report_sha256"] == "f" * 64
    assert any(entry["relative_path"].endswith("model_scheduler/llama_swap_contract.py") for entry in bundle["files"])
    assert any(entry["relative_path"].endswith("tests/test_llama_swap_fixture.py") for entry in bundle["files"])
    assert all(entry["size_bytes"] >= 0 and len(entry["sha256"]) == 64 for entry in bundle["files"])


def test_a_lab_or_edited_v3_manifest_is_never_released(tmp_path) -> None:
    lab = _v3_deployment(tmp_path / "lab")
    manifest = json.loads((lab / "manifest.json").read_text(encoding="utf-8"))
    manifest["mode"] = "lab"
    manifest["production"] = False
    from model_scheduler.preflight_v3 import manifest_identity
    manifest["identity_sha256"] = manifest_identity(manifest)  # re-signed: the lab rule itself must fire
    (lab / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    result = _build_release(lab, tmp_path / "lab-release", "lab-1")

    assert result.returncode != 0 and "lab rendering" in result.stderr
    assert not (tmp_path / "lab-release" / "bundle.json").exists()

    edited = _v3_deployment(tmp_path / "edited")
    manifest = json.loads((edited / "manifest.json").read_text(encoding="utf-8"))
    manifest["models"]["qwen-small"]["image_digest"] = "sha256:" + "9" * 64
    (edited / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    result = _build_release(edited, tmp_path / "edited-release", "edited-1")

    assert result.returncode != 0 and "does not verify" in result.stderr


def test_weights_credentials_and_video_material_are_refused(tmp_path) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("build_release_p28", Path("scripts/build-release.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path
    candidates = [root / "models" / "qwen.gguf", root / "deploy" / ".env", root / "sms-video-pipeline" / "run.py"]

    problems = module._verify_forbidden(candidates, root)

    assert any("forbidden material" in problem for problem in problems)
    assert any("video implementation" in problem for problem in problems)
    assert len(problems) == len(candidates)
