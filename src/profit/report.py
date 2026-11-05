"""Roll attributed order lines up into the numbers worth looking at.

Three cuts, because the owner asked three questions that turned out to be the
same question:

:class:`SkuProfit`
    What does this product actually earn per unit, after Amazon, after the
    parcel, after what it cost to buy.
:class:`ChannelProfit`
    Is self-shipping cheaper than FBA. The feed answers this directly: an
    ``MFN`` line carries a bought label, an ``AFN`` line carries a pick-and-pack
    fee, and the two are comparable per unit.
:class:`PeriodProfit`
    Does the whole thing make money once the monthly plan and the inbound
    freight are paid — the costs that belong to no product.

Every total carries how much of it rests on a known cost. A margin computed
over half-priced inventory is not half right, it is unusable, so
``units_without_cost`` sits next to the number rather than in a footnote.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Iterable, Sequence

from .ledger import OrderLine, Overhead, Settlement

ZERO = Decimal("0")


def _pct(part: Decimal, whole: Decimal) -> Decimal | None:
    return None if whole == ZERO else (part / whole * Decimal("100")).quantize(Decimal("0.1"))


@dataclass(frozen=True)
class SkuProfit:
    sku: str
    asin: str
    title: str
    units: int
    orders: int
    revenue: Decimal
    selling_fees: Decimal           # negative
    postage: Decimal                # negative
    cogs: Decimal | None            # negative, None when nothing is priced
    #: Units in this group whose SKU had no cost basis on the day they sold.
    units_without_cost: int

    @property
    def contribution(self) -> Decimal | None:
        if self.cogs is None or self.units_without_cost:
            return None
        return self.revenue + self.selling_fees + self.postage + self.cogs

    @property
    def margin_pct(self) -> Decimal | None:
        contribution = self.contribution
        return None if contribution is None else _pct(contribution, self.revenue)

    @property
    def per_unit(self) -> Decimal | None:
        contribution = self.contribution
        if contribution is None or self.units == 0:
            return None
        return (contribution / self.units).quantize(Decimal("0.01"))

    @property
    def avg_price(self) -> Decimal:
        return ZERO if self.units == 0 else (self.revenue / self.units).quantize(Decimal("0.01"))


@dataclass(frozen=True)
class ChannelProfit:
    """One fulfilment network, with the per-parcel numbers that compare."""

    network: str                    # "AFN" | "MFN"
    units: int
    orders: int
    revenue: Decimal
    selling_fees: Decimal
    postage: Decimal

    @property
    def amazon_take_pct(self) -> Decimal | None:
        """Share of revenue that never reaches the owner, carrier included."""
        return _pct(-(self.selling_fees + self.postage), self.revenue)

    @property
    def fulfilment_per_unit(self) -> Decimal:
        """What moving one unit cost, however it moved.

        For ``MFN`` that is the bought label. For ``AFN`` it is not separable
        from the selling fees here, so this stays at the label figure and is
        zero for ``AFN`` — :func:`fba_fulfilment_per_unit` reads it off the
        fee breakdown instead.
        """
        return ZERO if self.units == 0 else (-self.postage / self.units).quantize(Decimal("0.01"))

    @property
    def avg_order_value(self) -> Decimal:
        return ZERO if self.orders == 0 else (self.revenue / self.orders).quantize(Decimal("0.01"))


@dataclass(frozen=True)
class PeriodProfit:
    """The bottom line for a window, overhead included."""

    start: date
    end: date
    units: int
    orders: int
    revenue: Decimal
    selling_fees: Decimal
    postage: Decimal
    cogs: Decimal | None
    overhead: Decimal               # negative: subscription + inbound freight
    units_without_cost: int
    skus_missing_cost: list[str] = field(default_factory=list)

    @property
    def gross_contribution(self) -> Decimal:
        """Revenue less Amazon and the carrier, before cost of goods.

        Always knowable — it needs nothing the owner has to supply.
        """
        return self.revenue + self.selling_fees + self.postage

    @property
    def net_profit(self) -> Decimal | None:
        """The real number, or ``None`` while any sold unit is unpriced."""
        if self.cogs is None or self.units_without_cost:
            return None
        return self.gross_contribution + self.cogs + self.overhead

    @property
    def net_margin_pct(self) -> Decimal | None:
        profit = self.net_profit
        return None if profit is None else _pct(profit, self.revenue)

    @property
    def cost_coverage_pct(self) -> Decimal:
        """How much of the window's volume has a cost basis behind it."""
        if self.units == 0:
            return Decimal("100.0")
        known = self.units - self.units_without_cost
        return (Decimal(known) / Decimal(self.units) * Decimal("100")).quantize(Decimal("0.1"))


def summarise(settlement: Settlement) -> tuple[PeriodProfit, list[SkuProfit], list[ChannelProfit]]:
    """The three cuts in one pass over an already-attributed settlement."""
    return (period(settlement), by_sku(settlement.lines), by_channel(settlement.lines))


def by_sku(lines: Sequence[OrderLine]) -> list[SkuProfit]:
    """One row per SKU, richest contribution first, unpriced SKUs last."""
    groups: dict[str, list[OrderLine]] = {}
    for line in lines:
        groups.setdefault(line.sku, []).append(line)

    rows = [
        SkuProfit(
            sku=sku,
            asin=next((ln.asin for ln in group if ln.asin), ""),
            title=next((ln.title for ln in group if ln.title), ""),
            units=sum(ln.units for ln in group),
            orders=len({ln.order_id for ln in group}),
            revenue=_total(group, "revenue"),
            selling_fees=_total(group, "selling_fees"),
            postage=_total(group, "postage"),
            cogs=_cogs_total(group),
            units_without_cost=sum(ln.units for ln in group if ln.cogs is None),
        )
        for sku, group in groups.items()
    ]
    # A SKU with no cost basis sorts last rather than appearing to earn
    # nothing: its contribution is unknown, which is not the same as zero.
    return sorted(rows, key=lambda r: (r.contribution is None,
                                       -(r.contribution or ZERO), r.sku))


def by_channel(lines: Sequence[OrderLine]) -> list[ChannelProfit]:
    groups: dict[str, list[OrderLine]] = {}
    for line in lines:
        groups.setdefault(line.network or "?", []).append(line)
    return sorted(
        (ChannelProfit(
            network=network,
            units=sum(ln.units for ln in group),
            orders=len({ln.order_id for ln in group}),
            revenue=_total(group, "revenue"),
            selling_fees=_total(group, "selling_fees"),
            postage=_total(group, "postage"),
        ) for network, group in groups.items()),
        key=lambda c: c.network)


def period(settlement: Settlement) -> PeriodProfit:
    lines = settlement.lines
    days = [line.posted_on for line in lines] + [o.posted_on for o in settlement.overheads]
    return PeriodProfit(
        start=min(days) if days else date.min,
        end=max(days) if days else date.min,
        units=sum(ln.units for ln in lines),
        orders=len({ln.order_id for ln in lines}),
        revenue=_total(lines, "revenue"),
        selling_fees=_total(lines, "selling_fees"),
        postage=_total(lines, "postage"),
        cogs=_cogs_total(lines),
        overhead=sum((o.amount for o in settlement.overheads), ZERO),
        units_without_cost=sum(ln.units for ln in lines if ln.cogs is None),
        skus_missing_cost=settlement.skus_missing_cost,
    )


def overhead_by_kind(overheads: Iterable[Overhead]) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    for item in overheads:
        totals[item.kind] = totals.get(item.kind, ZERO) + item.amount
    return totals


def _total(lines: Iterable[OrderLine], field_name: str) -> Decimal:
    return sum((getattr(line, field_name) for line in lines), ZERO)


def _cogs_total(lines: Sequence[OrderLine]) -> Decimal | None:
    known = [line.cogs for line in lines if line.cogs is not None]
    return None if not known else sum(known, ZERO)
