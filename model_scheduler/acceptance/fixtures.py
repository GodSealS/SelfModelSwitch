"""P23 fixtures: deterministic inputs that reach the *declared* envelope boundary.

One fixture per declared capability of a model (plan/06-acceptance.md §3):

* `chat` — text that consumes the full `max_input_tokens` budget in one request
  while asking for `max_output_tokens`, so context input+output is reached in
  the same round instead of being split into smaller requests;
* `vision` — the maximum image edge, `max_images` images plus the remaining text
  budget, again in one request;
* `embeddings` / `rerank` — the interface-level batch/document limits
  (`MAX_EMBEDDING_BATCH`, `MAX_RERANK_DOCUMENTS`, P20's conservative defaults).

The fixtures are byte-deterministic (fixed seed, fixed filler, no clock), so the
material hash of a fixture is reproducible. Capabilities outside the closed set
are refused: this plan never claims audio or video quality, and a video fixture
must not be smuggled in as a "capability check".
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import json
from pathlib import Path
import struct
import zlib
from typing import Any, Mapping, Sequence

CAPABILITY_FIXTURES = ("chat", "vision", "embeddings", "rerank")
MAX_EMBEDDING_BATCH = 256
MAX_RERANK_DOCUMENTS = 256
FILLER_SEED = 1234
# Explicitly never a fixture kind: the plan forbids claiming video (or audio)
# quality, and M07's video work uses its own candidate and release flow.
FORBIDDEN_FIXTURE_KINDS = ("audio", "video", "transcription", "voiceprint", "face")


class FixtureError(RuntimeError):
    """A fixture request that cannot be honoured from the declared envelope."""


@dataclass(frozen=True)
class Fixture:
    """One capability fixture: the request body plus the boundary it reaches."""

    capability: str
    fixture_id: str
    payload: Mapping[str, Any]
    boundary: Mapping[str, int]
    artifact_name: str

    def document(self) -> dict:
        return {"fixture_id": self.fixture_id, "capabilities": [self.capability],
                "boundary": dict(self.boundary), "artifact_name": self.artifact_name}


@dataclass(frozen=True)
class FillerSpec:
    """How many real tokens one filler unit costs, as measured for that model.

    The count is a measurement of the model's own tokenizer, not an assumption:
    with 7 tokens per `tok000123` unit a request declared for 28 672 tokens was
    really 200 723 tokens and the model refused it for exceeding its context. The
    material that carries a fixture therefore carries the ratio too.
    """

    unit: str
    tokens_per_unit: float


def filler_text(tokens: int, *, filler: FillerSpec) -> str:
    """Deterministic filler sized in *real* tokens, from the measured ratio."""
    if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
        raise FixtureError("the filler token count must be a non-negative integer")
    ratio = filler.tokens_per_unit
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not math.isfinite(ratio) or ratio <= 0:
        raise FixtureError("the filler needs a measured positive tokens-per-unit ratio")
    if not isinstance(filler.unit, str) or not filler.unit or any(char.isspace() for char in filler.unit):
        raise FixtureError("the filler unit must be a single non-blank word")
    units = math.ceil(tokens / ratio) if tokens else 0
    return " ".join(filler.unit for _ in range(units))


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))


def render_test_png(width: int, height: int, *, seed: int = FILLER_SEED) -> bytes:
    """A deterministic RGB PNG with a per-pixel pattern (no clock, no randomness)."""
    if width <= 0 or height <= 0:
        raise FixtureError("image dimensions must be positive")
    rows = bytearray()
    for y in range(height):
        rows.append(0)  # filter: none
        for x in range(width):
            rows += bytes(((x * 7 + seed) % 256, (y * 13 + seed) % 256, (x + y + seed) % 256))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", header)
            + _png_chunk(b"IDAT", zlib.compress(bytes(rows), 6)) + _png_chunk(b"IEND", b""))


def _image_data_url(width: int, height: int) -> str:
    import base64

    return "data:image/png;base64," + base64.b64encode(render_test_png(width, height)).decode("ascii")


def _filler(tokens: int, filler: FillerSpec | None) -> str:
    """The filler of one request; a missing measurement is a refusal, not a default."""
    if filler is None:
        raise FixtureError("the text filler needs the measured tokens-per-unit ratio of this model's tokenizer")
    return filler_text(tokens, filler=filler)


def _text_tokens_within(envelope) -> int:
    """The text budget that is left once the image budget is accounted for."""
    return int(envelope.max_input_tokens)


def fixtures_for(model_id: str, capabilities: Sequence[str], envelope, *,
                 filler: FillerSpec | None = None) -> tuple[Fixture, ...]:
    """One fixture per declared capability, each reaching its declared boundary.

    `filler` is the measured token ratio of this model's tokenizer. Without it a
    text boundary cannot be constructed honestly, so the call is refused rather
    than silently assuming one word costs one token.
    """
    fixtures: list[Fixture] = []
    text_tokens = _text_tokens_within(envelope)  # a text budget is only usable with a measurement
    for capability in capabilities:
        if capability in FORBIDDEN_FIXTURE_KINDS:
            raise FixtureError(f"{model_id}: {capability!r} is not an acceptance capability: video/audio quality is "
                               "never claimed by this plan")
        if capability not in CAPABILITY_FIXTURES:
            raise FixtureError(f"{model_id}: no fixture is defined for capability {capability!r}")
        if capability in ("chat", "vision") and filler is None:
            raise FixtureError(f"{model_id}: the {capability} fixture needs the measured tokens-per-unit ratio of "
                               "this model's tokenizer (a declared boundary no one can reach is not a boundary)")
        if capability == "chat":
            payload = {"messages": [{"role": "user", "content": _filler(text_tokens, filler)}],
                       "max_tokens": envelope.max_output_tokens, "n_parallel": envelope.max_parallel}
            boundary = {"input_tokens": envelope.max_input_tokens, "output_tokens": envelope.max_output_tokens,
                        "parallel": envelope.max_parallel}
        elif capability == "vision":
            if envelope.max_images <= 0 or envelope.max_image_edge_pixels <= 0:
                raise FixtureError(f"{model_id}: the vision capability needs a positive image envelope")
            content = [{"type": "image_url", "image_url": {"url": _image_data_url(envelope.max_image_edge_pixels,
                                                                                   envelope.max_image_edge_pixels)}}]
            content += [{"type": "image_url", "image_url": {"url": _image_data_url(envelope.max_image_edge_pixels,
                                                                                   envelope.max_image_edge_pixels)}}
                        for _ in range(envelope.max_images - 1)]
            content.append({"type": "text", "text": _filler(text_tokens, filler)})
            payload = {"messages": [{"role": "user", "content": content}],
                       "max_tokens": envelope.max_output_tokens, "n_parallel": envelope.max_parallel}
            boundary = {"input_tokens": envelope.max_input_tokens, "output_tokens": envelope.max_output_tokens,
                        "images": envelope.max_images, "image_edge_pixels": envelope.max_image_edge_pixels,
                        "parallel": envelope.max_parallel}
        elif capability == "embeddings":
            payload = {"inputs": [f"doc {index:04d}" for index in range(MAX_EMBEDDING_BATCH)]}
            boundary = {"batch": MAX_EMBEDDING_BATCH}
        else:  # rerank
            payload = {"query": "doc query",
                       "documents": [f"doc {index:04d}" for index in range(MAX_RERANK_DOCUMENTS)]}
            boundary = {"documents": MAX_RERANK_DOCUMENTS}
        fixtures.append(Fixture(capability=capability, fixture_id=f"{model_id}-{capability}",
                                payload=payload, boundary=boundary,
                                artifact_name=f"{model_id}-{capability}.json"))
    if not fixtures:
        raise FixtureError(f"{model_id}: no declared capability has a fixture")
    return tuple(fixtures)


def fixture_bytes(fixture: Fixture) -> bytes:
    """The fixture material as written to disk: canonical, deterministic bytes."""
    return (json.dumps({"fixture_id": fixture.fixture_id, "capability": fixture.capability,
                        "boundary": dict(fixture.boundary), "payload": fixture.payload},
                       sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def write_fixture_material(directory: Path, fixtures: Sequence[Fixture]) -> list[dict]:
    """Write every fixture and return P22-style fixture entries with size/hash."""
    directory.mkdir(parents=True, exist_ok=True)
    entries: list[dict] = []
    for fixture in fixtures:
        path = directory / fixture.artifact_name
        payload = fixture_bytes(fixture)
        path.write_bytes(payload)
        entries.append({"fixture_id": fixture.fixture_id, "capabilities": [fixture.capability],
                        "artifact": {"relative_path": fixture.artifact_name, "size_bytes": len(payload),
                                     "sha256": hashlib.sha256(payload).hexdigest()}})
    return entries


def boundary_shortfalls(fixture: Fixture, observed: Mapping[str, Any]) -> list[str]:
    """Which declared boundary dimensions one *single* round failed to reach."""
    shortfalls: list[str] = []
    for dimension, declared in fixture.boundary.items():
        seen = observed.get(dimension)
        if isinstance(seen, bool) or not isinstance(seen, (int, float)) or seen < declared:
            shortfalls.append(f"{dimension}: {seen!r} < {declared}")
    return shortfalls
