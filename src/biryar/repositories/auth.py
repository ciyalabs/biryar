"""Signing in, issuing sessions, and resolving them again.

## Two lookups that happen before a tenant is known

Authentication is the part of the system that runs before the tenant scope
exists, so it is worth naming exactly where that happens and why each case
is safe.

**Finding the tenant.** ``tenants`` is deliberately not tenant-scoped — it
is the table the scope is defined against — so an admin session reads it.
A hosted deployment gets the slug from the subdomain; a self-hosted one has
exactly one tenant and :func:`sole_tenant` returns it.

**Finding the session.** ``user_sessions`` *is* tenant-scoped, and the
tenant is what the lookup is for. That circle is broken by one
``SECURITY DEFINER`` function, created in revision 0003, which takes an
exact token digest and returns five columns. See that migration for why the
alternatives are worse.

Everything after those two reads happens inside a tenant transaction.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, select, text, update
from sqlalchemy.orm import Session

from ..db.models.tenancy import Role, Tenant, TenantStatus, User, UserSession
from ..security import (
    hash_password,
    hash_session_token,
    needs_rehash,
    new_session_token,
    verify_password,
)

#: How long a freshly issued session lasts.
DEFAULT_SESSION_LIFETIME = timedelta(hours=12)


@dataclass(frozen=True)
class IssuedSession:
    """A new session, and the one moment its token exists in plaintext."""

    token: str
    session_id: uuid.UUID
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    expires_at: datetime


@dataclass(frozen=True)
class ResolvedSession:
    """Who a presented token belongs to, if it is still good for anything."""

    session_id: uuid.UUID
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    expires_at: datetime


# -- before a tenant is known --------------------------------------------------------

def tenant_by_slug(session: Session, slug: str) -> Tenant | None:
    """The tenant a hosted request is addressed to. Admin session."""
    return session.execute(
        select(Tenant).where(Tenant.slug == slug, Tenant.status == TenantStatus.ACTIVE)
    ).scalar_one_or_none()


def sole_tenant(session: Session) -> Tenant | None:
    """The only tenant, for a self-hosted install. Admin session.

    ``None`` when there is not exactly one, which is the honest answer in
    both directions: a fresh install has none and needs setup, and an
    install with several is hosted and must say which it means rather than
    be guessed at.
    """
    tenants = session.execute(
        select(Tenant).where(Tenant.status == TenantStatus.ACTIVE).limit(2)
    ).scalars().all()
    return tenants[0] if len(tenants) == 1 else None


def resolve_session(session: Session, token: str) -> ResolvedSession | None:
    """Who this bearer token belongs to, or ``None``.

    Runs without a tenant in scope, through the ``SECURITY DEFINER``
    resolver. Expiry and revocation are checked here rather than in the
    function's WHERE clause so that the function stays a plain lookup and
    the policy about what counts as a usable session lives in Python, where
    it can be read and tested.
    """
    row = session.execute(
        text(
            "SELECT session_id, tenant_id, user_id, expires_at, revoked_at "
            "FROM public.biryar_resolve_session(:token_hash)"
        ),
        {"token_hash": hash_session_token(token)},
    ).one_or_none()

    if row is None:
        return None

    # text() gives an untyped row; the resolver's signature is what gives
    # these their types, and it is pinned in revision 0003.
    session_id = cast("uuid.UUID", row[0])
    tenant_id = cast("uuid.UUID", row[1])
    user_id = cast("uuid.UUID", row[2])
    expires_at = cast("datetime", row[3])
    revoked_at = cast("datetime | None", row[4])

    if revoked_at is not None:
        return None
    if expires_at <= datetime.now(UTC):
        return None

    return ResolvedSession(
        session_id=session_id,
        tenant_id=tenant_id,
        user_id=user_id,
        expires_at=expires_at,
    )


# -- inside a tenant transaction -----------------------------------------------------

def create_user(
    session: Session,
    tenant_id: uuid.UUID,
    *,
    email: str,
    display_name: str,
    role: Role,
    password: str | None = None,
) -> User:
    """Add a user to a tenant.

    The one door, so that email normalisation happens in exactly one place.
    :func:`authenticate` looks up a lowercased address; a user inserted
    anywhere else with a capital letter in their email would simply never be
    able to sign in, and the failure would look like a wrong password.

    ``password=None`` creates an account that cannot sign in yet, which is
    what provisioning before an invitation is accepted looks like.
    """
    user = User(
        tenant_id=tenant_id,
        email=normalise_email(email),
        display_name=display_name,
        role=role,
        is_active=True,
        password_hash=hash_password(password) if password else None,
    )
    session.add(user)
    session.flush()
    return user


def role_of(session: Session, user_id: uuid.UUID) -> Role | None:
    """The signed-in user's role, or ``None`` if the row is not visible.

    Read inside the tenant transaction rather than returned by the session
    resolver. The resolver is ``SECURITY DEFINER`` and runs with no tenant in
    scope, so everything it returns is readable before any policy applies;
    the less it carries, the smaller that hole is. A role is only ever needed
    once a tenant is established, where an ordinary query answers it under
    row-level security.

    ``None`` means revoked or deleted between signing in and this request,
    which callers must treat as "not permitted" rather than as a default.
    """
    return session.execute(
        select(User.role).where(User.id == user_id)
    ).scalar_one_or_none()


def normalise_email(email: str) -> str:
    """Lowercased and stripped.

    Only the case of the domain is formally insignificant, but treating the
    local part as case-sensitive means Owner@example.com and
    owner@example.com are two accounts, which no one has ever wanted.
    """
    return email.strip().lower()


def authenticate(
    session: Session, tenant_id: uuid.UUID, email: str, password: str,
) -> User | None:
    """The user these credentials belong to, or ``None``.

    One return value for every kind of failure — no such user, wrong
    password, account deactivated — because distinguishing them tells an
    attacker which email addresses exist.

    The verification runs even when no user was found, against a throwaway
    hash. Without that, a missing user returns in microseconds and a real
    one takes the full Argon2 cost, and the difference is a reliable test
    for whether an address is registered.
    """
    user = session.execute(
        select(User).where(User.tenant_id == tenant_id, User.email == normalise_email(email))
    ).scalar_one_or_none()

    if user is None or not user.is_active:
        verify_password(password, _DECOY_HASH)
        return None

    if not verify_password(password, user.password_hash):
        return None

    if user.password_hash and needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)

    user.last_seen_at = datetime.now(UTC)
    return user


def issue_session(
    session: Session,
    user: User,
    *,
    lifetime: timedelta = DEFAULT_SESSION_LIFETIME,
    user_agent: str | None = None,
    ip: str | None = None,
) -> IssuedSession:
    """Mint a session for ``user``. The token is returned, never stored."""
    token = new_session_token()
    now = datetime.now(UTC)
    record = UserSession(
        tenant_id=user.tenant_id,
        user_id=user.id,
        token_hash=hash_session_token(token),
        expires_at=now + lifetime,
        user_agent=user_agent,
        ip=ip,
    )
    session.add(record)
    session.flush()

    return IssuedSession(
        token=token,
        session_id=record.id,
        tenant_id=record.tenant_id,
        user_id=record.user_id,
        expires_at=record.expires_at,
    )


def revoke_session(session: Session, session_id: uuid.UUID) -> bool:
    """Sign out. Returns whether a live session was ended.

    The row stays, carrying the time it was revoked, because when a session
    ended is the kind of thing an audit question asks about and a deleted
    row cannot answer.
    """
    result = session.execute(
        update(UserSession)
        .where(UserSession.id == session_id, UserSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
    return bool(cast("CursorResult[Any]", result).rowcount)


def revoke_all_sessions(session: Session, user_id: uuid.UUID) -> int:
    """End every live session for one user — a password change, or a theft."""
    result = session.execute(
        update(UserSession)
        .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
    return int(cast("CursorResult[Any]", result).rowcount)


#: A real Argon2 digest of a value nothing will ever present, so that the
#: no-such-user path costs what the found-user path costs. Computed once at
#: import rather than per request.
_DECOY_HASH = hash_password("biryar-timing-decoy")
