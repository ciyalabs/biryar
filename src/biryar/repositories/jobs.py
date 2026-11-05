"""Queueing work, claiming it, and recording how it went."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..db.models.jobs import Job, JobStatus

logger = logging.getLogger(__name__)

#: How long a claimed job may go unreported before another worker may take
#: it. Long enough that a slow job is not stolen from a worker still doing
#: it; short enough that a crash is not an hour of nothing happening.
STALE_AFTER = timedelta(minutes=15)

#: How many jobs one tenant may have running at once. The number that stops
#: one seller's backfill from becoming everyone's outage. Two rather than
#: one so a tenant is not serialised behind their own slowest job.
MAX_RUNNING_PER_TENANT = 2


@dataclass(frozen=True)
class ClaimedJob:
    """A job this worker now holds."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int

    @property
    def is_last_attempt(self) -> bool:
        """Whether failing this time is terminal.

        Worth knowing inside a handler: the last attempt is the one where a
        partial result is better written than discarded.
        """
        return self.attempts >= self.max_attempts


# -- queueing, inside a tenant transaction -------------------------------------------

def enqueue(
    session: Session,
    tenant_id: uuid.UUID,
    kind: str,
    *,
    payload: dict[str, Any] | None = None,
    run_after: datetime | None = None,
    max_attempts: int = 5,
    idempotency_key: str | None = None,
) -> Job | None:
    """Queue work for one tenant.

    Returns ``None`` when ``idempotency_key`` names something already
    queued or running for this tenant. That is a success, not a failure:
    the point of the key is that enqueueing twice leaves one job, so a
    scheduler that fires twice, or a retry of the thing that enqueued, does
    not double the work.
    """
    values: dict[str, Any] = {
        "tenant_id": tenant_id,
        "kind": kind,
        "payload": payload or {},
        "status": JobStatus.PENDING,
        "max_attempts": max_attempts,
        "attempts": 0,
        "last_error": "",
    }
    if run_after is not None:
        values["run_after"] = run_after
    if idempotency_key is not None:
        values["idempotency_key"] = idempotency_key

    statement = insert(Job).values(values)
    if idempotency_key is not None:
        # The index is partial, so this conflicts only against a job that is
        # still outstanding. A finished one frees the key again.
        statement = statement.on_conflict_do_nothing(
            index_elements=[Job.tenant_id, Job.idempotency_key],
            index_where=text("status IN ('PENDING', 'RUNNING')"),
        )

    result = session.execute(statement.returning(Job.id))
    job_id = result.scalar_one_or_none()
    if job_id is None:
        return None

    session.flush()
    return session.execute(select(Job).where(Job.id == job_id)).scalar_one()


# -- claiming, with no tenant in scope -----------------------------------------------

def claim(
    session: Session,
    worker: str,
    *,
    kinds: list[str] | None = None,
    max_running_per_tenant: int = MAX_RUNNING_PER_TENANT,
) -> ClaimedJob | None:
    """Take the next job this worker may run, or ``None``.

    Runs without a tenant, through the ``SECURITY DEFINER`` function created
    in revision 0004. The worker is infrastructure rather than a tenant
    acting, and has to see across tenants to be fair — but it gets one job
    at a time and cannot list the queue.
    """
    row = session.execute(
        text(
            "SELECT job_id, tenant_id, kind, payload, attempts, max_attempts "
            "FROM public.biryar_claim_job(:worker, :max_running, :kinds)"
        ),
        {
            "worker": worker[:128],
            "max_running": max_running_per_tenant,
            "kinds": kinds,
        },
    ).one_or_none()

    if row is None:
        return None

    return ClaimedJob(
        id=row[0],
        tenant_id=row[1],
        kind=row[2],
        payload=row[3] or {},
        attempts=row[4],
        max_attempts=row[5],
    )


def reclaim_stuck(session: Session, stale_after: timedelta = STALE_AFTER) -> int:
    """Return jobs whose worker stopped reporting. How many went back."""
    count: int = session.execute(
        text("SELECT public.biryar_reclaim_stuck_jobs(:stale)"),
        {"stale": stale_after},
    ).scalar_one()
    if count:
        logger.warning("jobs: reclaimed %d job(s) from workers that went away", count)
    return int(count)


# -- reporting, inside the job's own tenant transaction ------------------------------

def complete(session: Session, job_id: uuid.UUID) -> None:
    """Record that a job finished."""
    job = session.execute(select(Job).where(Job.id == job_id)).scalar_one()
    job.status = JobStatus.DONE
    job.finished_at = datetime.now(UTC)
    job.locked_at = None
    job.locked_by = None
    job.last_error = ""


def fail(
    session: Session,
    job_id: uuid.UUID,
    error: str,
    *,
    retry_in: timedelta | None = None,
) -> JobStatus:
    """Record a failure, and decide whether it will be tried again.

    ``retry_in=None`` means terminal — the handler said this will not
    succeed, so the attempts left do not matter. That distinction is the
    reason this takes a duration rather than a boolean: a caller that knows
    *when* to retry also knows *whether* to, and one that passes nothing is
    saying never rather than forgetting to say soon.
    """
    job = session.execute(select(Job).where(Job.id == job_id)).scalar_one()
    job.last_error = error[:1024]
    job.locked_at = None
    job.locked_by = None

    exhausted = job.attempts >= job.max_attempts
    if retry_in is None or exhausted:
        job.status = JobStatus.FAILED
        job.finished_at = datetime.now(UTC)
        logger.warning(
            "jobs: %s for tenant %s failed permanently after %d attempt(s): %s",
            job.kind, job.tenant_id, job.attempts, error[:200],
        )
    else:
        job.status = JobStatus.PENDING
        job.run_after = datetime.now(UTC) + retry_in
        logger.info(
            "jobs: %s for tenant %s failed, retrying in %s (attempt %d of %d)",
            job.kind, job.tenant_id, retry_in, job.attempts, job.max_attempts,
        )

    return job.status


def backoff(attempts: int, *, base: timedelta = timedelta(seconds=30)) -> timedelta:
    """How long to wait before attempt ``attempts + 1``.

    Exponential, capped at an hour. Uncapped doubling reaches days by the
    sixth attempt, which means a job that failed during a brief outage is
    still not retried long after the outage ended.
    """
    delay: timedelta = base * (2 ** max(attempts - 1, 0))
    return min(delay, timedelta(hours=1))
