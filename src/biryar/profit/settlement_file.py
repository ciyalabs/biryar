"""The settlement report a seller can download, without any API at all.

## Why this exists

Everything else in Biryar needs an SP-API application, which needs a
developer registration, which is a review with a human on the other end. A
seller who wants to know whether any of this is worth their evening cannot
find out until Amazon replies.

They can, however, download a settlement report from Seller Central today.
It is the same money, already settled, and it is enough to produce real
figures. So this reads one.

## What the file is

``GET_V2_SETTLEMENT_REPORT_DATA_FLAT_FILE_V2``: tab-separated, one header
row, then one row per amount. A single order line appears as several rows —
one for the principal, one for each fee — which is why nothing here can
decide anything from a row in isolation.

**The first row after the header is a summary, not a transaction.** It
carries the settlement id, the period, the deposit date and the payout
total, and nothing else: no ``transaction-type``, no order, no amount type.
Treating it as data adds the whole payout to revenue. It is recognised by
the absence of a transaction type rather than by position, because position
is a guess about a file someone may have edited in a spreadsheet.

## The part Amazon does not publish

The column names are documented. The *values* of ``amount-type`` and
``amount-description`` are not. There is no authoritative list of fee
descriptions, and a seller in a category this has never seen will have ones
that are not below.

So the rule here is the one rule that makes an incomplete map safe: an
amount this does not recognise is **never dropped**. It lands in
:attr:`~biryar.profit.ledger.Settlement.unmapped` under its own name, where
it is visible and can be reported, and it is counted in the totals that
check against Amazon's own figure. A classifier that guesses is worse than
one that admits what it has not seen, because a guess that puts a fee in
the wrong bucket is invisible and a missing one is not.

## Checking itself

The file states its own payout total, which nothing in the API path does.
Every amount in it must add up to that figure. :func:`read_settlement_file`
computes the difference and returns it, so a file that does not reconcile
says so rather than producing confident, wrong numbers.
"""

from __future__ import annotations

import csv
import io
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .ledger import ZERO, OrderLine, Overhead, Settlement

logger = logging.getLogger(__name__)

#: Columns the reader cannot work without. Everything else is optional, so
#: a report with extra or reordered columns still parses — rows are read by
#: header name, never by position.
REQUIRED_COLUMNS: frozenset[str] = frozenset({
    "transaction-type", "amount-type", "amount-description", "amount",
})


class SettlementFileError(ValueError):
    """The file is not a V2 settlement report."""


# -- the vocabulary ------------------------------------------------------------------
#
# Amazon publishes the column names and not these values. Each entry below
# is one this has actually been reasoned about; anything absent is reported
# rather than assumed. Keys are (amount-type, amount-description), both
# lower-cased, and a (type, None) entry matches any description for that
# type.

#: What the buyer paid. Positive in the file.
REVENUE: frozenset[tuple[str, str | None]] = frozenset({
    ("itemprice", "principal"),
    ("itemprice", "shipping"),
    ("itemprice", "giftwrap"),
    # Tax Amazon collects and remits itself passes through the settlement
    # as both a credit and a debit; both are kept so the pair nets out
    # rather than one side landing in unmapped.
    ("itemprice", "tax"),
    ("itemprice", "shippingtax"),
    ("itemprice", "giftwraptax"),
})

#: Money Amazon keeps out of the sale. Negative in the file.
SELLING_FEES: frozenset[tuple[str, str | None]] = frozenset({
    ("itemfees", None),
})

#: The commission specifically — a percentage of price, unlike the rest.
#: Pricing needs it separable; see ledger.COMMISSION_LEAVES.
COMMISSION_DESCRIPTIONS: frozenset[str] = frozenset({
    "commission",
})

#: Promotions the seller funded. Negative, and part of what the sale
#: actually earned rather than a separate cost.
PROMOTION: frozenset[tuple[str, str | None]] = frozenset({
    ("promotion", None),
})

#: Tax Amazon withheld and remits itself. Nets against the tax collected
#: above; kept visible as revenue so the pair cancels.
WITHHELD_TAX: frozenset[tuple[str, str | None]] = frozenset({
    ("itemwithheldtax", None),
})

#: Transaction types that move money already counted when it was earned.
#: A payout is not income; counting it doubles the period.
MOVEMENT_TYPES: frozenset[str] = frozenset({
    "transfer", "liquidations", "debt", "deposit",
})

#: Transaction types whose amounts belong to no order. The second element
#: is the overhead kind recorded against them.
OVERHEAD_TYPES: tuple[tuple[str, str], ...] = (
    ("servicefee", "subscription"),
    ("fbainboundtransportationfee", "fba_inbound"),
    ("storagefee", "fba_storage"),
)


@dataclass
class SettlementFile:
    """One downloaded report: what Amazon said, and what was made of it."""

    settlement_id: str
    period_start: date | None
    period_end: date | None
    deposit_date: date | None
    currency: str
    #: The payout Amazon states in the summary row.
    stated_total: Decimal
    #: Every amount in the file, added up. Should equal ``stated_total``.
    counted_total: Decimal
    settlement: Settlement
    rows_read: int = 0

    @property
    def reconciles(self) -> bool:
        """Whether this file's amounts add up to the payout it states.

        False means something was read wrong — not that a fee is
        unrecognised, since unmapped amounts are still counted. Trust the
        figures only when this is true.
        """
        return self.difference == ZERO

    @property
    def difference(self) -> Decimal:
        return self.counted_total - self.stated_total


def _money(raw: str) -> Decimal:
    """A column that should hold an amount.

    Empty is zero: most rows leave most columns blank. A value that is
    present and unreadable is an error, because silently reading it as zero
    would drop real money out of a total that is supposed to reconcile.
    """
    text = (raw or "").strip()
    if not text:
        return ZERO
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation as exc:
        raise SettlementFileError(f"{text!r} is not an amount") from exc


def _day(raw: str) -> date | None:
    """A date column, in any of the shapes these files have carried."""
    text = (raw or "").strip()
    if not text:
        return None
    # "2026-09-30", "2026-09-30 14:22:01 UTC", "30.09.2026"
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(text[:len(fmt) + 6].strip(), fmt).date()
        except ValueError:
            continue
    head = text.split(" ")[0]
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(head, fmt).date()
        except ValueError:
            continue
    return None


def _matches(table: frozenset[tuple[str, str | None]], kind: str, desc: str) -> bool:
    return (kind, desc) in table or (kind, None) in table


def _normalise(row: dict[str, str]) -> dict[str, str]:
    return {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}


@dataclass
class _Line:
    """An order line being accumulated across several rows."""

    order_id: str
    sku: str
    posted_on: date | None = None
    units: int = 0
    revenue: Decimal = ZERO
    fees: Decimal = ZERO
    commission: Decimal = ZERO


def read_settlement_file(
    text: str,
    *,
    cost_of: Any = None,
) -> SettlementFile:
    """Read a V2 settlement report into the same shape the API path builds.

    ``cost_of`` is the same ``(sku, on_date) -> Decimal | None`` callable
    :func:`biryar.profit.ledger.build_settlement` takes, so a file and a
    feed produce comparable figures from the same cost basis.
    """
    reader = csv.DictReader(io.StringIO(text, newline=""), delimiter="\t")
    if reader.fieldnames is None:
        raise SettlementFileError("the file is empty")

    present = {(name or "").strip().lower() for name in reader.fieldnames}
    missing = REQUIRED_COLUMNS - present
    if missing:
        raise SettlementFileError(
            "this does not look like a V2 settlement report; it is missing "
            f"{', '.join(sorted(missing))}. Download 'Flat File V2' from "
            "Seller Central → Reports → Payments → All Statements."
        )

    summary: dict[str, str] = {}
    lines: dict[tuple[str, str], _Line] = {}
    overheads: list[Overhead] = []
    unmapped: dict[str, Decimal] = defaultdict(lambda: ZERO)
    counted = ZERO
    rows_read = 0

    for raw in reader:
        row = _normalise(raw)
        if not any(row.values()):
            continue

        kind = row.get("transaction-type", "").lower()
        if not kind:
            # The summary row: no transaction type, and the only row that
            # carries the payout total. Recognised by shape, not position.
            if not summary and row.get("total-amount"):
                summary = row
            continue

        rows_read += 1
        amount = _money(row.get("amount", ""))
        counted += amount
        if amount == ZERO:
            continue

        amount_type = row.get("amount-type", "").lower()
        description = row.get("amount-description", "").lower()

        if kind in MOVEMENT_TYPES:
            # A payout moves money already counted when it was earned.
            # Excluded from the settlement, but still part of `counted`, so
            # the reconciliation below stays a real check.
            continue

        overhead_kind = next(
            (name for prefix, name in OVERHEAD_TYPES if kind.startswith(prefix)),
            None,
        )
        if overhead_kind is not None:
            overheads.append(
                Overhead(
                    posted_on=_day(row.get("posted-date", "")) or date.min,
                    kind=overhead_kind,
                    amount=amount,
                    description=row.get("amount-description", "") or kind,
                )
            )
            continue

        order_id = row.get("order-id") or row.get("merchant-order-id") or ""
        sku = row.get("sku", "")
        if not order_id or not sku:
            # An amount with no order and no SKU cannot be attributed to a
            # line. Reported rather than discarded.
            unmapped[f"{kind}/{amount_type}/{description}".strip("/")] += amount
            continue

        line = lines.setdefault((order_id, sku), _Line(order_id=order_id, sku=sku))
        posted = _day(row.get("posted-date", ""))
        if posted is not None and (line.posted_on is None or posted < line.posted_on):
            line.posted_on = posted
        units = row.get("quantity-purchased", "")
        if units.isdigit():
            line.units = max(line.units, int(units))

        if _matches(REVENUE, amount_type, description) or _matches(
            WITHHELD_TAX, amount_type, description
        ):
            line.revenue += amount
        elif _matches(PROMOTION, amount_type, description):
            line.revenue += amount          # negative: a seller-funded discount
        elif _matches(SELLING_FEES, amount_type, description):
            line.fees += amount
            if description in COMMISSION_DESCRIPTIONS:
                line.commission += amount
        else:
            unmapped[f"{kind}/{amount_type}/{description}".strip("/")] += amount

    if not summary:
        raise SettlementFileError(
            "no summary row: this file states no settlement total, so its "
            "figures cannot be checked against Amazon's."
        )

    built = Settlement(
        lines=[
            OrderLine(
                order_id=line.order_id,
                posted_on=line.posted_on or date.min,
                sku=line.sku,
                asin="",            # the settlement report carries no ASIN
                units=line.units,
                network="",         # nor the fulfilment network
                title="",
                revenue=line.revenue,
                selling_fees=line.fees,
                commission=line.commission,
                postage=ZERO,       # a bought label is not itemised per order here
                cogs=(
                    None
                    if cost_of is None or line.units <= 0
                    else _cogs(cost_of, line)
                ),
            )
            for line in lines.values()
        ],
        overheads=overheads,
        unmapped=dict(unmapped),
    )

    return SettlementFile(
        settlement_id=summary.get("settlement-id", ""),
        period_start=_day(summary.get("settlement-start-date", "")),
        period_end=_day(summary.get("settlement-end-date", "")),
        deposit_date=_day(summary.get("deposit-date", "")),
        currency=summary.get("currency", ""),
        stated_total=_money(summary.get("total-amount", "")),
        counted_total=counted,
        settlement=built,
        rows_read=rows_read,
    )


def _cogs(cost_of: Any, line: _Line) -> Decimal | None:
    """Landed cost of this line's units, negative, or None if unpriced."""
    unit = cost_of(line.sku, line.posted_on or date.min)
    if unit is None:
        return None
    return -(Decimal(unit) * line.units)
