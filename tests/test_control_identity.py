"""Owner identity and token tests (M04/P13).

Owner comes from the peer credential, tokens bind boot/session/model/owner and
expire, the HMAC input is fixed so a recomputation matches the original value,
a restart makes old tokens invalid, and no raw token ever leaves the authority.
"""
from __future__ import annotations

import pytest

from model_scheduler.control_identity import (
    IdentityError,
    PeerIdentity,
    TokenAuthority,
    check_owner,
    owner_from_peer,
)


def authority(now: float = 1_000.0, *, boot_key: bytes = b"k" * 32, boot_id: str = "boot-0001"):
    return TokenAuthority(boot_key=boot_key, boot_id=boot_id, clock=lambda: now)


def test_the_owner_can_only_come_from_a_trusted_peer_identity() -> None:
    peer = PeerIdentity(1000)

    assert peer.owner == "uid:1000"
    assert owner_from_peer(peer) == "uid:1000"
    with pytest.raises(IdentityError) as forbidden:
        owner_from_peer(None)
    assert forbidden.value.code == "peer_forbidden"
    with pytest.raises(IdentityError):
        PeerIdentity(-1)

    assert check_owner("uid:1000", peer) == "uid:1000"
    with pytest.raises(IdentityError) as foreign:
        check_owner("uid:1001", peer)  # a self-reported owner is never accepted
    assert foreign.value.code == "not_found"


def test_a_token_binds_boot_session_model_owner_and_execution() -> None:
    tokens = authority()
    token = tokens.issue(owner="uid:1000", model_id="qwen-small", session_id="session-1", execution_id="e-1", ttl_seconds=60, now=1_000.0)

    claims = tokens.verify(token, owner="uid:1000", model_id="qwen-small", session_id="session-1", execution_id="e-1", now=1_001.0)
    assert claims["boot_id"] == "boot-0001" and claims["owner"] == "uid:1000"

    for override in (
        {"owner": "uid:1001"},
        {"model_id": "qwen-large"},
        {"session_id": "session-2"},
        {"execution_id": "e-2"},
    ):
        binding = {"owner": "uid:1000", "model_id": "qwen-small", "session_id": "session-1", "execution_id": "e-1", **override}
        with pytest.raises(IdentityError) as stale:
            tokens.verify(token, now=1_001.0, **binding)
        assert stale.value.code == "stale_token"


def test_the_hmac_input_is_fixed_so_a_recomputation_matches_the_original() -> None:
    tokens = authority()
    first = tokens.issue(owner="uid:1000", model_id="qwen-small", session_id="session-1", ttl_seconds=60, now=1_000.0)
    second = tokens.issue(owner="uid:1000", model_id="qwen-small", session_id="session-1", ttl_seconds=60, now=1_000.0)

    assert first == second  # the canonical encoding makes an identical claim byte-identical
    assert tokens.verify(first, owner="uid:1000", model_id="qwen-small", session_id="session-1", now=1_000.0)


def test_a_restart_invalidates_every_old_token() -> None:
    before = authority()
    token = before.issue(owner="uid:1000", model_id="qwen-small", ttl_seconds=3_600, now=1_000.0)

    after = TokenAuthority(boot_key=b"other-key", boot_id="boot-0002", clock=lambda: 1_001.0)

    with pytest.raises(IdentityError) as stale:
        after.verify(token, owner="uid:1000", model_id="qwen-small", now=1_001.0)
    assert stale.value.code == "stale_token"


def test_tokens_expire_at_the_deadline_and_never_leak_their_plaintext() -> None:
    tokens = authority()
    token = tokens.issue(owner="uid:1000", model_id="qwen-small", ttl_seconds=10.0, now=1_000.0)

    assert tokens.verify(token, owner="uid:1000", model_id="qwen-small", now=1_009.999)
    with pytest.raises(IdentityError):
        tokens.verify(token, owner="uid:1000", model_id="qwen-small", now=1_010.0)

    reference = tokens.fingerprint(token)
    assert token not in reference and reference != token
    assert len(reference) == 16  # only a stable, non-reversible reference is exposed
    claims = tokens.verify(token, owner="uid:1000", model_id="qwen-small", now=1_000.5)
    assert token not in str(claims)


def test_a_tampered_token_is_rejected() -> None:
    tokens = authority()
    token = tokens.issue(owner="uid:1000", model_id="qwen-small", ttl_seconds=60.0, now=1_000.0)
    version, payload, signature = token.split(".")

    for tampered in (
        f"{version}.{payload}x.{signature}",
        f"{version}.{payload}.{signature[:8]}0",
        f"{version}.{payload}",
        "not-a-token",
        "",
    ):
        with pytest.raises(IdentityError) as stale:
            tokens.verify(tampered, owner="uid:1000", model_id="qwen-small", now=1_000.0)
        assert stale.value.code == "stale_token"
