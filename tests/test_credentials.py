"""Storing a tenant's Amazon authorisation, sealed."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from biryar.crypto import Keyring, SealingError
from biryar.db.models.credentials import CredentialStatus, Marketplace
from biryar.db.session import SessionFactory, tenant_session
from biryar.repositories import (
    active_credential,
    mark_needs_reauth,
    mark_refreshed,
    read_refresh_token,
    reseal_if_stale,
    store_credential,
)

pytestmark = pytest.mark.postgres

TOKEN = "Atzr|a-very-long-refresh-token-value"


@pytest.fixture
def keyring() -> Keyring:
    return Keyring({"v1": Keyring.generate_key()}, "v1")


def test_a_credential_round_trips(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
    keyring: Keyring,
) -> None:
    a, _ = two_tenants
    with tenant_session(factory, a) as s:
        store_credential(
            s, a, marketplace=Marketplace.DE, selling_partner_id="A1SELLER",
            refresh_token=TOKEN, keyring=keyring,
        )

    with tenant_session(factory, a) as s:
        credential = active_credential(s, a, Marketplace.DE)
        assert credential is not None
        assert read_refresh_token(credential, keyring) == TOKEN


def test_the_token_is_not_in_the_table(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
    keyring: Keyring,
) -> None:
    """A dump of this table must not be a set of working credentials."""
    a, _ = two_tenants
    with tenant_session(factory, a) as s:
        store_credential(
            s, a, marketplace=Marketplace.DE, selling_partner_id="A1SELLER",
            refresh_token=TOKEN, keyring=keyring,
        )

    with tenant_session(factory, a) as s:
        stored = s.execute(
            text("SELECT refresh_token_sealed FROM amazon_credentials")
        ).scalar_one()

    assert TOKEN.encode() not in bytes(stored)
    assert b"Atzr" not in bytes(stored)


def test_reauthorising_replaces_rather_than_accumulates(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
    keyring: Keyring,
) -> None:
    """Two live credentials for one marketplace is an unanswerable question."""
    a, _ = two_tenants
    with tenant_session(factory, a) as s:
        store_credential(s, a, marketplace=Marketplace.DE,
                         selling_partner_id="A1SELLER", refresh_token=TOKEN,
                         keyring=keyring)
        store_credential(s, a, marketplace=Marketplace.DE,
                         selling_partner_id="A1SELLER", refresh_token="Atzr|second",
                         keyring=keyring)

    with tenant_session(factory, a) as s:
        count = s.execute(text("SELECT count(*) FROM amazon_credentials")).scalar_one()
        credential = active_credential(s, a, Marketplace.DE)
        assert credential is not None
        assert read_refresh_token(credential, keyring) == "Atzr|second"

    assert count == 1


def test_reauthorising_clears_a_previous_failure(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
    keyring: Keyring,
) -> None:
    a, _ = two_tenants
    with tenant_session(factory, a) as s:
        credential = store_credential(
            s, a, marketplace=Marketplace.DE, selling_partner_id="A1SELLER",
            refresh_token=TOKEN, keyring=keyring,
        )
        mark_needs_reauth(credential, "Amazon said no")

    with tenant_session(factory, a) as s:
        assert active_credential(s, a, Marketplace.DE) is None
        store_credential(s, a, marketplace=Marketplace.DE,
                         selling_partner_id="A1SELLER", refresh_token="Atzr|fresh",
                         keyring=keyring)

    with tenant_session(factory, a) as s:
        credential = active_credential(s, a, Marketplace.DE)
        assert credential is not None
        assert credential.status is CredentialStatus.ACTIVE
        assert credential.last_error is None


def test_a_refused_credential_stops_being_handed_out(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
    keyring: Keyring,
) -> None:
    """A revoked authorisation never recovers, so retrying it is only noise."""
    a, _ = two_tenants
    with tenant_session(factory, a) as s:
        credential = store_credential(
            s, a, marketplace=Marketplace.DE, selling_partner_id="A1SELLER",
            refresh_token=TOKEN, keyring=keyring,
        )
        mark_needs_reauth(credential, "invalid_grant")

    with tenant_session(factory, a) as s:
        assert active_credential(s, a, Marketplace.DE) is None


def test_a_successful_refresh_is_recorded(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
    keyring: Keyring,
) -> None:
    a, _ = two_tenants
    with tenant_session(factory, a) as s:
        credential = store_credential(
            s, a, marketplace=Marketplace.DE, selling_partner_id="A1SELLER",
            refresh_token=TOKEN, keyring=keyring,
        )
        mark_refreshed(credential)

    with tenant_session(factory, a) as s:
        credential = active_credential(s, a, Marketplace.DE)
        assert credential is not None
        assert credential.last_refreshed_at is not None


def test_one_tenants_credential_is_invisible_to_another(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
    keyring: Keyring,
) -> None:
    a, b = two_tenants
    with tenant_session(factory, a) as s:
        store_credential(s, a, marketplace=Marketplace.DE,
                         selling_partner_id="A1SELLER", refresh_token=TOKEN,
                         keyring=keyring)

    with tenant_session(factory, b) as s:
        assert active_credential(s, b, Marketplace.DE) is None


# -- rotation ------------------------------------------------------------------------

def test_a_credential_moves_to_the_current_key_when_it_is_used(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Rotation finishes as tenants are used, not in one held transaction."""
    a, _ = two_tenants
    old, new = Keyring.generate_key(), Keyring.generate_key()
    before = Keyring({"v1": old}, "v1")
    after = Keyring({"v2": new, "v1": old}, "v2")

    with tenant_session(factory, a) as s:
        store_credential(s, a, marketplace=Marketplace.DE,
                         selling_partner_id="A1SELLER", refresh_token=TOKEN,
                         keyring=before)

    with tenant_session(factory, a) as s:
        credential = active_credential(s, a, Marketplace.DE)
        assert credential is not None
        assert credential.key_id == "v1"
        assert reseal_if_stale(credential, after) is True
        assert credential.key_id == "v2"

    with tenant_session(factory, a) as s:
        credential = active_credential(s, a, Marketplace.DE)
        assert credential is not None
        assert read_refresh_token(credential, after) == TOKEN
        # Already current: nothing to do a second time.
        assert reseal_if_stale(credential, after) is False


def test_a_credential_under_a_discarded_key_fails_loudly(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Not silently as a missing token, which would mean pointless reauth."""
    a, _ = two_tenants
    gone = Keyring({"v1": Keyring.generate_key()}, "v1")
    current = Keyring({"v2": Keyring.generate_key()}, "v2")

    with tenant_session(factory, a) as s:
        store_credential(s, a, marketplace=Marketplace.DE,
                         selling_partner_id="A1SELLER", refresh_token=TOKEN,
                         keyring=gone)

    with tenant_session(factory, a) as s:
        credential = active_credential(s, a, Marketplace.DE)
        assert credential is not None
        with pytest.raises(SealingError, match="no longer has"):
            read_refresh_token(credential, current)
