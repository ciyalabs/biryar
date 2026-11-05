"""What a unit actually cost, per SKU, over time.

Amazon can never supply this. It is the one number in the whole profit
calculation that only you hold, and until it exists every margin on every
page is revenue minus Amazon's cut, which is not profit.

## Landed, gross, per unit

The number stored is what one sellable unit cost **by the time it was ready to
ship**: the supplier's price, plus shipping in, plus whatever the border and
the bank took.

Whether that figure is gross or net depends on your own VAT position. A
seller who reclaims input VAT should store the net price. A seller who cannot
reclaim it pays the VAT and never gets it back, so for them the gross is the
cost. The same applies to a reverse-charge invoice, which arrives untaxed and
acquires the VAT on the way in. :mod:`src.profit.invoices` states this per
invoice rather than inferring it from the vendor.

## Why cost is dated

The same SKU bought again in November at a different price is a second entry,
not an edit. A sale in October must keep costing what October's stock cost,
or last month's margins silently change every time new stock arrives. Lookup
therefore asks "what was in force on the day it sold", and the newest entry
on or before that day wins.

This is deliberately *not* FIFO across individual purchase lots. Lot tracking
needs per-unit provenance that nothing in this platform records, and the
a typical reorder cycle is measured in weeks, so dated cost and FIFO agree
except in the days around a price change.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Iterable

logger = logging.getLogger(__name__)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class CostEntry:
    """One SKU's landed unit cost, in force from a date."""

    sku: str
    effective_from: date
    #: Landed cost of one unit, positive, in euro.
    unit_cost: Decimal
    #: Where the number came from — an invoice number, a supplier, a note to
    #: self. Free text, but an entry nobody can trace is an entry nobody can
    #: check.
    source: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        if self.unit_cost <= 0:
            raise ValueError(f"{self.sku}: unit cost must be positive, got {self.unit_cost}")
        if not self.sku.strip():
            raise ValueError("a cost entry needs a SKU")


class CostBasis:
    """Dated landed costs, with the lookup the ledger calls per line.

    Reads are cached for the life of the instance: one settlement pass asks
    for the same dozen SKUs hundreds of times, and the table changes only when
    the owner enters a cost. Build a new instance after writing, which
    :meth:`record` does for you.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._cache: dict[str, list[tuple[date, Decimal]]] = {}

    # -- reading -------------------------------------------------------------------

    def cost_for(self, sku: str, on: date) -> Decimal | None:
        """The landed unit cost in force for ``sku`` on ``on``.

        ``None`` when nothing covers it — either the SKU was never priced, or
        it sold before its first entry took effect. Both mean the same thing
        to a caller: do not pretend to know this margin.
        """
        history = self._history(sku)
        candidates = [cost for effective, cost in history if effective <= on]
        return candidates[-1] if candidates else None

    def __call__(self, sku: str, on: date) -> Decimal | None:
        """So a :class:`CostBasis` can be passed straight as ``cost_of``."""
        return self.cost_for(sku, on)

    def _history(self, sku: str) -> list[tuple[date, Decimal]]:
        if sku not in self._cache:
            self._cache[sku] = [
                (date.fromisoformat(row[0]), Decimal(row[1]))
                for row in self._conn.execute(
                    "SELECT effective_from, unit_cost FROM sku_costs "
                    "WHERE sku = ? ORDER BY effective_from", (sku,))
            ]
        return self._cache[sku]

    def entries(self, sku: str | None = None) -> list[CostEntry]:
        """Every stored cost, newest first, optionally for one SKU."""
        sql = ("SELECT sku, effective_from, unit_cost, source, note FROM sku_costs "
               + ("WHERE sku = ? " if sku else "")
               + "ORDER BY sku, effective_from DESC")
        rows = self._conn.execute(sql, (sku,) if sku else ())
        return [CostEntry(sku=r[0], effective_from=date.fromisoformat(r[1]),
                          unit_cost=Decimal(r[2]), source=r[3] or "", note=r[4] or "")
                for r in rows]

    def priced_skus(self) -> set[str]:
        return {r[0] for r in self._conn.execute("SELECT DISTINCT sku FROM sku_costs")}

    # -- writing -------------------------------------------------------------------

    def record(self, entries: Iterable[CostEntry], *, by: str) -> int:
        """Store costs, replacing any entry for the same SKU and date.

        Re-entering a cost for a date already held is a correction, not a
        second lot — there is only ever one cost in force on a given day.
        """
        rows = [(e.sku, e.effective_from.isoformat(), str(e.unit_cost),
                 e.source, e.note, by, _utcnow()) for e in entries]
        if not rows:
            return 0
        with self._conn:
            self._conn.executemany(
                "INSERT INTO sku_costs(sku, effective_from, unit_cost, source, note,"
                " entered_by, entered_at) VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(sku, effective_from) DO UPDATE SET"
                " unit_cost = excluded.unit_cost, source = excluded.source,"
                " note = excluded.note, entered_by = excluded.entered_by,"
                " entered_at = excluded.entered_at", rows)
        self._cache.clear()
        logger.info("cost basis: %d entries recorded by %s", len(rows), by)
        return len(rows)

    def forget(self, sku: str, effective_from: date) -> bool:
        """Drop one entry. Returns whether a row went."""
        with self._conn:
            cursor = self._conn.execute(
                "DELETE FROM sku_costs WHERE sku = ? AND effective_from = ?",
                (sku, effective_from.isoformat()))
        self._cache.clear()
        return cursor.rowcount > 0


def invoiced_costs(
    conn: sqlite3.Connection, skus: Iterable[str], *, on: date | None = None,
) -> dict[str, float]:
    """Landed cost per SKU from the recorded invoices. Absent where unknown.

    The one honest source of a cost basis: what the goods were actually
    invoiced at. Deliberately **no fallback** — not to ``price_bounds``, not
    to a share of the seller's floor. A SKU missing from this dict has no cost
    basis, and every caller is better off knowing that than holding a number
    derived from the price it is itself trying to set.

    That circularity is not hypothetical. ``price_bounds.landed_cost`` was
    filled as ``floor x (1 - referral_rate)`` for twenty of twenty-six SKUs,
    so the break-even computed from it was a restatement of the floor, and the
    bandit's reward was graded against it — €14.44 where the invoice said
    €6.74. Shared by the repricer's catalogue sync and the bandit so there is
    one answer to "what did this unit cost" and not two.
    """
    basis = CostBasis(conn)
    day = on or date.today()
    out: dict[str, float] = {}
    for sku in skus:
        cost = basis.cost_for(str(sku), day)
        if cost is not None and float(cost) > 0:
            out[str(sku)] = float(cost)
    return out


@dataclass(frozen=True)
class ResolvedCost:
    """A unit cost with the name of whatever vouched for it.

    The ``source`` matters as much as the number. A margin built on an
    invoice is a measurement; one built on a supplier's list price is an
    estimate wearing the same shape, and the two should never be presented
    as equally trustworthy.
    """

    sku: str
    unit_cost: Decimal | None
    #: "invoice" | "manual" | "estimate" | ""
    source: str
    detail: str = ""

    @property
    def is_measured(self) -> bool:
        """Whether a document or a human stands behind this, not a list price."""
        return self.source in ("invoice", "manual")
