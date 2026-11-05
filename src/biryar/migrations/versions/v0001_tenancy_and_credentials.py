"""Tenancy, users, sessions, credentials, and the tenant boundary.

Revision ID: 0001
Revises:
Create Date: 2026-11-05

This migration does two separable things, and the second is the important one.

It creates four tables. Then it turns on row-level security for the three
that hold tenant data, which is what makes a forgotten ``WHERE`` clause
return nothing instead of returning another seller's figures.

The table list below is written out rather than derived from the models on
purpose. A migration is a frozen description of one step in history; if it
read the current models it would change meaning every time a model changed,
and replaying history would stop reproducing it. ``tests/test_tenancy.py``
asserts that the live policies still cover exactly the tables the models mark
as tenant-scoped, so drift is caught there instead.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Tables that carry tenant data as of this revision.
TENANT_TABLES: tuple[str, ...] = ("users", "user_sessions", "amazon_credentials")

TENANT_GUC = "app.tenant_id"


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"), nullable=False,
        ),
        sa.Column("slug", sa.String(63), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column(
            "status",
            sa.Enum("ACTIVE", "SUSPENDED", "CLOSED", name="tenant_status"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.CheckConstraint(
            "slug ~ '^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$'",
            name="ck_tenants_slug_is_url_safe",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tenants"),
        sa.UniqueConstraint("slug", name="uq_tenants_slug"),
    )

    op.create_table(
        "users",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"), nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=True),
        sa.Column(
            "role", sa.Enum("OWNER", "ASSISTANT", name="user_role"), nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.CheckConstraint(
            "position('@' in email) > 1", name="ck_users_email_has_a_local_part",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"],
            name="fk_users_tenant_id_tenants", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("tenant_id", "email", name="uq_users_tenant_id_email"),
    )
    op.create_index("ix_users_tenant_id", "users", ["tenant_id"])

    op.create_table(
        "user_sessions",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"), nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column(
            "issued_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_agent", sa.String(256), nullable=True),
        sa.Column("ip", sa.String(45), nullable=True),
        sa.CheckConstraint("expires_at > issued_at", name="ck_user_sessions_expires_after_issue"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"],
            name="fk_user_sessions_tenant_id_tenants", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name="fk_user_sessions_user_id_users", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_user_sessions"),
        sa.UniqueConstraint("token_hash", name="uq_user_sessions_token_hash"),
    )
    op.create_index("ix_user_sessions_tenant_id", "user_sessions", ["tenant_id"])
    op.create_index("ix_user_sessions_user_id", "user_sessions", ["user_id"])
    op.create_index("ix_user_sessions_expires_at", "user_sessions", ["expires_at"])

    op.create_table(
        "amazon_credentials",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"), nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "marketplace",
            sa.Enum("DE", "UK", "FR", "IT", "ES", "NL", "US", name="marketplace"),
            nullable=False,
        ),
        sa.Column("selling_partner_id", sa.String(128), nullable=False),
        sa.Column("refresh_token_sealed", sa.LargeBinary(), nullable=False),
        sa.Column("key_id", sa.String(64), nullable=False),
        sa.Column(
            "status",
            sa.Enum("ACTIVE", "NEEDS_REAUTH", "REVOKED", name="credential_status"),
            nullable=False,
        ),
        sa.Column("scopes", sa.String(1024), nullable=False),
        sa.Column("last_refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(512), nullable=True),
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
            name="fk_amazon_credentials_tenant_id_tenants", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_amazon_credentials"),
        sa.UniqueConstraint(
            "tenant_id", "marketplace",
            name="uq_amazon_credentials_tenant_id_marketplace",
        ),
    )
    op.create_index(
        "ix_amazon_credentials_tenant_id", "amazon_credentials", ["tenant_id"],
    )

    _enable_tenant_isolation()


def _enable_tenant_isolation() -> None:
    """Turn on row-level security for every table holding tenant data.

    ``FORCE`` matters. Without it PostgreSQL exempts a table's owner from its
    own policies, and migrations necessarily run as the owner, so a
    deployment where the application reuses that role would have isolation
    that silently does nothing.

    ``current_setting(..., true)`` returns NULL rather than raising when the
    setting is absent, and NULL never equals a uuid, so a connection with no
    tenant in scope sees no rows. Default-deny is the right failure mode:
    the alternative is a query that works in testing and returns the wrong
    seller's data in production.
    """
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
                USING (tenant_id = current_setting('{TENANT_GUC}', true)::uuid)
                WITH CHECK (tenant_id = current_setting('{TENANT_GUC}', true)::uuid)
            """
        )


def downgrade() -> None:
    for table in TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.drop_table("amazon_credentials")
    op.drop_table("user_sessions")
    op.drop_table("users")
    op.drop_table("tenants")

    for enum in ("credential_status", "marketplace", "user_role", "tenant_status"):
        op.execute(f"DROP TYPE IF EXISTS {enum}")
