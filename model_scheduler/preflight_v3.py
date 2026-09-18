"""P26: the two-layer v3 site preflight and the deploy-manifest identity.

Layer 1 — `verify_environment`: **never loads a model**. It compares the
deployment manifest against the live site (machine/device-tree identity, kernel,
disk UUIDs and filesystem, the locally present image digests, the model files
byte-for-byte under the deployment's model directory, the source archive and
config digests, the mode and the report's validity window) and blocks any
mismatch before a container can start. There is no force flag.

Layer 2 — `production_gate`: after layer 1, it re-checks the final S/B/O
evidence through the P25 offline verifier. The positive acceptance of the full
gate belongs to P31 (a gate must not depend on the report it is producing), so
O05 uses layer 1 plus this gate's missing/tampered rejection paths.

`manifest_identity` gives the deploy manifest a tamper-evident identity block:
the runtime runner recomputes it on every load, so a replaced image/tag or an
edited identity field is refused before the process is started.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from typing import Any, Mapping

from .contracts_v2 import canonical_json_bytes
from .evidence_contracts import REPORT_FUTURE_TOLERANCE_SECONDS, REPORT_VALIDITY_SECONDS

SCHEMA_VERSION = 3
MODES = ("production", "lab")
IDENTITY_FIELDS = ("schema_version", "mode", "deployment_id", "candidate_sha256", "source_archive_sha256",
                   "config_sha256", "device_digest")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE = re.compile(r"(?:[A-Za-z0-9][A-Za-z0-9._/:@-]*@)?sha256:[0-9a-f]{64}\Z")
_DEPLOYMENT_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")


class PreflightError(RuntimeError):
    """A preflight refusal that carries its layer and CLI exit code."""

    def __init__(self, message: str, *, layer: str, exit_code: int) -> None:
        super().__init__(message)
        self.layer = layer
        self.exit_code = exit_code


def _identity_block(manifest: Mapping[str, Any]) -> dict:
    block: dict[str, Any] = {field: manifest.get(field) for field in IDENTITY_FIELDS}
    models = manifest.get("models")
    block["models"] = {str(model_id): {"container_name": entry.get("container_name"),
                                       "image_digest": entry.get("image_digest")}
                       for model_id, entry in sorted((models or {}).items())
                       if isinstance(entry, Mapping)}
    return block


def manifest_identity(manifest: Mapping[str, Any]) -> str:
    """The tamper-evident digest of the manifest's identity block (never of itself)."""
    return hashlib.sha256(canonical_json_bytes(_identity_block(manifest))).hexdigest()


def require_manifest_identity(manifest: Mapping[str, Any], *, model_id: str | None = None) -> None:
    """Refuse a manifest whose identity is incomplete, edited or of the wrong schema."""
    if not isinstance(manifest, Mapping):
        raise PreflightError("the manifest must be an object", layer="identity", exit_code=2)
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise PreflightError(f"the manifest must be schema version {SCHEMA_VERSION}", layer="identity", exit_code=2)
    if manifest.get("mode") not in MODES:
        raise PreflightError(f"the manifest mode must be one of {MODES}", layer="identity", exit_code=2)
    for field in ("candidate_sha256", "source_archive_sha256", "config_sha256", "device_digest"):
        value = manifest.get(field)
        if not isinstance(value, str) or not _SHA256.fullmatch(value):
            raise PreflightError(f"the manifest identity field {field!r} is missing or malformed",
                                 layer="identity", exit_code=2)
    deployment = manifest.get("deployment_id")
    if not isinstance(deployment, str) or not _DEPLOYMENT_ID.fullmatch(deployment):
        raise PreflightError("the manifest identity field 'deployment_id' is missing or malformed",
                             layer="identity", exit_code=2)
    models = manifest.get("models")
    if not isinstance(models, Mapping) or not models:
        raise PreflightError("the manifest carries no models", layer="identity", exit_code=2)
    for entry_id, entry in sorted(models.items()):
        if not isinstance(entry, Mapping):
            raise PreflightError(f"model {entry_id!r} has no launch entry", layer="identity", exit_code=2)
        image = entry.get("image_digest")
        if not isinstance(image, str) or not _IMAGE.fullmatch(image):
            raise PreflightError(f"model {entry_id!r} carries no pinned image digest", layer="identity",
                                 exit_code=2)
        if entry.get("container_name") != f"sms-{manifest['deployment_id']}-{entry_id}":
            raise PreflightError(f"model {entry_id!r} has an invalid container name", layer="identity",
                                 exit_code=2)
    if model_id is not None and model_id not in models:
        raise PreflightError(f"model {model_id!r} is not part of this manifest", layer="identity", exit_code=2)
    recorded = manifest.get("identity_sha256")
    if not isinstance(recorded, str) or recorded != manifest_identity(manifest):
        raise PreflightError("the manifest identity digest does not match its content: the file was edited",
                             layer="identity", exit_code=3)


def _utc(text: str, label: str) -> datetime:
    try:
        value = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError as exc:
        raise PreflightError(f"{label}: not a valid timestamp", layer="environment", exit_code=2) from exc
    if value.tzinfo is None:
        raise PreflightError(f"{label}: the timestamp must carry a UTC offset", layer="environment", exit_code=2)
    return value.astimezone(timezone.utc)


def verify_environment(manifest: Mapping[str, Any], *, site: Mapping[str, Any],
                       now: datetime | None = None) -> dict:
    """Layer 1: compare the manifest with the live site. Nothing is loaded."""
    moment = now if now is not None else datetime.now(timezone.utc)
    require_manifest_identity(manifest)
    problems: list[str] = []
    if manifest.get("mode") != "production":
        problems.append("a lab manifest is not a production deployment")
    if manifest.get("force") not in (None, False):
        problems.append("a preflight never honours a force flag")

    device = manifest.get("device")
    if not isinstance(device, Mapping):
        problems.append("the manifest carries no device facts")
    else:
        for field in ("machine_id_sha256", "architecture", "device_tree_sha256", "mem_total_bytes",
                      "kernel_release", "model_disk_uuid", "scratch_disk_uuid"):
            if device.get(field) != site.get(field):
                problems.append(f"the live site {field} does not match the manifest")
    if manifest.get("config_sha256") != site.get("config_sha256"):
        problems.append("the live config digest does not match the manifest")
    if manifest.get("source_archive_sha256") != site.get("source_archive_sha256"):
        problems.append("the live source archive digest does not match the manifest")

    filesystem = site.get("filesystem")
    if manifest.get("model_filesystem") not in (None, filesystem):
        problems.append("the live model filesystem does not match the manifest")

    present_images = site.get("images") if isinstance(site.get("images"), Mapping) else {}
    model_files = site.get("model_files") if isinstance(site.get("model_files"), Mapping) else {}
    for model_id, entry in sorted(manifest.get("models", {}).items()):
        image = entry.get("image_digest")
        if present_images.get(image) is not True:
            problems.append(f"model {model_id!r}: the image {image} is not present on this site")
        for asset in entry.get("assets", []) or []:
            name = asset.get("path")
            seen = model_files.get(name)
            if not isinstance(seen, Mapping):
                problems.append(f"model {model_id!r}: asset {name!r} is missing from the site listing")
                continue
            if seen.get("size_bytes") != asset.get("size_bytes") or seen.get("sha256") != asset.get("sha256"):
                problems.append(f"model {model_id!r}: asset {name!r} size/hash differs on this site")

    evidence = manifest.get("evidence")
    if not isinstance(evidence, Mapping):
        problems.append("the manifest carries no evidence reference")
    else:
        started = _utc(evidence.get("started_at"), "evidence.started_at")
        if started > moment + timedelta(seconds=REPORT_FUTURE_TOLERANCE_SECONDS):
            problems.append("the evidence starts in the future beyond the allowed tolerance")
        if moment - started >= timedelta(seconds=REPORT_VALIDITY_SECONDS):
            problems.append("the evidence is expired: a deployment cannot refresh its window")

    return {"layer": "verify_environment", "ok": not problems, "problems": problems, "loaded_models": 0,
            "checked": {"models": sorted(manifest.get("models", {})), "mode": manifest.get("mode")}}


def production_gate(manifest: Mapping[str, Any], *, candidate_path, evidence_dir, site: Mapping[str, Any],
                    now: datetime | None = None) -> dict:
    """Layer 2: layer 1 plus the full S/B/O evidence set, recomputed offline."""
    environment = verify_environment(manifest, site=site, now=now)
    problems = list(environment["problems"])
    from .acceptance.verify import verify_evidence  # local import: the deploy path must not load acceptance at import time

    code, document = verify_evidence(candidate_path=candidate_path, evidence_dir=evidence_dir, now=now)
    if code != 0:
        problems.append(f"the evidence does not verify offline (exit {code}): {document.get('error')}")
    return {"layer": "production_gate", "ok": not problems, "problems": problems, "loaded_models": 0,
            "environment": environment["ok"], "evidence_verdict": document.get("verdict") if code == 0 else None}
