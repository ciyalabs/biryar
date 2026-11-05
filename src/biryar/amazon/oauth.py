"""The seller-facing authorisation flow.

A seller is sent to Seller Central, approves the application, and Amazon
redirects back with a code worth about five minutes. The shape of that round
trip is Amazon's; the parts worth thinking about are the two things carried
across it.

## state, and why it is signed rather than stored

``state`` exists to stop someone handing a seller a link that completes an
authorisation into *their* account. Amazon returns whatever was sent, and the
application has to recognise it as its own.

The usual approach is a table of issued states. This signs them instead: the
tenant id, the time and a nonce, with an HMAC over all three. Nothing is
stored, nothing needs cleaning up, and a state cannot be replayed after it
expires — Amazon's own code is good for five minutes, so a ten-minute state
is already generous.

The signing key is derived from the instance's sealing key rather than being
the sealing key. Two purposes, two keys: a signature oracle must not also be
able to decrypt credentials.

## What comes back

``spapi_oauth_code`` and ``selling_partner_id``, plus the ``state`` sent out.
The selling partner id is Amazon's word for which account authorised, and is
the only trustworthy source of it — a client could claim anything.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
import uuid
from dataclasses import dataclass
from urllib.parse import urlencode

from ..db.models.credentials import Marketplace

#: Seller Central host per marketplace, which is where a seller consents.
SELLER_CENTRAL_HOST: dict[Marketplace, str] = {
    Marketplace.DE: "sellercentral.amazon.de",
    Marketplace.UK: "sellercentral.amazon.co.uk",
    Marketplace.FR: "sellercentral.amazon.fr",
    Marketplace.IT: "sellercentral.amazon.it",
    Marketplace.ES: "sellercentral.amazon.es",
    Marketplace.NL: "sellercentral.amazon.nl",
    Marketplace.US: "sellercentral.amazon.com",
}

#: Amazon's authorisation code expires in about five minutes, so a state that
#: outlives it by much is a replay window with nothing to redeem.
STATE_TTL_SECONDS = 600


class OAuthStateError(ValueError):
    """The state did not come back the way it went out."""


@dataclass(frozen=True)
class AuthorisationRequest:
    """Where to send the seller, and the state to expect back."""

    url: str
    state: str


def _signing_key(secret: str) -> bytes:
    """A key for signing state, derived from but not equal to the secret.

    Separating purposes means a flaw that leaks signatures cannot be turned
    into a way to read sealed credentials.
    """
    return hashlib.sha256(b"biryar.oauth.state\x00" + secret.encode("utf-8")).digest()


def sign_state(tenant_id: uuid.UUID, secret: str, *, now: float | None = None) -> str:
    """A state token bound to one tenant and one short window."""
    issued = int(now if now is not None else time.time())
    payload = f"{tenant_id}:{issued}:{secrets.token_urlsafe(8)}"
    mac = hmac.new(_signing_key(secret), payload.encode("utf-8"), hashlib.sha256)
    raw = f"{payload}:{mac.hexdigest()}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def verify_state(state: str, secret: str, *, now: float | None = None) -> uuid.UUID:
    """The tenant this state was issued for, or raise.

    Every failure is the same kind of failure to a caller: this state is not
    one we issued and still honour. The reasons are distinguished only in the
    message, which is for a log rather than for the browser.
    """
    try:
        padded = state + "=" * (-len(state) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except Exception as exc:
        raise OAuthStateError("state is not a token this application issued") from exc

    parts = raw.split(":")
    if len(parts) != 4:
        raise OAuthStateError("state is malformed")

    tenant_raw, issued_raw, nonce, presented_mac = parts
    payload = f"{tenant_raw}:{issued_raw}:{nonce}"
    expected = hmac.new(
        _signing_key(secret), payload.encode("utf-8"), hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, presented_mac):
        raise OAuthStateError("state signature does not match")

    try:
        issued = int(issued_raw)
        tenant_id = uuid.UUID(tenant_raw)
    except ValueError as exc:
        raise OAuthStateError("state contents are not usable") from exc

    age = (now if now is not None else time.time()) - issued
    if age > STATE_TTL_SECONDS:
        raise OAuthStateError("state has expired")
    if age < -60:
        # Clock skew is one thing; a state issued in the future is another.
        raise OAuthStateError("state was issued in the future")

    return tenant_id


def consent_url(
    marketplace: Marketplace,
    application_id: str,
    state: str,
    *,
    draft: bool = False,
) -> str:
    """Where to send a seller to approve this application.

    ``draft`` adds ``version=beta``, which Amazon requires while the
    application is still in Draft and rejects once it is published — so it
    is configuration rather than a constant.
    """
    host = SELLER_CENTRAL_HOST.get(marketplace)
    if host is None:                                            # pragma: no cover
        raise ValueError(f"no Seller Central host known for {marketplace.name}")

    query: dict[str, str] = {"application_id": application_id, "state": state}
    if draft:
        query["version"] = "beta"

    return f"https://{host}/apps/authorize/consent?{urlencode(query)}"


def begin(
    tenant_id: uuid.UUID,
    marketplace: Marketplace,
    application_id: str,
    secret: str,
    *,
    draft: bool = False,
) -> AuthorisationRequest:
    """Mint a state and build the URL that carries it."""
    state = sign_state(tenant_id, secret)
    return AuthorisationRequest(
        url=consent_url(marketplace, application_id, state, draft=draft),
        state=state,
    )
