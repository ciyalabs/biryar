"""The Finances feed and the cost basis.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-05

Two tables, both tenant-scoped and both covered by the same row-level
security policy the tenancy revision established.

``amazon_transactions`` holds what Amazon said, verbatim. Nothing derived is
stored: see the module docstring in ``biryar.db.models.finance`` for why a
materialised fee total freezes whichever classification bug was current on
the day it was written.

The ``marketplace`` enum already exists from revision 0001, so this revision
must reference it without trying to create it a second time. Alembic's
``sa.Enum`` emits a CREATE TYPE by default, which would fail the whole
migration on a database where 0001 has already run — that is, every database
this will ever be applied to.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Tables this revision adds that carry tenant data.
TENANT_TABLES: tuple[str, ...] = ("amazon_transactions", "sku_costs")

TENANT_GUC = "app.tenant_id"

#: The enum from revision 0001. create_type=False so this revision references
#: it rather than defining it again.
_marketplace = postgresql.ENUM(
    "DE", "UK", "FR", "IT", "ES", "NL", "US",
    name="marketplace",
    create_type=False,
)


def upgrade() -> None:
    op.create_table(
        "amazon_transactions",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"), nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("marketplace", _marketplace, nullable=False),
        sa.Column("transaction_id", sa.String(128), nullable=False),
        sa.Column("transaction_type", sa.String(128), nullable=False),
        sa.Column("status", sa.String(128), nullable=False),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("order_id", sa.String(128), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "fetched_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"],
            name="fk_amazon_transactions_tenant_id_tenants", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_amazon_transactions"),
        sa.UniqueConstraint(
            "tenant_id", "transaction_id",
            name="uq_amazon_transactions_tenant_id_transaction_id",
        ),
    )
    op.create_index(
        "ix_amazon_transactions_tenant_id", "amazon_transactions", ["tenant_id"],
    )
    op.create_index(
        "ix_amazon_transactions_tenant_id_local_date",
        "amazon_transactions", ["tenant_id", "local_date"],
    )
    op.create_index(
        "ix_amazon_transactions_tenant_id_order_id",
        "amazon_transactions", ["tenant_id", "order_id"],
    )

    op.create_table(
        "sku_costs",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"), nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sku", sa.String(64), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("unit_cost", sa.Numeric(16, 4), nullable=False),
        sa.Column("source", sa.String(128), nullable=False),
        sa.Column("note", sa.String(1024), nullable=False),
        sa.Column("entered_by", sa.String(128), nullable=False),
        sa.Column(
            "entered_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"],
            name="fk_sku_costs_tenant_id_tenants", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sku_costs"),
        sa.UniqueConstraint(
            "tenant_id", "sku", "effective_from",
            name="uq_sku_costs_tenant_id_sku_effective_from",
        ),
    )
    op.create_index("ix_sku_costs_tenant_id", "sku_costs", ["tenant_id"])
    op.create_index("ix_sku_costs_tenant_id_sku", "sku_costs", ["tenant_id", "sku"])

    isolation = f"NULLIF(current_setting('{TENANT_GUC}', true), '')::uuid"
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
                USING (tenant_id = {isolation})
                WITH CHECK (tenant_id = {isolation})
            """
        )


def downgrade() -> None:
    for table in TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.drop_table("sku_costs")
    op.drop_table("amazon_transactions")
    # The marketplace enum belongs to revision 0001 and is not dropped here.
