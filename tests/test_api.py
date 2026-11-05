"""The HTTP surface.

These go through the real app with a real database, because the things worth
checking here — that a 401 is a 401, that a token for one seller cannot read
another's — are exactly what a mocked dependency would assert into existence.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from biryar.db.models.tenancy import Role
from biryar.db.session import SessionFactory, tenant_session
from biryar.repositories import create_user

pytestmark = pytest.mark.postgres


def _slug(engine: Engine, tenant_id: uuid.UUID) -> str:
    with engine.connect() as conn:
        return str(
            conn.execute(
                text("SELECT slug FROM tenants WHERE id = :id"), {"id": tenant_id},
            ).scalar_one()
        )


def _sign_in(client: TestClient, slug: str, email: str, password: str):
    return client.post(
        "/v1/auth/sign-in",
        json={"email": email, "password": password},
        headers={"X-Biryar-Tenant": slug},
    )


# -- health --------------------------------------------------------------------------

def test_health_reports_the_database_rather_than_assuming_it(
    client: TestClient,
) -> None:
    body = client.get("/v1/health").json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["version"]


def test_the_api_is_versioned(client: TestClient) -> None:
    """Every path carries /v1, so a later shape need not break this one."""
    paths = client.get("/v1/openapi.json").json()["paths"]
    assert paths
    assert all(path.startswith("/v1/") for path in paths), sorted(paths)


# -- signing in ----------------------------------------------------------------------

def test_signing_in_returns_a_token(
    client: TestClient, admin_engine: Engine, factory: SessionFactory,
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, _ = two_tenants
    with tenant_session(factory, a) as s:
        create_user(s, a, email="owner@example.com", display_name="Owner",
                    role=Role.OWNER, password="correct horse")

    response = _sign_in(client, _slug(admin_engine, a), "owner@example.com", "correct horse")

    assert response.status_code == 200
    body = response.json()
    assert body["token"]
    assert body["user"]["email"] == "owner@example.com"
    assert body["user"]["role"] == "owner"


def test_a_wrong_password_and_an_unknown_address_look_identical(
    client: TestClient, admin_engine: Engine, factory: SessionFactory,
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Otherwise this endpoint answers 'is this address registered?'."""
    a, _ = two_tenants
    with tenant_session(factory, a) as s:
        create_user(s, a, email="owner@example.com", display_name="Owner",
                    role=Role.OWNER, password="correct horse")
    slug = _slug(admin_engine, a)

    wrong = _sign_in(client, slug, "owner@example.com", "wrong")
    unknown = _sign_in(client, slug, "nobody@example.com", "wrong")

    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


def test_a_malformed_address_is_refused_before_the_database_sees_it(
    client: TestClient, admin_engine: Engine,
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, _ = two_tenants
    response = _sign_in(client, _slug(admin_engine, a), "not-an-address", "x")
    assert response.status_code == 422


def test_an_unknown_tenant_is_not_a_server_error(client: TestClient) -> None:
    response = _sign_in(client, "no-such-tenant", "owner@example.com", "x")
    assert response.status_code == 404


# -- using a session -----------------------------------------------------------------

def test_me_requires_a_token(client: TestClient) -> None:
    assert client.get("/v1/auth/me").status_code == 401


def test_a_nonsense_token_is_refused(client: TestClient) -> None:
    response = client.get(
        "/v1/auth/me", headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert response.status_code == 401


def test_me_returns_the_signed_in_user(
    client: TestClient, admin_engine: Engine, factory: SessionFactory,
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, _ = two_tenants
    with tenant_session(factory, a) as s:
        create_user(s, a, email="owner@example.com", display_name="Owner",
                    role=Role.OWNER, password="correct horse")
    token = _sign_in(
        client, _slug(admin_engine, a), "owner@example.com", "correct horse",
    ).json()["token"]

    body = client.get(
        "/v1/auth/me", headers={"Authorization": f"Bearer {token}"},
    ).json()

    assert body["email"] == "owner@example.com"
    assert body["display_name"] == "Owner"


def test_signing_out_makes_the_token_useless(
    client: TestClient, admin_engine: Engine, factory: SessionFactory,
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, _ = two_tenants
    with tenant_session(factory, a) as s:
        create_user(s, a, email="owner@example.com", display_name="Owner",
                    role=Role.OWNER, password="correct horse")
    token = _sign_in(
        client, _slug(admin_engine, a), "owner@example.com", "correct horse",
    ).json()["token"]
    auth = {"Authorization": f"Bearer {token}"}

    assert client.post("/v1/auth/sign-out", headers=auth).status_code == 204
    assert client.get("/v1/auth/me", headers=auth).status_code == 401


def test_a_token_carries_its_own_tenant_whatever_the_header_says(
    client: TestClient, admin_engine: Engine, factory: SessionFactory,
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """The header names a tenant for sign-in; it never grants access to one.

    An authenticated request takes its tenant from the session, so pointing
    the header at someone else changes nothing.
    """
    a, b = two_tenants
    with tenant_session(factory, a) as s:
        create_user(s, a, email="a@example.com", display_name="A",
                    role=Role.OWNER, password="password-a")
    with tenant_session(factory, b) as s:
        create_user(s, b, email="b@example.com", display_name="B",
                    role=Role.OWNER, password="password-b")

    token = _sign_in(
        client, _slug(admin_engine, a), "a@example.com", "password-a",
    ).json()["token"]

    body = client.get(
        "/v1/auth/me",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Biryar-Tenant": _slug(admin_engine, b),
        },
    ).json()

    assert body["email"] == "a@example.com"


def test_the_same_address_in_two_tenants_signs_in_to_the_right_one(
    client: TestClient, admin_engine: Engine, factory: SessionFactory,
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, b = two_tenants
    with tenant_session(factory, a) as s:
        create_user(s, a, email="both@example.com", display_name="In A",
                    role=Role.OWNER, password="password-a")
    with tenant_session(factory, b) as s:
        create_user(s, b, email="both@example.com", display_name="In B",
                    role=Role.ASSISTANT, password="password-b")

    in_b = _sign_in(client, _slug(admin_engine, b), "both@example.com", "password-b")

    assert in_b.status_code == 200
    assert in_b.json()["user"]["display_name"] == "In B"
    # A's password must not work against B.
    assert _sign_in(
        client, _slug(admin_engine, b), "both@example.com", "password-a",
    ).status_code == 401
