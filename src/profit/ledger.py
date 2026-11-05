"""Turn the Amazon Finances feed into one row per SKU per order.

## Why the feed and not the Orders API

The Orders API says what shipped, but it leaves fees empty, because fees
arrive with the Finances feed instead. That feed itemises them **per SKU**
rather than as a settlement total, which is the difference between knowing
what a month cost and knowing what a product cost.

## The three shapes in the feed

``Shipment``
    One transaction per order. Its ``items`` carry a ``ProductContext``
    (``sku``, ``asin``, ``quantityShipped``, ``fulfillmentNetwork``) and the
    fees Amazon charged for *that* line. A two-SKU order is two items.

``Adjustment`` / ``LabmanLabelPurchase``
    A shipping label bought through Buy Shipping. It arrives as its own
    transaction, often hours after the order, and carries the ``ORDER_ID`` it
    belongs to. Postage, fuel surcharge and VAT are three separate items.

``ServiceFee``
    The monthly selling plan and FBA inbound transport. These belong to no
    order and are returned separately as :class:`Overhead`.

## Attribution rule for a label

A label is bought per *parcel*, not per SKU, so a multi-SKU order gets one
label covering all of it. It is split across that order's lines in proportion
to their revenue. Equal splitting would make a €3 add-on item look as
expensive to ship as the €40 item it travelled with, and pro-rata at least
keeps the ranking of margins honest.

## One sale, two postings

A sale on a deferred-settlement account is posted **twice**: once when it
happens, and again when Amazon releases the money a week or so later. The
two carry the same ``SHIPMENT_ID`` and the same amounts, and the second
points back at the first through ``DEFERRED_TRANSACTION_ID``.

Summing both counts the sale twice. On 2026-10-12 the portfolio showed 35
units of the Frosch Baby Waschpulver against 24 real ones, because 32 of the
80 shipments in the feed were release twins. Every total built on these
lines was wrong the same way — units, revenue, fees, velocity, days of
cover. Per-*unit* figures mostly survived, because numerator and denominator
doubled together, which is exactly why this hid for so long.

So a transaction carrying ``DEFERRED_TRANSACTION_ID`` is dropped: it is the
payout twin of a sale already counted, and it adds nothing. A sale still
sitting in ``DEFERRED`` has no twin yet and is counted once, where it
belongs — on the day it sold, not the day the money arrives.

The rule was verified against Amazon's own quarterly summary to the cent
before it was trusted here. If your totals disagree with Amazon's by roughly
the value of a week of sales, this is the first thing to check.

## Signs

Revenue is positive, every cost negative, exactly as Amazon sends them. So a
line's profit is a sum, never a subtraction, and a cost that arrives with an
unexpected sign cannot quietly improve the result.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, Iterator, Sequence
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

#: Amazon's own reports count days in German time, and so does the Finanzamt.
LOCAL = ZoneInfo("Europe/Berlin")

ZERO = Decimal("0")

#: Item leaves that are money the buyer paid. ``ShippingDiscount`` is the
#: negative twin of ``ShippingPrincipal`` on a free-shipping offer: Amazon
#: credits the buyer what it charged, and both sit in revenue so the pair
#: nets to nothing instead of looking like a cost.
REVENUE_LEAVES: frozenset[str] = frozenset({
    "ProductCharges/OurPricePrincipal",
    "Shipping/ShippingPrincipal",
    "PromoRebates/ShippingDiscount",
})

#: The commission, and only the commission. Kept apart from the rest because
#: it is the one selling fee that is a *percentage of the price*: 17.85% order
#: after order, 15.4% in the one lower-rate category. Everything else in
#: ``SELLING_FEE_LEAVES`` is per unit and does not move when the price does.
#:
#: Anything pricing a SKU needs that split. Rolled together, an FBA line
#: reads as a 43% rate because the pick-and-pack is inside it, which is about
#: right at the floor and EUR 1.50 wrong at the top of the band — exactly
#: where a repricer is deciding.
COMMISSION_LEAVES: frozenset[str] = frozenset({
    "AmazonFees/Commission/Base", "AmazonFees/Commission/Tax",
})

#: Item leaves Amazon keeps out of a sale, VAT included. ``Base`` and ``Tax``
#: are summed, because a seller who cannot deduct fee VAT pays both.
SELLING_FEE_LEAVES: frozenset[str] = frozenset({
    "AmazonFees/Commission/Base", "AmazonFees/Commission/Tax",
    "AmazonFees/FBAPerUnitFulfillmentFee/Base", "AmazonFees/FBAPerUnitFulfillmentFee/Tax",
    "AmazonFees/VariableClosingFee/Base", "AmazonFees/VariableClosingFee/Tax",
    "AmazonFees/ShippingHB/Base", "AmazonFees/ShippingHB/Tax",
})

#: Leaves of a bought label. All three are the carrier's price, not Amazon's.
#:
#: ``ShippingServiceChargebacks`` is the carrier correcting a label it has
#: already billed -- it weighed the parcel, or charged for its own packaging
#: and credited the seller's. First seen 2026-10-08, four leaves at once,
#: netting EUR -0.67 on one order. It is postage like the rest, so it is
#: attributed to the parcel rather than left out: a label that cost more than
#: quoted is exactly the kind of thing a per-unit margin must feel.
POSTAGE_LEAVES: frozenset[str] = frozenset({
    "ShippingServiceCharges", "Other",
    "ShippingServiceChargebacks",
    "ShippingServiceChargebacks/CustomerPackagingCharge",
    "ShippingServiceChargebacks/CarrierPackagingCharge/PostageBilling_Postage",
    "ShippingServiceChargebacks/CarrierPackagingCharge/PostageBilling_Other",
})

#: Period costs that belong to no order, and the kind each is filed under.
#: Kept out of unit economics on purpose — see the module docstring.
#:
#: Storage is charged monthly against whatever is sitting in the warehouse,
#: not against a sale, so it cannot be attributed to a line; it is still real
#: money and belongs in the overhead the portfolio has to clear. It gets its
#: own kind rather than joining ``fba_inbound`` because the two say different
#: things: inbound is what it cost to get stock there, storage is what it
#: costs to leave it there, and only the second argues for selling faster.
OVERHEAD_KINDS: tuple[tuple[str, str], ...] = (
    ("AmazonFees/Subscription/", "subscription"),
    ("AmazonFees/FBAInboundTransportationFee/", "fba_inbound"),
    ("AmazonFees/FBAInboundTransportationProgramFee/", "fba_inbound"),
    ("FBAFees/StorageBillingFee/", "fba_storage"),
)

#: Leaves that move money already counted when it was earned: payouts, debt
#: repayment, the EUR 0.01 bank check, and a payout the bank sent back
#: (``FailedDisbursement``, 07.10.2026 — the new business IBAN was not
#: verified yet). Not income, not a cost.
MOVEMENT_LEAVES: frozenset[str] = frozenset({
    "FundTransfer", "DebtPayment", "MicroDeposit", "FailedDisbursement",
})

#: Item descriptions that mark a transaction as a bought shipping label.
LABEL_DESCRIPTION_PREFIX = "PostageBilling_"


@dataclass(frozen=True)
class OrderLine:
    """One SKU on one order, with everything that happened to it."""

    order_id: str
    posted_on: date
    sku: str
    asin: str
    units: int
    #: ``"AFN"`` when Amazon shipped it, ``"MFN"`` when the owner did.
    network: str
    title: str
    #: What the buyer paid for this line, shipping included. Positive.
    revenue: Decimal
    #: Commission, FBA pick & pack, closing fee, VAT included. Negative.
    selling_fees: Decimal
    #: This line's share of the label bought for the parcel. Negative, and
    #: always zero on an ``AFN`` line — Amazon's own fulfilment is billed as
    #: a selling fee, not a label.
    postage: Decimal
    #: Landed cost of the units, negative, or ``None`` when no cost basis
    #: covers this SKU. ``None`` is not zero: it is the reason the profit
    #: below cannot be trusted.
    cogs: Decimal | None
    #: The commission alone, VAT included, and part of ``selling_fees`` rather
    #: than additional to it. Negative. Separated because it is the only cut
    #: that scales with the price, which is what anything choosing a price
    #: needs to know. See :data:`COMMISSION_LEAVES`.
    commission: Decimal = ZERO

    @property
    def amazon_cost(self) -> Decimal:
        """Everything Amazon and the carrier took, as a positive number."""
        return -(self.selling_fees + self.postage)

    @property
    def per_unit_fees(self) -> Decimal:
        """The cut that does *not* move with price, as a positive number.

        Pick & pack, the closing fee and the parcel. What is left of
        ``amazon_cost`` once the commission is taken out.
        """
        return -(self.selling_fees - self.commission + self.postage)

    @property
    def contribution(self) -> Decimal | None:
        """Revenue less every cost traceable to this line.

        ``None`` when the SKU has no cost basis, because a profit computed
        from an unknown cost is a guess wearing a number's clothes.
        """
        if self.cogs is None:
            return None
        return self.revenue + self.selling_fees + self.postage + self.cogs

    @property
    def margin_pct(self) -> Decimal | None:
        contribution = self.contribution
        if contribution is None or self.revenue == ZERO:
            return None
        return contribution / self.revenue * Decimal("100")


@dataclass(frozen=True)
class Overhead:
    """A period cost that belongs to no single order."""

    posted_on: date
    kind: str                       # "subscription" | "fba_inbound" | "fba_storage"
    amount: Decimal                 # negative
    description: str


@dataclass
class Settlement:
    """Everything the feed says about a window, already attributed."""

    lines: list[OrderLine] = field(default_factory=list)
    overheads: list[Overhead] = field(default_factory=list)
    #: Leaf paths the ledger does not classify, with their totals. A non-empty
    #: dict means a fee type is silently missing from every number below.
    unmapped: dict[str, Decimal] = field(default_factory=dict)
    #: Labels whose ``ORDER_ID`` matched no shipment in the window, with
    #: their totals. Normal at a window edge, a bug anywhere else.
    orphan_postage: dict[str, Decimal] = field(default_factory=dict)
    #: Release twins dropped as already counted. Not an error -- a healthy
    #: feed on a deferred account is roughly half of them -- but a number
    #: worth being able to see, because a sudden zero means either the feed
    #: changed shape or the account came off deferred settlement.
    releases_dropped: int = 0

    @property
    def skus_missing_cost(self) -> list[str]:
        return sorted({line.sku for line in self.lines if line.cogs is None})


def build_settlement(
    rows: Iterable[tuple[str, str, str]],
    *,
    cost_of: Any = None,
) -> Settlement:
    """Explode raw ``tax_amazon_txns`` rows into attributed order lines.

    ``rows`` yields ``(type, local_date, raw_json)`` — exactly the columns the
    table holds, so a caller can pass a cursor straight in.

    ``cost_of`` is any callable ``(sku, on_date) -> Decimal | None`` returning
    the **landed cost of one unit** as a positive number;
    :class:`~src.profit.costs.CostBasis` is one. ``None`` from it, or
    no callable at all, leaves ``OrderLine.cogs`` unset rather than zero.
    """
    settlement = Settlement()
    unmapped: dict[str, Decimal] = defaultdict(Decimal)
    # order_id -> the label cost of that parcel, negative.
    labels: dict[str, Decimal] = defaultdict(Decimal)
    # order_id -> lines, so a label can be split once every line is known.
    by_order: dict[str, list[OrderLine]] = defaultdict(list)

    for txn_type, local_date, raw in rows:
        payload = json.loads(raw)
        if _is_release(payload):
            # The payout twin of a sale already in this loop. Counting it
            # again doubles the order. See "One sale, two postings" above.
            settlement.releases_dropped += 1
            continue
        posted_on = _as_date(local_date, payload)
        order_id = _order_id(payload)

        if txn_type == "Shipment":
            for line in _shipment_lines(payload, posted_on, order_id, unmapped):
                by_order[order_id].append(line)
        elif _is_label(payload):
            labels[order_id] += _label_cost(payload, unmapped)
        else:
            settlement.overheads.extend(_overheads(payload, posted_on, unmapped))

    for order_id, lines in by_order.items():
        settlement.lines.extend(_with_postage(lines, labels.pop(order_id, ZERO), cost_of))

    settlement.orphan_postage = {oid: amount for oid, amount in labels.items() if amount != ZERO}
    settlement.unmapped = {path: total for path, total in unmapped.items() if total != ZERO}
    if settlement.unmapped:
        logger.warning("profit ledger: %d unclassified fee paths: %s",
                       len(settlement.unmapped), ", ".join(sorted(settlement.unmapped)))
    return settlement


# -- per transaction ---------------------------------------------------------------

def _shipment_lines(payload: dict[str, Any], posted_on: date, order_id: str,
                    unmapped: dict[str, Decimal]) -> Iterator[OrderLine]:
    for item in payload.get("items") or []:
        context = _product_context(item)
        if context is None:
            # A shipment item with no product is a fee Amazon could not pin to
            # a SKU. Count it so it cannot vanish, but it cannot become a line.
            for path, amount in _leaves(item.get("breakdowns")):
                unmapped[f"Shipment(no sku)/{path}"] += amount
            continue
        revenue, fees, commission = ZERO, ZERO, ZERO
        for path, amount in _leaves(item.get("breakdowns")):
            if path in REVENUE_LEAVES:
                revenue += amount
            elif path in SELLING_FEE_LEAVES:
                fees += amount
                if path in COMMISSION_LEAVES:
                    commission += amount
            else:
                unmapped[f"Shipment/{path}"] += amount
        yield OrderLine(
            order_id=order_id,
            posted_on=posted_on,
            sku=str(context.get("sku") or ""),
            asin=str(context.get("asin") or ""),
            units=int(context.get("quantityShipped") or 0),
            network=str(context.get("fulfillmentNetwork") or ""),
            title=str(item.get("description") or ""),
            revenue=revenue,
            selling_fees=fees,
            commission=commission,
            postage=ZERO,            # filled in once the whole order is known
            cogs=None,               # filled in by _with_postage
        )


def _is_label(payload: dict[str, Any]) -> bool:
    return any(str(item.get("description") or "").startswith(LABEL_DESCRIPTION_PREFIX)
               for item in payload.get("items") or [])


def _label_cost(payload: dict[str, Any], unmapped: dict[str, Decimal]) -> Decimal:
    total = ZERO
    for item in payload.get("items") or []:
        for path, amount in _leaves(item.get("breakdowns")):
            if path in POSTAGE_LEAVES:
                total += amount
            else:
                unmapped[f"Label/{path}"] += amount
    return total


def _overheads(payload: dict[str, Any], posted_on: date,
               unmapped: dict[str, Decimal]) -> Iterator[Overhead]:
    by_kind: dict[str, Decimal] = defaultdict(Decimal)
    for item in payload.get("items") or []:
        for path, amount in _leaves(item.get("breakdowns")):
            kind = next((k for prefix, k in OVERHEAD_KINDS
                         if path.startswith(prefix)), None)
            if kind is None:
                # Payouts and debt repayment move money that was already
                # counted when it was earned; they are not a cost.
                if path not in MOVEMENT_LEAVES:
                    unmapped[f"Other/{path}"] += amount
                continue
            by_kind[kind] += amount
    for kind, amount in by_kind.items():
        yield Overhead(posted_on=posted_on, kind=kind, amount=amount,
                       description=str(payload.get("description") or ""))


def _with_postage(lines: Sequence[OrderLine], label: Decimal,
                  cost_of: Any) -> Iterator[OrderLine]:
    """Split one parcel's label across its lines and attach the cost basis.

    The split is pro-rata on revenue, with the last line taking the rounding
    remainder so the parts always add back to the whole cent.
    """
    total_revenue = sum((line.revenue for line in lines), ZERO)
    spent = ZERO
    for index, line in enumerate(lines):
        last = index == len(lines) - 1
        if label == ZERO:
            share = ZERO
        elif last or total_revenue == ZERO:
            share = label - spent
        else:
            share = (label * line.revenue / total_revenue).quantize(Decimal("0.01"))
        spent += share
        yield OrderLine(
            order_id=line.order_id, posted_on=line.posted_on, sku=line.sku,
            asin=line.asin, units=line.units, network=line.network, title=line.title,
            revenue=line.revenue, selling_fees=line.selling_fees,
            commission=line.commission,
            postage=share, cogs=_cogs(line, cost_of),
        )


def _cogs(line: OrderLine, cost_of: Any) -> Decimal | None:
    if cost_of is None or not line.sku or line.units <= 0:
        return None
    unit_cost = cost_of(line.sku, line.posted_on)
    if unit_cost is None:
        return None
    return -(Decimal(unit_cost) * line.units)


# -- payload helpers ---------------------------------------------------------------

def _leaves(nodes: list[dict[str, Any]] | None, prefix: str = "") -> Iterator[tuple[str, Decimal]]:
    for node in nodes or []:
        path = f"{prefix}/{node['breakdownType']}" if prefix else node["breakdownType"]
        children = node.get("breakdowns")
        if children:
            yield from _leaves(children, path)
        else:
            # str() first: the API sends JSON numbers, and Decimal(19.71)
            # keeps the float's tail.
            yield path, Decimal(str(node["breakdownAmount"]["currencyAmount"]))


def _product_context(item: dict[str, Any]) -> dict[str, Any] | None:
    for context in item.get("contexts") or []:
        if context.get("contextType") == "ProductContext" and context.get("sku"):
            return context
    return None


def _is_release(payload: dict[str, Any]) -> bool:
    """Is this the payout twin of a sale already counted?

    Amazon marks it by pointing back at the deferred transaction it settles.
    The status field is not the test: both halves of the pair can read
    ``DEFERRED_RELEASED`` or ``RELEASED`` depending on when the feed was
    harvested, while the back-reference only ever appears on the second.
    """
    for related in payload.get("relatedIdentifiers") or []:
        if related.get("relatedIdentifierName") == "DEFERRED_TRANSACTION_ID":
            return bool(related.get("relatedIdentifierValue"))
    return False


def _order_id(payload: dict[str, Any]) -> str:
    for related in payload.get("relatedIdentifiers") or []:
        if related.get("relatedIdentifierName") == "ORDER_ID":
            return str(related.get("relatedIdentifierValue") or "")
    return ""


def _as_date(local_date: str, payload: dict[str, Any]) -> date:
    if local_date:
        return date.fromisoformat(local_date[:10])
    posted = str(payload.get("postedDate") or "")
    return datetime.fromisoformat(posted.replace("Z", "+00:00")).astimezone(LOCAL).date()
