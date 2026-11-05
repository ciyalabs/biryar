"""Background work, queued in the database.

## Why PostgreSQL and not a queue

Because the work and the reason for it commit together. Enqueueing a
settlement fetch in the same transaction that stored a credential means
there is no window where one exists without the other — no "the credential
saved but the job was lost", and no compensating cleanup for the reverse.
A separate broker buys throughput this product will not need for years and
sells that guarantee to get it.

It also keeps a self-hosted install to one service. ``SELECT ... FOR UPDATE
SKIP LOCKED`` is a perfectly good claim primitive, and the whole queue is
one table that `pg_dump` already backs up.

## Fairness is the hard part, not throughput

The obvious claim — oldest job first — lets one tenant with a large backfill
occupy every worker while everyone else waits. The queue is shared, so one
seller's catch-up becomes every seller's outage, and nothing in the system
reports that as a fault.

So a tenant may hold only so many running jobs at once, and the claim skips
tenants already at their limit. A backfill then proceeds at a steady trickle
instead of a flood, and a tenant with one urgent job is never behind ten
thousand of someone else's.

## Why failures carry a count and a time rather than a dead letter

A job that failed because Amazon was throttling should be tried again
shortly. One that failed because the authorisation was revoked should not be
tried again at all. The difference is not visible from the failure's shape,
so the handler says which by raising, and the row records the count, the
next attempt and the last message — enough to tell a transient wobble from a
tenant that needs to reconnect, without a second table to reconcile.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import CheckConstraint, Enum, Index, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base, LongText, ShortText, TenantScoped, Timestamp, tenant_fk


class JobStatus(StrEnum):
    """Where a job is.

    ``FAILED`` is terminal and deliberate: it means the handler said this
    will not succeed, or it ran out of attempts. It is not a synonym for
    "errored once".
    """

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class Job(Base, TenantScoped):
    """One unit of background work for one tenant."""

    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[uuid.UUID] = tenant_fk()

    #: Which handler runs this. Free text rather than an enum so that adding
    #: a job kind is a code change and not a migration — and so that a queue
    #: holding a kind this worker has never heard of can report that rather
    #: than failing to load the row at all.
    kind: Mapped[ShortText] = mapped_column(nullable=False)

    #: Arguments. Deliberately small and non-sensitive: a date range, a
    #: marketplace. Anything a handler needs that is worth protecting it
    #: should read from its own tables, inside the tenant transaction.
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}",
    )

    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="job_status", native_enum=True),
        nullable=False,
        default=JobStatus.PENDING,
    )

    #: Not before this. Both the schedule and the retry backoff are expressed
    #: here, so there is one thing to look at when asking why a job has not
    #: run.
    run_after: Mapped[Timestamp] = mapped_column(
        server_default=func.now(), nullable=False,
    )

    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)

    #: Who holds it and since when. The time matters more than the name: a
    #: worker that dies mid-job leaves a row claimed forever otherwise, and
    #: this is what lets one be reclaimed.
    locked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    last_error: Mapped[LongText] = mapped_column(nullable=False, default="")

    created_at: Mapped[Timestamp] = mapped_column(
        server_default=func.now(), nullable=False,
    )
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)

    #: Set by the enqueuer to make a job unique while it is outstanding —
    #: "the daily settlement fetch for this tenant and this day". Null means
    #: "no such constraint", which is why this is not simply the primary key
    #: of some other shape.
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)

    __table_args__ = (
        # The claim query: pending work, oldest eligible first.
        Index("ix_jobs_status_run_after", "status", "run_after"),
        # Counting what a tenant currently has running, which is how the
        # claim decides whether to skip them.
        Index("ix_jobs_tenant_id_status", "tenant_id", "status"),
        # One outstanding job per key per tenant. Partial, so the key is
        # free again once the job finishes: "the settlement fetch for the
        # third of November" should be enqueueable again next week if a
        # correction is needed, but not twice while it is still queued.
        Index(
            "uq_jobs_tenant_id_idempotency_key",
            "tenant_id", "idempotency_key",
            unique=True,
            postgresql_where=text("status IN ('PENDING', 'RUNNING')"),
        ),
        CheckConstraint("attempts >= 0", name="attempts_not_negative"),
        CheckConstraint("max_attempts >= 1", name="at_least_one_attempt"),
    )

    def __repr__(self) -> str:
        return f"<Job {self.kind} {self.status.value} attempt {self.attempts}>"
