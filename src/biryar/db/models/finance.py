"""What Amazon charged, and what the goods cost.

## Raw in, derived on read

``amazon_transactions`` holds the Finances feed verbatim. Nothing in it is a
figure Biryar worked out; the fee totals, the margins and the break-even
prices are all computed from it at read time by :mod:`biryar.profit.ledger`.

That is a deliberate trade of CPU for correctness. The classification of a
fee path is the part most likely to be wrong — an earlier version of this
ledger silently dropped seven of them, and a deferred-settlement account
posts every sale twice, which double-counted units for weeks before anyone
noticed. Both were fixed by changing the reading, not the data. Had the
derived numbers been materialised at ingest, yesterday's bug would be frozen
into the table and every historical total would stay wrong until someone
wrote a backfill.

So the rule is: store what Amazon said, derive everything else, and let a fix
correct the past for free.

## Idempotent ingest

Amazon's feed is fetched by date window, and windows overlap: a retry, a
schedule change or a backfill will re-deliver transactions already held. Each
one carries its own identifier, so that identifier is the key, and ingest
upserts. Re-running a fetch is therefore free rather than corrupting.

The identifier is unique per selling account, not globally, so the key is the
tenant and the identifier together.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from sqlalchemy import Date, Enum, Index, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import (
    Base,
    LongText,
    Money,
    ShortText,
    Sku,
    TenantScoped,
    Timestamp,
    TimestampMixin,
    tenant_fk,
)
from .credentials import Marketplace


class AmazonTransaction(Base, TenantScoped):
    """One transaction from the Finances feed, as Amazon sent it."""

    __tablename__ = "amazon_transactions"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[uuid.UUID] = tenant_fk()

    marketplace: Mapped[Marketplace] = mapped_column(
        Enum(Marketplace, name="marketplace", native_enum=True, create_type=False),
        nullable=False,
    )

    #: Amazon's own identifier. Unique within a selling account, which is why
    #: the uniqueness constraint pairs it with the tenant.
    transaction_id: Mapped[ShortText] = mapped_column(nullable=False)

    #: "Shipment", "Refund", "ServiceFee", and the rest. Deliberately free
    #: text rather than an enum: a type Biryar has never seen must land in the
    #: table and be reported, not rejected at the door. A transaction refused
    #: at ingest is money that silently never reaches any total.
    transaction_type: Mapped[ShortText] = mapped_column(nullable=False)
    status: Mapped[ShortText] = mapped_column(nullable=False, default="")

    posted_at: Mapped[Timestamp] = mapped_column(nullable=False)

    #: The marketplace's own calendar day, which is the day the money counts
    #: on. Not derivable from ``posted_at`` without knowing the marketplace's
    #: timezone, and a sale posted either side of local midnight otherwise
    #: lands in the wrong day's totals.
    local_date: Mapped[date] = mapped_column(Date, nullable=False)

    #: Empty for a transaction that belongs to no order, such as the monthly
    #: subscription or inbound transport.
    order_id: Mapped[ShortText] = mapped_column(nullable=False, default="")

    #: The API's JSON, verbatim. JSONB rather than text so it can be queried
    #: and indexed when a question about an unclassified fee path comes up,
    #: which it will.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    fetched_at: Mapped[Timestamp] = mapped_column(
        server_default=func.now(), nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "transaction_id",
            name="uq_amazon_transactions_tenant_id_transaction_id",
        ),
        # The ledger reads a date range for one tenant, every time.
        Index("ix_amazon_transactions_tenant_id_local_date", "tenant_id", "local_date"),
        Index("ix_amazon_transactions_tenant_id_order_id", "tenant_id", "order_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<AmazonTransaction {self.transaction_type} {self.transaction_id} "
            f"{self.local_date}>"
        )


class SkuCost(Base, TenantScoped, TimestampMixin):
    """What one sellable unit of a SKU cost, from a given day onward.

    Amazon can never supply this, and it is the number every margin depends
    on. See :mod:`biryar.profit.costs` for why cost is dated rather than
    edited in place: a sale in October has to keep costing what October's
    stock cost, or last month's margins change every time new stock arrives.
    """

    __tablename__ = "sku_costs"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[uuid.UUID] = tenant_fk()

    sku: Mapped[Sku] = mapped_column(nullable=False)

    #: The first day this cost applies. A restock at a new price is a second
    #: row, never an edit.
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)

    #: Landed, per unit, positive. Whether it includes VAT depends on the
    #: seller's position; :mod:`biryar.profit.invoices` records that per
    #: invoice rather than assuming it.
    unit_cost: Mapped[Money] = mapped_column(nullable=False)

    #: What vouched for the number: "invoice", "manual", "estimate". A margin
    #: built on a supplier's list price is a weaker claim than one built on a
    #: document, and the UI has to be able to say which it is.
    source: Mapped[ShortText] = mapped_column(nullable=False, default="")
    note: Mapped[LongText] = mapped_column(nullable=False, default="")

    entered_by: Mapped[ShortText] = mapped_column(nullable=False)
    entered_at: Mapped[Timestamp] = mapped_column(
        server_default=func.now(), nullable=False,
    )

    __table_args__ = (
        # One cost per SKU per day. Re-importing an invoice replaces that
        # day's entry rather than stacking a second one nobody can choose
        # between.
        UniqueConstraint(
            "tenant_id", "sku", "effective_from",
            name="uq_sku_costs_tenant_id_sku_effective_from",
        ),
        Index("ix_sku_costs_tenant_id_sku", "tenant_id", "sku"),
    )

    def __repr__(self) -> str:
        return f"<SkuCost {self.sku} {self.unit_cost} from {self.effective_from}>"

