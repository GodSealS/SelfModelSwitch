"""Idempotent writes without a route or a retry bypassing the state machine (M04/P13).

A record is keyed by `(route, owner, key)` and bound to the fingerprint of the
request payload under the current boot key; the same key with a different payload
is a conflict, a concurrent retry can never create a second object, and a restart
changes the key so an old token or an old index entry cannot replay an execution.

The store is synchronous and I/O-free: the caller runs it under the scheduler's
lock, so the "first claim wins" decision is atomic by construction.
"""
from __future__ import annotations

import hmac
import json
import secrets
import time
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Callable

from .control_identity import IdentityError, canonical_claims

PENDING = "pending"
COMPLETED = "completed"
FAILED = "failed"


@dataclass
class IdempotencyRecord:
    route: str
    owner: str
    key: str
    fingerprint: str
    state: str = PENDING
    created_at: float = 0.0
    completed_at: float | None = None
    status: int | None = None
    resource_id: str | None = None
    body: dict[str, Any] | None = None
    active_until: float | None = None
    token_fingerprint: str | None = None

    def replayable(self) -> bool:
        return self.state == COMPLETED and (self.status is not None or self.body is not None)


class IdempotencyError(IdentityError):
    """`idempotency_conflict` (different payload) or `busy` (first attempt running)."""


class IdempotencyStore:
    """One in-memory registry per boot; the boot key is never persisted."""

    def __init__(self, *, boot_key: bytes | None = None, retention_seconds: float = 24 * 3600.0, clock: Callable[[], float] = time.time) -> None:
        if retention_seconds <= 0:
            raise ValueError("retention must be positive")
        self._boot_key = boot_key or secrets.token_bytes(32)
        self.retention_seconds = retention_seconds
        self._clock = clock
        self.records: dict[tuple[str, str, str], IdempotencyRecord] = {}

    @property
    def boot_fingerprint(self) -> str:
        return sha256(self._boot_key).hexdigest()[:16]

    def fingerprint(self, *, route: str, owner: str, key: str, payload: Any) -> str:
        """The fixed HMAC input: route and owner are part of the namespace."""
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str).encode("utf-8")
        return hmac.new(self._boot_key, canonical_claims({"route": route, "owner": owner, "key": key}) + b"\x00" + body, sha256).hexdigest()

    def get(self, *, route: str, owner: str, key: str) -> IdempotencyRecord | None:
        return self.records.get((route, owner, key))

    def begin(self, *, route: str, owner: str, key: str, payload: Any, token_fingerprint: str | None = None, active_until: float | None = None) -> IdempotencyRecord:
        """Claim the key, or replay/refuse; exactly one creation per (route, owner, key, payload)."""
        fingerprint = self.fingerprint(route=route, owner=owner, key=key, payload=payload)
        existing = self.records.get((route, owner, key))
        if existing is not None:
            if not hmac.compare_digest(existing.fingerprint, fingerprint):
                raise IdempotencyError("idempotency_conflict", "the key was used with different input")
            if existing.state == PENDING:
                raise IdempotencyError("busy", "the first attempt is still running")
            if existing.state == COMPLETED:
                return existing
            self.records.pop((route, owner, key), None)
        record = IdempotencyRecord(
            route=route,
            owner=owner,
            key=key,
            fingerprint=fingerprint,
            state=PENDING,
            created_at=self._clock(),
            active_until=active_until,
            token_fingerprint=token_fingerprint,
        )
        self.records[(route, owner, key)] = record
        return record

    def complete(self, record: IdempotencyRecord, *, status: int, resource_id: str | None = None, body: dict[str, Any] | None = None, active_until: float | None = None, token_fingerprint: str | None = None) -> IdempotencyRecord:
        record.state = COMPLETED
        record.status = status
        record.resource_id = resource_id
        record.body = body
        record.completed_at = self._clock()
        if token_fingerprint is not None:
            record.token_fingerprint = token_fingerprint
        if active_until is not None:
            record.active_until = active_until
        return record

    def fail(self, record: IdempotencyRecord) -> None:
        """A failed attempt releases the key so the client may retry it."""
        self.records.pop((record.route, record.owner, record.key), None)

    def sweep(self, now: float | None = None) -> tuple[IdempotencyRecord, ...]:
        """Drop finished records older than the retention window.

        An active object (a session that is still running) keeps its record:
        `active_until` protects it even when the created record is old.
        """
        moment = self._clock() if now is None else now
        removed: list[IdempotencyRecord] = []
        for key, record in list(self.records.items()):
            if record.state not in {COMPLETED, FAILED}:
                continue
            if record.active_until is not None and record.active_until > moment:
                continue
            if moment - (record.completed_at or record.created_at) < self.retention_seconds:
                continue
            self.records.pop(key, None)
            removed.append(record)
        return tuple(removed)

    def replay(self, record: IdempotencyRecord, *, payload: Any) -> dict[str, Any]:
        """A GET recomputes the fingerprint and replays only its own outcome."""
        recomputed = self.fingerprint(route=record.route, owner=record.owner, key=record.key, payload=payload)
        if not hmac.compare_digest(recomputed, record.fingerprint):
            raise IdempotencyError("idempotency_conflict", "the stored outcome belongs to other input")
        if not record.replayable():
            raise IdempotencyError("busy", "the stored attempt has no outcome yet")
        return {"status": record.status, "resource_id": record.resource_id, "body": record.body}
