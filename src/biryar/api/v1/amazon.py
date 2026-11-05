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
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from ...amazon.lwa import AuthorisationRevoked, LwaError
from ...amazon.oauth import OAuthStateError, begin, verify_state
from ...db.models.credentials import Marketplace
from ...db.session import tenant_session
from ...repositories import active_credential, store_credential
from ..deps import CurrentSession, Factory, Lwa, Settings_, TenantDb

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
        return ConnectionStatus(
            marketplace=marketplace,
            connected=True,
            status=credential.status.value,
            selling_partner_id=credential.selling_partner_id,
        )


@router.get("/status", response_model=ConnectionStatus)
def connection_status(
    session: CurrentSession,
    db: TenantDb,
    marketplace: MarketplaceQuery = Marketplace.DE,
) -> ConnectionStatus:
    """Whether this tenant can currently reach Amazon.

    A seller whose authorisation was revoked needs to be told on their own
    screen. Finding out because the numbers quietly stopped moving is the
    failure this exists to prevent.
    """
    credential = active_credential(db, session.tenant_id, marketplace)
    if credential is None:
        return ConnectionStatus(marketplace=marketplace, connected=False)

    return ConnectionStatus(
        marketplace=marketplace,
        connected=True,
        status=credential.status.value,
        selling_partner_id=credential.selling_partner_id,
        last_error=credential.last_error,
    )
