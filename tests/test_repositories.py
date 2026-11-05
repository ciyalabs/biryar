"""Storage: round trips, idempotent ingest, and the tenant boundary again.

The margin arithmetic is tested without a database in test_profit.py. What
is left here is what only a database can answer — that an upsert really
replaces instead of duplicating, and that one seller's costs and settlements
are invisible to another.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from biryar.db.models.credentials import Marketplace
from biryar.db.session import SessionFactory, tenant_session
from biryar.profit import CostEntry, build_settlement
from biryar.repositories import (
    forget_cost,
    load_cost_basis,
    record_costs,
    record_transactions,
    settlement_rows,
)

pytestmark = pytest.mark.postgres


def _txn(
    transaction_id: str,
    *,
    sku: str = "SKU-1",
    revenue: str = "20.00",
    on: date = date(2026, 9, 15),
) -> dict[str, object]:
    """One Shipment transaction, in the shape the SP-API client hands over."""
    return {
        "transaction_id": transaction_id,
        "transaction_type": "Shipment",
        "status": "Released",
        "posted_at": datetime(on.year, on.month, on.day, 10, 0, tzinfo=UTC),
        "local_date": on,
        "order_id": f"ORDER-{transaction_id}",
        "payload": {
            "sellingPartnerMetadata": {"marketplaceId": Marketplace.DE.value},
            "relatedIdentifiers": [
                {"relatedIdentifierName": "ORDER_ID",
                 "relatedIdentifierValue": f"ORDER-{transaction_id}"},
            ],
            "items": [{
                "contexts": [{
                    "contextType": "ProductContext",
                    "sku": sku,
                    "quantityShipped": 1,
                    "fulfillmentNetwork": "MFN",
                }],
                "breakdowns": [{
                    "breakdownType": "Sales",
                    "breakdownAmount": {"currencyAmount": float(revenue),
                                        "currencyCode": "EUR"},
                    "breakdowns": [],
                }],
                "totalAmount": {"currencyAmount": float(revenue), "currencyCode": "EUR"},
            }],
        },
    }


# -- costs ---------------------------------------------------------------------------

def test_costs_round_trip(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, _ = two_tenants
    with tenant_session(factory, a) as s:
        record_costs(
            s, a,
            [CostEntry("SKU-1", date(2026, 9, 1), Decimal("8.40"), source="invoice")],
            by="owner",
        )

    with tenant_session(factory, a) as s:
        basis = load_cost_basis(s, a)

    assert basis.cost_for("SKU-1", date(2026, 9, 5)) == Decimal("8.40")
    assert basis.cost_for("SKU-1", date(2026, 8, 31)) is None


def test_re_importing_an_invoice_corrects_rather_than_duplicates(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Invoice import is exactly the thing people run twice when unsure."""
    a, _ = two_tenants
    with tenant_session(factory, a) as s:
        record_costs(s, a, [CostEntry("SKU-1", date(2026, 9, 1), Decimal("8.40"))],
                     by="owner")
        record_costs(s, a, [CostEntry("SKU-1", date(2026, 9, 1), Decimal("8.75"),
                                      note="freight found")], by="owner")

    with tenant_session(factory, a) as s:
        basis = load_cost_basis(s, a)

    assert len(basis.entries("SKU-1")) == 1
    assert basis.cost_for("SKU-1", date(2026, 9, 5)) == Decimal("8.75")


def test_a_restock_is_a_second_dated_entry(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """October's sales must keep costing what October's stock cost."""
    a, _ = two_tenants
    with tenant_session(factory, a) as s:
        record_costs(s, a, [
            CostEntry("SKU-1", date(2026, 9, 1), Decimal("8.40")),
            CostEntry("SKU-1", date(2026, 11, 1), Decimal("9.95")),
        ], by="owner")

    with tenant_session(factory, a) as s:
        basis = load_cost_basis(s, a)

    assert basis.cost_for("SKU-1", date(2026, 10, 15)) == Decimal("8.40")
    assert basis.cost_for("SKU-1", date(2026, 11, 2)) == Decimal("9.95")


def test_one_tenants_costs_are_invisible_to_another(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, b = two_tenants
    with tenant_session(factory, a) as s:
        record_costs(s, a, [CostEntry("SKU-1", date(2026, 9, 1), Decimal("8.40"))],
                     by="owner")

    with tenant_session(factory, b) as s:
        assert load_cost_basis(s, b).cost_for("SKU-1", date(2026, 9, 5)) is None


def test_forgetting_a_cost_reports_whether_one_went(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, _ = two_tenants
    with tenant_session(factory, a) as s:
        record_costs(s, a, [CostEntry("SKU-1", date(2026, 9, 1), Decimal("8.40"))],
                     by="owner")
        assert forget_cost(s, a, "SKU-1", date(2026, 9, 1)) is True
        assert forget_cost(s, a, "SKU-1", date(2026, 9, 1)) is False


# -- transactions --------------------------------------------------------------------

def test_re_fetching_an_overlapping_window_stores_each_sale_once(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Windows overlap by design, so ingest has to be idempotent.

    Without this, every backfill and every retry inflates revenue by the
    size of the overlap, and the figure still looks plausible.
    """
    a, _ = two_tenants
    batch = [_txn("T-1"), _txn("T-2")]

    with tenant_session(factory, a) as s:
        record_transactions(s, a, Marketplace.DE, batch)
    with tenant_session(factory, a) as s:
        record_transactions(s, a, Marketplace.DE, [*batch, _txn("T-3")])

    with tenant_session(factory, a) as s:
        rows = list(settlement_rows(s, a))

    assert len(rows) == 3


def test_stored_transactions_feed_the_ledger_unchanged(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """The round trip through PostgreSQL must not alter what the ledger sees."""
    a, _ = two_tenants
    with tenant_session(factory, a) as s:
        record_transactions(s, a, Marketplace.DE, [_txn("T-1", revenue="20.00")])

    with tenant_session(factory, a) as s:
        settlement = build_settlement(settlement_rows(s, a))

    assert len(settlement.lines) == 1
    line = settlement.lines[0]
    assert line.sku == "SKU-1"
    assert line.revenue == Decimal("20.00")
    assert line.posted_on == date(2026, 9, 15)


def test_a_date_range_is_inclusive_at_both_ends(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, _ = two_tenants
    with tenant_session(factory, a) as s:
        record_transactions(s, a, Marketplace.DE, [
            _txn("T-1", on=date(2026, 9, 1)),
            _txn("T-2", on=date(2026, 9, 15)),
            _txn("T-3", on=date(2026, 9, 30)),
        ])

    with tenant_session(factory, a) as s:
        rows = list(settlement_rows(s, a, start=date(2026, 9, 1), end=date(2026, 9, 15)))

    assert [row[1] for row in rows] == ["2026-09-01", "2026-09-15"]


def test_one_tenants_settlements_are_invisible_to_another(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, b = two_tenants
    with tenant_session(factory, a) as s:
        record_transactions(s, a, Marketplace.DE, [_txn("T-1")])

    with tenant_session(factory, b) as s:
        assert list(settlement_rows(s, b)) == []
