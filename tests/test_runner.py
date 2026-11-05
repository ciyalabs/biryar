"""Running queued work.

The transaction boundaries are what these mostly check. They are invisible
in the code's shape — a failure recorded in the handler's own transaction
looks exactly like one recorded outside it, right up until the rollback
takes the failure record with it.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from biryar.db.models.jobs import JobStatus
from biryar.db.session import SessionFactory, tenant_session
from biryar.jobs import Permanent, Registry, Retry, Runner
from biryar.repositories import ClaimedJob, enqueue

pytestmark = pytest.mark.postgres


def _status(factory: SessionFactory, tenant_id: uuid.UUID, job_id: uuid.UUID):
    with tenant_session(factory, tenant_id) as s:
        row = s.execute(
            text("SELECT status, attempts, last_error FROM jobs WHERE id = :id"),
            {"id": job_id},
        ).one()
    return row[0], row[1], row[2]


def _runner(factory: SessionFactory, registry: Registry) -> Runner:
    return Runner(factory=factory, registry=registry, name="test-worker")


# -- the happy path ------------------------------------------------------------------

def test_a_job_runs_and_is_marked_done(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, _ = two_tenants
    registry = Registry()
    seen: list[ClaimedJob] = []

    @registry.register("work")
    def _handle(db: Session, job: ClaimedJob) -> None:
        seen.append(job)

    with tenant_session(factory, a) as s:
        job = enqueue(s, a, "work", payload={"n": 1})
    assert job is not None

    assert _runner(factory, registry).run_once() is not None

    assert len(seen) == 1
    assert seen[0].payload == {"n": 1}
    assert _status(factory, a, job.id)[0] == "done"


def test_a_handler_runs_inside_its_tenants_scope(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """The handler must not be able to see another tenant's rows."""
    a, b = two_tenants
    registry = Registry()
    visible: list[int] = []

    @registry.register("count-jobs")
    def _handle(db: Session, job: ClaimedJob) -> None:
        visible.append(db.execute(text("SELECT count(*) FROM jobs")).scalar_one())

    with tenant_session(factory, b) as s:
        for _ in range(3):
            enqueue(s, b, "other")
    with tenant_session(factory, a) as s:
        enqueue(s, a, "count-jobs")

    _runner(factory, registry).run_once()

    # Its own job, and nothing of B's three.
    assert visible == [1]


def test_an_empty_queue_is_not_an_error(factory: SessionFactory) -> None:
    assert _runner(factory, Registry()).run_once() is None


def test_a_worker_only_claims_kinds_it_handles(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, _ = two_tenants
    registry = Registry()

    @registry.register("mine")
    def _handle(db: Session, job: ClaimedJob) -> None:
        pass

    with tenant_session(factory, a) as s:
        enqueue(s, a, "someone-elses")

    assert _runner(factory, registry).run_once() is None


# -- failure, and where it is written ------------------------------------------------

def test_a_failure_is_recorded_even_though_the_work_rolled_back(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """The subtle one.

    The handler writes a row and then raises. Its transaction rolls back, so
    the row must be gone — but the failure record must survive, because it
    is written in a transaction of its own. Recording it inside the
    handler's would lose it to the same rollback, leaving the job stuck in
    RUNNING with no explanation.
    """
    a, _ = two_tenants
    registry = Registry()

    @registry.register("writes-then-fails")
    def _handle(db: Session, job: ClaimedJob) -> None:
        enqueue(db, a, "a-side-effect")
        raise Retry("Amazon was throttling", delay=timedelta(minutes=5))

    with tenant_session(factory, a) as s:
        job = enqueue(s, a, "writes-then-fails")
    assert job is not None

    _runner(factory, registry).run_once()

    status, attempts, error = _status(factory, a, job.id)
    assert status == "pending"            # retried
    assert attempts == 1
    assert "throttling" in error          # the failure survived

    with tenant_session(factory, a) as s:
        side_effects = s.execute(
            text("SELECT count(*) FROM jobs WHERE kind = 'a-side-effect'")
        ).scalar_one()
    assert side_effects == 0, "the handler's work should have rolled back"


def test_work_and_completion_commit_together(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """No moment where the effect exists but the job still looks pending."""
    a, _ = two_tenants
    registry = Registry()

    @registry.register("writes")
    def _handle(db: Session, job: ClaimedJob) -> None:
        enqueue(db, a, "a-side-effect")

    with tenant_session(factory, a) as s:
        job = enqueue(s, a, "writes")
    assert job is not None

    _runner(factory, registry).run_once()

    with tenant_session(factory, a) as s:
        effects = s.execute(
            text("SELECT count(*) FROM jobs WHERE kind = 'a-side-effect'")
        ).scalar_one()
    assert effects == 1
    assert _status(factory, a, job.id)[0] == "done"


def test_a_permanent_failure_is_not_retried(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, _ = two_tenants
    registry = Registry()

    @registry.register("hopeless")
    def _handle(db: Session, job: ClaimedJob) -> None:
        raise Permanent("invalid_grant: the seller revoked it")

    with tenant_session(factory, a) as s:
        job = enqueue(s, a, "hopeless")
    assert job is not None

    _runner(factory, registry).run_once()

    status, _, error = _status(factory, a, job.id)
    assert status == "failed"
    assert "invalid_grant" in error
    assert _runner(factory, registry).run_once() is None


def test_an_unexpected_exception_is_assumed_transient(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """The cheaper error. A permanent fault retried stops by itself."""
    a, _ = two_tenants
    registry = Registry()

    @registry.register("boom")
    def _handle(db: Session, job: ClaimedJob) -> None:
        raise ZeroDivisionError("division by zero")

    with tenant_session(factory, a) as s:
        job = enqueue(s, a, "boom")
    assert job is not None

    _runner(factory, registry).run_once()

    status, _, error = _status(factory, a, job.id)
    assert status == "pending"
    assert "ZeroDivisionError" in error


def test_a_job_nothing_handles_stops_rather_than_circulating(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Otherwise it is a poison pill every worker picks up and puts back."""
    a, _ = two_tenants
    registry = Registry()

    @registry.register("known")
    def _handle(db: Session, job: ClaimedJob) -> None:
        pass

    with tenant_session(factory, a) as s:
        job = enqueue(s, a, "unknown")
    assert job is not None

    runner = Runner(
        factory=factory, registry=registry, name="w", restrict_to_own_kinds=False,
    )
    runner.run_once()

    status, _, error = _status(factory, a, job.id)
    assert status == "failed"
    assert "no handler" in error


def test_running_out_of_attempts_ends_it(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, _ = two_tenants
    registry = Registry()

    @registry.register("always-fails")
    def _handle(db: Session, job: ClaimedJob) -> None:
        raise Retry("not yet", delay=timedelta(seconds=0))

    with tenant_session(factory, a) as s:
        job = enqueue(s, a, "always-fails", max_attempts=3)
    assert job is not None

    runner = _runner(factory, registry)
    for _ in range(3):
        runner.run_once()

    status, attempts, _ = _status(factory, a, job.id)
    assert status == "failed"
    assert attempts == 3


# -- draining ------------------------------------------------------------------------

def test_drain_runs_everything_claimable(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, b = two_tenants
    registry = Registry()
    ran: list[uuid.UUID] = []

    @registry.register("work")
    def _handle(db: Session, job: ClaimedJob) -> None:
        ran.append(job.tenant_id)

    with tenant_session(factory, a) as s:
        for _ in range(4):
            enqueue(s, a, "work")
    with tenant_session(factory, b) as s:
        for _ in range(2):
            enqueue(s, b, "work")

    assert _runner(factory, registry).drain() == 6
    assert sorted(ran) == sorted([a] * 4 + [b] * 2)


def test_drain_stops_at_its_limit(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """A handler that requeues its own kind would otherwise never return."""
    a, _ = two_tenants
    registry = Registry()

    @registry.register("requeues-itself")
    def _handle(db: Session, job: ClaimedJob) -> None:
        enqueue(db, job.tenant_id, "requeues-itself")

    with tenant_session(factory, a) as s:
        enqueue(s, a, "requeues-itself")

    assert _runner(factory, registry).drain(limit=5) == 5


# -- the registry --------------------------------------------------------------------

def test_a_kind_cannot_be_registered_twice() -> None:
    """Silently replacing one would make which handler runs an import order."""
    registry = Registry()

    @registry.register("work")
    def _first(db: Session, job: ClaimedJob) -> None:
        pass

    with pytest.raises(ValueError, match="already has a handler"):

        @registry.register("work")
        def _second(db: Session, job: ClaimedJob) -> None:
            pass


def test_a_runner_reclaims_abandoned_work(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, _ = two_tenants
    registry = Registry()

    @registry.register("work")
    def _handle(db: Session, job: ClaimedJob) -> None:
        pass

    with tenant_session(factory, a) as s:
        job = enqueue(s, a, "work")
    assert job is not None

    session = factory()
    try:
        from biryar.repositories import claim

        claim(session, "a-worker-that-dies")
        session.commit()
    finally:
        session.close()

    with tenant_session(factory, a) as s:
        s.execute(
            text("UPDATE jobs SET locked_at = now() - interval '1 hour' WHERE id = :id"),
            {"id": job.id},
        )

    assert _runner(factory, registry).reclaim(timedelta(minutes=15)) == 1
    assert _status(factory, a, job.id)[0] == JobStatus.PENDING.value
