"""What a sale actually left behind.

The two figures here are deliberately separate, and the distinction is the
whole honesty of the product:

* **Gross contribution** — revenue less what Amazon and the carrier took. It
  needs nothing the seller has to supply, so it is always knowable.
* **Net profit** — gross contribution less cost of goods and period
  overhead. It is ``null`` while any sold unit has no cost on record,
  because the alternative is to treat an unpriced unit as free and report a
  profit that is too high by exactly the cost of the goods.

`skus_missing_cost` is therefore not an error list, it is the to-do list
that turns the second number on.

Nothing here is derived at write time. The ledger reads stored transactions
on every request, so a correction to how a fee is classified fixes history
instead of needing a backfill — which has mattered: seven fee paths were
once dropped and every deferred sale counted twice, and both were fixed by
changing the reading.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...profit import Settlement, build_settlement, by_sku, period
from ...repositories import load_cost_basis, settlement_rows
from ..deps import CurrentSession, TenantDb

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/profit", tags=["profit"])

Start = Annotated[date | None, Query(description="Inclusive first local date.")]
End = Annotated[date | None, Query(description="Inclusive last local date.")]


class PeriodResponse(BaseModel):
    """The bottom line for a window."""

    start: date
    end: date
    units: int
    orders: int
    revenue: Decimal
    selling_fees: Decimal = Field(description="Negative: what Amazon kept.")
    postage: Decimal = Field(description="Negative: what the carrier kept.")
    cogs: Decimal | None = Field(
        description="Negative. Null while nothing sold has a cost on record."
    )
    overhead: Decimal = Field(
        description="Negative. Subscription and inbound freight, kept as "
        "period cost rather than pushed onto whichever SKUs happened to sell."
    )
    gross_contribution: Decimal = Field(
        description="Revenue less Amazon and the carrier. Always knowable."
    )
    net_profit: Decimal | None = Field(
        description="Null while any sold unit is unpriced, rather than "
        "counting that unit as free."
    )
    units_without_cost: int
    skus_missing_cost: list[str]
    latency_ms: float


class SkuResponse(BaseModel):
    sku: str
    asin: str
    title: str
    units: int
    orders: int
    revenue: Decimal
    selling_fees: Decimal
    postage: Decimal
    cogs: Decimal | None
    contribution: Decimal | None
    units_without_cost: int


class SkusResponse(BaseModel):
    rows: list[SkuResponse]
    latency_ms: float


def _settlement(
    db: Session,
    tenant_id: uuid.UUID,
    start: date | None,
    end: date | None,
) -> Settlement:
    """Rebuild the settlement for a window from stored rows."""
    basis = load_cost_basis(db, tenant_id)
    return build_settlement(
        settlement_rows(db, tenant_id, start=start, end=end),
        cost_of=basis.cost_for,
    )


@router.get("/period", response_model=PeriodResponse)
async def period_profit(
    db: TenantDb, session: CurrentSession, start: Start = None, end: End = None
) -> PeriodResponse:
    """Profit for a date window. Both bounds inclusive, both optional."""
    began = time.monotonic()
    result = period(_settlement(db, session.tenant_id, start, end))
    latency_ms = (time.monotonic() - began) * 1000.0

    # Latency is logged on every ledger request even without an SLA: this
    # walks every stored transaction in the window, so it is the first place
    # a growing table shows up.
    logger.info(
        "profit/period latency=%.1fms units=%d unpriced=%d",
        latency_ms,
        result.units,
        result.units_without_cost,
    )
    return PeriodResponse(
        start=result.start,
        end=result.end,
        units=result.units,
        orders=result.orders,
        revenue=result.revenue,
        selling_fees=result.selling_fees,
        postage=result.postage,
        cogs=result.cogs,
        overhead=result.overhead,
        gross_contribution=result.gross_contribution,
        net_profit=result.net_profit,
        units_without_cost=result.units_without_cost,
        skus_missing_cost=result.skus_missing_cost,
        latency_ms=latency_ms,
    )


@router.get("/skus", response_model=SkusResponse)
async def profit_by_sku(
    db: TenantDb, session: CurrentSession, start: Start = None, end: End = None
) -> SkusResponse:
    """One row per SKU, richest contribution first, unpriced SKUs last."""
    began = time.monotonic()
    settlement = _settlement(db, session.tenant_id, start, end)
    rows = by_sku(settlement.lines)
    latency_ms = (time.monotonic() - began) * 1000.0

    logger.info("profit/skus latency=%.1fms rows=%d", latency_ms, len(rows))
    return SkusResponse(
        rows=[
            SkuResponse(
                sku=row.sku,
                asin=row.asin,
                title=row.title,
                units=row.units,
                orders=row.orders,
                revenue=row.revenue,
                selling_fees=row.selling_fees,
                postage=row.postage,
                cogs=row.cogs,
                contribution=row.contribution,
                units_without_cost=row.units_without_cost,
            )
            for row in rows
        ],
        latency_ms=latency_ms,
    )
