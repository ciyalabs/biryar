"""Deciding what should be queued, and for whom.

Separate from the worker on purpose. A worker runs whatever is in the queue;
this decides what goes in. Keeping them apart means several workers can run
without several of them independently concluding that today's fetch is due.

## Why this is safe to run as often as you like

Every job it enqueues carries an idempotency key naming the thing it is for
— "the settlement fetch for this tenant on this day". The partial unique
index means a second attempt while the first is still outstanding does
nothing. So a scheduler that fires twice, a deployment that briefly runs two
copies, or an operator running it by hand after an outage all leave exactly
one job.

That property is what lets the schedule be dumb. There is no "when did this
last run" state to keep correct, because asking twice costs nothing.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models.credentials import AmazonCredential, CredentialStatus
from ..db.models.tenancy import Tenant, TenantStatus
from ..db.session import SessionFactory, admin_session, tenant_session
from ..repositories.jobs import enqueue
from .handlers import SETTLEMENT_FETCH

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Scheduled:
    """What a scheduling pass decided."""

    considered: int
    enqueued: int

    @property
    def already_queued(self) -> int:
        return self.considered - self.enqueued


def due_tenants(session: Session) -> list[uuid.UUID]:
    """Active tenants. Admin session: ``tenants`` is not tenant-scoped."""
    return list(
        session.execute(
            select(Tenant.id).where(Tenant.status == TenantStatus.ACTIVE)
        ).scalars()
    )


def schedule_settlement_fetches(
    factory: SessionFactory, *, on: date | None = None,
) -> Scheduled:
    """Queue today's Finances fetch for every tenant that can do one.

    A tenant with no usable authorisation is skipped rather than queued and
    failed. Queueing it would be honest but useless: the job can only fail,
    the failure says what the connection screen already says, and the queue
    fills with work that cannot succeed until a human acts.
    """
    day = on or datetime.now().date()
    considered = 0
    enqueued = 0

    with admin_session(factory) as admin:
        tenants = due_tenants(admin)

    for tenant_id in tenants:
        with tenant_session(factory, tenant_id) as db:
            credentials = db.execute(
                select(AmazonCredential).where(
                    AmazonCredential.tenant_id == tenant_id,
                    AmazonCredential.status == CredentialStatus.ACTIVE,
                )
            ).scalars().all()

            for credential in credentials:
                considered += 1
                job = enqueue(
                    db,
                    tenant_id,
                    SETTLEMENT_FETCH,
                    payload={"marketplace": credential.marketplace.value},
                    idempotency_key=f"{SETTLEMENT_FETCH}:{credential.marketplace.name}:{day}",
                )
                if job is not None:
                    enqueued += 1

    logger.info(
        "schedule: %d connection(s) considered, %d queued, %d already outstanding",
        considered, enqueued, considered - enqueued,
    )
    return Scheduled(considered=considered, enqueued=enqueued)
