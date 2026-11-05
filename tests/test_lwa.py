"""Login with Amazon. No network: the interesting paths are the refusals.

Provoking a real invalid_grant from Amazon on demand is not something a test
suite can do, so the transport is answered by a mock. That is the one place
mocking is clearly right — the thing under test is how this code reacts to
Amazon's answers, not whether Amazon gives them.
"""

from __future__ import annotations

import httpx
import pytest

from biryar.amazon import (
    AuthorisationRevoked,
    LwaApplication,
    LwaClient,
    LwaError,
)
from biryar.amazon.lwa import EXPIRY_MARGIN_SECONDS

APPLICATION = LwaApplication(client_id="amzn1.application-oa2-client.x", client_secret="shh")


def _client(handler) -> LwaClient:
    return LwaClient(APPLICATION, client=httpx.Client(transport=httpx.MockTransport(handler)))


def _json(status: int, body: dict) -> callable:
    return lambda request: httpx.Response(status, json=body)


# -- the happy paths -----------------------------------------------------------------

def test_an_authorisation_code_becomes_a_refresh_token() -> None:
    sent: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={
            "refresh_token": "Atzr|refresh", "access_token": "Atza|access",
            "expires_in": 3600, "scope": "sellingpartnerapi::migration",
        })

    result = _client(handler).exchange_authorization_code("the-code", "https://x/callback")

    assert result.refresh_token == "Atzr|refresh"
    assert result.scopes == "sellingpartnerapi::migration"
    assert sent["grant_type"] == "authorization_code"
    assert sent["code"] == "the-code"
    assert sent["redirect_uri"] == "https://x/callback"
    assert sent["client_id"] == APPLICATION.client_id


def test_a_refresh_token_becomes_an_access_token() -> None:
    sent: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={"access_token": "Atza|access", "expires_in": 3600})

    token = _client(handler).refresh_access_token("Atzr|refresh")

    assert token.token == "Atza|access"
    assert sent["grant_type"] == "refresh_token"
    assert sent["refresh_token"] == "Atzr|refresh"


def test_an_access_token_is_treated_as_expiring_early() -> None:
    """So it cannot go stale between the check and the call it was fetched for."""
    handler = _json(200, {"access_token": "Atza|access", "expires_in": 3600})
    token = _client(handler).refresh_access_token("Atzr|refresh")
    assert token.usable_for_seconds == 3600 - EXPIRY_MARGIN_SECONDS


def test_a_token_already_inside_the_margin_is_not_negative() -> None:
    handler = _json(200, {"access_token": "Atza|access", "expires_in": 5})
    assert _client(handler).refresh_access_token("Atzr|refresh").usable_for_seconds == 0.0


# -- the refusals, which are the point -----------------------------------------------

def test_a_revoked_authorisation_is_told_apart_from_a_bad_day() -> None:
    """invalid_grant will never succeed; retrying it forever helps nobody."""
    handler = _json(400, {"error": "invalid_grant",
                          "error_description": "The request has an invalid grant parameter."})
    with pytest.raises(AuthorisationRevoked, match="invalid_grant"):
        _client(handler).refresh_access_token("Atzr|revoked")


@pytest.mark.parametrize("error", ["unauthorized_client", "invalid_client"])
def test_the_other_terminal_refusals_are_terminal_too(error: str) -> None:
    handler = _json(400, {"error": error})
    with pytest.raises(AuthorisationRevoked):
        _client(handler).refresh_access_token("Atzr|refresh")


def test_a_server_error_is_retryable_rather_than_terminal() -> None:
    """A 500 says nothing about whether the authorisation is still good."""
    handler = _json(500, {"error": "server_error"})
    with pytest.raises(LwaError) as caught:
        _client(handler).refresh_access_token("Atzr|refresh")
    assert not isinstance(caught.value, AuthorisationRevoked)


def test_a_throttle_is_retryable() -> None:
    handler = _json(429, {"error": "slow_down"})
    with pytest.raises(LwaError) as caught:
        _client(handler).refresh_access_token("Atzr|refresh")
    assert not isinstance(caught.value, AuthorisationRevoked)


def test_an_unreachable_endpoint_is_not_a_revocation() -> None:
    """The most important case to get right: the network is not Amazon's answer."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    with pytest.raises(LwaError) as caught:
        _client(handler).refresh_access_token("Atzr|refresh")
    assert not isinstance(caught.value, AuthorisationRevoked)
    assert "could not reach LWA" in str(caught.value)


def test_a_refusal_with_no_json_still_reports_something_usable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, content=b"<html>gateway</html>")

    with pytest.raises(LwaError, match="http_502"):
        _client(handler).refresh_access_token("Atzr|refresh")


def test_a_success_with_no_refresh_token_is_an_error() -> None:
    """Storing nothing and reporting success would strand the seller."""
    handler = _json(200, {"access_token": "Atza|access", "expires_in": 3600})
    with pytest.raises(LwaError, match="no refresh token"):
        _client(handler).exchange_authorization_code("the-code", "https://x/callback")
