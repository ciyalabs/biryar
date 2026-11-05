"""Reading and writing the Finances feed.

The ledger takes ``(type, local_date, raw_json)`` triples and knows nothing
about where they came from. :func:`settlement_rows` is what turns stored rows
back into that shape, so the ledger stays a pure function over a sequence and
can be tested with literals.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterable, Iterator
from datetime import date

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..db.models.credentials import Marketplace
from ..db.models.finance import AmazonTransaction

logger = logging.getLogger(__name__)


def record_transactions(
    session: Session,
    tenant_id: uuid.UUID,
    marketplace: Marketplace,
    transactions: Iterable[dict[str, object]],
) -> int:
    """Store transactions, replacing any already held under the same id.

    Amazon's feed is fetched by date window and windows overlap, so a retry,
    a schedule change or a backfill re-delivers transactions already stored.
    Upserting on the transaction's own identifier makes re-running a fetch
    free instead of duplicating every sale in the overlap.

    Each item must already carry the fields below; parsing the API's envelope
    belongs to the SP-API client, not here, so that this function can be
    tested without one.
    """
    rows = [
        {
            "tenant_id": tenant_id,
            "marketplace": marketplace,
            "transaction_id": txn["transaction_id"],
            "transaction_type": txn["transaction_type"],
            "status": txn.get("status", "") or "",
            "posted_at": txn["posted_at"],
            "local_date": txn["local_date"],
            "order_id": txn.get("order_id", "") or "",
            "payload": txn["payload"],
        }
        for txn in transactions
    ]
    if not rows:
        return 0

    statement = insert(AmazonTransaction).values(rows)
    session.execute(
        statement.on_conflict_do_update(
            index_elements=[
                AmazonTransaction.tenant_id, AmazonTransaction.transaction_id,
            ],
            set_={
                "transaction_type": statement.excluded.transaction_type,
                "status": statement.excluded.status,
                "posted_at": statement.excluded.posted_at,
                "local_date": statement.excluded.local_date,
                "order_id": statement.excluded.order_id,
                "payload": statement.excluded.payload,
                "fetched_at": statement.excluded.fetched_at,
            },
        )
    )
    logger.info("finances: %d transactions stored", len(rows))
    return len(rows)


def settlement_rows(
    session: Session,
    tenant_id: uuid.UUID,
    *,
    start: date | None = None,
    end: date | None = None,
) -> Iterator[tuple[str, str, str]]:
    """Stored transactions in the shape ``build_settlement`` consumes.

    ``(transaction_type, local_date, payload_json)``, oldest first. The date
    bounds are inclusive, and both are optional so that "everything" needs no
    sentinel date.

    The payload is re-serialised rather than handed over as a dict because
    the ledger parses JSON itself. That is not waste worth removing: it keeps
    the ledger's input a plain sequence of strings, which is what lets its
    tests be literals instead of fixtures.
    """
    query = select(AmazonTransaction).where(AmazonTransaction.tenant_id == tenant_id)
    if start is not None:
        query = query.where(AmazonTransaction.local_date >= start)
    if end is not None:
        query = query.where(AmazonTransaction.local_date <= end)
    query = query.order_by(AmazonTransaction.local_date, AmazonTransaction.transaction_id)

    for row in session.execute(query).scalars():
        yield (
            row.transaction_type,
            row.local_date.isoformat(),
            json.dumps(row.payload),
        )
