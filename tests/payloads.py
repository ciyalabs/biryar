"""Builders for Finances-feed payloads, shared by every test that needs one.

These live here rather than in one test module because hand-rolling a payload
in a second file is how a test ends up asserting against a shape the ledger
never sees. That already happened once: a synthetic transaction carried its
revenue under a "Sales" breakdown, the ledger reads it from ProductCharges →
OurPricePrincipal, and the test failed claiming revenue was zero when the
ledger was right and the fixture was wrong.

One builder, used everywhere, is the fix.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime


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
    payload["relatedIdentifiers"] = [
        *payload["relatedIdentifiers"],
        {"relatedIdentifierName": "DEFERRED_TRANSACTION_ID",
         "relatedIdentifierValue": deferred_id},
    ]
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


def transaction(
    transaction_id: str,
    row: tuple[str, str, str],
    *,
    on: date | None = None,
) -> dict[str, object]:
    """Adapt a ``(type, local_date, raw)`` row into what ingest stores.

    The ledger's own fixtures produce the triple; ``record_transactions``
    wants the columns around it. Deriving one from the other keeps a single
    description of what a transaction looks like, so a repository test and a
    ledger test cannot disagree about the shape of the thing they are both
    reading.
    """
    txn_type, local_date, raw = row
    payload = json.loads(raw)

    if on is not None:
        local_date = on.isoformat()
        payload["postedDate"] = f"{local_date}T10:00:00Z"

    day = date.fromisoformat(local_date)
    order_id = next(
        (
            identifier["relatedIdentifierValue"]
            for identifier in payload.get("relatedIdentifiers", [])
            if identifier["relatedIdentifierName"] == "ORDER_ID"
        ),
        "",
    )
    return {
        "transaction_id": transaction_id,
        "transaction_type": txn_type,
        "status": "Released",
        "posted_at": datetime(day.year, day.month, day.day, 10, 0, tzinfo=UTC),
        "local_date": day,
        "order_id": order_id,
        "payload": payload,
    }
