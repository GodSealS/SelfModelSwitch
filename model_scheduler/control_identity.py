"""Owner identity and boot-scoped tokens (M04/P13, C04/C08).

Owner comes from a trusted peer identity only (a Unix peer credential injected by
the control listener), never from a header or a body. Tokens are HMAC-signed over
a fixed canonical encoding of `boot/session/model/owner/execution`, the signing
key is generated in memory per boot and never written down, so a restart makes
every old token invalid. Comparisons use a constant-time function and nothing in
this module ever returns or logs the token itself.
"""
from __future__ import annotations

import base64
import hmac
import json
import math
import re
import secrets
import time
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Callable

TOKEN_VERSION = "v1"
_OWNER_RE = re.compile(r"uid:[0-9]{1,10}\Z")
_ID_RE = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,61}[a-z0-9])?\Z")


class IdentityError(RuntimeError):
    """A refusal with a stable code; the API layer maps these to statuses."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class PeerIdentity:
    """The trusted local identity of a control-socket peer."""

    uid: int

    def __post_init__(self) -> None:
        if isinstance(self.uid, bool) or not isinstance(self.uid, int) or self.uid < 0:
            raise IdentityError("peer_forbidden", "a peer identity needs a real uid")

    @property
    def owner(self) -> str:
        return f"uid:{self.uid}"


def owner_from_peer(peer: PeerIdentity | None) -> str:
    """The only accepted source of an owner: a trusted peer credential."""
    if not isinstance(peer, PeerIdentity):
        raise IdentityError("peer_forbidden", "the control listener must inject a peer credential")
    return peer.owner


def check_owner(candidate: str, peer: PeerIdentity | None) -> str:
    """Reject a self-reported owner; a mismatch is a 404-shaped refusal."""
    actual = owner_from_peer(peer)
    if not isinstance(candidate, str) or not _OWNER_RE.fullmatch(candidate) or candidate != actual:
        raise IdentityError("not_found", "the object does not belong to this owner")
    return actual


def canonical_claims(claims: dict[str, Any]) -> bytes:
    """The fixed HMAC input: one canonical JSON encoding, keys sorted."""
    return json.dumps(claims, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


class TokenAuthority:
    """Issues and verifies boot-scoped tokens; the key never leaves memory."""

    def __init__(self, *, boot_key: bytes | None = None, boot_id: str | None = None, clock: Callable[[], float] = time.time) -> None:
        self._boot_key = boot_key or secrets.token_bytes(32)
        self.boot_id = boot_id or secrets.token_hex(8)
        self._clock = clock

    def issue(
        self,
        *,
        owner: str,
        model_id: str,
        session_id: str | None = None,
        execution_id: str | None = None,
        ttl_seconds: float = 60.0,
        now: float | None = None,
    ) -> str:
        if not _OWNER_RE.fullmatch(owner) or not _ID_RE.fullmatch(model_id):
            raise IdentityError("contract_violation", "a token needs a valid owner and model")
        moment = self._clock() if now is None else now
        claims = self._claims(owner, model_id, session_id, execution_id, moment + ttl_seconds)
        payload = base64.urlsafe_b64encode(canonical_claims(claims)).decode("ascii").rstrip("=")
        return f"{TOKEN_VERSION}.{payload}.{self._signature(payload)}"

    def verify(
        self,
        token: str,
        *,
        owner: str,
        model_id: str,
        session_id: str | None = None,
        execution_id: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Constant-time verification; any mismatch raises `stale_token`."""
        claims = self._decode(token)
        expected = self._claims(owner, model_id, session_id, execution_id, claims.get("expires_at"))
        if canonical_claims(claims) != canonical_claims(expected):
            raise IdentityError("stale_token", "the token does not belong to this owner or object")
        if claims["boot_id"] != self.boot_id:
            raise IdentityError("stale_token", "the token belongs to another boot")
        moment = self._clock() if now is None else now
        if moment >= float(claims["expires_at"]):
            raise IdentityError("stale_token", "the token expired")
        return claims

    def fingerprint(self, token: str) -> str:
        """A stable, non-reversible reference safe for logs and status."""
        return sha256(f"{self.boot_id}:{token}".encode("utf-8")).hexdigest()[:16]

    def _claims(self, owner: str, model_id: str, session_id: str | None, execution_id: str | None, expires_at: float | None) -> dict[str, Any]:
        if expires_at is None or not math.isfinite(float(expires_at)):
            raise IdentityError("contract_violation", "a token needs a finite expiry")
        return {
            "boot_id": self.boot_id,
            "owner": owner,
            "model_id": model_id,
            "session_id": session_id,
            "execution_id": execution_id,
            "expires_at": round(float(expires_at), 3),
        }

    def _signature(self, payload: str) -> str:
        digest = hmac.new(self._boot_key, payload.encode("ascii"), sha256).hexdigest()
        return digest

    def _decode(self, token: str) -> dict[str, Any]:
        if not isinstance(token, str):
            raise IdentityError("stale_token", "a token is required")
        version, _, rest = token.partition(".")
        payload, _, signature = rest.rpartition(".")
        if version != TOKEN_VERSION or not payload or not signature:
            raise IdentityError("stale_token", "malformed token")
        if not hmac.compare_digest(signature, self._signature(payload)):
            raise IdentityError("stale_token", "the token signature does not match this boot")
        try:
            padded = payload + "=" * (-len(payload) % 4)
            claims = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise IdentityError("stale_token", "malformed token payload") from exc
        if not isinstance(claims, dict) or set(claims) != {"boot_id", "owner", "model_id", "session_id", "execution_id", "expires_at"}:
            raise IdentityError("stale_token", "unexpected token payload")
        return claims
