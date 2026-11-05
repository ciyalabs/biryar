"""Running queued work.

## The transaction shape, which is the whole design

A handler runs **inside** its tenant's transaction, and the job is marked
done in that same transaction. So the work and the record of the work commit
together: there is no moment where a settlement was stored but the job still
looks pending, and none where a job looks done but its effect was rolled
back.

Recording a *failure* has to happen in a **different** transaction. The
obvious version — catch the exception, write the failure, commit — writes
into a transaction that is already doomed, so the rollback that discards the
handler's half-finished work discards the failure record with it. The job
then sits in ``RUNNING`` with no explanation until something reclaims it,
and the reason it failed is gone.

That is a subtle enough mistake that it is worth stating rather than
implying: **success is recorded inside the work's transaction, failure in a
new one.**

## How a handler says what kind of failure it was

By raising. :class:`Permanent` means do not try again — the authorisation
was revoked, the payload is nonsense, the SKU does not exist. Anything else
is treated as transient and retried with backoff, because the common case
for an unexpected exception really is a bad minute rather than a bad job.

The default favours retrying, which is the cheaper error: a transient fault
treated as permanent needs a human to notice and requeue, while a permanent
fault treated as transient costs a handful of attempts and then stops by
itself.
"""

from __future__ import annotations

import logging
import os
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy.orm import Session

from ..db.session import SessionFactory, tenant_session
from ..repositories.jobs import (
    STALE_AFTER,
    ClaimedJob,
    backoff,
    claim,
    complete,
    fail,
    reclaim_stuck,
)

logger = logging.getLogger(__name__)

#: A handler takes the tenant's session and the job it is running.
Handler = Callable[[Session, ClaimedJob], None]


class Permanent(Exception):
    """This job will not succeed. Do not retry it."""


class Retry(Exception):
    """This job failed in a way worth trying again, after ``delay``."""

    def __init__(self, message: str, delay: timedelta | None = None) -> None:
        super().__init__(message)
        self.delay = delay


class UnknownKind(Permanent):
    """Nothing in this worker handles that kind of job.

    Permanent on purpose. A worker that does not handle a kind will not
    start handling it by being asked again, and leaving the job pending
    would make it a poison pill that every worker picks up, fails on and
    puts back. If the kind is real and this worker simply lacks it, the fix
    is to run a worker that has it and requeue — which is a decision for a
    person, not a retry loop.
    """


@dataclass
class Registry:
    """Which handler runs which kind of job."""

    handlers: dict[str, Handler] = field(default_factory=dict)

    def register(self, kind: str) -> Callable[[Handler], Handler]:
        def decorate(handler: Handler) -> Handler:
            if kind in self.handlers:
                raise ValueError(f"{kind} already has a handler")
            self.handlers[kind] = handler
            return handler

        return decorate

    def kinds(self) -> list[str]:
        return sorted(self.handlers)

    def handler_for(self, kind: str) -> Handler:
        try:
            return self.handlers[kind]
        except KeyError:
            raise UnknownKind(
                f"no handler for {kind!r}; this worker handles {self.kinds()}"
            ) from None


def worker_name() -> str:
    """Something a human can match to a process when a job is stuck."""
    return f"{socket.gethostname()}/{os.getpid()}"


@dataclass
class Runner:
    """Claims work and runs it, one job at a time."""

    factory: SessionFactory
    registry: Registry
    name: str = field(default_factory=worker_name)
    #: Only claim kinds this worker can actually run, so a queue holding
    #: something it cannot handle does not stop it taking what it can.
    restrict_to_own_kinds: bool = True

    def run_once(self) -> ClaimedJob | None:
        """Claim one job and run it. ``None`` when there was nothing to do."""
        session = self.factory()
        try:
            job = claim(
                session,
                self.name,
                kinds=self.registry.kinds() if self.restrict_to_own_kinds else None,
            )
            session.commit()
        finally:
            session.close()

        if job is None:
            return None

        self._run(job)
        return job

    def _run(self, job: ClaimedJob) -> None:
        started = time.monotonic()
        try:
            handler = self.registry.handler_for(job.kind)
            # The work and its completion, in one transaction.
            with tenant_session(self.factory, job.tenant_id) as db:
                handler(db, job)
                complete(db, job.id)
        except Permanent as exc:
            self._record_failure(job, str(exc), retry_in=None)
        except Retry as exc:
            # `exc.delay or backoff(...)` would be wrong: timedelta(0) is
            # falsy, so a handler asking to be retried immediately would
            # silently get the full backoff instead. The distinction that
            # matters is whether a delay was *given*, not whether it is
            # non-zero.
            delay = exc.delay if exc.delay is not None else backoff(job.attempts)
            self._record_failure(job, str(exc), retry_in=delay)
        except Exception as exc:
            # Unexpected: assume transient. See the module docstring on why
            # the default favours retrying.
            self._record_failure(
                job,
                f"{type(exc).__name__}: {exc}",
                retry_in=backoff(job.attempts),
            )
        else:
            logger.info(
                "jobs: %s for tenant %s done in %.2fs",
                job.kind, job.tenant_id, time.monotonic() - started,
            )

    def _record_failure(
        self, job: ClaimedJob, message: str, *, retry_in: timedelta | None,
    ) -> None:
        """Write the failure in its own transaction.

        The handler's transaction has already rolled back by the time this
        runs; writing into it would roll the failure back too, leaving the
        job stuck in RUNNING with no explanation.
        """
        try:
            with tenant_session(self.factory, job.tenant_id) as db:
                fail(db, job.id, message, retry_in=retry_in)
        except Exception:
            # If even this fails the database is in trouble, and the job will
            # be reclaimed when its lock goes stale. Losing the message is
            # better than losing the worker.
            logger.exception(
                "jobs: could not record the failure of %s for tenant %s",
                job.kind, job.tenant_id,
            )

    def drain(self, limit: int | None = None) -> int:
        """Run until the queue has nothing claimable. Returns how many ran.

        ``limit`` is a guard rather than a feature: without it a handler that
        requeues its own kind turns this into an infinite loop, and the first
        sign is a worker that never returns.
        """
        done = 0
        while limit is None or done < limit:
            if self.run_once() is None:
                break
            done += 1
        return done

    def reclaim(self, stale_after: timedelta = STALE_AFTER) -> int:
        """Return jobs abandoned by workers that stopped reporting."""
        session = self.factory()
        try:
            count = reclaim_stuck(session, stale_after)
            session.commit()
            return count
        finally:
            session.close()
