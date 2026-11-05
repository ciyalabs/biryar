"""Login with Amazon: turning an authorisation into tokens, and back again.

Two exchanges, both against the same endpoint:

* **authorization_code** — once, when a seller authorises the application.
  Returns a refresh token, which is the thing worth keeping.
* **refresh_token** — before every run of work. Returns an access token good
  for about an hour, which is deliberately never stored.

## Why a refusal is not a failure

An LWA error is one of two very different things wearing the same shape. A
network blip, a 500, a timeout: retry, it will work. ``invalid_grant``: the
seller revoked the authorisation, or it was replaced, and it will never work
again no matter how many times it is tried.

Treating the second as the first is how a tenant's jobs fail hourly forever
against an endpoint that has already given its final answer — filling logs,
burning rate limit, and never telling the seller the one thing they could
act on. :class:`AuthorisationRevoked` is raised separately so the caller can
mark the credential and stop.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

#: Login with Amazon. One endpoint for every region and every grant type.
LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"

#: Refresh an access token this long before it actually expires, so it cannot
#: go stale between the check and the call it was fetched for.
EXPIRY_MARGIN_SECONDS = 60.0

#: Errors LWA returns that mean "stop asking". Anything else is worth a retry.
TERMINAL_ERRORS = frozenset({"invalid_grant", "unauthorized_client", "invalid_client"})


class LwaError(RuntimeError):
    """LWA refused, in a way that may well succeed if tried again."""


class AuthorisationRevoked(LwaError):
    """LWA refused in a way that will not change until the seller reconnects."""


@dataclass(frozen=True)
class Authorisation:
    """What an authorisation code is worth: a long-lived refresh token."""

    refresh_token: str
    access_token: str
    expires_in: int
    scopes: str = ""


@dataclass(frozen=True)
class AccessToken:
    """A short-lived token. Never stored, so it has no key id and no row."""

    token: str
    expires_in: int

    @property
    def usable_for_seconds(self) -> float:
        """How long this is safe to reuse, margin already subtracted."""
        return max(self.expires_in - EXPIRY_MARGIN_SECONDS, 0.0)


@dataclass(frozen=True)
class LwaApplication:
    """The application's own identity. One per deployment, not per tenant."""

    client_id: str
    client_secret: str


class LwaClient:
    """Exchanges against Login with Amazon.

    The HTTP client is injected so tests can answer without a network. That
    matters more than usual here: the failure paths are the interesting part,
    and provoking a real ``invalid_grant`` from Amazon on demand is not
    something a test suite can do.
    """

    def __init__(
        self,
        application: LwaApplication,
        *,
        client: httpx.Client | None = None,
        token_url: str = LWA_TOKEN_URL,
        timeout: float = 20.0,
    ) -> None:
        self._application = application
        self._client = client or httpx.Client(timeout=timeout)
        self._token_url = token_url

    def exchange_authorization_code(
        self, code: str, redirect_uri: str,
    ) -> Authorisation:
        """Turn the code Amazon handed back into a refresh token."""
        payload = self._post({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self._application.client_id,
            "client_secret": self._application.client_secret,
        })

        refresh_token = payload.get("refresh_token")
        if not refresh_token:
            raise LwaError(
                "LWA returned no refresh token; the authorisation cannot be stored"
            )

        return Authorisation(
            refresh_token=str(refresh_token),
            access_token=str(payload.get("access_token", "")),
            expires_in=int(payload.get("expires_in") or 3600),
            scopes=str(payload.get("scope", "")),
        )

    def refresh_access_token(self, refresh_token: str) -> AccessToken:
        """Trade the stored refresh token for an hour of access."""
        payload = self._post({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self._application.client_id,
            "client_secret": self._application.client_secret,
        })

        token = payload.get("access_token")
        if not token:
            raise LwaError("LWA returned no access token")

        return AccessToken(
            token=str(token), expires_in=int(payload.get("expires_in") or 3600),
        )

    def _post(self, form: dict[str, str]) -> dict[str, Any]:
        try:
            response = self._client.post(self._token_url, data=form)
        except httpx.HTTPError as exc:
            # A transport failure is always worth retrying, and must not be
            # mistaken for a refusal.
            raise LwaError(f"could not reach LWA: {exc}") from exc

        try:
            payload: dict[str, Any] = response.json()
        except ValueError:
            payload = {}

        if response.status_code >= 400:
            error = str(payload.get("error", "")) or f"http_{response.status_code}"
            description = str(payload.get("error_description", "")).strip()
            message = f"{error}: {description}" if description else error

            if error in TERMINAL_ERRORS:
                raise AuthorisationRevoked(message)
            raise LwaError(message)

        return payload
