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
acquires the VAT on the way in. :mod:`biryar.profit.invoices` states this per
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
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

logger = logging.getLogger(__name__)


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
    """Dated landed costs, and the lookup the ledger calls once per line.

    Deliberately pure. It is built from entries someone else loaded and
    knows nothing about where they were stored, because the rule it
    implements — *the newest cost in force on or before the day it sold* —
    is domain logic, not persistence. Keeping the two apart means that rule
    can be tested on any machine in milliseconds, which matters: it decides
    every margin in the product, and it is the kind of off-by-one that a
    database test would hide behind a container.

    :func:`biryar.repositories.costs.load_cost_basis` builds one from the
    database.
    """

    def __init__(self, entries: Iterable[CostEntry] = ()) -> None:
        self._by_sku: dict[str, list[tuple[date, Decimal]]] = {}
        self._entries: list[CostEntry] = []
        for entry in entries:
            self.add(entry)

    def add(self, entry: CostEntry) -> None:
        """Take one entry, keeping each SKU's history in date order.

        A second entry for a day already held replaces it. There is only
        ever one cost in force on a given day, so re-entering one is a
        correction rather than a second lot.
        """
        history = self._by_sku.setdefault(entry.sku, [])
        for index, (effective, _) in enumerate(history):
            if effective == entry.effective_from:
                history[index] = (entry.effective_from, entry.unit_cost)
                self._entries = [
                    e for e in self._entries
                    if not (e.sku == entry.sku and e.effective_from == entry.effective_from)
                ]
                self._entries.append(entry)
                return
        history.append((entry.effective_from, entry.unit_cost))
        history.sort(key=lambda pair: pair[0])
        self._entries.append(entry)

    def extend(self, entries: Iterable[CostEntry]) -> None:
        """Take several entries. Later ones correct earlier ones for a day."""
        for entry in entries:
            self.add(entry)

    # -- reading -------------------------------------------------------------------

    def cost_for(self, sku: str, on: date) -> Decimal | None:
        """The landed unit cost in force for ``sku`` on ``on``.

        ``None`` when nothing covers it — either the SKU was never priced, or
        it sold before its first entry took effect. Both mean the same thing
        to a caller: do not pretend to know this margin.
        """
        candidates = [cost for effective, cost in self._by_sku.get(sku, ()) if effective <= on]
        return candidates[-1] if candidates else None

    def __call__(self, sku: str, on: date) -> Decimal | None:
        """So a :class:`CostBasis` can be passed straight as ``cost_of``."""
        return self.cost_for(sku, on)

    def entries(self, sku: str | None = None) -> list[CostEntry]:
        """Every entry, newest first, optionally for one SKU."""
        rows = [e for e in self._entries if sku is None or e.sku == sku]
        return sorted(rows, key=lambda e: (e.sku, e.effective_from), reverse=True)

    def priced_skus(self) -> set[str]:
        return set(self._by_sku)

    def __len__(self) -> int:
        return len(self._entries)


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
