"""Where a buy price comes from, and how much to trust it.

Several places in this platform claim to know what a unit cost. They disagree,
and one of them is not a cost at all. This module puts them in order and
records which one answered, because a margin built on a supplier's list price
and one built on a paid invoice are not the same claim.

=========================  ====================================================
Source                     What it actually is
=========================  ====================================================
``sku_costs``              The owner's own figure for a dated lot. Beats
                           everything: the only source that can know freight,
                           prep and what the bank took.
``costs.yaml``             The invoice lines, transcribed and keyed by EAN.
                           Real money. Stored net of the acquisition VAT that
                           §19 makes unrecoverable, so it is grossed up here
                           by the book's own ``vat_rate``.
``price_bounds``           ``landed_cost``, but **only** on a row carrying no
                           ``warning``.
``catalogue_offers``       A supplier list price. What the NEXT unit would
``nici_articles``          cost, not what the one that sold did. Last resort,
                           and flagged as an estimate wherever it is used.
``tax_documents``          The invoices themselves: vendor, number, date, and
                           the gross actually paid. No per-SKU split, so it
                           can never price a unit — but it is the only source
                           that proves money left the account, which makes it
                           the check on all of the above.
=========================  ====================================================

## The row that must never be trusted

``repricing/catalogue.py`` fills ``price_bounds`` for every active listing,
and when it has no invoice it derives a cost **backwards from the floor the
owner set in Seller Central**::

    cost = floor × (1 − referral_rate)

It marks the row ``cost basis derived, not from an invoice`` and that marking
is the whole safeguard. The number is circular: a break-even computed from it
lands back on the floor it was made from, so the floor always looks exactly
adequate and a loss-making price reads as healthy. On this account 22 of 26
rows were of that kind, every one of them produced a plausible-looking margin,
and a report was written off them before anyone checked the ``warning``
column.

So a warned row is dropped here rather than ranked lower. A cost that is
really a restated price is worse than no cost, because no cost is visibly
missing and this is not.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

#: The owner's transcription of the invoice lines, keyed by EAN. Same file the
#: listing job prices floors from, so a floor and a margin cannot disagree
#: about what a unit cost.
DEFAULT_COST_BOOK = Path(__file__).resolve().parents[3] / "cockpit" / "pricing" / "costs.yaml"

#: Vendors whose invoices buy stock rather than services. Everything else in
#: ``tax_documents`` — Google, Shopify, Lizenzero, the Amazon fee invoices —
#: is overhead and must not be counted as cost of goods.
GOODS_VENDORS: frozenset[str] = frozenset({
    "qogita", "nici", "mueller", "saturn", "mediamarkt", "terrashop",
    "ic-intracom", "klingele", "prepprime",
})

#: Vendors whose invoice buys a service applied to goods — prep, cartons —
#: rather than the goods themselves. Counted in goods spend because the money
#: lands in inventory, but flagged so it is not mistaken for units bought.
HANDLING_VENDORS: frozenset[str] = frozenset({"prepprime", "klingele"})


@dataclass(frozen=True)
class ResolvedCost:
    """A unit cost with the name of whatever vouched for it."""

    sku: str
    unit_cost: Decimal | None
    #: "owner" | "price_bounds" | "catalogue" | None
    source: str
    detail: str = ""

    @property
    def is_measured(self) -> bool:
        """Whether a human or an invoice stands behind this, not a list price."""
        return self.source in ("owner", "price_bounds")


@dataclass(frozen=True)
class GoodsInvoice:
    vendor: str
    doc_number: str
    doc_date: date
    gross: Decimal
    treatment: str
    #: True when the invoice bought handling (prep, packaging), not units.
    is_handling: bool

    @property
    def landed_gross(self) -> Decimal:
        """What the invoice really cost under §19.

        A Qogita invoice is 0% reverse charge, so its face value is net and the
        19% acquisition VAT the owner pays and cannot reclaim has to be added
        back. A domestic invoice already carries it.
        """
        if self.treatment in ("triangular_25b", "ig_erwerb"):
            return (self.gross * Decimal("1.19")).quantize(Decimal("0.01"))
        return self.gross


def resolve_costs(
    conn: sqlite3.Connection,
    skus: Iterable[str],
    *,
    on: date,
    cost_of: object = None,
    cost_book: Path | str | None = None,
) -> dict[str, ResolvedCost]:
    """Price every SKU from the best source that can honestly answer for it."""
    wanted = list(dict.fromkeys(skus))
    invoiced = invoice_costs(cost_book)
    bounds = _invoiced_bounds(conn)
    catalogue = _catalogue_prices(conn, wanted)

    resolved: dict[str, ResolvedCost] = {}
    for sku in wanted:
        entered = cost_of(sku, on) if cost_of is not None else None   # type: ignore[operator]
        asin = _asin_of(sku)
        if entered is not None:
            resolved[sku] = ResolvedCost(sku, Decimal(entered), "owner", "entered by you")
        elif asin in invoiced:
            cost, detail = invoiced[asin]
            resolved[sku] = ResolvedCost(sku, cost, "invoice", detail)
        elif sku in bounds:
            resolved[sku] = ResolvedCost(sku, bounds[sku], "price_bounds",
                                         "landed cost computed when the SKU was priced")
        elif sku in catalogue:
            price, supplier = catalogue[sku]
            # A list price is net and undated. Gross it up so it is at least
            # comparable with the others, and say plainly that it is a guess.
            resolved[sku] = ResolvedCost(
                sku, (price * Decimal("1.19")).quantize(Decimal("0.01")),
                "catalogue", f"{supplier} list price, VAT added, no freight or prep")
        else:
            resolved[sku] = ResolvedCost(sku, None, "", "nothing on file")
    return resolved


def _invoiced_bounds(conn: sqlite3.Connection) -> dict[str, Decimal]:
    """``price_bounds`` landed costs, minus every derived one.

    The ``warning`` column is the only thing separating a real landed cost
    from the floor restated as a cost. See the module docstring.
    """
    return {
        row[0]: Decimal(str(row[1]))
        for row in conn.execute(
            "SELECT sku, landed_cost, warning FROM price_bounds "
            "WHERE landed_cost IS NOT NULL AND COALESCE(warning, '') = ''")
    }


def invoice_costs(path: Path | str | None = None) -> dict[str, tuple[Decimal, str]]:
    """The owner's invoice book, keyed by ASIN, grossed up for §19.

    ``cockpit/pricing/costs.yaml`` holds the invoice line as printed. The book
    stores it net because the acquisition VAT is a tax treatment rather than a
    supplier price, so the gross-up happens here, using the book's own rate.
    """
    book = Path(path) if path else DEFAULT_COST_BOOK
    if not book.is_file():
        return {}
    try:
        import yaml                                             # noqa: PLC0415

        raw = yaml.safe_load(book.read_text("utf-8")) or {}
    except Exception as exc:                                    # noqa: BLE001
        logger.warning("cost book %s unreadable: %s", book, exc)
        return {}

    vat = Decimal("1") + Decimal(str(raw.get("vat_rate", 0.19)))
    found: dict[str, tuple[Decimal, str]] = {}
    for ean, entry in (raw.get("units") or {}).items():
        if not isinstance(entry, dict) or not entry.get("asin"):
            continue
        cost = entry.get("cost")
        if cost is None:
            continue
        net = Decimal(str(cost))
        gross = (net * vat).quantize(Decimal("0.01"))
        found[str(entry["asin"])] = (
            gross, f"invoice €{net} + {(vat - 1) * 100:.0f}% acquisition VAT (EAN {ean})")
    return found


def _asin_of(sku: str) -> str:
    """The ASIN inside a ``<supplier>-<ASIN>[-channel]`` SKU."""
    parts = sku.split("-")
    return parts[1] if len(parts) >= 2 else ""


def _catalogue_prices(conn: sqlite3.Connection,
                      skus: Iterable[str]) -> dict[str, tuple[Decimal, str]]:
    """Cheapest supplier offer per SKU, matched on the ASIN inside the SKU.

    Every SKU this account creates is ``<supplier>-<ASIN>`` with an optional
    channel suffix, so the ASIN is recoverable without a mapping table.
    """
    by_asin: dict[str, str] = {}
    for sku in skus:
        parts = sku.split("-")
        if len(parts) >= 2 and parts[1]:
            by_asin.setdefault(parts[1], sku)
    if not by_asin:
        return {}
    placeholders = ",".join("?" * len(by_asin))
    found: dict[str, tuple[Decimal, str]] = {}
    for asin, price, source in conn.execute(
        f"SELECT asin, MIN(price), source FROM catalogue_offers "
        f"WHERE asin IN ({placeholders}) AND price > 0 GROUP BY asin", tuple(by_asin)
    ):
        found[by_asin[asin]] = (Decimal(str(price)), str(source or "catalogue"))
    return found


def goods_invoices(conn: sqlite3.Connection, *, since: date | None = None) -> list[GoodsInvoice]:
    """Every invoice from the tax system that put money into inventory.

    This is the audit side: ``tax_documents`` has no per-SKU split, so it can
    never price a unit, but it is the only record that proves what was paid.
    """
    rows = conn.execute(
        "SELECT vendor, doc_number, doc_date, gross, treatment FROM tax_documents "
        "WHERE kind = 'invoice' AND gross IS NOT NULL ORDER BY doc_date")
    invoices = []
    for vendor, number, doc_date, gross, treatment in rows:
        if vendor not in GOODS_VENDORS:
            continue
        when = date.fromisoformat(str(doc_date)[:10])
        if since and when < since:
            continue
        invoices.append(GoodsInvoice(
            vendor=vendor, doc_number=str(number), doc_date=when,
            gross=Decimal(str(gross)), treatment=str(treatment or ""),
            is_handling=vendor in HANDLING_VENDORS))
    return invoices


def spend_by_vendor(invoices: Iterable[GoodsInvoice]) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    for invoice in invoices:
        totals[invoice.vendor] = totals.get(invoice.vendor, Decimal("0")) + invoice.landed_gross
    return dict(sorted(totals.items(), key=lambda kv: -kv[1]))
