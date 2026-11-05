"""The queue: claiming, fairness, retries, and recovering from a dead worker.

Fairness is the part that cannot be checked by reading the code. "Oldest
first" looks correct and drains the queue perfectly — all of it for one
seller, while everyone else waits and nothing reports a fault.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from biryar.db.models.jobs import JobStatus
from biryar.db.session import SessionFactory, tenant_session
from biryar.repositories import (
    backoff,
    claim,
    complete,
    enqueue,
    fail,
    reclaim_stuck,
)

pytestmark = pytest.mark.postgres


def _plain(factory: SessionFactory):
    """A session with no tenant, which is what a worker has."""
    return factory()


# -- queueing ------------------------------------------------------------------------

def test_a_queued_job_can_be_claimed(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, _ = two_tenants
    with tenant_session(factory, a) as s:
        enqueue(s, a, "settlement.fetch", payload={"day": "2026-11-03"})

    worker = _plain(factory)
    try:
        job = claim(worker, "worker-1")
        worker.commit()
    finally:
        worker.close()

    assert job is not None
    assert job.tenant_id == a
    assert job.kind == "settlement.fetch"
    assert job.payload == {"day": "2026-11-03"}
    assert job.attempts == 1


def test_an_empty_queue_hands_back_nothing(factory: SessionFactory) -> None:
    worker = _plain(factory)
    try:
        assert claim(worker, "worker-1") is None
    finally:
        worker.close()


def test_a_job_scheduled_for_later_is_not_claimed_yet(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, _ = two_tenants
    with tenant_session(factory, a) as s:
        enqueue(s, a, "later", run_after=datetime.now(UTC) + timedelta(hours=1))

    worker = _plain(factory)
    try:
        assert claim(worker, "worker-1") is None
    finally:
        worker.close()


def test_the_same_job_is_not_queued_twice(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """A scheduler that fires twice must not double the work."""
    a, _ = two_tenants
    with tenant_session(factory, a) as s:
        first = enqueue(s, a, "settlement.fetch", idempotency_key="2026-11-03")
        second = enqueue(s, a, "settlement.fetch", idempotency_key="2026-11-03")

    assert first is not None
    assert second is None


def test_the_key_is_free_again_once_the_job_is_done(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """A correction next week must be enqueueable; a duplicate today must not."""
    a, _ = two_tenants
    with tenant_session(factory, a) as s:
        job = enqueue(s, a, "settlement.fetch", idempotency_key="2026-11-03")
        assert job is not None
        complete(s, job.id)

    with tenant_session(factory, a) as s:
        assert enqueue(s, a, "settlement.fetch", idempotency_key="2026-11-03") is not None


def test_two_tenants_may_use_the_same_key(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, b = two_tenants
    with tenant_session(factory, a) as s:
        assert enqueue(s, a, "settlement.fetch", idempotency_key="2026-11-03") is not None
    with tenant_session(factory, b) as s:
        assert enqueue(s, b, "settlement.fetch", idempotency_key="2026-11-03") is not None


def test_one_tenants_queue_is_invisible_to_another(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, b = two_tenants
    with tenant_session(factory, a) as s:
        enqueue(s, a, "settlement.fetch")

    with tenant_session(factory, b) as s:
        assert s.execute(text("SELECT count(*) FROM jobs")).scalar_one() == 0


# -- fairness ------------------------------------------------------------------------

def test_one_tenants_backfill_does_not_occupy_every_worker(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """The reason the claim is not simply 'oldest first'.

    Tenant A queues a backfill. Tenant B queues one urgent job afterwards.
    Claiming repeatedly must reach B rather than working through all of A
    first — which is what ORDER BY run_after alone would do, while looking
    entirely healthy.
    """
    a, b = two_tenants
    with tenant_session(factory, a) as s:
        for day in range(20):
            enqueue(s, a, "settlement.fetch", payload={"day": day})
    with tenant_session(factory, b) as s:
        enqueue(s, b, "settlement.fetch", payload={"urgent": True})

    claimed: list[uuid.UUID] = []
    worker = _plain(factory)
    try:
        for _ in range(6):
            job = claim(worker, "worker-1")
            worker.commit()
            if job is None:
                break
            claimed.append(job.tenant_id)
    finally:
        worker.close()

    assert b in claimed, "one tenant's backfill starved another"
    assert claimed.count(a) <= 2, "a single tenant exceeded its running limit"


def test_a_tenant_at_their_limit_is_skipped(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, _ = two_tenants
    with tenant_session(factory, a) as s:
        for _ in range(5):
            enqueue(s, a, "work")

    worker = _plain(factory)
    try:
        assert claim(worker, "w", max_running_per_tenant=1) is not None
        worker.commit()
        assert claim(worker, "w", max_running_per_tenant=1) is None
        worker.commit()
    finally:
        worker.close()


def test_finishing_a_job_frees_the_tenants_slot(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, _ = two_tenants
    with tenant_session(factory, a) as s:
        enqueue(s, a, "work")
        enqueue(s, a, "work")

    worker = _plain(factory)
    try:
        first = claim(worker, "w", max_running_per_tenant=1)
        worker.commit()
        assert first is not None
        assert claim(worker, "w", max_running_per_tenant=1) is None
        worker.commit()
    finally:
        worker.close()

    with tenant_session(factory, a) as s:
        complete(s, first.id)

    worker = _plain(factory)
    try:
        assert claim(worker, "w", max_running_per_tenant=1) is not None
        worker.commit()
    finally:
        worker.close()


def test_a_worker_only_takes_the_kinds_it_handles(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """So a queue holding a kind nobody handles does not block the rest."""
    a, _ = two_tenants
    with tenant_session(factory, a) as s:
        enqueue(s, a, "needs.a.gpu")
        enqueue(s, a, "settlement.fetch")

    worker = _plain(factory)
    try:
        job = claim(worker, "w", kinds=["settlement.fetch"])
        worker.commit()
    finally:
        worker.close()

    assert job is not None
    assert job.kind == "settlement.fetch"


# -- failing and retrying ------------------------------------------------------------

def test_a_retryable_failure_comes_back_later(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, _ = two_tenants
    with tenant_session(factory, a) as s:
        enqueue(s, a, "work")

    worker = _plain(factory)
    try:
        job = claim(worker, "w")
        worker.commit()
    finally:
        worker.close()
    assert job is not None

    with tenant_session(factory, a) as s:
        status = fail(s, job.id, "Amazon was throttling", retry_in=timedelta(minutes=5))

    assert status is JobStatus.PENDING

    worker = _plain(factory)
    try:
        assert claim(worker, "w") is None, "a retry must wait for its backoff"
    finally:
        worker.close()


def test_a_terminal_failure_does_not_come_back(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """A revoked authorisation will not start working because we asked again."""
    a, _ = two_tenants
    with tenant_session(factory, a) as s:
        enqueue(s, a, "work")

    worker = _plain(factory)
    try:
        job = claim(worker, "w")
        worker.commit()
    finally:
        worker.close()
    assert job is not None

    with tenant_session(factory, a) as s:
        assert fail(s, job.id, "invalid_grant") is JobStatus.FAILED

    worker = _plain(factory)
    try:
        assert claim(worker, "w") is None
    finally:
        worker.close()


def test_a_job_that_runs_out_of_attempts_stops(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, _ = two_tenants
    with tenant_session(factory, a) as s:
        enqueue(s, a, "work", max_attempts=2)

    for _ in range(2):
        worker = _plain(factory)
        try:
            job = claim(worker, "w")
            worker.commit()
        finally:
            worker.close()
        assert job is not None
        with tenant_session(factory, a) as s:
            status = fail(s, job.id, "still failing", retry_in=timedelta(seconds=0))

    assert status is JobStatus.FAILED


def test_the_failure_message_is_kept(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Without it, 'failed' is a state with no way to find out why."""
    a, _ = two_tenants
    with tenant_session(factory, a) as s:
        enqueue(s, a, "work")
    worker = _plain(factory)
    try:
        job = claim(worker, "w")
        worker.commit()
    finally:
        worker.close()
    assert job is not None

    with tenant_session(factory, a) as s:
        fail(s, job.id, "invalid_grant: the seller revoked it")

    with tenant_session(factory, a) as s:
        message = s.execute(
            text("SELECT last_error FROM jobs WHERE id = :id"), {"id": job.id},
        ).scalar_one()

    assert "invalid_grant" in message


def test_backoff_grows_and_then_stops_growing() -> None:
    """Uncapped doubling reaches days, long after the outage ended."""
    delays = [backoff(n) for n in range(1, 10)]
    assert delays[0] < delays[1] < delays[2]
    assert all(d <= timedelta(hours=1) for d in delays)
    assert delays[-1] == timedelta(hours=1)


# -- workers that die ----------------------------------------------------------------

def test_a_job_held_by_a_dead_worker_is_reclaimed(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """A killed worker leaves a row that looks busy, not failed."""
    a, _ = two_tenants
    with tenant_session(factory, a) as s:
        enqueue(s, a, "work")

    worker = _plain(factory)
    try:
        job = claim(worker, "worker-that-dies")
        worker.commit()
    finally:
        worker.close()
    assert job is not None

    # Age its lock, as a crash an hour ago would have.
    with tenant_session(factory, a) as s:
        s.execute(
            text("UPDATE jobs SET locked_at = :then WHERE id = :id"),
            {"then": datetime.now(UTC) - timedelta(hours=1), "id": job.id},
        )

    reaper = _plain(factory)
    try:
        assert reclaim_stuck(reaper, timedelta(minutes=15)) == 1
        reaper.commit()
        assert claim(reaper, "worker-2") is not None
        reaper.commit()
    finally:
        reaper.close()


def test_a_job_still_being_worked_on_is_not_stolen(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Otherwise a slow job runs twice, which for a fetch means double data."""
    a, _ = two_tenants
    with tenant_session(factory, a) as s:
        enqueue(s, a, "work")

    worker = _plain(factory)
    try:
        assert claim(worker, "worker-1") is not None
        worker.commit()
    finally:
        worker.close()

    reaper = _plain(factory)
    try:
        assert reclaim_stuck(reaper, timedelta(minutes=15)) == 0
        reaper.commit()
    finally:
        reaper.close()


def test_a_reclaimed_job_keeps_its_attempt_count(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """A job that kills its worker must eventually stop, not loop forever."""
    a, _ = two_tenants
    with tenant_session(factory, a) as s:
        enqueue(s, a, "work", max_attempts=3)

    worker = _plain(factory)
    try:
        job = claim(worker, "w1")
        worker.commit()
    finally:
        worker.close()
    assert job is not None and job.attempts == 1

    with tenant_session(factory, a) as s:
        s.execute(
            text("UPDATE jobs SET locked_at = :then WHERE id = :id"),
            {"then": datetime.now(UTC) - timedelta(hours=1), "id": job.id},
        )

    reaper = _plain(factory)
    try:
        reclaim_stuck(reaper, timedelta(minutes=15))
        reaper.commit()
        again = claim(reaper, "w2")
        reaper.commit()
    finally:
        reaper.close()

    assert again is not None
    assert again.attempts == 2, "attempts must survive a reclaim"
