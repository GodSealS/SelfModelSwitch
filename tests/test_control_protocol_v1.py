"""Control-protocol-v1 contract tests (M01/P02).

The draft tracked in commit b3efb78 pinned part of the DTO surface. This file
completes it to the full C03--C06 contract: session-view phase/boot/hard-deadline
fields, terminal-evidence rules with a mandatory fence, dispatch/instance
pairing (a queued cancellation stays legal without a container identity),
per-capability parameter whitelists, byte limits, UTF-8/NUL rules and the
reproducible schema export.

Every rejection test supplies complete input and asserts the specific rule, so a
passing suite cannot be explained by a required field that happened to be
missing.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from model_scheduler import control_protocol_v1 as cp
from model_scheduler.contracts_v2 import MODEL_CAPABILITIES


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


def _fence(**overrides) -> dict:
    fence = {
        "boot_id": "boot-0001",
        "model_id": "qwen25vl-7b-q4",
        "generation": 3,
        "operation_id": "op-0001",
        "execution_id": "e-1",
        "attempt": 1,
    }
    fence.update(overrides)
    return fence


def _session_view(**overrides) -> dict:
    view = {
        "session_id": "s-1",
        "state": "active",
        "phase": None,
        "boot_id": "boot-0001",
        "model_id": "qwen25vl-7b-q4",
        "expires_in_ms": 25000,
        "hard_remaining_ms": 3550000,
        "owner_token": "boot-0001.token",
        "error": None,
    }
    view.update(overrides)
    return view


def _execution_view(**overrides) -> dict:
    view = {
        "execution_id": "e-1",
        "state": "running",
        "dispatch_state": "dispatched",
        "compute_quiescent": None,
        "result": None,
        "error": None,
        "instance": _instance(),
        "fence": _fence(),
    }
    view.update(overrides)
    return view


def _execution_create(**overrides) -> dict:
    request = {
        "session_token": "boot-0001.token",
        "operation": "chat",
        "input": {"inline": {"messages": [{"role": "user", "content": "hi"}]}},
        "parameters": {},
        "idempotency_key": "key-2",
    }
    request.update(overrides)
    return request


def test_protocol_version_and_limits_are_locked():
    assert cp.PROTOCOL_VERSION == 1
    assert cp.PROTOCOL_VERSION_HEADER == "X-SMS-Protocol-Version"
    assert cp.MAX_INLINE_INPUT_BYTES == 4 * 1024 * 1024
    assert cp.MAX_BLOB_BYTES == 1024**3
    assert cp.BLOB_RESULT_RETENTION_SECONDS == 86400
    assert cp.IDEMPOTENCY_KEY_MAX_LENGTH == 128
    assert cp.MAX_PARAMETERS_BYTES == 8192
    assert cp.CORRELATION_ID_MAX_LENGTH == 128
    assert cp.REQUEST_ID_MAX_LENGTH == 128


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

    contradictory = _error_document()
    contradictory["error"]["retryable"] = False
    with pytest.raises(cp.ContractError):
        cp.parse_error_document(contradictory)


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
    with pytest.raises(cp.ContractError):
        cp.is_retryable_error("teapot")


def test_error_table_matches_the_plan():
    expected = {
        "malformed_json": (400, False),
        "unsupported_protocol": (400, False),
        "peer_forbidden": (403, False),
        "not_found": (404, False),
        "stale_token": (409, False),
        "session_expired": (409, False),
        "idempotency_conflict": (409, False),
        "busy": (409, True),
        "blob_in_use": (409, True),
        "reference_expired": (410, False),
        "payload_too_large": (413, False),
        "unsupported_media_type": (415, False),
        "contract_violation": (422, False),
        "envelope_exceeded": (422, False),
        "capability_mismatch": (422, False),
        "queue_full": (429, True),
        "quota_exceeded": (429, True),
        "backend_failed": (502, False),
        "instance_unknown": (503, True),
        "storage_unavailable": (503, True),
        "resource_unavailable": (503, True),
        "temporarily_unavailable": (503, True),
        "queue_timeout": (504, True),
        "execution_timeout": (504, False),
    }
    assert dict(cp.ERROR_STATUS) == {code: status for code, (status, _) in expected.items()}
    assert cp.RETRYABLE_ERROR_CODES == frozenset(
        code for code, (_, retryable) in expected.items() if retryable
    )
    for code, (_, retryable) in expected.items():
        document = cp.error_document(code, "x", "req-1")
        assert document["error"]["retryable"] is retryable
        with pytest.raises(cp.ContractError):  # retryable must match the table
            cp.parse_error_document(
                {"error": {"code": code, "message": "x", "retryable": not retryable}, "request_id": "r"}
            )


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
        lambda d: d.update({"owner": "uid:1000"}),
    ):
        data = {"model_id": "qwen25vl-7b-q4", "idempotency_key": "key-1"}
        mutate(data)
        with pytest.raises(cp.ContractError):
            cp.parse_session_create_request(data)


def test_session_view_states_and_token_rules():
    view = cp.parse_session_view(_session_view())
    assert view.state == "active"
    assert view.owner_token == "boot-0001.token"

    closed = cp.parse_session_view(_session_view(state="closed", owner_token=None))
    assert closed.owner_token is None

    for bad_state in ("ready", "unknown"):
        with pytest.raises(cp.ContractError):
            cp.parse_session_view(_session_view(state=bad_state))
    with pytest.raises(cp.ContractError):  # a non-closed session must return a token
        cp.parse_session_view(_session_view(owner_token=None))
    with pytest.raises(cp.ContractError):  # a closed session must not return a token
        cp.parse_session_view(_session_view(state="closed", owner_token="boot-0001.token"))
    with pytest.raises(cp.ContractError):
        cp.parse_session_view(_session_view(expires_in_ms=True))
    with pytest.raises(cp.ContractError):
        cp.parse_session_view(_session_view(hard_remaining_ms=-1))


def test_session_view_phase_and_error_fields():
    preparing = cp.parse_session_view(_session_view(state="preparing", phase="queued", expires_in_ms=0))
    assert preparing.phase == "queued"
    assert preparing.expires_in_ms == 0

    blocked = cp.parse_session_view(
        _session_view(
            state="blocked",
            phase=None,
            error={"code": "temporarily_unavailable", "message": "cleanup not proven", "retryable": True},
        )
    )
    assert blocked.error is not None and blocked.error.retryable is True

    with pytest.raises(cp.ContractError):  # "ready" is not a preparing phase
        cp.parse_session_view(_session_view(phase="ready"))
    with pytest.raises(cp.ContractError):  # unknown error code inside the view
        cp.parse_session_view(_session_view(error={"code": "teapot", "message": "x", "retryable": False}))


def test_session_view_requires_every_field():
    for key in sorted(cp.SESSION_VIEW_KEYS):
        data = _session_view()
        del data[key]
        with pytest.raises(cp.ContractError):
            cp.parse_session_view(data)


def test_execution_create_request_is_strict():
    request = cp.parse_execution_create_request(
        _execution_create(
            operation="vision",
            input={"inline": {"image": "data:image/png;base64,AAAA"}},
            parameters={"max_tokens": 4096},
        )
    )
    assert request.operation == "vision"
    assert request.parameters == {"max_tokens": 4096}

    for mutate in (
        lambda d: d.update({"operation": "video"}),
        lambda d: d.update({"session_token": ""}),
        lambda d: d.update({"parameters": {"bad key": 1}}),
        lambda d: d.pop("input"),
        lambda d: d.update({"input": {"inline": {}, "blob": _blob_ref()}}),
        lambda d: d.pop("idempotency_key"),
        lambda d: d.update({"deadline": 10}),
    ):
        data = _execution_create()
        mutate(data)
        with pytest.raises(cp.ContractError):
            cp.parse_execution_create_request(data)


def test_control_operations_are_the_closed_execution_set():
    # TC01: the protocol's operation set is its own closed set; it is never
    # re-derived from the model capability matrix that also carries tools/thinking.
    assert cp.EXECUTION_OPERATIONS == frozenset({"chat", "vision", "embeddings", "rerank"})
    assert cp.CAPABILITIES == cp.EXECUTION_OPERATIONS
    assert set(cp.PARAMETER_RULES) == set(cp.EXECUTION_OPERATIONS)

    assert {"tools", "thinking"} <= MODEL_CAPABILITIES
    assert not ({"tools", "thinking"} & cp.EXECUTION_OPERATIONS)


def test_tools_and_thinking_are_not_execution_operations():
    # A model capability must not leak into the control protocol as an operation:
    # the request is refused with the existing contract error, never a KeyError.
    for operation in ("tools", "thinking"):
        with pytest.raises(cp.ContractError) as refused:
            cp.parse_execution_create_request(_execution_create(operation=operation))
        assert "operation" in str(refused.value)


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
        ("media_type", "image/png; charset=utf-8"),
        ("owner", ""),
        ("blob_id", "UPPER"),
    ):
        data = _blob_ref()
        data[key] = value
        with pytest.raises(cp.ContractError):
            cp.parse_blob_ref(data)


def test_execution_view_terminal_rules():
    succeeded = cp.parse_execution_view(
        _execution_view(state="succeeded", compute_quiescent=True, result=_blob_ref())
    )
    assert succeeded.state == "succeeded"
    assert succeeded.result is not None
    assert succeeded.compute_quiescent is True

    with pytest.raises(cp.ContractError):  # succeeded without result
        cp.parse_execution_view(_execution_view(state="succeeded", compute_quiescent=True))
    with pytest.raises(cp.ContractError):  # not quiescent
        cp.parse_execution_view(
            _execution_view(state="succeeded", compute_quiescent=False, result=_blob_ref())
        )
    with pytest.raises(cp.ContractError):  # missing quiescence proof
        cp.parse_execution_view(
            _execution_view(state="succeeded", compute_quiescent=None, result=_blob_ref())
        )
    with pytest.raises(cp.ContractError):  # succeeded with an error
        cp.parse_execution_view(
            _execution_view(
                state="succeeded",
                compute_quiescent=True,
                result=_blob_ref(),
                error={"code": "backend_failed", "message": "x", "retryable": False},
            )
        )

    failed = cp.parse_execution_view(
        _execution_view(
            state="failed",
            compute_quiescent=True,
            error={"code": "backend_failed", "message": "backend died", "retryable": False},
        )
    )
    assert failed.error is not None and failed.result is None
    with pytest.raises(cp.ContractError):  # failed without error
        cp.parse_execution_view(_execution_view(state="failed", compute_quiescent=True))
    with pytest.raises(cp.ContractError):  # failed with a result
        cp.parse_execution_view(
            _execution_view(
                state="failed",
                compute_quiescent=True,
                result=_blob_ref(),
                error={"code": "backend_failed", "message": "x", "retryable": False},
            )
        )

    cancelled = cp.parse_execution_view(
        _execution_view(
            state="cancelled",
            compute_quiescent=True,
            error={"code": "execution_timeout", "message": "cancelled", "retryable": False},
        )
    )
    assert cancelled.state == "cancelled"


def test_execution_view_requires_a_fence_identifying_the_execution():
    with pytest.raises(cp.ContractError):  # fence missing entirely
        data = _execution_view()
        del data["fence"]
        cp.parse_execution_view(data)
    with pytest.raises(cp.ContractError):  # a non-execution fence is not enough
        cp.parse_execution_view(_execution_view(fence=_fence(execution_id=None, attempt=None)))
    with pytest.raises(cp.ContractError):  # fence belongs to another execution
        cp.parse_execution_view(_execution_view(fence=_fence(execution_id="e-2")))


def test_execution_view_non_terminal_rejects_terminal_fields():
    running = cp.parse_execution_view(_execution_view())
    assert running.state == "running"
    assert running.compute_quiescent is None
    assert running.result is None
    assert running.error is None

    with pytest.raises(cp.ContractError):  # quiescent on a running execution
        cp.parse_execution_view(_execution_view(compute_quiescent=True))
    with pytest.raises(cp.ContractError):  # result on a cancelling execution
        cp.parse_execution_view(_execution_view(state="cancelling", result=_blob_ref()))
    with pytest.raises(cp.ContractError):  # error on a queued execution
        cp.parse_execution_view(
            _execution_view(
                state="queued",
                dispatch_state="not_started",
                instance=None,
                error={"code": "busy", "message": "x", "retryable": True},
            )
        )
    with pytest.raises(cp.ContractError):  # unknown field
        data = _execution_view()
        data["extra"] = 1
        cp.parse_execution_view(data)


def test_execution_view_dispatch_state_pairs_with_instance():
    queued = cp.parse_execution_view(
        _execution_view(state="queued", dispatch_state="not_started", instance=None)
    )
    assert queued.instance is None

    with pytest.raises(cp.ContractError):  # not_started must not invent a container
        cp.parse_execution_view(_execution_view(state="queued", dispatch_state="not_started"))
    with pytest.raises(cp.ContractError):  # dispatched requires a full identity
        cp.parse_execution_view(_execution_view(dispatch_state="dispatched", instance=None))
    with pytest.raises(cp.ContractError):  # unknown dispatch state
        cp.parse_execution_view(_execution_view(dispatch_state="sent"))

    # a queued cancellation is legal with not_started evidence and no container
    cancelled = cp.parse_execution_view(
        _execution_view(
            state="cancelled",
            dispatch_state="not_started",
            instance=None,
            compute_quiescent=True,
            error={"code": "queue_timeout", "message": "cancelled before dispatch", "retryable": True},
        )
    )
    assert cancelled.instance is None
    assert cancelled.error is not None


def test_fence_requires_paired_execution_fields():
    fence = cp.parse_fence(_fence())
    assert fence.execution_id == "e-1"
    assert fence.attempt == 1

    operation = cp.parse_fence(_fence(execution_id=None, attempt=None))
    assert operation.execution_id is None and operation.attempt is None

    for pair in ({"execution_id": "e-1", "attempt": None}, {"execution_id": None, "attempt": 1}):
        with pytest.raises(cp.ContractError):
            cp.parse_fence(_fence(**pair))

    for bad in (
        {"generation": 0},
        {"generation": True},
        {"attempt": 0},
        {"attempt": True},
        {"model_id": "UPPER"},
        {"operation_id": "op 1"},
        {"boot_id": ""},
        {"execution_id": "UPPER"},
    ):
        with pytest.raises(cp.ContractError):
            cp.parse_fence(_fence(**bad))


def test_instance_identity_is_strict():
    identity = cp.parse_instance_identity(_instance())
    assert identity.model_id == "qwen25vl-7b-q4"

    for key, value in (
        ("candidate_digest", "z" * 64),
        ("image_digest", "repo/img:latest"),
        ("model_id", "UPPER"),
        ("started_at", ""),
        ("container_id", ""),
        ("started_at", "2026-09-17T05:00:00"),
        ("started_at", "2026-09-17T05:00:00+08:00"),
    ):
        data = _instance()
        data[key] = value
        with pytest.raises(cp.ContractError):
            cp.parse_instance_identity(data)


def test_parameters_follow_the_capability_whitelist():
    chat = cp.parse_execution_create_request(
        _execution_create(
            parameters={"max_tokens": 512, "temperature": 0.7, "top_p": 0.9, "seed": 7}
        )
    )
    assert chat.parameters == {"max_tokens": 512, "temperature": 0.7, "top_p": 0.9, "seed": 7}

    # A boundary round keeps generating to its declared budget instead of stopping early.
    boundary = cp.parse_execution_create_request(_execution_create(parameters={"max_tokens": 4096,
                                                                               "ignore_eos": True}))
    assert boundary.parameters == {"max_tokens": 4096, "ignore_eos": True}
    with pytest.raises(cp.ContractError):  # it is a boolean, not a number
        cp.parse_execution_create_request(_execution_create(parameters={"ignore_eos": 1}))
    with pytest.raises(cp.ContractError):  # and embeddings never takes it
        cp.parse_execution_create_request(_execution_create(operation="embeddings",
                                                            parameters={"ignore_eos": True}))

    with pytest.raises(cp.ContractError):  # not a chat parameter
        cp.parse_execution_create_request(_execution_create(parameters={"repetition_penalty": 1.1}))
    with pytest.raises(cp.ContractError):  # text belongs to vision only
        cp.parse_execution_create_request(_execution_create(parameters={"text": "hello"}))
    with pytest.raises(cp.ContractError):  # max_tokens is not an embeddings parameter
        cp.parse_execution_create_request(_execution_create(operation="embeddings", parameters={"max_tokens": 1}))

    vision = cp.parse_execution_create_request(
        _execution_create(operation="vision", parameters={"max_tokens": 4096, "text": "describe"})
    )
    assert vision.parameters["text"] == "describe"

    embeddings = cp.parse_execution_create_request(
        _execution_create(operation="embeddings", parameters={"encoding_format": "float"})
    )
    assert embeddings.parameters == {"encoding_format": "float"}
    with pytest.raises(cp.ContractError):  # first release accepts only float
        cp.parse_execution_create_request(
            _execution_create(operation="embeddings", parameters={"encoding_format": "base64"})
        )

    rerank = cp.parse_execution_create_request(
        _execution_create(operation="rerank", parameters={"top_n": 3, "return_documents": True})
    )
    assert rerank.parameters == {"top_n": 3, "return_documents": True}
    with pytest.raises(cp.ContractError):  # top_n must be a positive integer
        cp.parse_execution_create_request(_execution_create(operation="rerank", parameters={"top_n": 0}))


def test_parameter_ranges_are_locked():
    for parameters in (
        {"temperature": 2.5},
        {"temperature": -0.1},
        {"top_p": 0},
        {"top_p": 1.5},
        {"seed": -1},
        {"seed": 2**31},
        {"max_tokens": 0},
    ):
        with pytest.raises(cp.ContractError):
            cp.parse_execution_create_request(_execution_create(parameters=parameters))

    boundary = cp.parse_execution_create_request(
        _execution_create(parameters={"temperature": 0, "top_p": 1, "seed": 0})
    )
    assert boundary.parameters["temperature"] == 0
    assert boundary.parameters["top_p"] == 1


def test_parameters_reject_non_finite_and_boolean_values():
    for parameters in (
        {"temperature": float("nan")},
        {"temperature": float("inf")},
        {"top_p": float("-inf")},
        {"max_tokens": True},
        {"seed": False},
    ):
        with pytest.raises(cp.ContractError):
            cp.parse_execution_create_request(_execution_create(parameters=parameters))

    with pytest.raises(cp.ContractError):  # boolean parameter must be a real bool
        cp.parse_execution_create_request(
            _execution_create(operation="rerank", parameters={"return_documents": 1})
        )


def test_inline_and_parameter_byte_limits():
    huge_inline = {"payload": "x" * (cp.MAX_INLINE_INPUT_BYTES + 1)}
    with pytest.raises(cp.ContractError):
        cp.parse_execution_input({"inline": huge_inline})

    ok_inline = {"payload": "x" * 1024}
    assert cp.parse_execution_input({"inline": ok_inline}).inline is not None

    huge_text = "y" * (cp.MAX_PARAMETERS_BYTES + 1)
    with pytest.raises(cp.ContractError):
        cp.parse_execution_create_request(
            _execution_create(operation="vision", parameters={"text": huge_text})
        )


def test_strings_reject_nul_and_surrogates():
    with pytest.raises(cp.ContractError):
        cp.parse_session_create_request({"model_id": "m-1", "idempotency_key": "bad\x00key"})
    with pytest.raises(cp.ContractError):
        cp.parse_session_create_request({"model_id": "m-1", "idempotency_key": "bad\ud800"})
    with pytest.raises(cp.ContractError):
        cp.parse_execution_input({"inline": {"prompt": "bad\x00text"}})
    with pytest.raises(cp.ContractError):
        cp.parse_execution_input({"inline": {"nested": {"deep": ["ok", "bad\ud800"]}}})
    with pytest.raises(cp.ContractError):  # nested non-finite number
        cp.parse_execution_input({"inline": {"nested": [1, float("inf")]}})


def test_idempotency_fingerprint_is_owner_key_and_payload_sensitive():
    first = cp.idempotency_fingerprint("client-a", "key-1", {"operation": "chat", "input": {"prompt": "hi"}})
    assert len(first) == 64
    assert first == cp.idempotency_fingerprint("client-a", "key-1", {"input": {"prompt": "hi"}, "operation": "chat"})
    assert first != cp.idempotency_fingerprint("client-b", "key-1", {"operation": "chat", "input": {"prompt": "hi"}})
    assert first != cp.idempotency_fingerprint("client-a", "key-2", {"operation": "chat", "input": {"prompt": "hi"}})
    assert first != cp.idempotency_fingerprint("client-a", "key-1", {"operation": "chat", "input": {"prompt": "ho"}})
    with pytest.raises(cp.ContractError):  # non-finite payloads cannot be canonicalised
        cp.idempotency_fingerprint("client-a", "key-1", {"temperature": float("nan")})


def test_reference_expiry_helper():
    now = 1_000_000.0
    assert cp.reference_expired(now - cp.BLOB_RESULT_RETENTION_SECONDS - 1, now) is True
    assert cp.reference_expired(now - cp.BLOB_RESULT_RETENTION_SECONDS + 1, now) is False
    assert cp.reference_expired(now - cp.BLOB_RESULT_RETENTION_SECONDS, now) is True
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
    with pytest.raises(cp.ContractError):
        cp.parse_error_document(
            {"error": {"code": "busy", "message": "x", "retryable": True}, "request_id": "r" * 129}
        )


def test_strict_json_parsing_rejects_duplicates_and_non_finite():
    with pytest.raises(cp.ContractError):
        cp.parse_json_document('{"a": 1, "a": 2}')
    with pytest.raises(cp.ContractError):
        cp.parse_json_document('{"a": 1e999}')
    with pytest.raises(cp.ContractError):
        cp.parse_json_document('{"a": NaN}')

    assert cp.parse_json_document('{"a": [1, 2], "b": "x"}') == {"a": [1, 2], "b": "x"}


def test_schema_document_covers_every_capability_parameter():
    schema = cp.schema_document()
    assert schema["protocol_version"] == cp.PROTOCOL_VERSION
    assert sorted(schema["errors"]) == sorted(cp.ERROR_STATUS)
    for operation in sorted(cp.CAPABILITIES):
        assert schema["capabilities"][operation] == sorted(cp.PARAMETER_RULES[operation])
        parameters = schema["$defs"][f"Parameters_{operation}"]
        assert sorted(parameters["properties"]) == sorted(cp.PARAMETER_RULES[operation])
        assert parameters["additionalProperties"] is False


def test_schema_export_is_reproducible(tmp_path):
    output = tmp_path / "control-v1.json"
    cp.write_schema(output)
    committed = Path(__file__).resolve().parents[1] / "schemas" / "control-v1.json"
    assert committed.read_bytes() == output.read_bytes()


def test_export_schema_cli(tmp_path):
    output = tmp_path / "schema.json"
    assert cp.main(["export-schema", "--output", str(output)]) == 0
    assert output.read_text(encoding="utf-8") == cp.render_schema_text()


# ---------------------------------------------------------------------------
# P10: the single write-back fence rule.
# ---------------------------------------------------------------------------


def _typed_fence(**overrides) -> cp.Fence:
    values = {
        "boot_id": "boot-0001",
        "model_id": "qwen25vl-7b-q4",
        "generation": 3,
        "operation_id": "op-0001",
        "execution_id": "e-1",
        "attempt": 2,
    }
    values.update(overrides)
    return cp.Fence(**values)


def test_a_write_back_is_accepted_only_for_the_same_fence() -> None:
    current = _typed_fence()

    assert cp.writeback_decision(current, _typed_fence()).accepted is True
    accepted = cp.writeback_decision(current, _typed_fence(attempt=3))
    assert accepted.accepted is True  # a newer attempt of the same operation may apply
    assert accepted.reason is None


@pytest.mark.parametrize(
    ("incoming", "reason"),
    [
        (_typed_fence(boot_id="boot-0002"), "stale_boot"),
        (_typed_fence(model_id="other-model"), "foreign_model"),
        (_typed_fence(generation=4), "stale_generation"),
        (_typed_fence(operation_id="op-0002"), "stale_operation"),
        (_typed_fence(execution_id="e-2"), "foreign_execution"),
        (_typed_fence(attempt=1), "stale_attempt"),
        (_typed_fence(attempt=None, execution_id=None), "foreign_execution"),
    ],
)
def test_a_stale_write_back_is_rejected_with_its_reason_and_raw_fence(incoming: cp.Fence, reason: str) -> None:
    decision = cp.writeback_decision(_typed_fence(), incoming)

    assert decision.accepted is False
    assert decision.reason == reason
    assert decision.reason in cp.WRITEBACK_REJECTIONS
    document = decision.as_dict()
    assert document["fence"]["boot_id"] == incoming.boot_id  # the raw material is preserved
    assert document["fence"]["attempt"] == incoming.attempt
    assert document["accepted"] is False


def test_an_operation_level_fence_has_no_attempt() -> None:
    current = _typed_fence(execution_id=None, attempt=None)

    decision = cp.writeback_decision(current, _typed_fence(execution_id=None, attempt=None))

    assert decision.accepted is True  # load/stop fences carry no execution attempt
    mismatch = cp.writeback_decision(current, _typed_fence())
    assert (mismatch.accepted, mismatch.reason) == (False, "attempt_mismatch")
    with pytest.raises(cp.ContractError):
        cp.writeback_decision(current, "not-a-fence")
    with pytest.raises(cp.ContractError):
        cp.fence_document({"boot_id": "boot-0001"})
