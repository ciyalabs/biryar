"""What every request needs before a route sees it.

## The shape of an authenticated request

1. A bearer token arrives in ``Authorization``.
2. :func:`~biryar.repositories.auth.resolve_session` turns it into a tenant
   and a user, through the one function allowed to read a session without a
   tenant in scope.
3. Everything after that runs inside a transaction scoped to that tenant, so
   a route cannot read another seller's data even if it tries.

Step 3 is the reason these are dependencies rather than something each route
remembers to do. A route that forgets to open a tenant transaction gets no
database at all, which fails loudly, rather than getting an unscoped one,
which fails silently and wrongly.

## Which tenant a sign-in is for

Sign-in happens before there is a session, so the tenant has to come from
somewhere else. A hosted deployment reads it from the ``X-Biryar-Tenant``
header, which the front end fills from the subdomain. A self-hosted one has
exactly one tenant and uses it without being told.

The header is trusted only to *name* a tenant, never to grant access to it:
every request still has to authenticate against that tenant, and the session
it gets back carries the tenant id from the database rather than from the
header.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from ..config import Settings, settings
from ..db.session import SessionFactory, admin_session, tenant_session
from ..repositories.auth import ResolvedSession, resolve_session, sole_tenant, tenant_by_slug

#: auto_error=False so a missing header becomes our own 401 with a body the
#: front end can read, rather than FastAPI's bare one.
bearer = HTTPBearer(auto_error=False, scheme_name="Session token")

UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not signed in.",
    headers={"WWW-Authenticate": "Bearer"},
)


def get_settings() -> Settings:
    return settings()


def get_session_factory(request: Request) -> SessionFactory:
    """The factory built once at startup and kept on the app."""
    factory: SessionFactory = request.app.state.session_factory
    return factory


def resolve_tenant_id(
    factory: Annotated[SessionFactory, Depends(get_session_factory)],
    x_biryar_tenant: Annotated[str | None, Header()] = None,
) -> uuid.UUID:
    """Which tenant this request is addressed to, before it has a session.

    Only used by sign-in. An authenticated request takes its tenant from the
    session instead, because a header is something a client chooses and a
    session is something the database remembers.
    """
    with admin_session(factory) as session:
        tenant = (
            tenant_by_slug(session, x_biryar_tenant)
            if x_biryar_tenant
            else sole_tenant(session)
        )
        if tenant is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    "Unknown tenant."
                    if x_biryar_tenant
                    else "This installation has no single tenant; name one with "
                    "the X-Biryar-Tenant header."
                ),
            )
        return tenant.id


def current_session(
    factory: Annotated[SessionFactory, Depends(get_session_factory)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)] = None,
) -> ResolvedSession:
    """The session this request presents, or 401.

    Expired and revoked tokens are both simply "not signed in". Saying which
    tells an attacker whether a token they hold was ever real.
    """
    if credentials is None or not credentials.credentials:
        raise UNAUTHENTICATED

    plain = factory()
    try:
        resolved = resolve_session(plain, credentials.credentials)
    finally:
        plain.close()

    if resolved is None:
        raise UNAUTHENTICATED
    return resolved


def tenant_db(
    factory: Annotated[SessionFactory, Depends(get_session_factory)],
    session: Annotated[ResolvedSession, Depends(current_session)],
) -> Iterator[Session]:
    """A transaction scoped to the signed-in tenant, committed on success.

    The tenant comes from the resolved session, never from anything the
    client sent.
    """
    with tenant_session(factory, session.tenant_id) as db:
        yield db


CurrentSession = Annotated[ResolvedSession, Depends(current_session)]
TenantDb = Annotated[Session, Depends(tenant_db)]
TenantId = Annotated[uuid.UUID, Depends(resolve_tenant_id)]
Factory = Annotated[SessionFactory, Depends(get_session_factory)]
