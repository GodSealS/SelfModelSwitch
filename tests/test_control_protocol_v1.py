from __future__ import annotations

import json

import pytest

from model_scheduler import control_protocol_v1 as cp


def _error_document() -> dict:
    return {
        "error": {"code": "queue_full", "message": "queue capacity reached", "retryable": True},
        "request_id": "req-0001",
    }


def _blob_ref() -> dict:
    return {
        "blob_id": "b-0001",
        "owner": "client-a",
        "sha256": "a" * 64,
        "size_bytes": 1024,
        "media_type": "image/png",
    }


def _instance() -> dict:
    return {
        "container_id": "c-abc",
        "started_at": "2026-09-17T05:00:00Z",
        "deployment_id": "orin-local",
        "model_id": "qwen25vl-7b-q4",
        "runtime_id": "llama-cpp-cuda-sm87-4bc272f",
        "candidate_digest": "b" * 64,
        "image_digest": "ghcr.io/example/llama-cuda@sha256:" + "c" * 64,
    }


def test_protocol_version_and_limits_are_locked():
    assert cp.PROTOCOL_VERSION == 1
    assert cp.MAX_INLINE_INPUT_BYTES == 4 * 1024 * 1024
    assert cp.MAX_BLOB_BYTES == 1024**3
    assert cp.BLOB_RESULT_RETENTION_SECONDS == 86400
    assert cp.IDEMPOTENCY_KEY_MAX_LENGTH == 128
    assert cp.MAX_PARAMETERS_BYTES == 8192


def test_error_document_roundtrip_and_default_retryable():
    detail, request_id = cp.parse_error_document(_error_document())
    assert detail.code == "queue_full"
    assert detail.retryable is True
    assert request_id == "req-0001"

    document = cp.error_document("contract_violation", "unknown field", "req-2")
    assert document["error"]["retryable"] is False
    assert document["error"]["code"] == "contract_violation"
    with pytest.raises(cp.ContractError):
        cp.error_document("no_such_code", "x", "req-3")


def test_error_document_rejects_unknown_and_typed_fields():
    extra = _error_document()
    extra["error"]["hint"] = "x"
    with pytest.raises(cp.ContractError):
        cp.parse_error_document(extra)

    bad_bool = _error_document()
    bad_bool["error"]["retryable"] = 1
    with pytest.raises(cp.ContractError):
        cp.parse_error_document(bad_bool)

    unknown_code = _error_document()
    unknown_code["error"]["code"] = "teapot"
    with pytest.raises(cp.ContractError):
        cp.parse_error_document(unknown_code)

    no_request = _error_document()
    del no_request["request_id"]
    with pytest.raises(cp.ContractError):
        cp.parse_error_document(no_request)


def test_error_status_map_is_consistent():
    for code, status in cp.ERROR_STATUS.items():
        assert isinstance(status, int) and 400 <= status <= 599
        assert isinstance(cp.is_retryable_error(code), bool)
    assert cp.ERROR_STATUS["contract_violation"] == 422
    assert cp.ERROR_STATUS["envelope_exceeded"] == 422
    assert cp.ERROR_STATUS["stale_token"] == 409
    assert cp.ERROR_STATUS["reference_expired"] == 410
    assert cp.ERROR_STATUS["queue_full"] == 429
    assert cp.ERROR_STATUS["instance_unknown"] == 503
    assert cp.ERROR_STATUS["queue_timeout"] == 504
    assert cp.is_retryable_error("queue_full") is True
    assert cp.is_retryable_error("contract_violation") is False


def test_session_create_request_is_strict():
    request = cp.parse_session_create_request(
        {"model_id": "qwen25vl-7b-q4", "idempotency_key": "key-1", "correlation_id": "corr-1"}
    )
    assert request.model_id == "qwen25vl-7b-q4"
    assert request.correlation_id == "corr-1"

    minimal = cp.parse_session_create_request({"model_id": "qwen25vl-7b-q4", "idempotency_key": "key-1"})
    assert minimal.correlation_id is None

    for mutate in (
        lambda d: d.update({"unknown": 1}),
        lambda d: d.update({"model_id": "UPPER"}),
        lambda d: d.update({"idempotency_key": ""}),
        lambda d: d.update({"idempotency_key": "k" * 129}),
        lambda d: d.update({"correlation_id": "c" * 129}),
        lambda d: d.pop("idempotency_key"),
    ):
        data = {"model_id": "qwen25vl-7b-q4", "idempotency_key": "key-1"}
        mutate(data)
        with pytest.raises(cp.ContractError):
            cp.parse_session_create_request(data)


def test_session_view_states_and_token_rules():
    view = cp.parse_session_view({"session_id": "s-1", "state": "active", "expires_in_ms": 25000, "owner_token": "tok"})
    assert view.state == "active"
    assert view.owner_token == "tok"

    closed = cp.parse_session_view({"session_id": "s-1", "state": "closed", "expires_in_ms": 0, "owner_token": None})
    assert closed.owner_token is None

    for bad_state in ("ready", "unknown"):
        with pytest.raises(cp.ContractError):
            cp.parse_session_view({"session_id": "s-1", "state": bad_state, "expires_in_ms": 1, "owner_token": None})
    with pytest.raises(cp.ContractError):
        cp.parse_session_view({"session_id": "s-1", "state": "active", "expires_in_ms": 1})
    with pytest.raises(cp.ContractError):
        cp.parse_session_view({"session_id": "s-1", "state": "active", "expires_in_ms": True, "owner_token": None})


def test_execution_create_request_is_strict():
    request = cp.parse_execution_create_request(
        {
            "session_token": "tok",
            "operation": "vision",
            "input": {"inline": {"image": "data:image/png;base64,AAAA"}},
            "parameters": {"max_tokens": 4096},
            "idempotency_key": "key-2",
        }
    )
    assert request.operation == "vision"
    assert request.parameters == {"max_tokens": 4096}

    for mutate in (
        lambda d: d.update({"operation": "video"}),
        lambda d: d.update({"session_token": ""}),
        lambda d: d.update({"parameters": {"bad key": 1}}),
        lambda d: d.pop("input"),
        lambda d: d.update({"input": {"inline": {}, "blob": _blob_ref()}}),
    ):
        data = {
            "session_token": "tok",
            "operation": "chat",
            "input": {"inline": {"prompt": "hi"}},
            "parameters": {},
            "idempotency_key": "key-2",
        }
        mutate(data)
        with pytest.raises(cp.ContractError):
            cp.parse_execution_create_request(data)


def test_execution_input_inline_or_blob_is_exclusive():
    inline = cp.parse_execution_input({"inline": {"prompt": "hello"}})
    assert inline.inline == {"prompt": "hello"}
    assert inline.blob is None

    blob = cp.parse_execution_input({"blob": _blob_ref()})
    assert blob.blob is not None
    assert blob.blob.blob_id == "b-0001"

    for bad in ({}, {"inline": {}}, {"other": 1}, {"inline": {"a": 1}, "blob": _blob_ref()}):
        with pytest.raises(cp.ContractError):
            cp.parse_execution_input(bad)


def test_blob_ref_is_strict():
    ref = cp.parse_blob_ref(_blob_ref())
    assert ref.media_type == "image/png"

    for key, value in (
        ("sha256", "XYZ"),
        ("size_bytes", 0),
        ("size_bytes", cp.MAX_BLOB_BYTES + 1),
        ("media_type", "png"),
        ("media_type", "IMAGE/PNG"),
        ("owner", ""),
        ("blob_id", "UPPER"),
    ):
        data = _blob_ref()
        data[key] = value
        with pytest.raises(cp.ContractError):
            cp.parse_blob_ref(data)


def test_execution_view_terminal_rules():
    succeeded = cp.parse_execution_view(
        {
            "execution_id": "e-1",
            "state": "succeeded",
            "compute_quiescent": True,
            "result": _blob_ref(),
            "error": None,
            "instance": _instance(),
        }
    )
    assert succeeded.state == "succeeded"
    assert succeeded.result is not None

    with pytest.raises(cp.ContractError):
        cp.parse_execution_view(
            {
                "execution_id": "e-1",
                "state": "succeeded",
                "compute_quiescent": True,
                "result": None,
                "error": None,
                "instance": _instance(),
            }
        )
    with pytest.raises(cp.ContractError):
        cp.parse_execution_view(
            {
                "execution_id": "e-1",
                "state": "succeeded",
                "compute_quiescent": False,
                "result": _blob_ref(),
                "error": None,
                "instance": _instance(),
            }
        )
    with pytest.raises(cp.ContractError):
        cp.parse_execution_view(
            {
                "execution_id": "e-1",
                "state": "failed",
                "compute_quiescent": True,
                "result": None,
                "error": None,
                "instance": _instance(),
            }
        )


def test_execution_view_non_terminal_rejects_terminal_fields():
    running = cp.parse_execution_view(
        {
            "execution_id": "e-1",
            "state": "running",
            "compute_quiescent": None,
            "result": None,
            "error": None,
            "instance": None,
        }
    )
    assert running.state == "running"

    with pytest.raises(cp.ContractError):
        cp.parse_execution_view(
            {
                "execution_id": "e-1",
                "state": "running",
                "compute_quiescent": True,
                "result": None,
                "error": None,
                "instance": None,
            }
        )
    with pytest.raises(cp.ContractError):
        cp.parse_execution_view(
            {
                "execution_id": "e-1",
                "state": "cancelling",
                "compute_quiescent": None,
                "result": _blob_ref(),
                "error": None,
                "instance": None,
            }
        )
    with pytest.raises(cp.ContractError):
        cp.parse_execution_view(
            {
                "execution_id": "e-1",
                "state": "queued",
                "compute_quiescent": None,
                "result": None,
                "error": None,
                "instance": None,
                "extra": 1,
            }
        )


def test_instance_identity_is_strict():
    identity = cp.parse_instance_identity(_instance())
    assert identity.model_id == "qwen25vl-7b-q4"

    for key, value in (
        ("candidate_digest", "z" * 64),
        ("image_digest", "repo/img:latest"),
        ("model_id", "UPPER"),
        ("started_at", ""),
        ("container_id", ""),
    ):
        data = _instance()
        data[key] = value
        with pytest.raises(cp.ContractError):
            cp.parse_instance_identity(data)


def test_idempotency_fingerprint_is_owner_key_and_payload_sensitive():
    first = cp.idempotency_fingerprint("client-a", "key-1", {"operation": "chat", "input": {"prompt": "hi"}})
    assert len(first) == 64
    assert first == cp.idempotency_fingerprint("client-a", "key-1", {"input": {"prompt": "hi"}, "operation": "chat"})
    assert first != cp.idempotency_fingerprint("client-b", "key-1", {"operation": "chat", "input": {"prompt": "hi"}})
    assert first != cp.idempotency_fingerprint("client-a", "key-2", {"operation": "chat", "input": {"prompt": "hi"}})
    assert first != cp.idempotency_fingerprint("client-a", "key-1", {"operation": "chat", "input": {"prompt": "ho"}})


def test_reference_expiry_helper():
    now = 1_000_000.0
    assert cp.reference_expired(now - cp.BLOB_RESULT_RETENTION_SECONDS - 1, now) is True
    assert cp.reference_expired(now - cp.BLOB_RESULT_RETENTION_SECONDS + 1, now) is False
    with pytest.raises(cp.ContractError):
        cp.reference_expired(now, now, retention_seconds=0)


def test_protocol_documents_roundtrip_through_json():
    document = cp.error_document("queue_timeout", "no slot within budget", "req-9")
    text = json.dumps(document)
    detail, request_id = cp.parse_error_document(json.loads(text))
    assert detail.code == "queue_timeout"
    assert detail.retryable is True
    assert request_id == "req-9"


def test_correlation_and_request_id_lengths_are_bounded():
    with pytest.raises(cp.ContractError):
        cp.parse_session_create_request(
            {"model_id": "m-1", "idempotency_key": "k", "correlation_id": "c" * (cp.CORRELATION_ID_MAX_LENGTH + 1)}
        )
    with pytest.raises(cp.ContractError):
        cp.parse_error_document({"error": {"code": "busy", "message": "x", "retryable": True}, "request_id": ""})
