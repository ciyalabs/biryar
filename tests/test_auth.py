"""Signing in, and the one function that reads a session without a tenant.

The resolver is a deliberate hole in the tenant boundary, so it gets the
same scrutiny the boundary itself does: that it cannot be used to enumerate,
that it refuses sessions it should refuse, and that what comes out of it is
still scoped to one tenant.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from biryar.db.models.tenancy import Role
from biryar.db.session import SessionFactory, tenant_session
from biryar.repositories import (
    authenticate,
    create_user,
    issue_session,
    resolve_session,
    revoke_all_sessions,
    revoke_session,
)

pytestmark = pytest.mark.postgres


def _owner(session, tenant_id: uuid.UUID, email: str = "owner@example.com"):
    return create_user(
        session, tenant_id,
        email=email, display_name="Owner", role=Role.OWNER, password="correct horse",
    )


# -- signing in ----------------------------------------------------------------------

def test_the_right_password_signs_in(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, _ = two_tenants
    with tenant_session(factory, a) as s:
        _owner(s, a)
    with tenant_session(factory, a) as s:
        assert authenticate(s, a, "owner@example.com", "correct horse") is not None


def test_the_wrong_password_does_not(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, _ = two_tenants
    with tenant_session(factory, a) as s:
        _owner(s, a)
    with tenant_session(factory, a) as s:
        assert authenticate(s, a, "owner@example.com", "wrong") is None


def test_email_case_does_not_decide_whether_you_can_sign_in(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Otherwise a capital at sign-up locks someone out, looking like a typo."""
    a, _ = two_tenants
    with tenant_session(factory, a) as s:
        _owner(s, a, email="Owner@Example.COM")
    with tenant_session(factory, a) as s:
        assert authenticate(s, a, "owner@example.com", "correct horse") is not None


def test_a_deactivated_account_cannot_sign_in(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, _ = two_tenants
    with tenant_session(factory, a) as s:
        user = _owner(s, a)
        user.is_active = False
    with tenant_session(factory, a) as s:
        assert authenticate(s, a, "owner@example.com", "correct horse") is None


def test_the_same_address_in_two_tenants_is_two_people(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """A consultant working for two sellers has two accounts, not one."""
    a, b = two_tenants
    with tenant_session(factory, a) as s:
        create_user(s, a, email="both@example.com", display_name="A",
                    role=Role.OWNER, password="password-a")
    with tenant_session(factory, b) as s:
        create_user(s, b, email="both@example.com", display_name="B",
                    role=Role.ASSISTANT, password="password-b")

    with tenant_session(factory, a) as s:
        assert authenticate(s, a, "both@example.com", "password-b") is None
        assert authenticate(s, a, "both@example.com", "password-a") is not None


# -- sessions ------------------------------------------------------------------------

def test_a_session_resolves_without_a_tenant_in_scope(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """The whole point. A request carries a token and nothing else."""
    a, _ = two_tenants
    with tenant_session(factory, a) as s:
        issued = issue_session(s, _owner(s, a))

    plain = factory()
    try:
        resolved = resolve_session(plain, issued.token)
    finally:
        plain.close()

    assert resolved is not None
    assert resolved.tenant_id == a
    assert resolved.user_id == issued.user_id


def test_an_unknown_token_resolves_to_nothing(factory: SessionFactory) -> None:
    plain = factory()
    try:
        assert resolve_session(plain, "not-a-real-token") is None
    finally:
        plain.close()


def test_an_expired_session_is_refused(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, _ = two_tenants
    with tenant_session(factory, a) as s:
        issued = issue_session(s, _owner(s, a), lifetime=timedelta(seconds=1))
        s.execute(
            text("UPDATE user_sessions SET expires_at = :past WHERE id = :id"),
            {"past": datetime.now(UTC) - timedelta(hours=1), "id": issued.session_id},
        )

    plain = factory()
    try:
        assert resolve_session(plain, issued.token) is None
    finally:
        plain.close()


def test_signing_out_refuses_the_token_afterwards(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, _ = two_tenants
    with tenant_session(factory, a) as s:
        issued = issue_session(s, _owner(s, a))
    with tenant_session(factory, a) as s:
        assert revoke_session(s, issued.session_id) is True

    plain = factory()
    try:
        assert resolve_session(plain, issued.token) is None
    finally:
        plain.close()


def test_signing_out_twice_reports_that_nothing_happened(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, _ = two_tenants
    with tenant_session(factory, a) as s:
        issued = issue_session(s, _owner(s, a))
    with tenant_session(factory, a) as s:
        assert revoke_session(s, issued.session_id) is True
        assert revoke_session(s, issued.session_id) is False


def test_revoking_everything_ends_every_live_session(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """What a password change, or a stolen laptop, has to do."""
    a, _ = two_tenants
    with tenant_session(factory, a) as s:
        user = _owner(s, a)
        tokens = [issue_session(s, user).token for _ in range(3)]
    with tenant_session(factory, a) as s:
        assert revoke_all_sessions(s, user.id) == 3

    plain = factory()
    try:
        assert all(resolve_session(plain, t) is None for t in tokens)
    finally:
        plain.close()


def test_a_revoked_session_keeps_its_row(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """When a session ended is an audit question a deleted row cannot answer."""
    a, _ = two_tenants
    with tenant_session(factory, a) as s:
        issued = issue_session(s, _owner(s, a))
    with tenant_session(factory, a) as s:
        revoke_session(s, issued.session_id)
    with tenant_session(factory, a) as s:
        revoked_at = s.execute(
            text("SELECT revoked_at FROM user_sessions WHERE id = :id"),
            {"id": issued.session_id},
        ).scalar_one()

    assert revoked_at is not None


# -- the resolver is still a hole, so check its edges --------------------------------

def test_the_resolver_cannot_be_used_to_enumerate(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """It takes an exact digest. No prefix, no pattern, no listing."""
    a, _ = two_tenants
    with tenant_session(factory, a) as s:
        issue_session(s, _owner(s, a))

    plain = factory()
    try:
        rows = plain.execute(
            text("SELECT * FROM public.biryar_resolve_session(:h)"), {"h": ""},
        ).all()
        assert rows == []
    finally:
        plain.close()


def test_a_token_resolves_only_to_its_own_tenant(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, b = two_tenants
    with tenant_session(factory, a) as s:
        token_a = issue_session(s, _owner(s, a, "a@example.com")).token
    with tenant_session(factory, b) as s:
        token_b = issue_session(s, _owner(s, b, "b@example.com")).token

    plain = factory()
    try:
        assert resolve_session(plain, token_a).tenant_id == a      # type: ignore[union-attr]
        assert resolve_session(plain, token_b).tenant_id == b      # type: ignore[union-attr]
    finally:
        plain.close()


def test_the_tenant_boundary_still_holds_after_resolving(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Resolving a session must not have widened anything.

    The resolver runs as the table owner. If that privilege leaked into the
    connection, the next query on it would see every tenant's rows.
    """
    a, b = two_tenants
    with tenant_session(factory, a) as s:
        token = issue_session(s, _owner(s, a, "a@example.com")).token
    with tenant_session(factory, b) as s:
        _owner(s, b, "b@example.com")

    plain = factory()
    try:
        resolve_session(plain, token)
        assert plain.execute(text("SELECT count(*) FROM users")).scalar_one() == 0
    finally:
        plain.close()
