"""Connecting a seller's Amazon account.

## Why the callback is not authenticated

Every other route demands a bearer token. This one cannot: Amazon redirects
the seller's *browser* here, and a browser arriving from Seller Central
carries no Authorization header.

What stands in for it is the ``state`` minted when the flow began. It is
signed, short-lived, and names the tenant, so an arriving callback proves
which account this authorisation belongs to without the request having to
assert it. A callback whose state does not verify is refused before anything
is exchanged or stored.

That is the whole reason state is signed rather than merely random: a random
token would need a table to mean anything, and the table would be read on an
unauthenticated route.

## Why there are two ways in

The OAuth round trip above is the only way a *hosted* deployment can work:
one application, many sellers, none of whom can be asked to paste a secret.

It is also, for a self-hosted install, unusable — which was established the
expensive way, by trying it. A private SP-API application in Draft has no
OAuth configuration at all: the registration form offers a name, an API
type, the business entities and the roles, and no redirect URI anywhere. The
Solution Provider Portal states it outright, "OAuthorizations: no
authorization allowed", and sending a seller to the consent URL for such an
application answers ``MD9100`` — *this app does not have the right
application details* — with or without ``version=beta``.

What a private application does have is **self-authorisation**: the owner
authorises their own application from the portal and is handed a refresh
token, up to ten times. That is the path ``POST /credentials`` exists for.
It is not a lesser path or a convenience; for everyone who self-hosts it is
the only one that works.

Both end in the same row, sealed by the same keyring, so nothing downstream
— the worker, resealing, the needs-reauth transition — knows or cares which
way a token arrived.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...amazon.lwa import AuthorisationRevoked, LwaError
from ...amazon.oauth import OAuthStateError, begin, verify_state
from ...db.models.credentials import Marketplace
from ...db.session import tenant_session
from ...jobs.handlers import SETTLEMENT_FETCH
from ...repositories import active_credential, enqueue, store_credential
from ..deps import CurrentSession, Factory, Lwa, OwnerSession, Settings_, TenantDb

router = APIRouter(prefix="/amazon", tags=["amazon"])

#: Which marketplace a call is about. Defaulted rather than required, since
#: most sellers have one and asking for it every time is noise.
MarketplaceQuery = Annotated[Marketplace, Query()]


class AuthorisationStart(BaseModel):
    url: str
    marketplace: Marketplace


class ConnectionStatus(BaseModel):
    marketplace: Marketplace
    connected: bool
    status: str | None = None
    selling_partner_id: str | None = None
    last_error: str | None = None
    oauth_available: bool = Field(
        default=False,
        description=(
            "Whether this installation has an Amazon application configured, "
            "and so can send a seller through the consent flow. False on a "
            "self-hosted install, where a private application has no OAuth "
            "configuration and the consent URL answers MD9100 — the interface "
            "offers self-authorisation instead of a button that cannot work."
        ),
    )


def _queue_first_fetch(db: Session, tenant_id: uuid.UUID, marketplace: Marketplace) -> None:
    """Fetch this seller's finances now rather than at the next schedule.

    Without this, connecting an account enqueued nothing and the first
    figures waited for the worker's next scheduling pass — or, before the
    worker scheduled at all, forever. Someone who has just connected is
    watching the screen, and a quarter of an hour of nothing is
    indistinguishable from a broken install.

    Shares the scheduler's idempotency key, so this and the periodic pass
    cannot both queue the same day's fetch.
    """
    enqueue(
        db,
        tenant_id,
        SETTLEMENT_FETCH,
        payload={"marketplace": marketplace.value},
        idempotency_key=(
            f"{SETTLEMENT_FETCH}:{marketplace.name}:{datetime.now(UTC).date()}"
        ),
    )


@router.post("/authorize", response_model=AuthorisationStart)
def authorize(
    session: CurrentSession,
    config: Settings_,
    marketplace: MarketplaceQuery = Marketplace.DE,
) -> AuthorisationStart:
    """Where to send this tenant's seller to approve the application."""
    if not config.amazon_is_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "This installation has no Amazon application configured, so "
                "there is nothing for a seller to authorise."
            ),
        )

    request = begin(
        session.tenant_id,
        marketplace,
        config.amazon_application_id,
        config.secret_key.get_secret_value(),
        draft=config.amazon_application_is_draft,
    )
    return AuthorisationStart(url=request.url, marketplace=marketplace)


@router.get("/callback", response_model=ConnectionStatus)
def callback(
    factory: Factory,
    config: Settings_,
    client: Lwa,
    state: Annotated[str, Query()],
    spapi_oauth_code: Annotated[str, Query()],
    selling_partner_id: Annotated[str, Query()],
    marketplace: MarketplaceQuery = Marketplace.DE,
) -> ConnectionStatus:
    """Where Amazon returns the seller, carrying a code worth five minutes.

    Unauthenticated by necessity; the state is what proves whose account this
    is. See the module docstring.
    """
    try:
        tenant_id = verify_state(state, config.secret_key.get_secret_value())
    except OAuthStateError as exc:
        # Deliberately vague to the browser, specific in the log. The person
        # reading this page cannot act on which check failed, and saying so
        # helps only someone probing.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This authorisation link is not valid any more. Start again.",
        ) from exc

    try:
        authorisation = client.exchange_authorization_code(
            spapi_oauth_code, config.amazon_redirect_uri,
        )
    except AuthorisationRevoked as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Amazon refused this authorisation: {exc}",
        ) from exc
    except LwaError as exc:
        # Transport or a 5xx: the seller did nothing wrong and retrying is
        # the right advice.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not reach Amazon to complete this: {exc}",
        ) from exc

    with tenant_session(factory, tenant_id) as db:
        credential = store_credential(
            db,
            tenant_id,
            marketplace=marketplace,
            selling_partner_id=selling_partner_id,
            refresh_token=authorisation.refresh_token,
            keyring=config.keyring(),
            scopes=authorisation.scopes,
        )
        _queue_first_fetch(db, tenant_id, marketplace)
        return ConnectionStatus(
            marketplace=marketplace,
            connected=True,
            status=credential.status.value,
            selling_partner_id=credential.selling_partner_id,
            oauth_available=config.amazon_is_configured,
        )


@router.get("/status", response_model=ConnectionStatus)
def connection_status(
    session: CurrentSession,
    db: TenantDb,
    config: Settings_,
    marketplace: MarketplaceQuery = Marketplace.DE,
) -> ConnectionStatus:
    """Whether this tenant can currently reach Amazon.

    A seller whose authorisation was revoked needs to be told on their own
    screen. Finding out because the numbers quietly stopped moving is the
    failure this exists to prevent.
    """
    credential = active_credential(db, session.tenant_id, marketplace)
    if credential is None:
        return ConnectionStatus(
            marketplace=marketplace,
            connected=False,
            oauth_available=config.amazon_is_configured,
        )

    return ConnectionStatus(
        marketplace=marketplace,
        connected=True,
        status=credential.status.value,
        selling_partner_id=credential.selling_partner_id,
        last_error=credential.last_error,
        oauth_available=config.amazon_is_configured,
    )


class SelfAuthorisation(BaseModel):
    """A refresh token the owner obtained by authorising their own app.

    The token is write-only: it is sealed on arrival and no route ever
    returns it, so this model has no response counterpart carrying it back.
    """

    marketplace: Marketplace = Marketplace.DE
    selling_partner_id: str = Field(
        min_length=1,
        description="Your merchant token, shown beside the authorisation.",
    )
    refresh_token: str = Field(
        min_length=1,
        description=(
            "The Atzr|... token the Solution Provider Portal showed after "
            "you authorised the application against your own account."
        ),
    )


@router.post("/credentials", response_model=ConnectionStatus)
async def store_self_authorisation(
    db: TenantDb,
    session: OwnerSession,
    config: Settings_,
    body: SelfAuthorisation,
) -> ConnectionStatus:
    """Accept a refresh token from the portal's self-authorisation.

    Owner-only: this is the credential that reads the seller's finances.

    Deliberately permissive about the token's shape beyond being non-empty.
    Amazon's refresh tokens have looked like ``Atzr|...`` for years, but
    rejecting anything else here would turn a format change into "Biryar is
    broken" rather than a clear error from Amazon on first use — and the
    first use is seconds away, in the settlement job.
    """
    credential = store_credential(
        db,
        session.tenant_id,
        marketplace=body.marketplace,
        selling_partner_id=body.selling_partner_id,
        refresh_token=body.refresh_token,
        keyring=config.keyring(),
        scopes="self-authorisation",
    )
    _queue_first_fetch(db, session.tenant_id, body.marketplace)
    return ConnectionStatus(
        marketplace=body.marketplace,
        connected=True,
        status=credential.status.value,
        selling_partner_id=credential.selling_partner_id,
        oauth_available=config.amazon_is_configured,
    )
