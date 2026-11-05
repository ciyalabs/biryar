"""Deciding what to queue.

The property worth checking is that running it twice is harmless, because
that is what lets the schedule keep no state about when it last ran.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import text

from biryar.crypto import Keyring
from biryar.db.models.credentials import Marketplace
from biryar.db.session import SessionFactory, tenant_session
from biryar.jobs import SETTLEMENT_FETCH, schedule_settlement_fetches
from biryar.repositories import complete, mark_needs_reauth, store_credential
from biryar.repositories.credentials import active_credential

pytestmark = pytest.mark.postgres

TODAY = date(2026, 11, 5)


@pytest.fixture
def keyring() -> Keyring:
    return Keyring({"v1": Keyring.generate_key()}, "v1")


def _connect(
    factory: SessionFactory, tenant_id: uuid.UUID, keyring: Keyring,
    marketplace: Marketplace = Marketplace.DE,
) -> None:
    with tenant_session(factory, tenant_id) as s:
        store_credential(
            s, tenant_id, marketplace=marketplace,
            selling_partner_id="A1SELLER", refresh_token="Atzr|token",
            keyring=keyring,
        )


def _queued(factory: SessionFactory, tenant_id: uuid.UUID) -> int:
    with tenant_session(factory, tenant_id) as s:
        return int(
            s.execute(
                text("SELECT count(*) FROM jobs WHERE kind = :k"),
                {"k": SETTLEMENT_FETCH},
            ).scalar_one()
        )


def test_a_connected_tenant_gets_a_fetch(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
    keyring: Keyring,
) -> None:
    a, _ = two_tenants
    _connect(factory, a, keyring)

    result = schedule_settlement_fetches(factory, on=TODAY)

    assert result.enqueued >= 1
    assert _queued(factory, a) == 1


def test_running_it_twice_queues_one_job(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
    keyring: Keyring,
) -> None:
    """What lets the schedule keep no state about when it last ran."""
    a, _ = two_tenants
    _connect(factory, a, keyring)

    first = schedule_settlement_fetches(factory, on=TODAY)
    second = schedule_settlement_fetches(factory, on=TODAY)

    assert first.enqueued >= 1
    assert second.enqueued == 0
    assert second.already_queued >= 1
    assert _queued(factory, a) == 1


def test_tomorrow_is_a_different_job(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
    keyring: Keyring,
) -> None:
    a, _ = two_tenants
    _connect(factory, a, keyring)

    schedule_settlement_fetches(factory, on=TODAY)
    with tenant_session(factory, a) as s:
        job_id = s.execute(
            text("SELECT id FROM jobs WHERE kind = :k"), {"k": SETTLEMENT_FETCH},
        ).scalar_one()
        complete(s, job_id)

    schedule_settlement_fetches(factory, on=date(2026, 11, 6))

    assert _queued(factory, a) == 2


def test_a_tenant_with_no_connection_is_skipped(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Queueing it would only fill the queue with work that cannot succeed."""
    a, _ = two_tenants

    result = schedule_settlement_fetches(factory, on=TODAY)

    assert result.considered == 0
    assert _queued(factory, a) == 0


def test_a_tenant_that_must_reconnect_is_skipped(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
    keyring: Keyring,
) -> None:
    """The failure already said what to do; repeating it daily adds nothing."""
    a, _ = two_tenants
    _connect(factory, a, keyring)
    with tenant_session(factory, a) as s:
        credential = active_credential(s, a, Marketplace.DE)
        assert credential is not None
        mark_needs_reauth(credential, "invalid_grant")

    schedule_settlement_fetches(factory, on=TODAY)

    assert _queued(factory, a) == 0


def test_each_connected_marketplace_gets_its_own_fetch(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
    keyring: Keyring,
) -> None:
    a, _ = two_tenants
    _connect(factory, a, keyring, Marketplace.DE)
    _connect(factory, a, keyring, Marketplace.UK)

    schedule_settlement_fetches(factory, on=TODAY)

    assert _queued(factory, a) == 2


def test_each_tenant_gets_their_own(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
    keyring: Keyring,
) -> None:
    a, b = two_tenants
    _connect(factory, a, keyring)
    _connect(factory, b, keyring)

    schedule_settlement_fetches(factory, on=TODAY)

    assert _queued(factory, a) == 1
    assert _queued(factory, b) == 1
