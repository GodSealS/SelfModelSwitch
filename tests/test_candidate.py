"""P22: the source archive and the candidate are reproducible, traceable and refuse to guess.

The candidate freezes facts + measurements + policy + fixtures + source into one
body whose digest is recomputable and never references itself; an unmeasured
model, an uncovered capability, a missing evaluator or a mismatched material
all stop the build instead of producing a plausible-looking candidate.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
import yaml

from model_scheduler import evidence_contracts as ec
from model_scheduler.acceptance import candidate as ca

REPO_FILES = {
    "app.py": "APP = 1\n",
    "run.py": "RUN = 1\n",
    "pyproject.toml": "[project]\nname = 'self-model-switch'\n",
    "model_scheduler/__init__.py": "",
    "model_scheduler/acceptance/__init__.py": "EXIT_OK = 0\n",
    "model_scheduler/acceptance/collector.py": "COLLECTOR_VERSION = 1\n",
    "plan/notes.md": "plan notes v1\n",
    "deploy/INSTALL.md": "install\n",
    "deploy/.env": "SECRET=never-archive-me\n",
}
ALLOWLIST = ("app.py", "run.py", "model_scheduler", "scripts", "deploy", "tests", "pyproject.toml",
             "requirements.in", "requirements-dev.in", "requirements.lock", "requirements-dev.lock")


def _git(root: Path, *args: str) -> None:
    import subprocess

    result = subprocess.run(["git", "-C", str(root), "-c", "user.email=test@example.invalid",
                             "-c", "user.name=test", *args], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for relative, content in REPO_FILES.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "fixture")
    return root


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ref(path: Path, relative: str) -> ec.ArtifactRef:
    return ec.ArtifactRef(relative_path=relative, size_bytes=path.stat().st_size, sha256=_sha256(path))


def _material(directory: Path) -> list[ec.ArtifactRef]:
    return [_ref(path, str(path.relative_to(directory)))
            for path in sorted(path for path in directory.rglob("*") if path.is_file())]


# ---------------------------------------------------------------------------
# the source archive


def test_the_source_archive_is_deterministic_and_ignores_plan_changes(tmp_path) -> None:
    root = _repo(tmp_path)
    first = tmp_path / "source-1.tar.gz"
    result = ca.build_source_archive(root=root, output=first)

    assert result["sha256"] == _sha256(first)
    assert result["members"][0].startswith("source/")
    assert "source/plan/notes.md" not in result["members"]  # plan documents are not source
    assert "source/deploy/.env" not in result["members"] and "deploy/.env" in result["excluded"]  # credentials stay out

    again = tmp_path / "source-2.tar.gz"
    ca.build_source_archive(root=root, output=again)
    assert _sha256(again) == result["sha256"]  # same bytes, same hash

    (root / "plan" / "more-notes.md").write_text("plan notes v2\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "plan only")
    third = tmp_path / "source-3.tar.gz"
    ca.build_source_archive(root=root, output=third)
    assert _sha256(third) == result["sha256"]  # plan documents are not source identity


def test_the_source_archive_has_stable_metadata(tmp_path) -> None:
    import tarfile

    root = _repo(tmp_path)
    output = tmp_path / "source.tar.gz"
    ca.build_source_archive(root=root, output=output)

    with tarfile.open(output, "r:gz") as archive:
        infos = archive.getmembers()
    assert infos == sorted(infos, key=lambda info: info.name)
    assert all(info.mtime == 0 and info.uid == 0 and info.gid == 0 and info.uname == "" for info in infos)
    assert all(info.isfile() for info in infos)
    assert all(info.name.startswith("source/") for info in infos)
    assert output.read_bytes()[:4] == b"\x1f\x8b\x08\x00" and output.read_bytes()[4:8] == b"\x00\x00\x00\x00"  # gzip mtime 0


def test_a_dirty_allowlisted_file_is_refused(tmp_path) -> None:
    root = _repo(tmp_path)
    (root / "app.py").write_text("APP = 2\n", encoding="utf-8")

    with pytest.raises(ca.CandidateError, match="dirty") as refused:
        ca.build_source_archive(root=root, output=tmp_path / "source.tar.gz")

    assert refused.value.input_error is True


def test_a_source_archive_without_the_collector_member_is_refused(tmp_path) -> None:
    root = _repo(tmp_path)
    (root / "model_scheduler" / "acceptance" / "collector.py").unlink()
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "drop collector")
    archive = tmp_path / "source.tar.gz"
    ca.build_source_archive(root=root, output=archive)

    with pytest.raises(ca.CandidateError, match="collector"):
        ca.member_sha256(archive, ca.COLLECTOR_MEMBER)


# ---------------------------------------------------------------------------
# the candidate


def _site(tmp_path: Path, **overrides) -> dict:
    """Complete candidate inputs: repo, source archive, config, facts, measurements, policy, fixtures.

    The single-directory form serves exactly one registered model (K6/RP10), so
    the fixture keeps the measured vision model; `mixed=True` restores its
    unmeasured sibling for the refusal cases only.
    """
    root = _repo(tmp_path)
    source = tmp_path / "source.tar.gz"
    ca.build_source_archive(root=root, output=source)

    spec = importlib.util.spec_from_file_location("sms_v2_candidate_fixture",
                                                  Path(__file__).resolve().parent / "test_config.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    document = yaml.safe_load(module.V2)
    model_directory = tmp_path / "ssd" / "models"
    model_directory.mkdir(parents=True)
    document["storage"]["mount_path"] = str(tmp_path / "ssd")
    document["storage"]["model_directory"] = str(model_directory)
    document["storage"]["expected_uuid"] = "11111111-2222-3333-4444-555555555555"

    assets = {"embedding.gguf": b"embedding-asset", "qwen-small.gguf": b"qwen-asset", "mmproj.gguf": b"projector"}
    for name, payload in assets.items():
        (model_directory / name).write_bytes(payload)
    registered = {model["model_id"]: model for model in document["registration"]["models"]}
    vision, embedding = registered["qwen-small"], registered["embedding"]
    vision["capabilities"] = ["chat", "vision"]  # a vision chat model, so S05 can exercise the chat surface
    vision["assets"] = [
        {"role": "model", "path": "qwen-small.gguf", "sha256": hashlib.sha256(assets["qwen-small.gguf"]).hexdigest(),
         "size_bytes": len(assets["qwen-small.gguf"])},
        {"role": "projector", "path": "mmproj.gguf", "sha256": hashlib.sha256(assets["mmproj.gguf"]).hexdigest(),
         "size_bytes": len(assets["mmproj.gguf"])},
    ]
    vision["envelope"].update({"max_image_tokens": 1280, "max_image_edge_pixels": 1024, "max_images": 1})
    embedding["assets"][0].update(
        {"sha256": hashlib.sha256(assets["embedding.gguf"]).hexdigest(),
         "size_bytes": len(assets["embedding.gguf"])})

    # K6/RP10: one directory of material serves exactly one model. `mixed` keeps
    # the unmeasured sibling registered, which must be refused.
    kept = ("embedding", "qwen-small") if overrides.pop("mixed", False) else ("qwen-small",)
    document["registration"]["models"] = [registered[model_id] for model_id in kept]
    for key in ("pinned_models", "preload_models"):
        document["scheduler"][key] = [model_id for model_id in document["scheduler"].get(key, [])
                                      if model_id in kept]

    measurements = tmp_path / "measurements"
    (measurements / "raw" / "run1").mkdir(parents=True)
    (measurements / "raw" / "run1" / "meminfo.csv").write_text("t_mono,utc\n", encoding="utf-8")
    summary = {"verdict": "passed", "model_id": "qwen-small", "measured_peak_bytes": 5_300_000_000,
               "reserved_bytes": 6_095_000_000, "physical_resident_peak_bytes": 31_536_000_000,
               "physical_bound_proven": True, "stops_proven": True}
    summary.update(overrides.pop("summary", {}))
    (measurements / "measurements.json").write_text(json.dumps({"schema_version": 1, "summary": summary}),
                                                    encoding="utf-8")
    measurement_ref = ec.artifact_manifest_digest(_material(measurements))

    vision["measured"] = overrides.pop("measured", True)
    vision["measurement_ref"] = overrides.pop("measurement_ref", measurement_ref)
    vision["physical_resident_peak_bytes"] = overrides.pop("physical_peak", summary["physical_resident_peak_bytes"])
    vision["reserved_bytes"] = summary["reserved_bytes"]
    if not vision["measured"]:
        vision["measurement_ref"] = None
        vision["physical_resident_peak_bytes"] = None

    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(document), encoding="utf-8")

    facts = tmp_path / "facts.json"
    facts.write_text(json.dumps(_facts_document()), encoding="utf-8")

    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({
        "performance": [entry for entry in [
            {"model_id": "embedding", "cold_start_seconds_max": 30.0, "infer_milliseconds_p95_max": 50.0,
             "infer_milliseconds_p99_max": 80.0, "max_input_milliseconds_max": 200.0},
            {"model_id": "qwen-small", "cold_start_seconds_max": 60.0, "infer_milliseconds_p95_max": 900.0,
             "infer_milliseconds_p99_max": 1200.0, "max_input_milliseconds_max": 6000.0},
        ] if entry["model_id"] in kept],
        "operational": {"duration_seconds": 1800, "arrival_requests": 100, "arrival_gap_seconds_max": 15.0,
                        "send_deviation_milliseconds_max": 1000.0, "error_rate_max": 0.1, "queue_full_rate_max": 0.1,
                        "timeout_rate_max": 0.1}}), encoding="utf-8")

    fixtures_dir = tmp_path / "fixtures"
    fixtures_dir.mkdir()
    chat = fixtures_dir / "chat-text-max.json"
    chat.write_text('{"kind": "chat-text-max"}\n', encoding="utf-8")
    batch = fixtures_dir / "embeddings-batch.json"
    batch.write_text('{"kind": "embeddings-batch"}\n', encoding="utf-8")
    image = fixtures_dir / "image-max.json"
    image.write_text('{"kind": "image-max"}\n', encoding="utf-8")
    evaluator = fixtures_dir / "evaluator.py"
    evaluator.write_text("def evaluate():\n    return None\n", encoding="utf-8")
    fixture_document = {
        "schema_version": 1,
        "evaluator": {"relative_path": "fixtures/evaluator.py", "size_bytes": evaluator.stat().st_size,
                      "sha256": _sha256(evaluator)},
        "fixtures": [
            {"fixture_id": "chat-text-max", "capabilities": ["chat"],
             "artifact": {"relative_path": "fixtures/chat-text-max.json", "size_bytes": chat.stat().st_size,
                          "sha256": _sha256(chat)}},
            {"fixture_id": "embeddings-batch", "capabilities": ["embeddings"],
             "artifact": {"relative_path": "fixtures/embeddings-batch.json", "size_bytes": batch.stat().st_size,
                          "sha256": _sha256(batch)}},
            {"fixture_id": "image-max", "capabilities": ["vision"],
             "artifact": {"relative_path": "fixtures/image-max.json", "size_bytes": image.stat().st_size,
                          "sha256": _sha256(image)}},
        ],
    }
    fixtures = tmp_path / "fixtures.json"
    fixtures.write_text(json.dumps(fixture_document), encoding="utf-8")

    return {"root": root, "source": source, "config": config, "facts": facts, "measurements": measurements,
            "policy": policy, "fixtures": fixtures, "deployment_id": "sms-orin-lab", "output": tmp_path / "candidate.json"}


def _facts_document() -> dict:
    from model_scheduler.acceptance.collect import collect_facts

    import importlib.util as ilu

    spec = ilu.spec_from_file_location("sms_collect_fixture", Path(__file__).resolve().parent / "test_calibration.py")
    module = ilu.module_from_spec(spec)
    spec.loader.exec_module(module)
    return collect_facts(model_disk="/models", scratch_disk="/var/lib/self-model-switch",
                         reader=module._reader()).document()


def _build(site: dict) -> dict:
    return ca.build_candidate(config_path=site["config"], facts_path=site["facts"],
                              measurements_dir=site["measurements"], policy_path=site["policy"],
                              fixtures_path=site["fixtures"], source_path=site["source"],
                              deployment_id=site["deployment_id"], output=site["output"])


def test_the_candidate_is_reproducible_parsable_and_has_no_self_hash(tmp_path) -> None:
    site = _site(tmp_path)
    result = _build(site)

    body = json.loads(site["output"].read_text(encoding="utf-8"))
    assert set(body) == set(ec.CANDIDATE_KEYS)  # no candidate_sha256, no report, no time
    assert body["schema_version"] == ec.EVIDENCE_SCHEMA_VERSION
    assert body["deployment_id"] == "sms-orin-lab"
    assert body["source_archive_sha256"] == _sha256(site["source"])
    assert body["collector_sha256"] == ca.member_sha256(site["source"], ca.COLLECTOR_MEMBER)
    assert body["evaluator_sha256"] == _sha256(site["fixtures"].parent / "fixtures" / "evaluator.py")

    parsed = ec.parse_candidate(body)
    digest = ec.candidate_digest(parsed)
    assert digest == result["candidate_sha256"]
    assert result["candidate_sha256"] == _digest_of(site["output"])

    site["output"].unlink()
    again = _build(site)
    assert again["candidate_sha256"] == result["candidate_sha256"]  # same inputs, same digest


def _digest_of(path: Path) -> str:
    return ec.candidate_digest(ec.parse_candidate(json.loads(path.read_text(encoding="utf-8"))))


def test_the_config_digest_ignores_the_backfilled_candidate_digest(tmp_path) -> None:
    site = _site(tmp_path)
    first = _build(site)["candidate_sha256"]
    body = yaml.safe_load(site["config"].read_text(encoding="utf-8"))
    body["candidate_sha256"] = first
    site["config"].write_text(yaml.safe_dump(body), encoding="utf-8")
    site["output"].unlink()

    again = _build(site)

    assert again["candidate_sha256"] == first  # the backfill cannot change the digest (no cycle)
    assert json.loads(site["output"].read_text(encoding="utf-8"))["config_sha256"] == again["config_sha256"]


def test_an_unmeasured_model_is_refused(tmp_path) -> None:
    with pytest.raises(ca.CandidateError, match="production requires") as refused:
        _build(_site(tmp_path, measured=False))

    assert refused.value.input_error is True


def test_a_measured_model_without_a_physical_bound_is_refused(tmp_path) -> None:
    """K6/RP10: `require_production_openable` is the one production gate."""
    with pytest.raises(ca.CandidateError, match="physical_resident_peak_bytes") as refused:
        _build(_site(tmp_path, physical_peak=None))

    assert refused.value.input_error is True


def test_a_mixed_model_set_is_refused_before_any_material_is_read(tmp_path) -> None:
    """K6/RP10: one directory of material cannot serve a mixed registration."""
    site = _site(tmp_path, mixed=True)
    site["source"].unlink()  # an expensive input the refusal must never open

    with pytest.raises(ca.CandidateError, match="exactly one registered model") as refused:
        _build(site)

    assert refused.value.input_error is True
    assert not site["output"].exists()


def test_a_measurement_of_another_model_is_refused(tmp_path) -> None:
    with pytest.raises(ca.CandidateError, match="measured") as refused:
        _build(_site(tmp_path, summary={"model_id": "embedding"}))

    assert refused.value.input_error is True


def test_the_candidate_cli_refuses_a_mixed_set_without_creating_an_output(tmp_path) -> None:
    from model_scheduler.acceptance.__main__ import main

    site = _site(tmp_path, mixed=True)
    output = tmp_path / "cli-mixed-candidate.json"

    code = main(["candidate", "--config", str(site["config"]), "--facts", str(site["facts"]),
                 "--measurements", str(site["measurements"]), "--policy", str(site["policy"]),
                 "--fixtures", str(site["fixtures"]), "--source", str(site["source"]),
                 "--deployment-id", "sms-orin-lab", "--output", str(output)])

    assert code == 2
    assert not output.exists()


def test_a_blocked_measurement_cannot_produce_a_candidate(tmp_path) -> None:
    # the registration still claims a measured model; the *material* says blocked (P21's real outcome)
    with pytest.raises(ca.CandidateError, match="physical bound"):
        _build(_site(tmp_path, summary={"verdict": "blocked", "physical_bound_proven": False}))


def test_a_measurement_ref_that_does_not_match_the_material_is_refused(tmp_path) -> None:
    with pytest.raises(ca.CandidateError, match="measurement_ref"):
        _build(_site(tmp_path, measurement_ref="f" * 64))


def test_a_capability_without_a_fixture_is_refused(tmp_path) -> None:
    site = _site(tmp_path)
    document = json.loads(site["fixtures"].read_text(encoding="utf-8"))
    document["fixtures"] = [entry for entry in document["fixtures"] if "vision" not in entry["capabilities"]]
    site["fixtures"].write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ca.CandidateError, match="vision"):
        _build(site)


def test_a_missing_evaluator_or_a_bad_fixture_hash_is_refused(tmp_path) -> None:
    site = _site(tmp_path)
    document = json.loads(site["fixtures"].read_text(encoding="utf-8"))
    del document["evaluator"]
    site["fixtures"].write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ca.CandidateError, match="evaluator") as refused:
        _build(site)
    assert refused.value.input_error is True

    other = _site(tmp_path / "other")
    document = json.loads(other["fixtures"].read_text(encoding="utf-8"))
    document["fixtures"][0]["artifact"]["sha256"] = "e" * 64
    other["fixtures"].write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ca.CandidateError, match="hash"):
        _build(other)


def test_a_model_asset_that_is_missing_or_wrong_is_refused(tmp_path) -> None:
    site = _site(tmp_path)
    (Path(site["config"].parent) / "ssd" / "models" / "qwen-small.gguf").write_bytes(b"tampered")

    with pytest.raises(ca.CandidateError, match="asset"):
        _build(site)


def test_the_cli_wires_source_and_candidate(tmp_path) -> None:
    from model_scheduler.acceptance.__main__ import main

    site = _site(tmp_path)
    archive = tmp_path / "cli-source.tar.gz"
    assert main(["source", "--root", str(site["root"]), "--output", str(archive)]) == 0
    assert archive.is_file() and archive.stat().st_size > 0
    assert main(["source", "--root", str(site["root"]), "--output", str(archive)]) == 2  # no overwrite

    site["source"] = archive
    output = tmp_path / "cli-candidate.json"
    code = main(["candidate", "--config", str(site["config"]), "--facts", str(site["facts"]),
                 "--measurements", str(site["measurements"]), "--policy", str(site["policy"]),
                 "--fixtures", str(site["fixtures"]), "--source", str(archive), "--deployment-id", "sms-orin-lab",
                 "--output", str(output)])
    assert code == 0
    assert set(json.loads(output.read_text(encoding="utf-8"))) == set(ec.CANDIDATE_KEYS) and output.is_file()
    assert main(["candidate", "--config", str(site["config"]), "--facts", str(site["facts"]),
                 "--measurements", str(site["measurements"]), "--policy", str(site["policy"]),
                 "--fixtures", str(site["fixtures"]), "--source", str(archive), "--deployment-id", "sms-orin-lab",
                 "--output", str(output)]) == 2
