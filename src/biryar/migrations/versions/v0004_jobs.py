"""Background work, and the two functions that run it fairly.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-05

The table is ordinary. The functions are where the thinking is.

## Claiming, and why it is a function

A worker has no tenant. It is infrastructure, not a tenant acting, and it
has to look across every tenant to decide what to run next. ``jobs`` is
tenant-scoped like everything else, so default-deny means the worker sees
nothing — the same wall authentication hits, and solved the same way: one
``SECURITY DEFINER`` function, as narrow as the job allows.

It takes no tenant and offers no listing. It returns exactly one claimed
job, already marked running, and it is the only way to get one. A worker
cannot browse the queue, only be handed the next thing.

## Fairness, which is the actual problem

``ORDER BY run_after LIMIT 1`` is wrong, and wrong in a way that only shows
up in production. One tenant starting a year's backfill fills every worker
with their work; everyone else's jobs sit behind it; and nothing anywhere
reports a fault, because the queue is draining perfectly — just all for one
seller.

So the claim skips tenants that already hold ``p_max_running`` jobs. A
backfill becomes a steady trickle rather than a flood, and a tenant with one
urgent job waits behind at most a handful of someone else's rather than all
of them.

## Reclaiming, because workers die

A worker killed mid-job leaves its row ``RUNNING`` forever. Nothing retries
it, because it does not look failed — it looks busy. The second function
returns jobs whose lock has gone stale, which is the difference between a
restart costing nothing and a restart silently dropping work.

``attempts`` was already incremented when the job was claimed, so a job that
repeatedly kills its worker exhausts its attempts rather than looping
forever. That is deliberate: a job that takes a process down with it is not
one to keep trying.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_TABLES: tuple[str, ...] = ("jobs",)

TENANT_GUC = "app.tenant_id"

CLAIM = "biryar_claim_job"
RECLAIM = "biryar_reclaim_stuck_jobs"


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"), nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(128), nullable=False),
        sa.Column(
            "payload", postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"), nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum("PENDING", "RUNNING", "DONE", "FAILED", name="job_status"),
            nullable=False,
        ),
        sa.Column(
            "run_after", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.String(128), nullable=True),
        sa.Column("last_error", sa.String(1024), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idempotency_key", sa.String(200), nullable=True),
        sa.CheckConstraint("attempts >= 0", name="attempts_not_negative"),
        sa.CheckConstraint("max_attempts >= 1", name="at_least_one_attempt"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"],
            name="fk_jobs_tenant_id_tenants", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_jobs"),
    )
    op.create_index("ix_jobs_tenant_id", "jobs", ["tenant_id"])
    op.create_index("ix_jobs_status_run_after", "jobs", ["status", "run_after"])
    op.create_index("ix_jobs_tenant_id_status", "jobs", ["tenant_id", "status"])
    op.create_index(
        "uq_jobs_tenant_id_idempotency_key",
        "jobs", ["tenant_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("status IN ('PENDING', 'RUNNING')"),
    )

    isolation = f"NULLIF(current_setting('{TENANT_GUC}', true), '')::uuid"
    op.execute("ALTER TABLE jobs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE jobs FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON jobs
            USING (tenant_id = {isolation})
            WITH CHECK (tenant_id = {isolation})
        """
    )

    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION public.{CLAIM}(
            p_worker       text,
            p_max_running  integer DEFAULT 2,
            p_kinds        text[]  DEFAULT NULL
        )
        RETURNS TABLE (
            job_id        uuid,
            tenant_id     uuid,
            kind          text,
            payload       jsonb,
            attempts      integer,
            max_attempts  integer
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = ''
        AS $$
        DECLARE
            v_id uuid;
        BEGIN
            SELECT j.id INTO v_id
            FROM public.jobs AS j
            WHERE j.status = 'PENDING'
              AND j.run_after <= now()
              AND (p_kinds IS NULL OR j.kind = ANY(p_kinds))
              AND (
                  SELECT count(*)
                  FROM public.jobs AS running
                  WHERE running.tenant_id = j.tenant_id
                    AND running.status = 'RUNNING'
              ) < p_max_running
            ORDER BY j.run_after, j.created_at
            FOR UPDATE SKIP LOCKED
            LIMIT 1;

            IF v_id IS NULL THEN
                RETURN;
            END IF;

            RETURN QUERY
            UPDATE public.jobs AS j
            SET status     = 'RUNNING',
                locked_at  = now(),
                locked_by  = p_worker,
                attempts   = j.attempts + 1
            WHERE j.id = v_id
            RETURNING j.id, j.tenant_id, j.kind::text, j.payload,
                      j.attempts, j.max_attempts;
        END
        $$
        """
    )

    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION public.{RECLAIM}(p_stale_after interval)
        RETURNS integer
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = ''
        AS $$
        DECLARE
            v_count integer;
        BEGIN
            WITH stuck AS (
                UPDATE public.jobs
                SET status    = 'PENDING',
                    locked_at = NULL,
                    locked_by = NULL,
                    last_error = 'the worker holding this job stopped reporting'
                WHERE status = 'RUNNING'
                  AND locked_at < now() - p_stale_after
                RETURNING 1
            )
            SELECT count(*) INTO v_count FROM stuck;
            RETURN v_count;
        END
        $$
        """
    )

    # Neither is callable by anything that has not been granted it. A
    # SECURITY DEFINER function is PUBLIC-executable by default, and these
    # two reach every tenant's queue.
    op.execute(f"REVOKE ALL ON FUNCTION public.{CLAIM}(text, integer, text[]) FROM PUBLIC")
    op.execute(f"REVOKE ALL ON FUNCTION public.{RECLAIM}(interval) FROM PUBLIC")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS public.{RECLAIM}(interval)")
    op.execute(f"DROP FUNCTION IF EXISTS public.{CLAIM}(text, integer, text[])")

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON jobs")
    op.execute("ALTER TABLE jobs NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE jobs DISABLE ROW LEVEL SECURITY")

    op.drop_table("jobs")
    op.execute("DROP TYPE IF EXISTS job_status")
