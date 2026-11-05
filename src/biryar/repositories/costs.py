"""Reading and writing the cost basis."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from datetime import date
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..db.models.finance import SkuCost
from ..profit.costs import CostBasis, CostEntry

logger = logging.getLogger(__name__)


def load_cost_basis(session: Session, tenant_id: uuid.UUID) -> CostBasis:
    """Every stored cost for one tenant, as the lookup the ledger wants.

    Loaded in one query rather than per SKU. A settlement pass asks for the
    same dozen SKUs hundreds of times, and the whole cost table for one
    seller is small enough that a round trip per lookup would be the slowest
    part of the calculation by a wide margin.
    """
    rows = session.execute(
        select(SkuCost)
        .where(SkuCost.tenant_id == tenant_id)
        .order_by(SkuCost.sku, SkuCost.effective_from)
    ).scalars()

    return CostBasis(
        CostEntry(
            sku=row.sku,
            effective_from=row.effective_from,
            unit_cost=row.unit_cost,
            source=row.source,
            note=row.note,
        )
        for row in rows
    )


def record_costs(
    session: Session,
    tenant_id: uuid.UUID,
    entries: Iterable[CostEntry],
    *,
    by: str,
) -> int:
    """Store costs, replacing any entry for the same SKU and date.

    Re-entering a cost for a date already held is a correction, not a second
    lot: there is only ever one cost in force on a given day. Re-importing
    the same invoice is therefore safe, which matters because invoice import
    is the kind of thing people run twice when they are not sure it worked.
    """
    rows = [
        {
            "tenant_id": tenant_id,
            "sku": entry.sku,
            "effective_from": entry.effective_from,
            "unit_cost": entry.unit_cost,
            "source": entry.source,
            "note": entry.note,
            "entered_by": by,
        }
        for entry in entries
    ]
    if not rows:
        return 0

    statement = insert(SkuCost).values(rows)
    session.execute(
        statement.on_conflict_do_update(
            index_elements=[SkuCost.tenant_id, SkuCost.sku, SkuCost.effective_from],
            set_={
                "unit_cost": statement.excluded.unit_cost,
                "source": statement.excluded.source,
                "note": statement.excluded.note,
                "entered_by": statement.excluded.entered_by,
            },
        )
    )
    logger.info("cost basis: %d entries recorded by %s", len(rows), by)
    return len(rows)


def forget_cost(
    session: Session, tenant_id: uuid.UUID, sku: str, effective_from: date,
) -> bool:
    """Drop one entry. Returns whether a row went."""
    result = session.execute(
        delete(SkuCost).where(
            SkuCost.tenant_id == tenant_id,
            SkuCost.sku == sku,
            SkuCost.effective_from == effective_from,
        )
    )
    # CursorResult carries rowcount; the base Result protocol does not, and
    # execute() is typed as the latter.
    return bool(cast("CursorResult[Any]", result).rowcount)
