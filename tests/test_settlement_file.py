"""Reading a downloaded settlement report.

No database and no network: the adapter is a pure function from text to
figures, which is the whole reason it can be the thing someone tries first.

The property that matters most here is conservation. Amazon documents the
column names of this report and not the values of `amount-type` and
`amount-description`, so any fee map is incomplete by construction. That is
survivable only if an unrecognised amount is still *counted* — reported
under its own name, and included in the total that is checked against the
payout Amazon states. A classifier that quietly drops what it does not know
produces confident wrong numbers, which is the one outcome worse than an
error.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from biryar.profit.ledger import ZERO
from biryar.profit.settlement_file import (
    SettlementFileError,
    read_settlement_file,
)

COLUMNS = [
    "settlement-id", "settlement-start-date", "settlement-end-date",
    "deposit-date", "total-amount", "currency", "transaction-type",
    "order-id", "merchant-order-id", "adjustment-id", "shipment-id",
    "marketplace-name", "amount-type", "amount-description", "amount",
    "fulfillment-id", "posted-date", "posted-date-time", "order-item-code",
    "merchant-order-item-id", "merchant-adjustment-item-id", "sku",
    "quantity-purchased", "promotion-id",
]


def _row(**values: str) -> str:
    return "\t".join(values.get(name, "") for name in COLUMNS)


def _file(*rows: str, total: str = "0.00") -> str:
    """A report: header, the summary row, then the given rows."""
    summary = _row(
        **{
            "settlement-id": "1234567890",
            "settlement-start-date": "2026-09-01",
            "settlement-end-date": "2026-09-15",
            "deposit-date": "2026-09-17",
            "total-amount": total,
            "currency": "EUR",
        }
    )
    return "\t".join(COLUMNS) + "\n" + summary + "\n" + "\n".join(rows) + "\n"


def _sale(
    *, order: str = "ORDER-1", sku: str = "SKU-1",
    amount_type: str, description: str, amount: str, units: str = "1",
) -> str:
    return _row(**{
        "transaction-type": "Order",
        "order-id": order,
        "sku": sku,
        "amount-type": amount_type,
        "amount-description": description,
        "amount": amount,
        "posted-date": "2026-09-10",
        "quantity-purchased": units,
    })


# -- the shape of the file -----------------------------------------------------------

def test_the_summary_row_is_not_a_transaction() -> None:
    """It carries the payout total; counting it doubles the period.

    It is recognised by having no transaction type rather than by being
    first, because a file someone has opened in a spreadsheet may not have
    it first any more.
    """
    report = read_settlement_file(
        _file(
            _sale(amount_type="ItemPrice", description="Principal", amount="20.00"),
            total="20.00",
        )
    )

    assert report.rows_read == 1
    assert report.settlement.lines[0].revenue == Decimal("20.00")
    assert report.stated_total == Decimal("20.00")


def test_a_file_that_is_not_a_settlement_report_says_so() -> None:
    with pytest.raises(SettlementFileError) as caught:
        read_settlement_file("a\tb\tc\n1\t2\t3\n")
    assert "amount-type" in str(caught.value)
    assert "Flat File V2" in str(caught.value)


def test_a_file_with_no_summary_row_is_refused() -> None:
    """Without the stated total there is nothing to check the figures against."""
    text = "\t".join(COLUMNS) + "\n" + _sale(
        amount_type="ItemPrice", description="Principal", amount="20.00"
    ) + "\n"
    with pytest.raises(SettlementFileError) as caught:
        read_settlement_file(text)
    assert "cannot be checked" in str(caught.value)


def test_columns_are_read_by_name_not_position() -> None:
    """A reordered export, or one with an extra column, still reads."""
    reordered = ["amount", "amount-description", "amount-type", *(
        c for c in COLUMNS if c not in {"amount", "amount-description", "amount-type"}
    ), "some-new-column"]

    def row(**values: str) -> str:
        return "\t".join(values.get(name, "") for name in reordered)

    text = (
        "\t".join(reordered) + "\n"
        + row(**{"settlement-id": "1", "total-amount": "20.00", "currency": "EUR"}) + "\n"
        + row(**{
            "transaction-type": "Order", "order-id": "O-1", "sku": "SKU-1",
            "amount-type": "ItemPrice", "amount-description": "Principal",
            "amount": "20.00", "quantity-purchased": "1",
        }) + "\n"
    )

    report = read_settlement_file(text)
    assert report.settlement.lines[0].revenue == Decimal("20.00")


# -- the property that makes an incomplete map safe ----------------------------------

def test_an_unknown_fee_is_reported_and_still_counted() -> None:
    """Amazon does not publish these values, so some will be unrecognised.

    The unrecognised one must appear in `unmapped` under its own name, and
    must still be part of the total that reconciles — otherwise an
    unfamiliar fee silently improves the figures.
    """
    report = read_settlement_file(
        _file(
            _sale(amount_type="ItemPrice", description="Principal", amount="20.00"),
            _sale(amount_type="ItemFees", description="Commission", amount="-3.00"),
            _sale(
                amount_type="SomethingNew", description="NeverSeenBefore",
                amount="-1.50",
            ),
            total="15.50",
        )
    )

    assert "order/somethingnew/neverseenbefore" in report.settlement.unmapped
    assert report.settlement.unmapped["order/somethingnew/neverseenbefore"] == Decimal("-1.50")
    # And it did not quietly vanish from the arithmetic.
    assert report.counted_total == Decimal("15.50")
    assert report.reconciles


def test_everything_in_the_file_adds_up_to_the_stated_payout() -> None:
    """The check the API path cannot make.

    The file says what Amazon paid. If the amounts do not add up to it,
    something was read wrong, and the figures should not be trusted — which
    is a thing this can know and say.
    """
    report = read_settlement_file(
        _file(
            _sale(amount_type="ItemPrice", description="Principal", amount="20.00"),
            _sale(amount_type="ItemPrice", description="Shipping", amount="3.99"),
            _sale(amount_type="ItemFees", description="Commission", amount="-3.57"),
            _sale(amount_type="ItemFees", description="FBAPerUnitFulfillmentFee", amount="-2.10"),
            _sale(amount_type="Promotion", description="Shipping", amount="-3.99"),
            _row(**{
                "transaction-type": "ServiceFee",
                "amount-type": "FBA Fees",
                "amount-description": "Subscription",
                "amount": "-39.00",
                "posted-date": "2026-09-01",
            }),
            total="-24.67",
        )
    )

    assert report.reconciles, report.difference
    assert report.difference == ZERO


def test_a_payout_is_not_counted_as_income_but_still_reconciles() -> None:
    """A transfer moves money already earned.

    It must stay out of the figures and stay in the reconciliation, which
    is the only combination that is both correct and checkable.
    """
    report = read_settlement_file(
        _file(
            _sale(amount_type="ItemPrice", description="Principal", amount="20.00"),
            _row(**{
                "transaction-type": "Transfer",
                "amount-type": "Transfer",
                "amount-description": "Payable to Amazon",
                "amount": "-20.00",
            }),
            total="0.00",
        )
    )

    assert report.reconciles
    assert sum(line.revenue for line in report.settlement.lines) == Decimal("20.00")
    assert report.settlement.unmapped == {}


def test_a_file_that_does_not_reconcile_says_so() -> None:
    """Rather than producing confident figures from a misread file."""
    report = read_settlement_file(
        _file(
            _sale(amount_type="ItemPrice", description="Principal", amount="20.00"),
            total="999.00",
        )
    )

    assert not report.reconciles
    assert report.difference == Decimal("-979.00")


# -- what it builds ------------------------------------------------------------------

def test_rows_for_one_sku_become_one_line() -> None:
    """A line's principal and its fees arrive as separate rows."""
    report = read_settlement_file(
        _file(
            _sale(amount_type="ItemPrice", description="Principal", amount="20.00"),
            _sale(amount_type="ItemFees", description="Commission", amount="-3.00"),
            _sale(amount_type="ItemFees", description="VariableClosingFee", amount="-1.10"),
            total="15.90",
        )
    )

    assert len(report.settlement.lines) == 1
    line = report.settlement.lines[0]
    assert line.revenue == Decimal("20.00")
    assert line.selling_fees == Decimal("-4.10")
    # Commission is separable, because it is the one fee that is a
    # percentage of price and pricing needs it apart from the rest.
    assert line.commission == Decimal("-3.00")


def test_two_skus_on_one_order_stay_apart() -> None:
    report = read_settlement_file(
        _file(
            _sale(sku="SKU-1", amount_type="ItemPrice", description="Principal", amount="20.00"),
            _sale(sku="SKU-2", amount_type="ItemPrice", description="Principal", amount="5.00"),
            total="25.00",
        )
    )

    assert {line.sku for line in report.settlement.lines} == {"SKU-1", "SKU-2"}


def test_period_costs_do_not_land_on_a_sale() -> None:
    """Subscription is period overhead, not a cost of whatever sold."""
    report = read_settlement_file(
        _file(
            _sale(amount_type="ItemPrice", description="Principal", amount="20.00"),
            _row(**{
                "transaction-type": "ServiceFee",
                "amount-description": "Subscription",
                "amount-type": "FBA Fees",
                "amount": "-39.00",
                "posted-date": "2026-09-01",
            }),
            total="-19.00",
        )
    )

    assert [o.kind for o in report.settlement.overheads] == ["subscription"]
    assert report.settlement.overheads[0].amount == Decimal("-39.00")
    assert report.settlement.lines[0].revenue == Decimal("20.00")


def test_a_cost_basis_turns_cogs_on() -> None:
    """The same callable the feed path takes, so figures are comparable."""
    report = read_settlement_file(
        _file(
            _sale(amount_type="ItemPrice", description="Principal", amount="20.00", units="2"),
            total="20.00",
        ),
        cost_of=lambda sku, on: Decimal("4.00") if sku == "SKU-1" else None,
    )

    assert report.settlement.lines[0].cogs == Decimal("-8.00")
    assert report.settlement.skus_missing_cost == []


def test_without_a_cost_basis_nothing_is_priced() -> None:
    """An unpriced unit is None, never zero. Zero would mean free."""
    report = read_settlement_file(
        _file(
            _sale(amount_type="ItemPrice", description="Principal", amount="20.00"),
            total="20.00",
        )
    )

    assert report.settlement.lines[0].cogs is None
    assert report.settlement.skus_missing_cost == ["SKU-1"]


def test_an_unreadable_amount_is_an_error_not_a_zero() -> None:
    """Reading it as zero would drop real money from a total that must add up."""
    with pytest.raises(SettlementFileError):
        read_settlement_file(
            _file(
                _sale(amount_type="ItemPrice", description="Principal", amount="not-a-number"),
                total="0.00",
            )
        )
