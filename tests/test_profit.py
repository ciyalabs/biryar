"""The profit ledger: attribution, cost basis, and what it refuses to claim.

Payload shapes here are trimmed copies of real ``tax_amazon_txns`` rows — the
nesting (``items[].breakdowns`` with ``Base``/``Tax`` leaves under a named fee)
is what the live feed sends, and a flattened fixture would test a parser that
does not exist.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from src.profit.store import init_costs_db
from src.profit import (
    CostBasis,
    CostEntry,
    build_settlement,
    by_channel,
    by_sku,
    overhead_by_kind,
    period,
)


def _money(amount: str) -> dict[str, object]:
    return {"currencyAmount": float(amount), "currencyCode": "EUR"}


def _leaf(name: str, amount: str) -> dict[str, object]:
    return {"breakdownType": name, "breakdownAmount": _money(amount), "breakdowns": []}


def _fee(name: str, base: str, tax: str) -> dict[str, object]:
    return {"breakdownType": "AmazonFees", "breakdownAmount": _money("0"), "breakdowns": [
        {"breakdownType": name, "breakdownAmount": _money("0"),
         "breakdowns": [_leaf("Base", base), _leaf("Tax", tax)]}]}


def _product(name: str, amount: str) -> dict[str, object]:
    return {"breakdownType": "ProductCharges", "breakdownAmount": _money(amount),
            "breakdowns": [_leaf(name, amount)]}


def shipment(order_id: str, lines: list[dict[str, object]]) -> tuple[str, str, str]:
    """``(type, local_date, raw)`` for one order, one item per line."""
    items = []
    for line in lines:
        items.append({
            "description": line.get("title", ""),
            "contexts": [{"contextType": "ProductContext", "sku": line["sku"],
                          "asin": line.get("asin", ""), "quantityShipped": line.get("units", 1),
                          "fulfillmentNetwork": line.get("network", "MFN")}],
            "breakdowns": [_product("OurPricePrincipal", line["revenue"]),
                           _fee(line.get("fee_name", "Commission"),
                                line["fee_base"], line["fee_tax"])],
        })
    payload = {
        "description": "Order Payment",
        "postedDate": "2026-09-23T10:00:00Z",
        "relatedIdentifiers": [{"relatedIdentifierName": "ORDER_ID",
                                "relatedIdentifierValue": order_id}],
        "items": items,
    }
    return ("Shipment", "2026-09-23", json.dumps(payload))


def released(row: tuple[str, str, str], *, on: str = "2026-10-02",
             deferred_id: str = "t-deferred") -> tuple[str, str, str]:
    """The payout twin Amazon posts a week later for the same shipment.

    Same items, same amounts, same ORDER_ID; the one difference is the
    back-reference to the deferred transaction it settles.
    """
    txn_type, _, raw = row
    payload = json.loads(raw)
    payload["postedDate"] = f"{on}T10:00:00Z"
    payload["relatedIdentifiers"] = list(payload["relatedIdentifiers"]) + [
        {"relatedIdentifierName": "DEFERRED_TRANSACTION_ID",
         "relatedIdentifierValue": deferred_id}]
    return (txn_type, on, json.dumps(payload))


def label(order_id: str, postage: str, fuel: str, vat: str) -> tuple[str, str, str]:
    payload = {
        "description": "LabmanLabelPurchase",
        "postedDate": "2026-09-23T14:00:00Z",
        "relatedIdentifiers": [{"relatedIdentifierName": "ORDER_ID",
                                "relatedIdentifierValue": order_id}],
        "items": [
            {"description": "PostageBilling_Postage",
             "breakdowns": [_leaf("ShippingServiceCharges", postage)]},
            {"description": "PostageBilling_FuelSurcharge",
             "breakdowns": [_leaf("Other", fuel)]},
            {"description": "PostageBilling_VAT",
             "breakdowns": [_leaf("Other", vat)]},
        ],
    }
    return ("Adjustment", "2026-09-23", json.dumps(payload))


def chargeback(order_id: str, adjustment: str, carrier_postage: str,
               carrier_vat: str, customer_credit: str) -> tuple[str, str, str]:
    """A carrier correcting a label it already billed (first seen 08.10.2026)."""
    payload = {
        "description": "LabmanLabelPurchase",
        "postedDate": "2026-10-08T14:00:00Z",
        "relatedIdentifiers": [{"relatedIdentifierName": "ORDER_ID",
                                "relatedIdentifierValue": order_id}],
        "items": [
            {"description": "PostageBilling_PostageAdjustment",
             "breakdowns": [_leaf("ShippingServiceChargebacks", adjustment)]},
            {"description": "PostageBilling_VAT", "breakdowns": [
                {"breakdownType": "ShippingServiceChargebacks",
                 "breakdownAmount": _money("0"), "breakdowns": [
                     {"breakdownType": "CarrierPackagingCharge",
                      "breakdownAmount": _money("0"), "breakdowns": [
                          _leaf("PostageBilling_Postage", carrier_postage),
                          _leaf("PostageBilling_Other", carrier_vat)]}]}]},
            {"description": "PostageBilling_VAT",
             "breakdowns": [
                 {"breakdownType": "ShippingServiceChargebacks",
                  "breakdownAmount": _money("0"), "breakdowns": [
                      _leaf("CustomerPackagingCharge", customer_credit)]}]},
        ],
    }
    return ("Adjustment", "2026-10-08", json.dumps(payload))


def storage_fee(base: str, tax: str) -> tuple[str, str, str]:
    payload = {
        "description": "FBA Inventory Storage Fee",
        "postedDate": "2026-10-07T00:00:00Z",
        "relatedIdentifiers": [],
        "items": [{"description": "FBA Inventory Storage Fee", "contexts": None,
                   "breakdowns": [
                       {"breakdownType": "FBAFees", "breakdownAmount": _money("0"),
                        "breakdowns": [
                            {"breakdownType": "StorageBillingFee",
                             "breakdownAmount": _money("0"), "breakdowns": [
                                 {"breakdownType": "FBAStorageFee",
                                  "breakdownAmount": _money("0"), "breakdowns": [
                                      _leaf("Base", base), _leaf("Tax", tax)]}]}]}]}],
    }
    return ("ServiceFee", "2026-10-07", json.dumps(payload))


def failed_disbursement(amount: str) -> tuple[str, str, str]:
    payload = {
        "description": "FailedDisbursement",
        "postedDate": "2026-10-07T00:00:00Z",
        "relatedIdentifiers": [],
        "items": [{"description": "FailedDisbursement", "contexts": None,
                   "breakdowns": [_leaf("FailedDisbursement", amount)]}],
    }
    return ("AdhocDisbursement", "2026-10-07", json.dumps(payload))


def subscription(amount_base: str, amount_tax: str) -> tuple[str, str, str]:
    payload = {
        "description": "Subscription",
        "postedDate": "2026-09-01T00:00:00Z",
        "relatedIdentifiers": [],
        "items": [{"description": None, "contexts": None,
                   "breakdowns": [_fee("Subscription", amount_base, amount_tax)]}],
    }
    return ("ServiceFee", "2026-09-01", json.dumps(payload))


# -- one sale, two postings --------------------------------------------------------

def test_the_release_twin_is_not_a_second_sale():
    """The release-twin bug: a deferred sale is posted again when
    Amazon releases the money, and summing both sold 35 units out of 24."""
    sale = shipment("111", [{"sku": "MU-1", "revenue": "17.94", "units": 2,
                             "fee_base": "-2.53", "fee_tax": "-0.48"}])

    settlement = build_settlement([sale, released(sale)])

    assert len(settlement.lines) == 1
    assert settlement.lines[0].units == 2
    assert settlement.lines[0].revenue == Decimal("17.94")
    assert settlement.releases_dropped == 1


def test_a_sale_still_deferred_is_counted_once_on_the_day_it_sold():
    """No twin has arrived yet. Dropping it would make the newest week of
    sales disappear, which is the half of the feed that matters most."""
    sale = shipment("111", [{"sku": "MU-1", "revenue": "17.94",
                             "fee_base": "-2.53", "fee_tax": "-0.48"}])

    settlement = build_settlement([sale])

    assert len(settlement.lines) == 1
    assert settlement.lines[0].posted_on == date(2026, 9, 23)
    assert settlement.releases_dropped == 0


def test_a_release_takes_its_label_with_it():
    """A label re-posted alongside the release must not be charged twice
    either -- it would land as orphan postage on an order already paid for."""
    sale = shipment("111", [{"sku": "MU-1", "revenue": "17.94",
                             "fee_base": "-2.53", "fee_tax": "-0.48"}])
    parcel = label("111", "-4.12", "-0.11", "-0.80")

    settlement = build_settlement([sale, parcel, released(parcel)])

    assert settlement.lines[0].postage == Decimal("-5.03")
    assert settlement.orphan_postage == {}


# -- attribution -------------------------------------------------------------------

def test_a_single_sku_order_carries_its_own_label():
    rows = [shipment("111", [{"sku": "MU-1", "revenue": "17.94",
                              "fee_base": "-2.53", "fee_tax": "-0.48"}]),
            label("111", "-4.12", "-0.11", "-0.80")]

    settlement = build_settlement(rows)

    assert len(settlement.lines) == 1
    line = settlement.lines[0]
    assert line.revenue == Decimal("17.94")
    assert line.selling_fees == Decimal("-3.01")
    assert line.postage == Decimal("-5.03")       # postage + fuel + VAT
    # No cost basis was supplied, so the ledger declines to invent a margin.
    assert line.cogs is None
    assert line.contribution is None
    assert settlement.orphan_postage == {}


def test_a_two_sku_parcel_splits_one_label_by_revenue():
    rows = [shipment("222", [
                {"sku": "A", "revenue": "30.00", "fee_base": "-4.00", "fee_tax": "-0.76"},
                {"sku": "B", "revenue": "10.00", "fee_base": "-1.50", "fee_tax": "-0.28"}]),
            label("222", "-6.00", "-0.00", "-0.00")]

    lines = {line.sku: line for line in build_settlement(rows).lines}

    assert lines["A"].postage == Decimal("-4.50")      # 30/40 of the label
    assert lines["B"].postage == Decimal("-1.50")
    # The parts add back to the whole: a parcel cost what it cost.
    assert lines["A"].postage + lines["B"].postage == Decimal("-6.00")


def test_the_rounding_remainder_lands_on_the_last_line_not_in_thin_air():
    rows = [shipment("333", [
                {"sku": "A", "revenue": "10.00", "fee_base": "-1.00", "fee_tax": "-0.19"},
                {"sku": "B", "revenue": "10.00", "fee_base": "-1.00", "fee_tax": "-0.19"},
                {"sku": "C", "revenue": "10.00", "fee_base": "-1.00", "fee_tax": "-0.19"}]),
            label("333", "-1.00", "0", "0")]

    lines = build_settlement(rows).lines

    assert sum(line.postage for line in lines) == Decimal("-1.00")


def test_an_fba_line_carries_no_label():
    rows = [shipment("444", [{"sku": "NC-1", "revenue": "13.98", "network": "AFN",
                              "fee_base": "-2.10", "fee_tax": "-0.40"}])]

    line = build_settlement(rows).lines[0]

    assert line.network == "AFN"
    assert line.postage == Decimal("0")


def test_a_label_for_an_order_outside_the_window_is_reported_not_dropped():
    settlement = build_settlement([label("999", "-4.00", "0", "0")])

    assert settlement.lines == []
    assert settlement.orphan_postage == {"999": Decimal("-4.00")}


def test_the_subscription_stays_overhead_and_never_reaches_a_unit():
    rows = [shipment("555", [{"sku": "A", "revenue": "20.00",
                              "fee_base": "-3.00", "fee_tax": "-0.57"}]),
            subscription("-39.00", "-7.41")]

    settlement = build_settlement(rows)

    assert settlement.lines[0].selling_fees == Decimal("-3.57")
    assert overhead_by_kind(settlement.overheads) == {"subscription": Decimal("-46.41")}
    assert period(settlement).overhead == Decimal("-46.41")


def test_a_carrier_chargeback_is_postage_on_the_parcel_it_corrects():
    """The carrier reweighed the parcel and charged for its own packaging,
    crediting the seller's. It is still the cost of that label."""
    rows = [shipment("777", [{"sku": "A", "revenue": "20.00",
                              "fee_base": "-3.00", "fee_tax": "-0.57"}]),
            chargeback("777", "-0.71", "-3.91", "-0.74", "4.69")]

    settlement = build_settlement(rows)

    assert settlement.lines[0].postage == Decimal("-0.67")
    assert settlement.unmapped == {}
    assert settlement.orphan_postage == {}


def test_storage_is_overhead_of_its_own_kind_not_inbound():
    """Inbound is what it cost to get stock there; storage is what it costs
    to leave it there, and only the second argues for selling faster."""
    rows = [shipment("888", [{"sku": "A", "revenue": "20.00",
                              "fee_base": "-3.00", "fee_tax": "-0.57"}]),
            storage_fee("-7.83", "-1.48")]

    settlement = build_settlement(rows)

    assert overhead_by_kind(settlement.overheads) == {"fba_storage": Decimal("-9.31")}
    assert period(settlement).overhead == Decimal("-9.31")
    assert settlement.lines[0].selling_fees == Decimal("-3.57")
    assert settlement.unmapped == {}


def test_a_bounced_payout_is_movement_not_income():
    """07.10.2026: the new business IBAN was not verified, so Amazon's payout
    came back. Counting it would invent EUR 63.43 of earnings."""
    settlement = build_settlement([failed_disbursement("63.43")])

    assert settlement.overheads == []
    assert settlement.unmapped == {}
    assert period(settlement).overhead == Decimal("0")


def test_an_unknown_fee_path_is_surfaced_rather_than_silently_ignored():
    rows = [shipment("666", [{"sku": "A", "revenue": "20.00", "fee_name": "SomeNewFee",
                              "fee_base": "-1.00", "fee_tax": "-0.19"}])]

    settlement = build_settlement(rows)

    assert settlement.unmapped == {
        "Shipment/AmazonFees/SomeNewFee/Base": Decimal("-1.00"),
        "Shipment/AmazonFees/SomeNewFee/Tax": Decimal("-0.19"),
    }
    # And it is NOT quietly counted as a fee, which would make the margin
    # look right while the cause stayed invisible.
    assert settlement.lines[0].selling_fees == Decimal("0")


# -- cost basis --------------------------------------------------------------------

@pytest.fixture()
def basis(tmp_path):
    return CostBasis(init_costs_db(tmp_path / "t.db"))


def test_a_cost_applies_from_its_date_onward_and_not_before(basis):
    basis.record([CostEntry("MU-1", date(2026, 9, 10), Decimal("8.40"), source="invoice")],
                 by="owner")

    assert basis.cost_for("MU-1", date(2026, 9, 9)) is None
    assert basis.cost_for("MU-1", date(2026, 9, 10)) == Decimal("8.40")
    assert basis.cost_for("MU-1", date(2026, 12, 1)) == Decimal("8.40")


def test_restocking_at_a_new_price_leaves_old_sales_costing_what_they_cost(basis):
    basis.record([CostEntry("MU-1", date(2026, 9, 1), Decimal("8.40")),
                  CostEntry("MU-1", date(2026, 11, 1), Decimal("9.95"))], by="owner")

    assert basis.cost_for("MU-1", date(2026, 10, 15)) == Decimal("8.40")
    assert basis.cost_for("MU-1", date(2026, 11, 2)) == Decimal("9.95")


def test_re_entering_a_date_corrects_it_rather_than_adding_a_second_lot(basis):
    basis.record([CostEntry("MU-1", date(2026, 9, 1), Decimal("8.40"))], by="owner")
    basis.record([CostEntry("MU-1", date(2026, 9, 1), Decimal("8.75"), note="freight")],
                 by="owner")

    assert basis.cost_for("MU-1", date(2026, 9, 5)) == Decimal("8.75")
    assert len(basis.entries("MU-1")) == 1


def test_a_cost_must_be_positive():
    with pytest.raises(ValueError):
        CostEntry("MU-1", date(2026, 9, 1), Decimal("-1.00"))


# -- the whole join ----------------------------------------------------------------

def test_profit_is_revenue_less_amazon_less_postage_less_what_it_cost(basis):
    basis.record([CostEntry("MU-1", date(2026, 9, 1), Decimal("8.00"))], by="owner")
    rows = [shipment("777", [{"sku": "MU-1", "revenue": "17.94",
                              "fee_base": "-2.53", "fee_tax": "-0.48"}]),
            label("777", "-4.12", "-0.11", "-0.80")]

    line = build_settlement(rows, cost_of=basis).lines[0]

    assert line.cogs == Decimal("-8.00")
    #  17.94 − 3.01 − 5.03 − 8.00
    assert line.contribution == Decimal("1.90")


def test_two_units_cost_twice(basis):
    basis.record([CostEntry("A", date(2026, 9, 1), Decimal("5.00"))], by="owner")
    rows = [shipment("888", [{"sku": "A", "units": 2, "revenue": "27.96",
                              "fee_base": "-4.00", "fee_tax": "-0.76"}])]

    assert build_settlement(rows, cost_of=basis).lines[0].cogs == Decimal("-10.00")


def test_one_unpriced_sku_withholds_the_total_but_not_the_gross_contribution(basis):
    basis.record([CostEntry("A", date(2026, 9, 1), Decimal("5.00"))], by="owner")
    rows = [shipment("1", [{"sku": "A", "revenue": "20.00",
                            "fee_base": "-3.00", "fee_tax": "-0.57"}]),
            shipment("2", [{"sku": "B", "revenue": "30.00",
                            "fee_base": "-4.50", "fee_tax": "-0.86"}])]

    summary = period(build_settlement(rows, cost_of=basis))

    assert summary.net_profit is None, "an unpriced unit must not round to profit"
    assert summary.units_without_cost == 1
    assert summary.skus_missing_cost == ["B"]
    assert summary.cost_coverage_pct == Decimal("50.0")
    # What Amazon took is knowable regardless, and still reported.
    assert summary.gross_contribution == Decimal("41.07")


def test_the_channel_cut_makes_fba_and_self_shipping_comparable(basis):
    rows = [shipment("m1", [{"sku": "A", "revenue": "20.00", "network": "MFN",
                             "fee_base": "-2.80", "fee_tax": "-0.53"}]),
            label("m1", "-4.50", "-0.10", "-0.87"),
            shipment("a1", [{"sku": "B", "revenue": "20.00", "network": "AFN",
                             "fee_base": "-2.80", "fee_tax": "-0.53"}])]

    channels = {c.network: c for c in by_channel(build_settlement(rows).lines)}

    assert channels["MFN"].fulfilment_per_unit == Decimal("5.47")
    assert channels["AFN"].fulfilment_per_unit == Decimal("0")
    # Self-shipping takes a far bigger bite of the same €20 sale.
    assert channels["MFN"].amazon_take_pct > channels["AFN"].amazon_take_pct


def test_sku_rows_rank_by_contribution_and_park_the_unpriced_at_the_bottom(basis):
    basis.record([CostEntry("rich", date(2026, 9, 1), Decimal("2.00")),
                  CostEntry("thin", date(2026, 9, 1), Decimal("18.00"))], by="owner")
    rows = [shipment("1", [{"sku": "thin", "revenue": "20.00",
                            "fee_base": "-3.00", "fee_tax": "-0.57"}]),
            shipment("2", [{"sku": "rich", "revenue": "20.00",
                            "fee_base": "-3.00", "fee_tax": "-0.57"}]),
            shipment("3", [{"sku": "unknown", "revenue": "20.00",
                            "fee_base": "-3.00", "fee_tax": "-0.57"}])]

    ranked = by_sku(build_settlement(rows, cost_of=basis).lines)

    assert [row.sku for row in ranked] == ["rich", "thin", "unknown"]
    assert ranked[0].per_unit == Decimal("14.43")
    assert ranked[2].contribution is None


# -- invoice line extraction -------------------------------------------------------

def test_a_gross_priced_line_lands_with_its_share_of_the_delivery():
    """5 x 5.95 + 3.95 delivery = 33.70, so a unit costs 6.74, not 5.95.

    The invoice already carries VAT, so none is added on top.
    """
    from src.profit.invoices import landed_costs, InvoiceLine

    line = InvoiceLine(vendor="acme", doc_number="INV-1", doc_date=date(2026, 9, 30),
                       name="Widget Blue", article_no="2904994",
                       qty=5, unit_price=Decimal("5.95"))

    landed = landed_costs([line], shipping=Decimal("3.95"), prices_include_vat=True)

    assert landed[0].unit_cost == Decimal("6.74")
    assert landed[0].key == "acme:2904994"


def test_a_net_priced_line_carries_the_vat_that_cannot_be_reclaimed():
    """An invoice that arrives untaxed still costs the VAT on the way in."""
    from src.profit.invoices import landed_costs, InvoiceLine

    line = InvoiceLine(vendor="acme", doc_number="INV-2", doc_date=date(2026, 8, 25),
                       name="Widget Red", ean="6290361916889", qty=10,
                       unit_price=Decimal("11.89"))

    assert landed_costs([line])[0].unit_cost == Decimal("14.15")


def test_a_seller_who_reclaims_vat_sets_the_rate_to_zero():
    """Not every seller pays the VAT in the end, so it must be a parameter."""
    from src.profit.invoices import landed_costs, InvoiceLine

    line = InvoiceLine(vendor="acme", doc_number="INV-3", doc_date=date(2026, 8, 25),
                       name="Widget Red", qty=10, unit_price=Decimal("11.89"))

    assert landed_costs([line], vat_rate=Decimal("0"))[0].unit_cost == Decimal("11.89")


def test_delivery_is_split_across_units_not_across_lines():
    """Two lines, nine units, 9.00 delivery — one a unit however they group."""
    from src.profit.invoices import landed_costs, InvoiceLine

    lines = [InvoiceLine(vendor="acme", doc_number="x", doc_date=date(2026, 9, 1),
                         name="a", article_no="1", qty=6, unit_price=Decimal("10.00")),
             InvoiceLine(vendor="acme", doc_number="x", doc_date=date(2026, 9, 1),
                         name="b", article_no="2", qty=3, unit_price=Decimal("20.00"))]

    landed = {c.name: c.unit_cost for c in
              landed_costs(lines, shipping=Decimal("9.00"), prices_include_vat=True)}

    assert landed == {"a": Decimal("11.00"), "b": Decimal("21.00")}


# -- the cost file -----------------------------------------------------------------

def _cost_file(tmp_path, body: str):
    path = tmp_path / "costs.csv"
    path.write_text(
        "vendor,doc_number,doc_date,name,qty,unit_price,ean,article_no,"
        "shipping,prices_include_vat,vat_rate\n" + body, encoding="utf-8")
    return path


def test_a_cost_file_groups_its_rows_into_invoices(tmp_path):
    """Two rows sharing a document number are one invoice, not two."""
    from src.profit.invoices import read_cost_csv

    path = _cost_file(tmp_path,
                      "acme,INV-1,2026-09-30,Blue,5,8.40,,W-1,3.95,false,0.19\n"
                      "acme,INV-1,2026-09-30,Red,3,9.10,,W-2,3.95,false,0.19\n"
                      "acme,INV-2,2026-10-01,Green,2,4.00,,W-3,0,true,0.19\n")

    invoices = read_cost_csv(path)

    assert [inv.doc_number for inv in invoices] == ["INV-1", "INV-2"]
    assert len(invoices[0].lines) == 2
    assert invoices[0].shipping == Decimal("3.95")
    assert invoices[1].prices_include_vat is True


def test_the_delivery_on_a_cost_file_is_charged_once_per_invoice(tmp_path):
    """Repeating the shipping on every row must not charge it twice.

    8 units, 4.00 delivery, so 0.50 a unit. The first line lands at
    (8.00 + 0.50) x 1.19 = 10.12 rather than at twice the delivery.
    """
    from src.profit.invoices import costs_from_file

    path = _cost_file(tmp_path,
                      "acme,INV-1,2026-09-30,Blue,5,8.00,,W-1,4.00,false,0.19\n"
                      "acme,INV-1,2026-09-30,Red,3,8.00,,W-2,4.00,false,0.19\n")

    landed = {c.name: c.unit_cost for c in costs_from_file(path)}

    assert landed == {"Blue": Decimal("10.12"), "Red": Decimal("10.12")}


def test_a_cost_file_missing_a_required_column_says_which(tmp_path):
    """Failing loudly beats landing every cost at zero."""
    import pytest as _pytest
    from src.profit.invoices import CostFileError, read_cost_csv

    path = tmp_path / "bad.csv"
    path.write_text("vendor,doc_number,name,qty\nacme,INV-1,Blue,5\n", encoding="utf-8")

    with _pytest.raises(CostFileError) as caught:
        read_cost_csv(path)

    assert "doc_date" in str(caught.value)
    assert "unit_price" in str(caught.value)


def test_one_unreadable_row_does_not_cost_you_the_others(tmp_path):
    """A bad line is skipped and logged, not fatal."""
    from src.profit.invoices import read_cost_csv

    path = _cost_file(tmp_path,
                      "acme,INV-1,2026-09-30,Blue,5,8.40,,W-1,0,true,0.19\n"
                      "acme,INV-1,not-a-date,Red,3,9.10,,W-2,0,true,0.19\n")

    assert len(read_cost_csv(path)[0].lines) == 1


def test_european_and_english_decimals_both_parse_in_a_cost_file(tmp_path):
    """1.234,56 and 1,234.56 are the same number written two ways."""
    from src.profit.invoices import read_cost_csv

    path = _cost_file(tmp_path,
                      "acme,INV-1,30.09.2026,Blue,1,\"1.234,56\",,W-1,0,true,0.19\n")

    assert read_cost_csv(path)[0].lines[0].unit_price == Decimal("1234.56")


def test_german_and_english_decimals_both_parse():
    from src.profit.invoices import _money

    assert _money("5,95") == Decimal("5.95")
    assert _money("11.89") == Decimal("11.89")
    assert _money("1.234,56") == Decimal("1234.56")


def test_packaging_is_charged_on_a_self_shipped_unit_and_not_on_an_fba_one():
    """Box, tape and filler are real money on no invoice.

    Charged per FBM unit shipped. An FBA line is left alone because Amazon's
    fulfilment fee already paid for the box.
    """
    from src.profit.growth import build_positions
    from src.profit.costs import ResolvedCost

    rows = [shipment("1", [{"sku": "MFN-A", "revenue": "20.00",
                            "fee_base": "-3.00", "fee_tax": "-0.57"}]),
            label("1", "-4.00", "0", "0"),
            shipment("2", [{"sku": "AFN-B", "revenue": "20.00", "network": "AFN",
                            "fee_base": "-3.00", "fee_tax": "-0.57"}])]
    costs = {s: ResolvedCost(s, Decimal("5.00"), "invoice") for s in ("MFN-A", "AFN-B")}

    positions = {p.sku: p for p in build_positions(
        build_settlement(rows).lines, bounds={}, stock={}, costs=costs,
        until=date(2026, 9, 30), packaging_rate=Decimal("1.50"))}

    assert positions["MFN-A"].packaging_per_unit == Decimal("1.50")
    assert positions["AFN-B"].packaging_per_unit == Decimal("0")
    #  20.00 − 3.57 − 4.00 − 1.50 − 5.00
    assert positions["MFN-A"].per_unit == Decimal("5.93")
    assert positions["AFN-B"].per_unit == Decimal("11.43")


def test_packaging_scales_with_the_units_that_shipped():
    from src.profit.growth import build_positions
    from src.profit.costs import ResolvedCost

    rows = [shipment("1", [{"sku": "A", "units": 3, "revenue": "60.00",
                            "fee_base": "-9.00", "fee_tax": "-1.71"}]),
            label("1", "-6.00", "0", "0")]

    position = build_positions(
        build_settlement(rows).lines, bounds={}, stock={},
        costs={"A": ResolvedCost("A", Decimal("5.00"), "invoice")},
        until=date(2026, 9, 30), packaging_rate=Decimal("1.50"))[0]

    assert position.packaging == Decimal("4.50")
