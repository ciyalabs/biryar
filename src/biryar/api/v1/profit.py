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

from fastapi import APIRouter, Body, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...profit import (
    Settlement,
    SettlementFileError,
    build_settlement,
    by_sku,
    period,
    read_settlement_file,
)
from ...repositories import load_cost_basis, settlement_rows
from ..deps import CurrentSession, TenantDb

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/profit", tags=["profit"])

#: A settlement report is text. Fifteen megabytes is far more than a
#: fortnight of a busy seller, and a bound here is the difference
#: between a refused request and a worker holding an arbitrary upload
#: in memory while it parses.
MAX_SETTLEMENT_FILE_BYTES = 15_000_000

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


# -- the settlement file, which needs no Amazon application at all -------------------

class UnmappedAmount(BaseModel):
    """A fee this does not recognise, and what it came to."""

    name: str
    total: Decimal


class SettlementFileReport(BaseModel):
    """What one downloaded settlement report says.

    Deliberately stateless: nothing is stored. A settlement file and the
    Finances feed describe the same money in different shapes, and merging
    them would risk counting a period twice — the exact failure the ledger
    was once wrong about. So this answers a question and keeps nothing.

    That also makes it the thing to try first: it needs no Amazon
    application, no developer registration and no connected account.
    """

    settlement_id: str
    period_start: date | None
    period_end: date | None
    deposit_date: date | None
    currency: str

    stated_total: Decimal = Field(
        description="The payout Amazon states in the file's summary row."
    )
    counted_total: Decimal = Field(
        description="Every amount in the file, added up."
    )
    reconciles: bool = Field(
        description=(
            "Whether the two agree. False means the file was read wrong and "
            "the figures below should not be trusted — not that a fee is "
            "unrecognised, since unrecognised amounts are still counted."
        )
    )
    difference: Decimal

    rows_read: int
    units: int
    orders: int
    revenue: Decimal
    selling_fees: Decimal
    cogs: Decimal | None
    overhead: Decimal
    gross_contribution: Decimal
    net_profit: Decimal | None
    units_without_cost: int
    skus_missing_cost: list[str]
    unmapped: list[UnmappedAmount] = Field(
        description=(
            "Amazon publishes this report's column names but not the "
            "values of amount-type and amount-description, so some fees "
            "will be unfamiliar. They are listed rather than dropped."
        )
    )
    latency_ms: float


@router.post("/settlement-file", response_model=SettlementFileReport)
async def read_settlement_report(
    db: TenantDb,
    session: CurrentSession,
    report: Annotated[
        str,
        Body(
            media_type="text/tab-separated-values",
            description=(
                "A settlement report downloaded from Seller Central: "
                "Reports → Payments → All Statements → Flat File V2."
            ),
        ),
    ],
) -> SettlementFileReport:
    """Figures from a downloaded settlement report. Nothing is stored."""
    if len(report.encode("utf-8")) > MAX_SETTLEMENT_FILE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"A settlement report may be at most "
                f"{MAX_SETTLEMENT_FILE_BYTES // 1_000_000} MB."
            ),
        )

    began = time.monotonic()
    basis = load_cost_basis(db, session.tenant_id)
    try:
        parsed = read_settlement_file(report, cost_of=basis.cost_for)
    except SettlementFileError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    totals = period(parsed.settlement)
    latency_ms = (time.monotonic() - began) * 1000.0
    logger.info(
        "settlement file: %d rows, %d lines, reconciles=%s, %d unmapped",
        parsed.rows_read,
        len(parsed.settlement.lines),
        parsed.reconciles,
        len(parsed.settlement.unmapped),
    )

    return SettlementFileReport(
        settlement_id=parsed.settlement_id,
        period_start=parsed.period_start,
        period_end=parsed.period_end,
        deposit_date=parsed.deposit_date,
        currency=parsed.currency,
        stated_total=parsed.stated_total,
        counted_total=parsed.counted_total,
        reconciles=parsed.reconciles,
        difference=parsed.difference,
        rows_read=parsed.rows_read,
        units=totals.units,
        orders=totals.orders,
        revenue=totals.revenue,
        selling_fees=totals.selling_fees,
        cogs=totals.cogs,
        overhead=totals.overhead,
        gross_contribution=totals.gross_contribution,
        net_profit=totals.net_profit,
        units_without_cost=totals.units_without_cost,
        skus_missing_cost=totals.skus_missing_cost,
        unmapped=[
            UnmappedAmount(name=name, total=total)
            for name, total in sorted(parsed.settlement.unmapped.items())
        ],
        latency_ms=latency_ms,
    )
