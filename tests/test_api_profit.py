"""The two endpoints the product exists for, and the costs that feed them.

Everything here goes through HTTP against a real database. The arithmetic
is tested without either in test_profit.py; what is checked here is the
part only the API can get wrong — who may write a cost, what an import
reports about rows it could not read, and whether an unpriced unit is
allowed to become a profit figure.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from biryar.db.models.credentials import Marketplace
from biryar.db.models.tenancy import Role
from biryar.db.session import SessionFactory, tenant_session
from biryar.repositories import create_user, record_transactions

from .payloads import shipment, transaction

pytestmark = pytest.mark.postgres

COST_CSV = (
    "vendor,doc_number,doc_date,name,qty,unit_price,sku,shipping,"
    "prices_include_vat,vat_rate\n"
    "acme,INV-1,2026-09-01,Widget,10,8.00,SKU-1,0,true,0.19\n"
)

#: The same invoice without a sku column: the cost is right and the product
#: is identified, but nothing in the Finances feed carries an EAN.
COST_CSV_NO_SKU = (
    "vendor,doc_number,doc_date,name,qty,unit_price,ean,shipping,"
    "prices_include_vat,vat_rate\n"
    "acme,INV-2,2026-09-01,Widget,10,8.00,4001234567890,0,true,0.19\n"
)


def _slug(engine: Engine, tenant_id: uuid.UUID) -> str:
    with engine.connect() as conn:
        return str(
            conn.execute(
                text("SELECT slug FROM tenants WHERE id = :id"), {"id": tenant_id}
            ).scalar_one()
        )


def _signed_in(
    client: TestClient,
    engine: Engine,
    factory: SessionFactory,
    tenant_id: uuid.UUID,
    *,
    role: Role = Role.OWNER,
    email: str = "owner@example.com",
) -> dict[str, str]:
    with tenant_session(factory, tenant_id) as s:
        create_user(
            s, tenant_id, email=email, display_name="Someone",
            role=role, password="correct horse",
        )
    token = client.post(
        "/v1/auth/sign-in",
        json={"email": email, "password": "correct horse"},
        headers={"X-Biryar-Tenant": _slug(engine, tenant_id)},
    ).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _sold(
    factory: SessionFactory,
    tenant_id: uuid.UUID,
    *,
    sku: str,
    transaction_id: str = "T-1",
) -> None:
    """One shipment of one unit, stored the way the fetch job stores it."""
    with tenant_session(factory, tenant_id) as s:
        record_transactions(
            s,
            tenant_id,
            Marketplace.DE,
            [
                transaction(
                    transaction_id,
                    shipment(
                        f"ORDER-{transaction_id}",
                        [{"sku": sku, "revenue": "20.00",
                          "fee_base": "-3.00", "fee_tax": "-0.57"}],
                    ),
                )
            ],
        )


# -- who may write a cost ------------------------------------------------------------

def test_importing_costs_is_owner_only(
    client: TestClient, admin_engine: Engine, factory: SessionFactory,
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """An assistant may prepare; a cost sets a price floor, so it may not.

    Not an arbitrary line: every floor is derived from a unit cost, so a
    wrong one sells stock below cost and books a profit while doing it.
    """
    a, _ = two_tenants
    auth = _signed_in(
        client, admin_engine, factory, a,
        role=Role.ASSISTANT, email="assistant@example.com",
    )

    response = client.post("/v1/costs/import", content=COST_CSV, headers=auth)

    assert response.status_code == 403
    assert client.get("/v1/costs", headers=auth).json()["rows"] == []


def test_an_owner_may_import_and_read_back(
    client: TestClient, admin_engine: Engine, factory: SessionFactory,
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, _ = two_tenants
    auth = _signed_in(client, admin_engine, factory, a)

    imported = client.post("/v1/costs/import", content=COST_CSV, headers=auth)
    assert imported.status_code == 200, imported.text
    assert imported.json()["recorded"] == 1
    assert imported.json()["invoices"] == 1
    assert imported.json()["skipped"] == []

    listed = client.get("/v1/costs", headers=auth).json()
    assert listed["skus"] == 1
    assert listed["rows"][0]["sku"] == "SKU-1"
    # 8.00 gross with VAT included and nothing to reclaim here: the stored
    # cost is the printed price, not a figure with VAT added on top.
    assert Decimal(listed["rows"][0]["unit_cost"]) == Decimal("8.00")


def test_reimporting_the_same_file_corrects_rather_than_duplicates(
    client: TestClient, admin_engine: Engine, factory: SessionFactory,
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Running an import twice is what people do when unsure it worked."""
    a, _ = two_tenants
    auth = _signed_in(client, admin_engine, factory, a)

    client.post("/v1/costs/import", content=COST_CSV, headers=auth)
    client.post("/v1/costs/import", content=COST_CSV, headers=auth)

    assert len(client.get("/v1/costs", headers=auth).json()["rows"]) == 1


# -- what an import says about what it could not read --------------------------------

def test_a_cost_with_no_sku_is_reported_as_unmatchable(
    client: TestClient, admin_engine: Engine, factory: SessionFactory,
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """The cost is right, the product is identified, and no sale will find it.

    The Finances feed reports the seller SKU and never a supplier's EAN, so
    an import keyed on an EAN succeeds, stores a correct number, and changes
    no margin. Without this field that reads as a broken ledger rather than
    an incomplete cost file.
    """
    a, _ = two_tenants
    auth = _signed_in(client, admin_engine, factory, a)

    body = client.post(
        "/v1/costs/import", content=COST_CSV_NO_SKU, headers=auth
    ).json()

    assert body["recorded"] == 1
    assert body["skipped"] == []
    assert body["unmatchable"] == ["4001234567890"]


def test_a_bad_row_is_reported_not_merely_skipped(
    client: TestClient, admin_engine: Engine, factory: SessionFactory,
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """A silent skip is worse than a failure.

    The row is dropped so one typo does not cost the other two hundred, but
    a dropped row is a SKU with no cost, and an unpriced SKU raises nothing
    — it quietly leaves profit unanswerable. So the response has to say it.
    """
    a, _ = two_tenants
    auth = _signed_in(client, admin_engine, factory, a)

    csv = COST_CSV + "acme,INV-1,2026-09-01,Broken,not-a-number,8.00,SKU-2,0,true,0.19\n"
    body = client.post("/v1/costs/import", content=csv, headers=auth).json()

    assert body["recorded"] == 1
    assert [row["line"] for row in body["skipped"]] == [3]
    assert body["skipped"][0]["reason"]


def test_the_wrong_file_is_refused_rather_than_imported_empty(
    client: TestClient, admin_engine: Engine, factory: SessionFactory,
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """A missing column is not one bad row, it is the wrong file.

    Reported as a failure rather than a successful import of nothing, which
    is what a per-row skip would have made it look like.
    """
    a, _ = two_tenants
    auth = _signed_in(client, admin_engine, factory, a)

    response = client.post(
        "/v1/costs/import", content="a,b,c\n1,2,3\n", headers=auth
    )

    assert response.status_code == 422
    assert "missing required column" in response.json()["detail"]


# -- the honesty of the profit figure ------------------------------------------------

def test_net_profit_is_null_while_a_sold_unit_has_no_cost(
    client: TestClient, admin_engine: Engine, factory: SessionFactory,
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """An unpriced unit must not be treated as free.

    This is the assertion the whole endpoint exists to keep: gross
    contribution needs nothing the seller supplies and is always answered,
    while net profit stays null and names the SKU that would turn it on.
    """
    a, _ = two_tenants
    auth = _signed_in(client, admin_engine, factory, a)
    _sold(factory, a, sku="SKU-UNPRICED")

    body = client.get("/v1/profit/period", headers=auth).json()

    assert body["units"] == 1
    assert body["net_profit"] is None
    assert body["cogs"] is None
    assert body["units_without_cost"] == 1
    assert body["skus_missing_cost"] == ["SKU-UNPRICED"]
    # Still knowable, and still correct: 20.00 less 3.57 of Amazon's cut.
    assert Decimal(body["gross_contribution"]) == Decimal("16.43")


def test_a_cost_turns_net_profit_on(
    client: TestClient, admin_engine: Engine, factory: SessionFactory,
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, _ = two_tenants
    auth = _signed_in(client, admin_engine, factory, a)
    _sold(factory, a, sku="SKU-1")
    client.post("/v1/costs/import", content=COST_CSV, headers=auth)

    body = client.get("/v1/profit/period", headers=auth).json()

    assert body["units_without_cost"] == 0
    assert body["skus_missing_cost"] == []
    # 16.43 gross contribution less the 8.00 the unit cost. Compared as a
    # number: money is not rounded on the way out, so a cost stored to four
    # decimal places makes this "8.4300" and that is not a difference worth
    # asserting about.
    assert Decimal(body["net_profit"]) == Decimal("8.43")


def test_profit_only_sees_this_tenants_sales(
    client: TestClient, admin_engine: Engine, factory: SessionFactory,
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """The boundary again, through the endpoint rather than the repository."""
    a, b = two_tenants
    auth = _signed_in(client, admin_engine, factory, a)
    _sold(factory, b, sku="SOMEONE-ELSES", transaction_id="T-OTHER")

    assert client.get("/v1/profit/period", headers=auth).json()["units"] == 0
    assert client.get("/v1/profit/skus", headers=auth).json()["rows"] == []


def test_by_sku_reports_a_row_per_sku(
    client: TestClient, admin_engine: Engine, factory: SessionFactory,
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, _ = two_tenants
    auth = _signed_in(client, admin_engine, factory, a)
    _sold(factory, a, sku="SKU-1", transaction_id="T-1")
    _sold(factory, a, sku="SKU-2", transaction_id="T-2")

    rows = client.get("/v1/profit/skus", headers=auth).json()["rows"]

    assert {row["sku"] for row in rows} == {"SKU-1", "SKU-2"}
    assert all(row["units"] == 1 for row in rows)


def test_reading_profit_needs_a_session(client: TestClient) -> None:
    assert client.get("/v1/profit/period").status_code == 401
    assert client.get("/v1/costs").status_code == 401
