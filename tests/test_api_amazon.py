"""Connecting a seller's Amazon account, end to end through the API.

Amazon is answered by a mock transport; everything else is real. The callback
is the one unauthenticated route in the product, so most of what is checked
here is what it refuses.
"""

from __future__ import annotations

import uuid
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from biryar.amazon import LwaApplication, LwaClient
from biryar.amazon.oauth import sign_state
from biryar.api.deps import get_lwa_client
from biryar.db.models.tenancy import Role
from biryar.db.session import SessionFactory, tenant_session
from biryar.repositories import create_user

pytestmark = pytest.mark.postgres


def _answer_lwa(client: TestClient, handler) -> None:
    """Make the callback's exchange go to ``handler`` instead of Amazon."""
    client.app.dependency_overrides[get_lwa_client] = lambda: LwaClient(
        LwaApplication(client_id="id", client_secret="secret"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _granted(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "refresh_token": "Atzr|the-sellers-refresh-token",
            "access_token": "Atza|short-lived",
            "expires_in": 3600,
            "scope": "sellingpartnerapi::migration",
        },
    )


def _signed_in(
    client: TestClient,
    engine: Engine,
    factory: SessionFactory,
    tenant_id: uuid.UUID,
) -> dict[str, str]:
    with tenant_session(factory, tenant_id) as s:
        create_user(
            s, tenant_id, email="owner@example.com", display_name="Owner",
            role=Role.OWNER, password="correct horse",
        )
    with engine.connect() as conn:
        slug = conn.execute(
            text("SELECT slug FROM tenants WHERE id = :id"), {"id": tenant_id},
        ).scalar_one()
    token = client.post(
        "/v1/auth/sign-in",
        json={"email": "owner@example.com", "password": "correct horse"},
        headers={"X-Biryar-Tenant": slug},
    ).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _state_for(client: TestClient, auth: dict[str, str]) -> str:
    url = client.post("/v1/amazon/authorize", headers=auth).json()["url"]
    return str(parse_qs(urlparse(url).query)["state"][0])


# -- starting the flow ---------------------------------------------------------------

def test_authorize_returns_a_consent_url_carrying_state(
    client: TestClient, admin_engine: Engine, factory: SessionFactory,
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, _ = two_tenants
    auth = _signed_in(client, admin_engine, factory, a)

    parsed = urlparse(client.post("/v1/amazon/authorize", headers=auth).json()["url"])
    query = parse_qs(parsed.query)

    assert parsed.netloc == "sellercentral.amazon.de"
    assert parsed.path == "/apps/authorize/consent"
    assert query["application_id"] == ["amzn1.sellerapps.app.test"]
    assert query["state"]
    assert query["version"] == ["beta"]     # the test application is a draft


def test_authorize_needs_a_signed_in_user(client: TestClient) -> None:
    assert client.post("/v1/amazon/authorize").status_code == 401


# -- the callback, which is the unauthenticated one ----------------------------------

def test_a_completed_authorisation_is_stored_sealed(
    client: TestClient, admin_engine: Engine, factory: SessionFactory,
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, _ = two_tenants
    auth = _signed_in(client, admin_engine, factory, a)
    state = _state_for(client, auth)
    _answer_lwa(client, _granted)

    response = client.get(
        "/v1/amazon/callback",
        params={
            "state": state,
            "spapi_oauth_code": "the-code",
            "selling_partner_id": "A1SELLERPARTNER",
        },
    )

    assert response.status_code == 200
    assert response.json()["connected"] is True
    assert response.json()["selling_partner_id"] == "A1SELLERPARTNER"

    with admin_engine.connect() as conn:
        sealed = conn.execute(
            text("SELECT refresh_token_sealed FROM amazon_credentials")
        ).scalar_one()
    assert b"Atzr" not in bytes(sealed)


def test_the_tenant_comes_from_the_state_not_the_caller(
    client: TestClient, admin_engine: Engine, factory: SessionFactory,
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """The callback has no session, so state is the only thing that can say."""
    a, b = two_tenants
    auth = _signed_in(client, admin_engine, factory, a)
    state = _state_for(client, auth)
    _answer_lwa(client, _granted)

    client.get(
        "/v1/amazon/callback",
        params={"state": state, "spapi_oauth_code": "c", "selling_partner_id": "A1"},
    )

    with admin_engine.connect() as conn:
        owners = [
            row[0]
            for row in conn.execute(text("SELECT tenant_id FROM amazon_credentials"))
        ]

    assert owners == [a]
    assert b not in owners


def test_a_state_this_instance_did_not_mint_is_refused(
    client: TestClient, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Otherwise anyone could complete an authorisation into another account."""
    a, _ = two_tenants
    _answer_lwa(client, _granted)

    response = client.get(
        "/v1/amazon/callback",
        params={
            "state": sign_state(a, "not-this-instances-secret"),
            "spapi_oauth_code": "c",
            "selling_partner_id": "A1",
        },
    )

    assert response.status_code == 400
    assert "not valid" in response.json()["detail"]


def test_the_refusal_does_not_say_which_check_failed(
    client: TestClient, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """A browser cannot act on it; only someone probing benefits."""
    a, _ = two_tenants
    _answer_lwa(client, _granted)

    forged = client.get(
        "/v1/amazon/callback",
        params={
            "state": sign_state(a, "wrong-secret"),
            "spapi_oauth_code": "c",
            "selling_partner_id": "A1",
        },
    ).json()["detail"]
    junk = client.get(
        "/v1/amazon/callback",
        params={
            "state": "not-even-a-token",
            "spapi_oauth_code": "c",
            "selling_partner_id": "A1",
        },
    ).json()["detail"]

    assert forged == junk


def test_nothing_is_exchanged_or_stored_when_the_state_is_bad(
    client: TestClient, admin_engine: Engine,
) -> None:
    """The code must not even be presented to Amazon before state verifies."""
    exchanged = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal exchanged
        exchanged = True
        return _granted(request)

    _answer_lwa(client, handler)
    client.get(
        "/v1/amazon/callback",
        params={"state": "rubbish", "spapi_oauth_code": "c", "selling_partner_id": "A1"},
    )

    with admin_engine.connect() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM amazon_credentials")
        ).scalar_one()

    assert not exchanged
    assert count == 0


def test_amazon_refusing_the_code_is_a_client_error(
    client: TestClient, admin_engine: Engine, factory: SessionFactory,
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, _ = two_tenants
    auth = _signed_in(client, admin_engine, factory, a)
    state = _state_for(client, auth)
    _answer_lwa(client, lambda r: httpx.Response(400, json={"error": "invalid_grant"}))

    response = client.get(
        "/v1/amazon/callback",
        params={"state": state, "spapi_oauth_code": "stale", "selling_partner_id": "A1"},
    )

    assert response.status_code == 400


def test_amazon_being_unreachable_is_not_the_sellers_fault(
    client: TestClient, admin_engine: Engine, factory: SessionFactory,
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """502, not 400: the authorisation may be perfectly good."""
    a, _ = two_tenants
    auth = _signed_in(client, admin_engine, factory, a)
    state = _state_for(client, auth)

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    _answer_lwa(client, unreachable)
    response = client.get(
        "/v1/amazon/callback",
        params={"state": state, "spapi_oauth_code": "c", "selling_partner_id": "A1"},
    )

    assert response.status_code == 502


# -- status --------------------------------------------------------------------------

def test_status_says_not_connected_before_anything_happens(
    client: TestClient, admin_engine: Engine, factory: SessionFactory,
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, _ = two_tenants
    auth = _signed_in(client, admin_engine, factory, a)
    assert client.get("/v1/amazon/status", headers=auth).json()["connected"] is False


def test_status_reflects_a_completed_authorisation(
    client: TestClient, admin_engine: Engine, factory: SessionFactory,
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, _ = two_tenants
    auth = _signed_in(client, admin_engine, factory, a)
    state = _state_for(client, auth)
    _answer_lwa(client, _granted)
    client.get(
        "/v1/amazon/callback",
        params={"state": state, "spapi_oauth_code": "c", "selling_partner_id": "A1SELLER"},
    )

    body = client.get("/v1/amazon/status", headers=auth).json()

    assert body["connected"] is True
    assert body["selling_partner_id"] == "A1SELLER"


# -- the promise the setup guide makes in print --------------------------------------

def test_the_refresh_token_never_comes_back_out(
    client: TestClient, admin_engine: Engine, factory: SessionFactory,
    two_tenants: tuple[uuid.UUID, uuid.UUID],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """docs/setup/sp-api.md tells a seller the token is never shown again.

    That sentence is the reason someone is willing to paste a client secret
    into this at all, so it is checked rather than asserted: the plaintext
    must appear in no response body and no log record anywhere along the
    flow that produces it. Storage is covered separately by
    ``test_a_completed_authorisation_is_stored_sealed``; this is about
    everything that is not storage.
    """
    secret = "Atzr|the-sellers-refresh-token"       # what _granted hands back
    a, _ = two_tenants
    auth = _signed_in(client, admin_engine, factory, a)
    state = _state_for(client, auth)
    _answer_lwa(client, _granted)

    with caplog.at_level("DEBUG", logger="biryar"):
        callback = client.get(
            "/v1/amazon/callback",
            params={
                "state": state,
                "spapi_oauth_code": "the-code",
                "selling_partner_id": "A1SELLERPARTNER",
            },
        )
        status = client.get("/v1/amazon/status", headers=auth)

    assert callback.status_code == 200 and status.status_code == 200

    for name, body in (("callback", callback.text), ("status", status.text)):
        assert secret not in body, f"the refresh token is in the {name} response"

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert secret not in logged, "the refresh token was written to a log"
