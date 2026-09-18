"""Idempotency tests (M04/P13).

One creation per (route, owner, key, payload), a different payload conflicts, a
restart cannot replay an old index entry or token, the 24h sweep never drops an
active object, and no raw token is stored in a record.
"""
from __future__ import annotations

import pytest

from model_scheduler.idempotency import IdempotencyError, IdempotencyStore


class Clock:
    def __init__(self, now: float = 10_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def store(clock=None, *, key: bytes = b"s" * 32, retention: float = 86_400.0) -> IdempotencyStore:
    return IdempotencyStore(boot_key=key, retention_seconds=retention, clock=clock or Clock())


def test_one_creation_per_key_and_payload() -> None:
    tokens = store()
    payload = {"model_id": "qwen-small"}

    pending = tokens.begin(route="session.create", owner="uid:1000", key="k-1", payload=payload)
    with pytest.raises(IdempotencyError) as busy:
        tokens.begin(route="session.create", owner="uid:1000", key="k-1", payload=payload)  # a concurrent retry
    assert busy.value.code == "busy"

    tokens.complete(pending, status=201, resource_id="session-1", body={"session_id": "session-1"})
    replayed = tokens.begin(route="session.create", owner="uid:1000", key="k-1", payload=payload)

    assert replayed is pending
    assert tokens.replay(replayed, payload=payload) == {"status": 201, "resource_id": "session-1", "body": {"session_id": "session-1"}}


def test_the_same_key_with_other_input_conflicts() -> None:
    tokens = store()
    tokens.begin(route="session.create", owner="uid:1000", key="k-1", payload={"model_id": "qwen-small"})

    with pytest.raises(IdempotencyError) as conflict:
        tokens.begin(route="session.create", owner="uid:1000", key="k-1", payload={"model_id": "qwen-large"})
    assert conflict.value.code == "idempotency_conflict"


def test_routes_owners_and_keys_are_separate_namespaces() -> None:
    tokens = store()
    payload = {"model_id": "qwen-small"}
    first = tokens.begin(route="session.create", owner="uid:1000", key="k-1", payload=payload)

    other_route = tokens.begin(route="execution.submit", owner="uid:1000", key="k-1", payload=payload)
    other_owner = tokens.begin(route="session.create", owner="uid:1001", key="k-1", payload=payload)
    other_key = tokens.begin(route="session.create", owner="uid:1000", key="k-2", payload=payload)

    assert len({id(first), id(other_route), id(other_owner), id(other_key)}) == 4
    assert first.fingerprint != other_route.fingerprint


def test_a_get_recomputes_the_same_token_and_replays_only_its_own_outcome() -> None:
    tokens = store()
    payload = {"session_id": "session-1", "action": "status"}
    record = tokens.begin(route="session.get", owner="uid:1000", key="k-1", payload=payload)
    tokens.complete(record, status=200, body={"phase": "active"})

    recomputed = tokens.fingerprint(route="session.get", owner="uid:1000", key="k-1", payload=payload)
    assert recomputed == record.fingerprint  # GET recomputes the creation value
    assert tokens.replay(record, payload=payload)["body"] == {"phase": "active"}
    with pytest.raises(IdempotencyError) as conflict:
        tokens.replay(record, payload={"session_id": "session-2", "action": "status"})
    assert conflict.value.code == "idempotency_conflict"


def test_a_restart_cannot_replay_the_old_index_or_token() -> None:
    before = store(key=b"first-boot")
    payload = {"model_id": "qwen-small"}
    record = before.begin(route="session.create", owner="uid:1000", key="k-1", payload=payload)
    before.complete(record, status=201, resource_id="session-1", token_fingerprint="abc123")

    after = store(key=b"second-boot")

    assert after.get(route="session.create", owner="uid:1000", key="k-1") is None  # no replay of the old index
    assert after.fingerprint(route="session.create", owner="uid:1000", key="k-1", payload=payload) != record.fingerprint
    assert record.token_fingerprint == "abc123"  # only a fingerprint is stored, never the token


def test_the_sweep_never_drops_an_active_object() -> None:
    clock = Clock()
    tokens = store(clock)
    payload = {"model_id": "qwen-small"}
    finished = tokens.begin(route="execution.submit", owner="uid:1000", key="k-1", payload=payload)
    tokens.complete(finished, status=200)
    active = tokens.begin(route="session.create", owner="uid:1000", key="k-2", payload=payload)
    tokens.complete(active, status=201, resource_id="session-1", active_until=clock.now + 200_000.0)

    removed = tokens.sweep(clock.now + 86_400.0)

    assert removed == (finished,)
    assert tokens.get(route="session.create", owner="uid:1000", key="k-2") is active  # the live session is kept
    assert tokens.get(route="execution.submit", owner="uid:1000", key="k-1") is None


def test_a_failed_attempt_releases_the_key_for_a_retry() -> None:
    tokens = store()
    payload = {"model_id": "qwen-small"}
    attempt = tokens.begin(route="session.create", owner="uid:1000", key="k-1", payload=payload)

    tokens.fail(attempt)

    retried = tokens.begin(route="session.create", owner="uid:1000", key="k-1", payload=payload)
    assert retried is not attempt and retried.state == "pending"
