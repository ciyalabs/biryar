"""Unit costs: the one number Amazon never reports.

Without these, every profit figure stops at gross contribution — revenue
less what Amazon and the carrier took — and the number a seller actually
wants is unreachable. So this is not a convenience endpoint; it is the other
half of the product's input, and the reason release 0.1 can answer anything.

Writing a cost is **owner-only**. It writes no money, but every price floor
is derived from it: a cost that is wrong by a euro is a floor wrong by more
than a euro, and the repricer will sell below cost and book a profit while
doing it. See ``docs/methodology/fees.md``.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, Query, status
from pydantic import BaseModel, Field

from ...profit import CostEntry, CostFileError, landed_costs, read_cost_rows
from ...repositories import forget_cost, load_cost_basis, record_costs
from ..deps import CurrentSession, OwnerSession, TenantDb

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/costs", tags=["costs"])

#: A cost file is a text upload, not an archive. Twenty thousand invoice
#: lines is a wide margin over any hand-kept file, and a bound here is the
#: difference between a rejected request and a worker holding the whole body
#: in memory while it parses.
MAX_COST_FILE_BYTES = 2_000_000


class CostRow(BaseModel):
    """One unit cost in force from a date."""

    sku: str
    effective_from: date
    unit_cost: Decimal
    source: str = ""
    note: str = ""


class CostBasisResponse(BaseModel):
    rows: list[CostRow]
    skus: int


class SkippedRowResponse(BaseModel):
    line: int
    reason: str


class CostImportResponse(BaseModel):
    """What an import did, including what it could not read.

    ``skipped`` is the field that matters. Parsing continues past a bad row
    so that one typo does not cost the other two hundred lines, which means
    the only way a seller learns that thirty rows were dropped is if the
    response says so. Thirty dropped rows are thirty SKUs with no cost, and
    an unpriced SKU does not raise anything — it quietly leaves `net_profit`
    unanswerable and the floor for that SKU unknown.
    """

    recorded: int = Field(description="Cost entries written or corrected.")
    invoices: int
    skipped: list[SkippedRowResponse]
    unmatchable: list[str] = Field(
        default_factory=list,
        description=(
            "Keys stored from a line with no sku column. The Finances feed "
            "reports your seller SKU and never a supplier's EAN or article "
            "number, so a cost keyed on one of those is kept but will not "
            "be found by any sale. Add a sku column and import again."
        ),
    )


class ForgetResponse(BaseModel):
    forgotten: bool


@router.get("", response_model=CostBasisResponse)
async def list_costs(db: TenantDb, session: CurrentSession) -> CostBasisResponse:
    """Every stored cost for this tenant. Either role may read."""
    basis = load_cost_basis(db, session.tenant_id)
    rows = [
        CostRow(
            sku=entry.sku,
            effective_from=entry.effective_from,
            unit_cost=entry.unit_cost,
            source=entry.source,
            note=entry.note,
        )
        for entry in basis.entries()
    ]
    return CostBasisResponse(rows=rows, skus=len({row.sku for row in rows}))


@router.post(
    "/import",
    response_model=CostImportResponse,
    status_code=status.HTTP_200_OK,
)
async def import_costs(
    db: TenantDb,
    session: OwnerSession,
    csv_text: Annotated[
        str,
        Body(
            media_type="text/csv",
            description=(
                "The cost file, as CSV text. Required columns: vendor, "
                "doc_number, doc_date, name, qty, unit_price. Optional: "
                "sku (your Amazon SKU — without it a cost cannot be matched "
                "to a sale), ean, article_no, shipping, "
                "prices_include_vat, vat_rate."
            ),
        ),
    ],
) -> CostImportResponse:
    """Read a cost file and store the landed unit costs it implies.

    Re-importing the same file is safe: a cost for a SKU on a date replaces
    the one held for that date rather than adding a second lot. That matters
    because an import is exactly the kind of thing someone runs twice when
    they are not sure the first one worked.
    """
    if len(csv_text.encode("utf-8")) > MAX_COST_FILE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"A cost file may be at most {MAX_COST_FILE_BYTES} bytes.",
        )

    try:
        parsed = read_cost_rows(csv_text, label="uploaded cost file")
    except CostFileError as exc:
        # The header is wrong, which is a different thing from a bad row:
        # this is the wrong file, and continuing would store nothing while
        # reporting success.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    entries: list[CostEntry] = []
    unmatchable: list[str] = []
    for invoice in parsed.invoices:
        for cost in landed_costs(
            invoice.lines,
            shipping=invoice.shipping,
            prices_include_vat=invoice.prices_include_vat,
            vat_rate=invoice.vat_rate,
        ):
            if cost.keyed_by != "sku":
                # Stored anyway: the number is right and the product is
                # identified, so re-reading the invoices later would be
                # pointless work. But it cannot be found by a sale, and
                # saying nothing would make a working import look like a
                # broken ledger.
                unmatchable.append(cost.key)
            entries.append(
                CostEntry(
                    sku=cost.key,
                    effective_from=cost.doc_date,
                    unit_cost=cost.unit_cost,
                    source=f"{cost.vendor} {cost.doc_number}",
                    note=cost.detail,
                )
            )

    recorded = record_costs(db, session.tenant_id, entries, by=str(session.user_id))
    logger.info(
        "cost import: %d entries from %d invoices, %d rows skipped, "
        "%d not keyed on a sku",
        recorded,
        len(parsed.invoices),
        len(parsed.skipped),
        len(unmatchable),
    )
    return CostImportResponse(
        recorded=recorded,
        invoices=len(parsed.invoices),
        skipped=[
            SkippedRowResponse(line=row.line, reason=row.reason)
            for row in parsed.skipped
        ],
        unmatchable=unmatchable,
    )


@router.delete("/{sku}", response_model=ForgetResponse)
async def forget(
    db: TenantDb,
    session: OwnerSession,
    sku: str,
    effective_from: Annotated[date, Query(description="The dated entry to remove.")],
) -> ForgetResponse:
    """Remove one dated cost entry.

    One entry, not a SKU's history: a cost is dated, and dropping the lot
    would silently reprice every past sale of that SKU.
    """
    return ForgetResponse(
        forgotten=forget_cost(db, session.tenant_id, sku, effective_from)
    )
