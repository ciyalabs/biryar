"""Password hashing and session tokens. No database, no framework."""

from __future__ import annotations

import pytest

from biryar.security import (
    hash_password,
    hash_session_token,
    new_session_token,
    tokens_match,
    verify_password,
)


def test_the_same_password_hashes_differently_every_time() -> None:
    """Argon2 salts each hash, so two users with one password are not obvious."""
    assert hash_password("correct horse") != hash_password("correct horse")


def test_a_password_verifies_against_its_own_hash() -> None:
    digest = hash_password("correct horse")
    assert verify_password("correct horse", digest)
    assert not verify_password("Correct horse", digest)


def test_an_account_with_no_hash_cannot_sign_in() -> None:
    """Revoked, or provisioned and not yet activated. Same answer either way."""
    assert not verify_password("anything", None)
    assert not verify_password("anything", "")


def test_a_corrupt_hash_fails_rather_than_raising() -> None:
    """A malformed stored value must not turn a login into a 500."""
    assert not verify_password("anything", "not-an-argon2-hash")


def test_refusing_to_hash_nothing() -> None:
    with pytest.raises(ValueError, match="empty password"):
        hash_password("")


def test_tokens_are_unique_and_url_safe() -> None:
    tokens = {new_session_token() for _ in range(200)}
    assert len(tokens) == 200
    assert all(t.isascii() and "/" not in t and "+" not in t for t in tokens)


def test_the_stored_digest_is_not_the_token() -> None:
    """A database dump must not be a pile of working credentials."""
    token = new_session_token()
    digest = hash_session_token(token)
    assert digest != token
    assert len(digest) == 64
    assert hash_session_token(token) == digest


def test_token_comparison_accepts_only_the_real_token() -> None:
    token = new_session_token()
    stored = hash_session_token(token)
    assert tokens_match(token, stored)
    assert not tokens_match(new_session_token(), stored)
    assert not tokens_match(token[:-1], stored)
