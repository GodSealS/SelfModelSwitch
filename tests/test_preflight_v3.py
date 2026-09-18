"""P26: the two-layer preflight blocks a mismatch before any model is started.

Layer 1 compares the manifest with the live site and never loads anything;
layer 2 re-checks the full evidence set through the offline verifier. The
manifest identity block is tamper-evident, and the runtime runner verifies it on
every load so a replaced image cannot be launched.
"""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from model_scheduler import preflight_v3 as pf
from model_scheduler import deploy


def _helpers(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_for_p26", Path(__file__).resolve().parent / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rendered(tmp_path: Path, *, mode: str = "production"):
    render_helpers = _helpers("test_deploy_render")
    candidate_path, evidence, _model_directory = render_helpers._p26_verified_site(tmp_path)
    output = tmp_path / ("lab" if mode == "lab" else "deploy")
    deploy.render_v3(candidate_path=candidate_path, evidence_dir=evidence, output=output, mode=mode,
                     model_directory=tmp_path / "ssd" / "models",
                     temporary_budget_bytes=16_000_000_000 if mode == "lab" else None)
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    return manifest, candidate_path, evidence


def _site_for(manifest: dict) -> dict:
    site = {field: manifest["device"][field] for field in
            ("machine_id_sha256", "architecture", "device_tree_sha256", "mem_total_bytes", "kernel_release",
             "model_disk_uuid", "scratch_disk_uuid")}
    site["config_sha256"] = manifest["config_sha256"]
    site["source_archive_sha256"] = manifest["source_archive_sha256"]
    site["filesystem"] = "ext4"
    site["images"] = {entry["image_digest"]: True for entry in manifest["models"].values()}
    site["model_files"] = {asset["path"]: {"size_bytes": asset["size_bytes"], "sha256": asset["sha256"]}
                           for entry in manifest["models"].values() for asset in entry["assets"]}
    return site


def test_a_matching_site_passes_layer_one_without_loading_a_model(tmp_path) -> None:
    manifest, _candidate, _evidence = _rendered(tmp_path)

    report = pf.verify_environment(manifest, site=_site_for(manifest))

    assert report["ok"] is True and report["problems"] == []
    assert report["loaded_models"] == 0  # layer 1 never starts anything


def test_every_site_mismatch_blocks_before_a_load(tmp_path) -> None:
    manifest, _candidate, _evidence = _rendered(tmp_path)
    site = _site_for(manifest)

    for field in ("machine_id_sha256", "architecture", "device_tree_sha256", "mem_total_bytes", "kernel_release",
                  "model_disk_uuid", "scratch_disk_uuid", "config_sha256", "source_archive_sha256"):
        broken = dict(site)
        broken[field] = "0" * 64 if field.endswith("sha256") else "mismatch"
        report = pf.verify_environment(manifest, site=broken)
        assert report["ok"] is False and report["problems"], field
        assert report["loaded_models"] == 0

    missing_image = dict(site, images={})
    assert pf.verify_environment(manifest, site=missing_image)["ok"] is False

    tampered_asset = json.loads(json.dumps(site))
    first = sorted(tampered_asset["model_files"])[0]
    tampered_asset["model_files"][first] = {"size_bytes": 1, "sha256": "0" * 64}
    report = pf.verify_environment(manifest, site=tampered_asset)
    assert report["ok"] is False and any("size/hash differs" in problem for problem in report["problems"])

    expired = json.loads(json.dumps(manifest))
    expired["evidence"]["started_at"] = "2026-09-01T00:00:00Z"
    report = pf.verify_environment(expired, site=site, now=datetime(2026, 9, 19, tzinfo=timezone.utc))
    assert report["ok"] is False and any("expired" in problem for problem in report["problems"])


def test_a_lab_manifest_is_never_a_production_preflight(tmp_path) -> None:
    manifest, _candidate, _evidence = _rendered(tmp_path, mode="lab")

    report = pf.verify_environment(manifest, site=_site_for(manifest))

    assert report["ok"] is False and any("lab manifest" in problem for problem in report["problems"])
    assert report["loaded_models"] == 0


def test_the_manifest_identity_block_is_tamper_evident(tmp_path) -> None:
    manifest, _candidate, _evidence = _rendered(tmp_path)

    edited = json.loads(json.dumps(manifest))
    model_id = sorted(edited["models"])[0]
    edited["models"][model_id]["image_digest"] = "sha256:" + "b" * 64  # replace the accepted image
    with pytest.raises(pf.PreflightError, match="was edited") as refused:
        pf.require_manifest_identity(edited, model_id=model_id)
    assert refused.value.exit_code == 3

    no_identity = json.loads(json.dumps(manifest))
    del no_identity["identity_sha256"]
    with pytest.raises(pf.PreflightError, match="does not match its content"):
        pf.require_manifest_identity(no_identity)

    with pytest.raises(pf.PreflightError, match="not part of this manifest"):
        pf.require_manifest_identity(manifest, model_id="ghost-model")


def test_the_gate_rechecks_the_full_evidence_set(tmp_path) -> None:
    manifest, candidate_path, evidence = _rendered(tmp_path)

    ok = pf.production_gate(manifest, candidate_path=candidate_path, evidence_dir=evidence, site=_site_for(manifest))
    assert ok["ok"] is True and ok["loaded_models"] == 0 and ok["evidence_verdict"] == "passed"

    helpers = _helpers("test_verify")
    broken = helpers._build_evidence(tmp_path / "broken", candidate_path, drop_final="S04")
    refused = pf.production_gate(manifest, candidate_path=candidate_path, evidence_dir=broken,
                                 site=_site_for(manifest))
    assert refused["ok"] is False and any("does not verify offline" in problem for problem in refused["problems"])

    missing = pf.production_gate(manifest, candidate_path=candidate_path, evidence_dir=tmp_path / "nowhere",
                                 site=_site_for(manifest))
    assert missing["ok"] is False and missing["loaded_models"] == 0

    lab_manifest, _candidate, _evidence = _rendered(tmp_path / "lab-site", mode="lab")
    lab = pf.production_gate(lab_manifest, candidate_path=candidate_path, evidence_dir=evidence,
                             site=_site_for(lab_manifest))
    assert lab["ok"] is False and any("lab manifest" in problem for problem in lab["problems"])


def test_the_runtime_runner_verifies_the_manifest_identity_on_every_load(tmp_path, monkeypatch) -> None:
    spec = importlib.util.spec_from_file_location("model_runner_for_p26",
                                                  Path(__file__).resolve().parent.parent / "deploy" / "model-runner.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    manifest, _candidate, _evidence = _rendered(tmp_path)
    manifest_path = tmp_path / "etc-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(runner, "MANIFEST", manifest_path)

    runner._require_v3_identity(sorted(manifest["models"])[0])  # a faithful manifest passes

    edited = json.loads(json.dumps(manifest))
    model_id = sorted(edited["models"])[0]
    edited["models"][model_id]["image_digest"] = "sha256:" + "b" * 64
    manifest_path.write_text(json.dumps(edited), encoding="utf-8")
    with pytest.raises(runner.RunnerError, match="was edited"):
        runner._require_v3_identity(model_id)

    legacy = {"deployment_id": "x", "config_sha256": "c" * 64, "models": {}}
    manifest_path.write_text(json.dumps(legacy), encoding="utf-8")
    runner._require_v3_identity("embedding")  # a legacy manifest is not this path's business


def test_the_preflight_cli_runs_layer_one_and_the_gate(tmp_path, capsys) -> None:
    manifest, candidate_path, evidence = _rendered(tmp_path)
    manifest_path = tmp_path / "deploy" / "manifest.json"

    code = deploy.main(["preflight", "--manifest", str(manifest_path)])
    document = json.loads(capsys.readouterr().out)
    assert code in (0, 3)
    assert document["layer"] == "verify_environment" and document["loaded_models"] == 0

    code = deploy.main(["preflight", "--manifest", str(manifest_path), "--candidate", str(candidate_path),
                        "--evidence", str(evidence)])
    document = json.loads(capsys.readouterr().out)
    assert code == 3  # the live site in the test environment is not the recorded device
    assert document["layer"] == "production_gate" and document["loaded_models"] == 0
