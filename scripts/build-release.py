#!/usr/bin/env python3
"""Create a deterministic, source-only release archive; never install it."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tarfile


class ReleaseError(ValueError):
    pass


_DEPLOYMENT_FILES = frozenset({"manifest.json", "config.yaml", "llama-swap.yaml", "fstab.fragment", "model-scheduler.service", "llama-swap.service"})
_OPTIONAL_DEPLOYMENT_FILES = frozenset({"hardware-report.json"})


_FORBIDDEN_SUFFIXES = (".gguf", ".safetensors", ".onnx", ".pt", ".bin", ".pem", ".key", ".crt", ".env")
_FORBIDDEN_NAMES = (".env", "id_rsa", "credentials.json", ".netrc")
_VIDEO_MARKERS = ("video", "media_pipeline", "transcode")
# P06a material that must ship with the model service (P28 AC2): without the pinned
# control contract and its measured fixture, publishing is blocked.
_REQUIRED_SOURCE_FILES = ("model_scheduler/llama_swap_contract.py", "tests/test_llama_swap_fixture.py")


def _verify_forbidden(files: list[Path], root: Path) -> list[str]:
    """Refuse weights, credentials and any video implementation in a release."""
    problems: list[str] = []
    for path in files:
        relative = path.relative_to(root).as_posix()
        name = path.name.lower()
        if name in _FORBIDDEN_NAMES or name.endswith(_FORBIDDEN_SUFFIXES):
            problems.append(f"forbidden material: {relative}")
        if any(marker in relative.lower() for marker in _VIDEO_MARKERS):
            problems.append(f"video implementation must not be packaged: {relative}")
    return problems


def _verify_v3_manifest(deployment: Path) -> dict | None:
    """A v3 deployment must prove its own identity before it is packaged (P26/P28)."""
    try:
        manifest = json.loads((deployment / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 3:
        return None
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    try:
        from model_scheduler.preflight_v3 import PreflightError, require_manifest_identity
    except ImportError as exc:  # pragma: no cover - a v3 manifest without the module cannot be released
        raise ReleaseError(f"the v3 preflight module is not installed: {exc}") from exc
    try:
        require_manifest_identity(manifest)
    except PreflightError as exc:
        raise ReleaseError(f"the deployment manifest does not verify: {exc}") from exc
    if manifest.get("mode") != "production" or manifest.get("production") is not True:
        raise ReleaseError("a lab rendering must never be released as production")
    evidence = manifest.get("evidence")
    if not isinstance(evidence, dict) or not isinstance(evidence.get("report_sha256"), str):
        raise ReleaseError("the deployment carries no evidence reference: a production release requires it")
    return manifest


def _tracked_files(root: Path) -> list[Path]:
    result = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True)
    excluded = {"config.yaml", "requirements.txt"}
    return [root / item for item in result.stdout.decode().split("\0") if item and item not in excluded]


def _verify_hardware_report(deployment: Path, deployment_files: set[str]) -> None:
    report = deployment / "hardware-report.json"
    try:
        manifest = json.loads((deployment / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseError("cannot read deployment manifest") from exc
    if "hardware-report.json" not in deployment_files:
        if isinstance(manifest, dict) and "hardware_report_sha256" in manifest:
            raise ReleaseError("Hardware report is missing")
        return
    expected = manifest.get("hardware_report_sha256") if isinstance(manifest, dict) else None
    actual = hashlib.sha256(report.read_bytes()).hexdigest()
    if not isinstance(expected, str) or expected != actual:
        raise ReleaseError("Hardware report digest mismatch")


def build(root: Path, deployment: Path, output: Path, release_id: str) -> Path:
    if not release_id or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for char in release_id):
        raise ReleaseError("invalid release id")
    deployment_files = {path.name for path in deployment.iterdir()} if deployment.is_dir() else set()
    if not _DEPLOYMENT_FILES <= deployment_files or deployment_files - _DEPLOYMENT_FILES - _OPTIONAL_DEPLOYMENT_FILES:
        raise ReleaseError("deployment directory is incomplete")
    _verify_hardware_report(deployment, deployment_files)
    manifest = _verify_v3_manifest(deployment)
    source_files = _tracked_files(root)
    tracked = {path.relative_to(root).as_posix() for path in source_files}
    missing = [name for name in _REQUIRED_SOURCE_FILES if name not in tracked]
    if missing:
        raise ReleaseError(f"the pinned control contract and its measured fixture must ship: "
                           f"missing {', '.join(missing)}")
    forbidden = _verify_forbidden(source_files, root)
    if forbidden:
        raise ReleaseError("; ".join(sorted(forbidden)[:3]))
    if output.exists() and any(output.iterdir()):
        raise ReleaseError("release output directory must be empty")
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"self-model-switch-{release_id}.tar.gz"
    prefix = PurePosixPath(f"self-model-switch-{release_id}")
    entries = [(path, prefix / path.relative_to(root).as_posix()) for path in source_files]
    entries.extend((path, prefix / "deployment" / path.name) for path in sorted(deployment.iterdir()))
    with archive.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as bundle:
            for source, name in entries:
                if not source.is_file() or source.is_symlink():
                    raise ReleaseError(f"unsafe source: {source}")
                info = bundle.gettarinfo(str(source), arcname=str(name))
                info.uid, info.gid, info.uname, info.gname, info.mtime = 0, 0, "", "", 0
                with source.open("rb") as handle:
                    bundle.addfile(info, handle)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (output / "SHA256SUMS").write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    files = [{"relative_path": name.as_posix(), "size_bytes": source.stat().st_size,
              "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
             for source, name in sorted(entries, key=lambda item: item[1].as_posix())]
    bundle = {"schema_version": 1, "release_id": release_id, "archive": archive.name, "archive_sha256": digest,
              "files": files}
    if isinstance(manifest, dict):
        bundle["manifest_sha256"] = hashlib.sha256((deployment / "manifest.json").read_bytes()).hexdigest()
        bundle["candidate_sha256"] = manifest.get("candidate_sha256")
        bundle["evidence"] = manifest.get("evidence")
    (output / "bundle.json").write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return archive


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--deployment", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--release-id", required=True)
    args = parser.parse_args(argv)
    try:
        archive = build(Path(__file__).resolve().parent.parent, Path(args.deployment), Path(args.output), args.release_id)
    except (OSError, ReleaseError, subprocess.SubprocessError) as exc:
        print(f"release error: {exc}", file=sys.stderr)
        return 78
    print(archive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
