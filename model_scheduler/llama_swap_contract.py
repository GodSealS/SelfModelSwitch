"""The pinned llama-swap v217 control contract (M02/P06a).

llama-swap publishes no stable control API, so this module derives every path
and response check from one measured fixture
(`tests/fixtures/llama_swap_contract.json`) that was captured on the target
against the pinned ARM64 release:

* `GET /health` answers `200 text/plain` with `OK`;
* `GET /running` answers `200 application/json` with either an empty list or a
  list of upstream objects (`model`, `name`, `proxy`, `state`, `ttl`, `cmd`,
  `description`) — a bare string list is *not* the v217 shape;
* a request to `GET /upstream/<model_id>/health` starts the upstream and blocks
  until it is healthy; that proxied answer is `{"status":"ok"}`;
* `POST /api/models/unload/<model_id>` answers `200 text/plain` with `OK`;
* the previously guessed `GET /props?model=<model_id>` is captured as a
  negative sample (404) and never counts as a successful load.

If the fixture is missing or inconsistent this module refuses to build a
contract, so `run.py` cannot start a service that depends on an unpinned
control protocol.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from .llama_swap_client import ControlRequest, LlamaSwapControlContract, LlamaSwapResponse

FIXTURE_SCHEMA_VERSION = 2
FIXTURE_PATH = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "llama_swap_contract.json"

RUNNING_ENTRY_KEYS = frozenset({"cmd", "description", "model", "name", "proxy", "state", "ttl"})
UNLOAD_OK_BODY = "OK"


class FixtureError(RuntimeError):
    """The shipped control fixture is missing or does not match its pinned shape."""


@dataclass(frozen=True)
class ControlFixture:
    """The measured facts a contract is built from."""

    release_version: str
    release_sha256: str
    binary_sha256: str
    health: dict
    running_empty: dict
    load_trigger: dict
    running_loaded: dict
    unload: dict
    negative_samples: tuple[dict, ...]


def _require(mapping: Any, key: str, where: str) -> Any:
    if not isinstance(mapping, dict) or key not in mapping:
        raise FixtureError(f"{where}: missing {key!r}")
    return mapping[key]


def load_fixture(path: Path | None = None) -> ControlFixture:
    """Read and validate the pinned fixture; never guess a missing value."""
    fixture_path = Path(path) if path is not None else FIXTURE_PATH
    try:
        document = json.loads(fixture_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FixtureError(f"cannot read the llama-swap control fixture {fixture_path}: {exc}") from exc
    if _require(document, "schema_version", "fixture") != FIXTURE_SCHEMA_VERSION:
        raise FixtureError("fixture: unsupported schema_version")
    release = _require(document, "release", "fixture")
    binary = _require(document, "binary", "fixture")
    control = _require(document, "control", "fixture")
    negative = _require(document, "negative_samples", "fixture")
    if _require(binary, "listen_address_source", "binary") != "command-line --listen (the config port key is ignored)":
        raise FixtureError("binary: the listen-address measurement changed")
    for key in ("health", "running_empty", "load_trigger", "running_loaded", "unload", "running_empty_after_unload"):
        _require(control, key, "control")
    entries = _require(control["running_loaded"], "entry_keys", "control.running_loaded")
    if set(entries) != RUNNING_ENTRY_KEYS:
        raise FixtureError("control.running_loaded: the measured /running entry shape changed")
    rejected = [sample for sample in negative if sample.get("name") == "rejected_load_probe"]
    if not rejected or rejected[0].get("counts_as_success") is not False or rejected[0].get("status") != 404:
        raise FixtureError("negative_samples: the rejected load probe must stay a recorded 404")
    return ControlFixture(
        release_version=_require(release, "version", "release"),
        release_sha256=_require(release, "sha256", "release"),
        binary_sha256=_require(binary, "sha256", "binary"),
        health=control["health"],
        running_empty=control["running_empty"],
        load_trigger=control["load_trigger"],
        running_loaded=control["running_loaded"],
        unload=control["unload"],
        negative_samples=tuple(negative),
    )


def running_parser(payload: Any) -> list[str]:
    """The measured v217 `/running` shape: a list of upstream objects."""
    if not isinstance(payload, dict):
        raise FixtureError("/running did not answer with a JSON object")
    entries = payload.get("running")
    if not isinstance(entries, list):
        raise FixtureError("/running did not answer with a running list")
    models: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict) or not set(entry) >= {"model", "state", "proxy"}:
            raise FixtureError(f"/running entry does not match the measured shape: {entry!r}")
        model = entry["model"]
        if not isinstance(model, str) or not model:
            raise FixtureError(f"/running entry has no model name: {entry!r}")
        models.append(model)
    return models


def _media_type(response: LlamaSwapResponse) -> str | None:
    return response.content_type.split(";", 1)[0].strip().lower() if response.content_type else None


def load_request(model_id: str) -> ControlRequest:
    """The measured load trigger: the /upstream proxy path blocks until healthy."""
    if not isinstance(model_id, str) or not model_id:
        raise FixtureError("load request requires a model id")
    return ControlRequest("GET", f"/upstream/{quote(model_id, safe='')}/health")


def unload_path() -> str:
    """`POST /api/models/unload/<model_id>` answered 200 with the body `OK`."""
    return "/api/models/unload/{model_id}"


def validate_health_response(response: LlamaSwapResponse) -> None:
    if response.status_code != 200 or _media_type(response) != "text/plain" or response.body != b"OK":
        raise FixtureError(f"unexpected /health answer: {response.status_code} {response.body[:80]!r}")


def validate_load_response(response: LlamaSwapResponse) -> None:
    """The proxied upstream health answer must be the measured `{"status":"ok"}`."""
    if response.status_code != 200 or _media_type(response) != "application/json":
        raise FixtureError(f"unexpected load answer: {response.status_code} {response.body[:80]!r}")
    try:
        payload = json.loads(response.body)
    except json.JSONDecodeError as exc:
        raise FixtureError(f"load answer is not JSON: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise FixtureError(f"load answer does not report an upstream in the measured shape: {payload!r}")


def validate_unload_response(response: LlamaSwapResponse) -> None:
    if response.status_code != 200 or _media_type(response) != "text/plain" or response.body.strip() != b"OK":
        raise FixtureError(f"unexpected unload answer: {response.status_code} {response.body[:80]!r}")


def build_contract(fixture: ControlFixture | None = None) -> LlamaSwapControlContract:
    """Build the release contract; the fixture argument exists for tests only."""
    measured = fixture or load_fixture()
    if measured.load_trigger["request"]["path"] != "/upstream/{model_id}/health":
        raise FixtureError("the load trigger path changed in the fixture")
    if measured.unload["request"]["path"] != "/api/models/unload/{model_id}":
        raise FixtureError("the unload path changed in the fixture")
    return LlamaSwapControlContract(
        running_parser=running_parser,
        load_request=load_request,
        unload_path=unload_path(),
        validate_load_response=validate_load_response,
        validate_unload_response=validate_unload_response,
    )


def contract_validators() -> dict[str, Callable[[LlamaSwapResponse], None]]:
    return {"health": validate_health_response, "load": validate_load_response, "unload": validate_unload_response}


# run.py imports this module for the single pinned contract. A missing or
# inconsistent fixture raises here, so the service cannot start against an
# unpinned control protocol.
CONTROL_CONTRACT = build_contract()
