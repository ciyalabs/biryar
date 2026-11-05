"""Where the next euro should go: depth, breadth, or neither.

## The question this answers

Monthly profit is small, and the reason is not that the products are bad —
it is that there are nine of them. The owner's decision each month is which
of two things to buy:

**Depth** — more units of a SKU already listed.
**Breadth** — a new SKU nobody has sold yet.

They look similar on an invoice and behave nothing alike.

## Why depth stops paying, and breadth does not

A SKU sells at a rate the market sets. Stock does not raise that rate; it only
decides whether the rate can be met. So the return on a unit of depth is a
step function: positive while the SKU would otherwise run out, and **zero**
afterwards. Buying the tenth month of cover for a product that sells 0.3 a day
converts cash into a shelf.

Breadth has no such ceiling, because each new SKU brings its own demand. Its
return is the contribution that SKU earns, and the overhead it has to clear is
already paid — the €46.41 selling plan is charged once, not once per product.
This is why a catalogue of nine good products earns less than a catalogue of
thirty mediocre ones.

## What the model will not do

It will not forecast a *specific* new product. Nothing here knows what has not
been bought yet. What it does instead is project the portfolio the owner
already has: take the SKUs that have actually sold, measure their spread, and
say what adding N more products *like these* would earn. The quartiles are
reported with the median because nine observations do not support a point
estimate, and a single projected number would be read as a promise.

## The allocation metric

Not margin. **Contribution per euro of working capital per month.** A product
earning €4 a unit on €20 of stock that turns twice a year is worse than one
earning €1.50 on €5 that turns monthly, and ranking by margin hides that
exactly when capital is the binding constraint — which it is, at the moment,
for every decision the owner makes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from statistics import median
from typing import Iterable, Sequence

from .ledger import OrderLine
from .sources import ResolvedCost

ZERO = Decimal("0")
DAYS_PER_MONTH = Decimal("30.4")

#: Cover a reorder is sized to. Below this a SKU risks running out before new
#: stock lands; above it, depth is parking money. Eight weeks is the owner's
#: observed supplier cycle.
TARGET_COVER_DAYS = Decimal("56")


@dataclass(frozen=True)
class SkuPosition:
    """One product: what it earns, what it ties up, and how fast it moves."""

    sku: str
    asin: str
    title: str
    channel: str
    #: Units sold in the measured window.
    units_sold: int
    #: Days this SKU has been selling — first sale to the end of the window.
    days_live: int
    revenue: Decimal
    #: Amazon's own cut — referral, FBA pick & pack, closing fee. Positive.
    selling_fees: Decimal
    #: The parcel: a label bought through Buy Shipping. Positive, and zero on
    #: an FBA line because Amazon's fulfilment fee already covers the move.
    postage: Decimal
    #: Box, tape, filler and label pouch for a merchant-fulfilled unit. Real
    #: money, on no invoice and in no Amazon report, so it comes from a rate
    #: in config rather than from a measurement. Zero on an FBA line.
    packaging_rate: Decimal
    #: Landed cost of one unit, from whichever source could answer.
    unit_cost: Decimal | None
    #: Where that cost came from in words, for the detail panel.
    cost_detail: str
    #: Which one did — "owner", "invoice", "price_bounds", "catalogue", or "".
    #: A margin built on a catalogue list price is a weaker claim than one
    #: built on the owner's own figure, and the page has to be able to say so.
    cost_source: str
    #: Units Amazon can ship right now.
    stock: int
    current_price: Decimal | None
    min_price: Decimal | None
    max_price: Decimal | None
    break_even: Decimal | None

    @property
    def packaging(self) -> Decimal:
        """What boxing the units that sold cost, in total."""
        if self.postage == ZERO:
            return ZERO                      # Amazon packed it
        return (self.packaging_rate * self.units_sold).quantize(Decimal("0.01"))

    @property
    def amazon_cost(self) -> Decimal:
        """Everything Amazon, the carrier and the packing bench took."""
        return self.selling_fees + self.postage + self.packaging

    @property
    def price(self) -> Decimal:
        """What a buyer paid for one unit, shipping they were charged included."""
        if self.units_sold == 0:
            return self.current_price or ZERO
        return (self.revenue / self.units_sold).quantize(Decimal("0.01"))

    @property
    def fees_per_unit(self) -> Decimal:
        if self.units_sold == 0:
            return ZERO
        return (self.selling_fees / self.units_sold).quantize(Decimal("0.01"))

    @property
    def postage_per_unit(self) -> Decimal:
        if self.units_sold == 0:
            return ZERO
        return (self.postage / self.units_sold).quantize(Decimal("0.01"))

    @property
    def packaging_per_unit(self) -> Decimal:
        return ZERO if self.postage == ZERO else self.packaging_rate

    @property
    def velocity(self) -> Decimal:
        """Units a day, measured. Zero days live reads as zero, not infinity."""
        if self.days_live <= 0:
            return ZERO
        return (Decimal(self.units_sold) / Decimal(self.days_live)).quantize(Decimal("0.001"))

    @property
    def contribution(self) -> Decimal | None:
        """What the measured sales left behind, after everything."""
        if self.unit_cost is None:
            return None
        return self.revenue - self.amazon_cost - self.unit_cost * self.units_sold

    @property
    def per_unit(self) -> Decimal | None:
        contribution = self.contribution
        if contribution is None or self.units_sold == 0:
            return None
        return (contribution / self.units_sold).quantize(Decimal("0.01"))

    @property
    def monthly_contribution(self) -> Decimal | None:
        """What this SKU earns in a month at its measured rate."""
        per_unit = self.per_unit
        if per_unit is None:
            return None
        return (per_unit * self.velocity * DAYS_PER_MONTH).quantize(Decimal("0.01"))

    @property
    def capital_tied(self) -> Decimal | None:
        if self.unit_cost is None:
            return None
        return (self.unit_cost * self.stock).quantize(Decimal("0.01"))

    @property
    def days_of_cover(self) -> Decimal | None:
        """How long the shelf lasts at the measured rate."""
        velocity = self.velocity
        if velocity == ZERO:
            return None
        return (Decimal(self.stock) / velocity).quantize(Decimal("0.1"))

    @property
    def return_on_capital(self) -> Decimal | None:
        """Monthly contribution per euro tied up, as a percentage.

        The allocation metric. Undefined when nothing is on the shelf, which
        is itself the answer: a SKU with stock zero is earning nothing because
        it cannot be bought.
        """
        monthly, capital = self.monthly_contribution, self.capital_tied
        if monthly is None or not capital:
            return None
        return (monthly / capital * Decimal("100")).quantize(Decimal("0.1"))

    @property
    def restock_units(self) -> int:
        """Units to buy to reach the target cover. Zero when already deep enough.

        This is the whole of depth's value: past this point another unit earns
        nothing until the one in front of it sells.
        """
        velocity = self.velocity
        if velocity == ZERO:
            return 0
        want = int((velocity * TARGET_COVER_DAYS).to_integral_value(rounding="ROUND_CEILING"))
        return max(0, want - self.stock)

    @property
    def verdict(self) -> str:
        """One word on what to do with this product."""
        if self.unit_cost is None:
            return "price it"
        per_unit = self.per_unit
        if self.units_sold and per_unit is not None and per_unit <= ZERO:
            return "losing"
        if self.units_sold == 0:
            return "unproven"
        cover = self.days_of_cover
        if cover is not None and cover < Decimal("14"):
            return "restock"
        if cover is not None and cover > Decimal("120"):
            return "overstocked"
        return "healthy"


@dataclass(frozen=True)
class Spread:
    """A measured quantity across the portfolio, with its spread.

    Nine products is too few for a point estimate, so every projected input
    carries the quartiles it came from.
    """

    low: Decimal          # 25th percentile
    mid: Decimal          # median
    high: Decimal         # 75th percentile
    n: int

    @classmethod
    def of(cls, values: Sequence[Decimal]) -> "Spread":
        if not values:
            return cls(ZERO, ZERO, ZERO, 0)
        ordered = sorted(values)
        return cls(low=_quantile(ordered, 0.25), mid=Decimal(str(median(ordered))),
                   high=_quantile(ordered, 0.75), n=len(ordered))


@dataclass(frozen=True)
class Projection:
    """What the portfolio earns at a given size, and what it costs to get there."""

    skus: int
    #: Monthly contribution before overhead, at the median SKU.
    monthly_contribution: Decimal
    #: After the fixed monthly costs.
    monthly_profit: Decimal
    #: Working capital the catalogue needs at target cover.
    capital_required: Decimal
    #: The same at the pessimistic and optimistic quartiles.
    monthly_profit_low: Decimal
    monthly_profit_high: Decimal


@dataclass(frozen=True)
class GrowthModel:
    """The measured portfolio, and what more of it would earn."""

    #: Contribution per unit across SKUs that have sold and are priced.
    unit_contribution: Spread
    #: Units a day across the same SKUs.
    velocity: Spread
    #: Landed cost per unit — what a new SKU ties up per unit of stock.
    unit_cost: Spread
    #: Monthly fixed cost the catalogue has to clear.
    monthly_overhead: Decimal
    #: SKUs that have actually sold — the ones the spread is measured over.
    proven_skus: int
    #: Listed but never sold. They tie up capital and prove nothing yet.
    unproven_skus: int

    @property
    def monthly_per_sku(self) -> Decimal:
        """What one more product like the median one earns each month."""
        return (self.unit_contribution.mid * self.velocity.mid
                * DAYS_PER_MONTH).quantize(Decimal("0.01"))

    @property
    def capital_per_sku(self) -> Decimal:
        """What one more product ties up, stocked to target cover."""
        units = (self.velocity.mid * TARGET_COVER_DAYS).to_integral_value(rounding="ROUND_CEILING")
        return (self.unit_cost.mid * units).quantize(Decimal("0.01"))

    @property
    def break_even_skus(self) -> int | None:
        """How many median products it takes to pay the monthly overhead."""
        per_sku = self.monthly_per_sku
        if per_sku <= ZERO:
            return None
        return int((self.monthly_overhead / per_sku).to_integral_value(rounding="ROUND_CEILING"))

    def project(self, skus: int) -> Projection:
        """The whole catalogue at ``skus`` products, all like the measured ones."""
        count = Decimal(max(skus, 0))
        monthly = (self.unit_contribution.mid * self.velocity.mid
                   * DAYS_PER_MONTH * count).quantize(Decimal("0.01"))
        low = (self.unit_contribution.low * self.velocity.low
               * DAYS_PER_MONTH * count).quantize(Decimal("0.01"))
        high = (self.unit_contribution.high * self.velocity.high
                * DAYS_PER_MONTH * count).quantize(Decimal("0.01"))
        return Projection(
            skus=int(count),
            monthly_contribution=monthly,
            monthly_profit=monthly - self.monthly_overhead,
            capital_required=(self.capital_per_sku * count).quantize(Decimal("0.01")),
            monthly_profit_low=low - self.monthly_overhead,
            monthly_profit_high=high - self.monthly_overhead,
        )

    def skus_for(self, monthly_profit: Decimal) -> int | None:
        """How many products it takes to earn ``monthly_profit`` a month."""
        per_sku = self.monthly_per_sku
        if per_sku <= ZERO:
            return None
        needed = (monthly_profit + self.monthly_overhead) / per_sku
        return int(needed.to_integral_value(rounding="ROUND_CEILING"))


def build_positions(
    lines: Iterable[OrderLine],
    *,
    bounds: dict[str, dict[str, object]],
    stock: dict[str, dict[str, object]],
    costs: dict[str, "ResolvedCost"],
    until: date,
    packaging_rate: Decimal = ZERO,
) -> list[SkuPosition]:
    """Join sold lines, price bounds and inventory into one row per SKU.

    ``bounds`` and ``stock`` are keyed by SKU — the ``price_bounds`` and
    ``listing_health`` rows; ``costs`` comes from
    :func:`~cockpit.src.profit.sources.resolve_costs`. A SKU present in any of
    them but never sold still gets a position, with zero units: a listing that
    has never sold is a fact worth seeing, not a row to omit.
    """
    grouped: dict[str, list[OrderLine]] = {}
    for line in lines:
        grouped.setdefault(line.sku, []).append(line)

    positions: list[SkuPosition] = []
    for sku in sorted(set(grouped) | set(bounds) | set(stock)):
        resolved = costs.get(sku)
        group = grouped.get(sku, [])
        bound = bounds.get(sku, {})
        held = stock.get(sku, {})
        first_sale = min((line.posted_on for line in group), default=None)
        positions.append(SkuPosition(
            sku=sku,
            asin=str(next((ln.asin for ln in group if ln.asin), bound.get("asin") or
                          held.get("asin") or "")),
            title=str(next((ln.title for ln in group if ln.title), "")),
            channel=str(held.get("channel") or (group[0].network if group else "")),
            units_sold=sum(ln.units for ln in group),
            days_live=((until - first_sale).days + 1) if first_sale else 0,
            revenue=sum((ln.revenue for ln in group), ZERO),
            selling_fees=-sum((ln.selling_fees for ln in group), ZERO),
            postage=-sum((ln.postage for ln in group), ZERO),
            packaging_rate=packaging_rate,
            unit_cost=resolved.unit_cost if resolved else None,
            cost_detail=resolved.detail if resolved else "",
            cost_source=resolved.source if resolved else "",
            stock=int(held.get("fulfillable") or 0),
            current_price=_dec(bound.get("current_price")),
            min_price=_dec(bound.get("min_price")),
            max_price=_dec(bound.get("max_price")),
            break_even=_dec(bound.get("break_even")),
        ))
    return positions


def model(positions: Sequence[SkuPosition], *, monthly_overhead: Decimal) -> GrowthModel:
    """Measure the portfolio's spread over the SKUs that have actually sold."""
    proven = [p for p in positions
              if p.units_sold > 0 and p.per_unit is not None and p.velocity > ZERO]
    return GrowthModel(
        unit_contribution=Spread.of([p.per_unit for p in proven if p.per_unit is not None]),
        velocity=Spread.of([p.velocity for p in proven]),
        unit_cost=Spread.of([p.unit_cost for p in positions if p.unit_cost is not None]),
        monthly_overhead=monthly_overhead,
        proven_skus=len(proven),
        unproven_skus=sum(1 for p in positions if p.units_sold == 0),
    )


def _dec(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _quantile(ordered: Sequence[Decimal], q: float) -> Decimal:
    if len(ordered) == 1:
        return ordered[0]
    position = q * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = Decimal(str(position - low))
    return ordered[low] + (ordered[high] - ordered[low]) * weight
