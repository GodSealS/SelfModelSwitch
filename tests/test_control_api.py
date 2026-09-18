"""P18 unit surface for the C05/C07 control routes (M04).

These tests drive the ASGI app directly, with no socket: they pin the version
gate, the C05 error document, the request_id echo and the Blob lifecycle
(201/200/204/404/409/410/413/415/422). The real Unix-socket round trip is in
`tests/integration/test_control_roundtrip.py`.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from model_scheduler import control_protocol_v1 as cp
from model_scheduler.blob_store import MAX_BLOB_BYTES, BlobStore
from model_scheduler.control_api import ControlAPI
from model_scheduler.control_identity import PeerIdentity
from model_scheduler.execution_service import blob_owner_for

VERSION = (cp.PROTOCOL_VERSION_HEADER, str(cp.PROTOCOL_VERSION))
REQUEST_ID = "req-0f1e2d3c4b5a"


class _Response:
    def __init__(self) -> None:
        self.status = 0
        self.headers: dict[str, str] = {}
        self.body = b""

    @property
    def document(self) -> dict:
        return json.loads(self.body.decode("utf-8"))


def _scope(method: str, path: str, headers, peer) -> dict:
    return {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "http_version": "1.1",
        "method": method, "scheme": "unix", "path": path, "raw_path": path.encode("utf-8"),
        "query_string": b"", "root_path": "",
        "headers": [(name.lower().encode("latin-1"), value.encode("latin-1")) for name, value in headers],
        "client": None, "server": ("/run/self-model-switch/control.sock", 0),
        "sms.peer": peer,
        "sms.request_id": REQUEST_ID,
    }


async def _call(api, method: str, path: str, *, headers=(), body: bytes = b"", chunks=None,
                peer: PeerIdentity | None = None) -> _Response:
    response = _Response()
    if chunks is not None:
        messages = [{"type": "http.request", "body": block, "more_body": True} for block in chunks]
        messages.append({"type": "http.request", "body": b"", "more_body": False})
    else:
        messages = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            response.status = int(message["status"])
            response.headers = {k.decode().lower(): v.decode() for k, v in message.get("headers", [])}
        elif message["type"] == "http.response.body":
            response.body += bytes(message.get("body", b""))

    await api(_scope(method, path, headers, peer or PeerIdentity(os.getuid())), receive, send)
    return response


def _error(response: _Response) -> cp.ErrorDetail:
    detail, request_id = cp.parse_error_document(response.document)
    assert request_id == REQUEST_ID
    return detail


def _owner() -> str:
    return blob_owner_for(f"uid:{os.getuid()}")


@pytest.fixture
def api(tmp_path: Path) -> ControlAPI:
    return ControlAPI(boot_id="boot-18", blobs=BlobStore(tmp_path / "blobs", clock=lambda: 1_000.0),
                      clock=lambda: 1_000.0)


async def _upload(api, *, media_type: str = "application/json", payload: bytes = b'{"messages": []}',
                  digest: str | None = None, headers=(), chunks=None) -> _Response:
    digest = hashlib.sha256(payload).hexdigest() if digest is None else digest
    return await _call(api, "POST", "/internal/blobs", chunks=chunks,
                       headers=(VERSION, *headers, ("Content-Type", media_type), ("X-Content-SHA256", digest)),
                       body=payload)


async def test_new_control_routes_require_the_version_header(api) -> None:
    for headers in ((), (("X-SMS-Protocol-Version", "9"),)):
        response = await _call(api, "POST", "/internal/blobs", headers=headers, body=b"{}")
        assert response.status == 400
        assert _error(response).code == "unsupported_protocol"


async def test_an_unknown_internal_path_answers_the_c05_error_document(api) -> None:
    response = await _call(api, "GET", "/internal/nothing", headers=(VERSION,))

    assert response.status == 404
    detail = _error(response)
    assert detail.code == "not_found" and detail.retryable is False


async def test_paths_outside_internal_are_never_served_here(api) -> None:
    response = await _call(api, "GET", "/v1/models")

    assert response.status == 404
    assert _error(response).code == "not_found"


async def test_a_blob_uploads_reads_back_and_deletes_to_a_tombstone(api) -> None:
    payload = b'{"messages": []}'
    digest = hashlib.sha256(payload).hexdigest()

    created = await _upload(api, payload=payload)
    assert created.status == 201
    ref = cp.parse_blob_ref(created.document)
    assert ref.owner == _owner() and ref.sha256 == digest
    assert ref.size_bytes == len(payload) and ref.media_type == "application/json"

    fetched = await _call(api, "GET", f"/internal/blobs/{ref.blob_id}", headers=(VERSION,))
    assert fetched.status == 200 and fetched.body == payload
    assert fetched.headers["content-type"] == "application/json"

    deleted = await _call(api, "DELETE", f"/internal/blobs/{ref.blob_id}", headers=(VERSION,))
    assert deleted.status == 204 and deleted.body == b""

    gone = await _call(api, "GET", f"/internal/blobs/{ref.blob_id}", headers=(VERSION,))
    assert gone.status == 410  # the owner tombstone keeps answering 410 until it is purged
    assert _error(gone).code == "reference_expired"


async def test_delete_and_a_missing_id_answer_204_for_the_same_owner(api) -> None:
    created = await _upload(api)
    ref = cp.parse_blob_ref(created.document)

    first = await _call(api, "DELETE", f"/internal/blobs/{ref.blob_id}", headers=(VERSION,))
    again = await _call(api, "DELETE", f"/internal/blobs/{ref.blob_id}", headers=(VERSION,))
    unknown = await _call(api, "DELETE", "/internal/blobs/does-not-exist", headers=(VERSION,))

    assert first.status == again.status == unknown.status == 204


async def test_an_unsupported_media_type_is_415_and_a_bad_hash_is_422(api) -> None:
    payload = b"x"

    bad_type = await _upload(api, media_type="text/plain", payload=payload)
    assert bad_type.status == 415 and _error(bad_type).code == "unsupported_media_type"

    missing = await _call(api, "POST", "/internal/blobs", headers=(VERSION, ("Content-Type", "application/json")),
                          body=payload)
    assert missing.status == 422 and _error(missing).code == "contract_violation"

    upper = await _upload(api, payload=payload, digest=hashlib.sha256(payload).hexdigest().upper())
    assert upper.status == 422


async def test_a_checksum_mismatch_publishes_nothing(api) -> None:
    response = await _upload(api, payload=b"real bytes", digest=hashlib.sha256(b"other bytes").hexdigest())

    assert response.status == 422 and _error(response).code == "contract_violation"
    assert api.blobs.metadata.usage(_owner()).total_bytes == 0


async def test_a_five_mib_body_over_the_inline_cap_streams_into_one_blob(api) -> None:
    blocks = [b"a" * (1024 * 1024)] * 5  # 5 MiB: past the 4 MiB inline cap, inside the 1 GiB blob bound
    payload = b"".join(blocks)
    digest = hashlib.sha256(payload).hexdigest()

    response = await _upload(api, media_type="image/png", payload=payload, chunks=blocks,
                             headers=(("Content-Length", str(len(payload))),))

    assert response.status == 201
    assert cp.parse_blob_ref(response.document).size_bytes == len(payload)


async def test_a_declared_size_over_one_gib_is_413_before_any_byte(api) -> None:
    response = await _call(api, "POST", "/internal/blobs",
                           headers=(VERSION, ("Content-Type", "application/json"),
                                    ("X-Content-SHA256", "0" * 64),
                                    ("Content-Length", str(MAX_BLOB_BYTES + 1))),
                           body=b"")

    assert response.status == 413 and _error(response).code == "payload_too_large"


async def test_a_blob_held_by_a_reader_refuses_delete_with_409(api) -> None:
    ref = cp.parse_blob_ref((await _upload(api)).document)
    assert api.blobs.metadata.acquire_lease(ref.blob_id, "exec-1") is True
    try:
        held = await _call(api, "DELETE", f"/internal/blobs/{ref.blob_id}", headers=(VERSION,))
    finally:
        api.blobs.metadata.release_lease(ref.blob_id, "exec-1")

    assert held.status == 409 and _error(held).code == "blob_in_use"


async def test_a_missing_or_foreign_blob_is_404(api) -> None:
    ref = cp.parse_blob_ref((await _upload(api)).document)
    other = os.getuid() + 1 if os.getuid() < 65534 else os.getuid() - 1

    unknown = await _call(api, "GET", "/internal/blobs/does-not-exist", headers=(VERSION,))
    foreign = await _call(api, "GET", f"/internal/blobs/{ref.blob_id}", headers=(VERSION,),
                          peer=PeerIdentity(other))

    assert unknown.status == 404 and _error(unknown).code == "not_found"
    assert foreign.status == 404  # another owner must not learn that this blob exists


async def test_a_malformed_blob_id_never_matches_a_route(api) -> None:
    response = await _call(api, "GET", "/internal/blobs/BAD_ID", headers=(VERSION,))

    assert response.status == 404 and _error(response).code == "not_found"
