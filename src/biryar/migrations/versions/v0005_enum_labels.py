"""One spelling per state.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-05

SQLAlchemy stores a Python enum by its *name* unless told otherwise. These
enums are ``StrEnum`` with lowercase values, so every status existed in two
spellings at once: ``JobStatus.PENDING.value`` was ``"pending"`` while the
column held ``"PENDING"``.

Nothing was broken by it — the application compared enum members to enum
members and never noticed. The damage is reserved for the first person to
write SQL using the vocabulary the API gave them:

    SELECT * FROM jobs WHERE status = 'pending';   -- zero rows, no error

That is the failure this codebase keeps running into: a query that answers
confidently and wrongly. So the labels become the values, the models are
told to store values, and there is one spelling of each state everywhere —
in the database, in `.value`, and in JSON.

``marketplace`` is deliberately left alone. Its names (DE, UK) and its
values (Amazon's marketplace ids) are two different *things* rather than two
spellings of one, so there is nothing to reconcile, and the short name is
the readable choice in a column.

Renaming a label rewrites no rows: the enum's labels are stored once in the
catalogue, and every column referencing them follows.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_TABLES: tuple[str, ...] = ()

#: type name -> the labels it has, which are being lowercased.
RENAMED: dict[str, tuple[str, ...]] = {
    "job_status": ("PENDING", "RUNNING", "DONE", "FAILED"),
    "tenant_status": ("ACTIVE", "SUSPENDED", "CLOSED"),
    "user_role": ("OWNER", "ASSISTANT"),
    "credential_status": ("ACTIVE", "NEEDS_REAUTH", "REVOKED"),
}


def upgrade() -> None:
    for type_name, labels in RENAMED.items():
        for label in labels:
            op.execute(
                f"ALTER TYPE {type_name} RENAME VALUE '{label}' TO '{label.lower()}'"
            )

    # The partial index on jobs names two labels in its predicate, and a
    # predicate is stored as parsed text — renaming the labels does not
    # rewrite it, so it would silently stop matching anything.
    op.drop_index("uq_jobs_tenant_id_idempotency_key", table_name="jobs")
    op.create_index(
        "uq_jobs_tenant_id_idempotency_key",
        "jobs", ["tenant_id", "idempotency_key"],
        unique=True,
        postgresql_where="status IN ('pending', 'running')",
    )

    # The claim and reclaim functions compare against the old labels too.
    # A function body is stored as text and is not rewritten by a rename,
    # so both have to be replaced.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.biryar_claim_job(
            p_worker       text,
            p_max_running  integer DEFAULT 2,
            p_kinds        text[]  DEFAULT NULL
        )
        RETURNS TABLE (
            job_id uuid, tenant_id uuid, kind text, payload jsonb,
            attempts integer, max_attempts integer
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
            WHERE j.status = 'pending'
              AND j.run_after <= now()
              AND (p_kinds IS NULL OR j.kind = ANY(p_kinds))
              AND (
                  SELECT count(*)
                  FROM public.jobs AS running
                  WHERE running.tenant_id = j.tenant_id
                    AND running.status = 'running'
              ) < p_max_running
            ORDER BY j.run_after, j.created_at
            FOR UPDATE SKIP LOCKED
            LIMIT 1;

            IF v_id IS NULL THEN
                RETURN;
            END IF;

            RETURN QUERY
            UPDATE public.jobs AS j
            SET status    = 'running',
                locked_at = now(),
                locked_by = p_worker,
                attempts  = j.attempts + 1
            WHERE j.id = v_id
            RETURNING j.id, j.tenant_id, j.kind::text, j.payload,
                      j.attempts, j.max_attempts;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.biryar_reclaim_stuck_jobs(
            p_stale_after interval
        )
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
                SET status     = 'pending',
                    locked_at  = NULL,
                    locked_by  = NULL,
                    last_error = 'the worker holding this job stopped reporting'
                WHERE status = 'running'
                  AND locked_at < now() - p_stale_after
                RETURNING 1
            )
            SELECT count(*) INTO v_count FROM stuck;
            RETURN v_count;
        END
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.biryar_claim_job(text, integer, text[]) FROM PUBLIC"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.biryar_reclaim_stuck_jobs(interval) FROM PUBLIC"
    )


def downgrade() -> None:
    for type_name, labels in RENAMED.items():
        for label in labels:
            op.execute(
                f"ALTER TYPE {type_name} RENAME VALUE '{label.lower()}' TO '{label}'"
            )

    op.drop_index("uq_jobs_tenant_id_idempotency_key", table_name="jobs")
    op.create_index(
        "uq_jobs_tenant_id_idempotency_key",
        "jobs", ["tenant_id", "idempotency_key"],
        unique=True,
        postgresql_where="status IN ('PENDING', 'RUNNING')",
    )
