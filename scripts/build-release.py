#!/usr/bin/env python3
"""Create a deterministic, source-only release archive; never install it."""
from __future__ import annotations

import argparse
import gzip
import hashlib
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tarfile


class ReleaseError(ValueError):
    pass


_DEPLOYMENT_FILES = frozenset({"manifest.json", "config.yaml", "llama-swap.yaml", "fstab.fragment", "model-scheduler.service", "llama-swap.service"})
_OPTIONAL_DEPLOYMENT_FILES = frozenset({"thor-report.json"})


def _tracked_files(root: Path) -> list[Path]:
    result = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True)
    excluded = {"config.yaml", "requirements.txt"}
    return [root / item for item in result.stdout.decode().split("\0") if item and item not in excluded]


def build(root: Path, deployment: Path, output: Path, release_id: str) -> Path:
    if not release_id or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for char in release_id):
        raise ReleaseError("invalid release id")
    deployment_files = {path.name for path in deployment.iterdir()} if deployment.is_dir() else set()
    if not _DEPLOYMENT_FILES <= deployment_files or deployment_files - _DEPLOYMENT_FILES - _OPTIONAL_DEPLOYMENT_FILES:
        raise ReleaseError("deployment directory is incomplete")
    if output.exists() and any(output.iterdir()):
        raise ReleaseError("release output directory must be empty")
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"self-model-switch-{release_id}.tar.gz"
    prefix = PurePosixPath(f"self-model-switch-{release_id}")
    entries = [(path, prefix / path.relative_to(root).as_posix()) for path in _tracked_files(root)]
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
