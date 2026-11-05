"""Pulling the Finances feed, end to end into the ledger.

A real database and a real job runner; Amazon is answered by a mock
transport. What matters here is which failures stop and which retry — the
distinction that decides whether a revoked seller is told to reconnect or
simply accumulates errors nobody reads.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text

from biryar.amazon import LwaApplication, LwaClient
from biryar.crypto import Keyring
from biryar.db.models.credentials import CredentialStatus, Marketplace
from biryar.db.session import SessionFactory, tenant_session
from biryar.jobs import SETTLEMENT_FETCH, Registry, Runner, settlement_fetch
from biryar.profit import build_settlement
from biryar.repositories import active_credential, enqueue, settlement_rows, store_credential

from .payloads import shipment

pytestmark = pytest.mark.postgres

TOKEN = "Atzr|a-refresh-token"


@pytest.fixture
def keyring() -> Keyring:
    return Keyring({"v1": Keyring.generate_key()}, "v1")


def _transaction(transaction_id: str, *, revenue: str = "20.00") -> dict:
    """One Shipment, in the shape the Finances API returns it."""
    import json

    _, _, raw = shipment(f"ORDER-{transaction_id}", [
        {"sku": "SKU-1", "revenue": revenue, "fee_base": "-3.00", "fee_tax": "-0.57"},
    ])
    payload = json.loads(raw)
    return {
        "transactionId": transaction_id,
        "transactionType": "Shipment",
        "transactionStatus": "Released",
        "postedDate": "2026-09-23T10:00:00Z",
        "relatedIdentifiers": payload["relatedIdentifiers"],
        "items": payload["items"],
        "totalAmount": {"currencyAmount": float(revenue), "currencyCode": "EUR"},
    }


def _runner(factory, keyring, *, lwa_handler, api_handler) -> Runner:
    registry = Registry()
    registry.register(SETTLEMENT_FETCH)(
        settlement_fetch(
            lwa=LwaClient(
                LwaApplication(client_id="id", client_secret="secret"),
                client=httpx.Client(transport=httpx.MockTransport(lwa_handler)),
            ),
            keyring=keyring,
            http=httpx.Client(transport=httpx.MockTransport(api_handler)),
            factory=factory,
        )
    )
    return Runner(factory=factory, registry=registry, name="test-worker")


def _token_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"access_token": "Atza|good", "expires_in": 3600})


def _connected(factory: SessionFactory, tenant_id: uuid.UUID, keyring: Keyring) -> None:
    with tenant_session(factory, tenant_id) as s:
        store_credential(
            s, tenant_id, marketplace=Marketplace.DE,
            selling_partner_id="A1SELLER", refresh_token=TOKEN, keyring=keyring,
        )


def _status(factory: SessionFactory, tenant_id: uuid.UUID, job_id: uuid.UUID):
    with tenant_session(factory, tenant_id) as s:
        return s.execute(
            text("SELECT status, last_error FROM jobs WHERE id = :id"), {"id": job_id},
        ).one()


# -- the happy path ------------------------------------------------------------------

def test_a_fetch_stores_transactions_the_ledger_can_read(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
    keyring: Keyring,
) -> None:
    """The whole point: Amazon's feed becomes a number in the ledger."""
    a, _ = two_tenants
    _connected(factory, a, keyring)

    def api(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"payload": {
            "transactions": [_transaction("T-1"), _transaction("T-2")],
        }})

    with tenant_session(factory, a) as s:
        enqueue(s, a, SETTLEMENT_FETCH)

    _runner(factory, keyring, lwa_handler=_token_ok, api_handler=api).run_once()

    with tenant_session(factory, a) as s:
        settlement = build_settlement(settlement_rows(s, a))

    assert len(settlement.lines) == 2
    assert settlement.lines[0].revenue == Decimal("20.00")


def test_pages_are_followed(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
    keyring: Keyring,
) -> None:
    a, _ = two_tenants
    _connected(factory, a, keyring)
    seen: list[str | None] = []

    def api(request: httpx.Request) -> httpx.Response:
        token = request.url.params.get("nextToken")
        seen.append(token)
        if token is None:
            return httpx.Response(200, json={"payload": {
                "transactions": [_transaction("T-1")], "nextToken": "more",
            }})
        return httpx.Response(200, json={"payload": {
            "transactions": [_transaction("T-2")],
        }})

    with tenant_session(factory, a) as s:
        enqueue(s, a, SETTLEMENT_FETCH)

    _runner(factory, keyring, lwa_handler=_token_ok, api_handler=api).run_once()

    assert seen == [None, "more"]
    with tenant_session(factory, a) as s:
        assert len(list(settlement_rows(s, a))) == 2


def test_refetching_the_same_window_does_not_double_anything(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
    keyring: Keyring,
) -> None:
    """Windows overlap on purpose, because Amazon restates transactions."""
    a, _ = two_tenants
    _connected(factory, a, keyring)

    def api(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"payload": {
            "transactions": [_transaction("T-1")],
        }})

    runner = _runner(factory, keyring, lwa_handler=_token_ok, api_handler=api)
    for _ in range(2):
        with tenant_session(factory, a) as s:
            enqueue(s, a, SETTLEMENT_FETCH)
        runner.run_once()

    with tenant_session(factory, a) as s:
        assert len(list(settlement_rows(s, a))) == 1


def test_a_successful_fetch_records_the_refresh(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
    keyring: Keyring,
) -> None:
    a, _ = two_tenants
    _connected(factory, a, keyring)

    def api(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"payload": {"transactions": []}})

    with tenant_session(factory, a) as s:
        enqueue(s, a, SETTLEMENT_FETCH)
    _runner(factory, keyring, lwa_handler=_token_ok, api_handler=api).run_once()

    with tenant_session(factory, a) as s:
        credential = active_credential(s, a, Marketplace.DE)
        assert credential is not None
        assert credential.last_refreshed_at is not None


# -- the failures, which are the point -----------------------------------------------

def test_a_revoked_authorisation_stops_and_asks_the_seller_to_reconnect(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
    keyring: Keyring,
) -> None:
    """The one that matters.

    Merely retrying forever looks exactly like an outage, and the one person
    who could fix it is never asked to.
    """
    a, _ = two_tenants
    _connected(factory, a, keyring)

    def revoked(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    def api(request: httpx.Request) -> httpx.Response:   # pragma: no cover
        raise AssertionError("the API must not be called without a token")

    with tenant_session(factory, a) as s:
        job = enqueue(s, a, SETTLEMENT_FETCH)
    assert job is not None

    _runner(factory, keyring, lwa_handler=revoked, api_handler=api).run_once()

    status, error = _status(factory, a, job.id)
    assert status == "failed"
    assert "reconnect" in error

    with tenant_session(factory, a) as s:
        assert active_credential(s, a, Marketplace.DE) is None
        state = s.execute(
            text("SELECT status, last_error FROM amazon_credentials")
        ).one()
    assert state[0] == CredentialStatus.NEEDS_REAUTH.value
    assert "invalid_grant" in state[1]


def test_throttling_is_retried_rather_than_failed(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
    keyring: Keyring,
) -> None:
    a, _ = two_tenants
    _connected(factory, a, keyring)

    def api(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"errors": [{"code": "QuotaExceeded"}]})

    with tenant_session(factory, a) as s:
        job = enqueue(s, a, SETTLEMENT_FETCH)
    assert job is not None

    _runner(factory, keyring, lwa_handler=_token_ok, api_handler=api).run_once()

    status, error = _status(factory, a, job.id)
    assert status == "pending"
    assert "throttling" in error


def test_amazon_being_down_is_retried(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
    keyring: Keyring,
) -> None:
    a, _ = two_tenants
    _connected(factory, a, keyring)

    def api(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    with tenant_session(factory, a) as s:
        job = enqueue(s, a, SETTLEMENT_FETCH)
    assert job is not None

    _runner(factory, keyring, lwa_handler=_token_ok, api_handler=api).run_once()

    assert _status(factory, a, job.id)[0] == "pending"


def test_a_tenant_that_never_connected_fails_rather_than_retrying(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
    keyring: Keyring,
) -> None:
    """Retrying would not produce an authorisation nobody has granted."""
    a, _ = two_tenants

    def unused(request: httpx.Request) -> httpx.Response:   # pragma: no cover
        raise AssertionError("nothing should be called")

    with tenant_session(factory, a) as s:
        job = enqueue(s, a, SETTLEMENT_FETCH)
    assert job is not None

    _runner(factory, keyring, lwa_handler=unused, api_handler=unused).run_once()

    status, error = _status(factory, a, job.id)
    assert status == "failed"
    assert "must be connected" in error


def test_a_credential_under_a_lost_key_is_not_blamed_on_the_seller(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Reconnecting would not help, so it must not say to reconnect."""
    a, _ = two_tenants
    gone = Keyring({"v1": Keyring.generate_key()}, "v1")
    current = Keyring({"v2": Keyring.generate_key()}, "v2")
    _connected(factory, a, gone)

    def unused(request: httpx.Request) -> httpx.Response:   # pragma: no cover
        raise AssertionError("nothing should be called")

    with tenant_session(factory, a) as s:
        job = enqueue(s, a, SETTLEMENT_FETCH)
    assert job is not None

    _runner(factory, current, lwa_handler=unused, api_handler=unused).run_once()

    status, error = _status(factory, a, job.id)
    assert status == "failed"
    assert "cannot be read" in error

    with tenant_session(factory, a) as s:
        state = s.execute(text("SELECT status FROM amazon_credentials")).scalar_one()
    assert state == CredentialStatus.ACTIVE.value, "the seller did nothing wrong"


def test_one_tenants_fetch_cannot_store_into_another(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
    keyring: Keyring,
) -> None:
    a, b = two_tenants
    _connected(factory, a, keyring)

    def api(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"payload": {
            "transactions": [_transaction("T-1")],
        }})

    with tenant_session(factory, a) as s:
        enqueue(s, a, SETTLEMENT_FETCH)
    _runner(factory, keyring, lwa_handler=_token_ok, api_handler=api).run_once()

    with tenant_session(factory, b) as s:
        assert list(settlement_rows(s, b)) == []


def test_an_explicit_window_is_used_for_a_backfill(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
    keyring: Keyring,
) -> None:
    a, _ = two_tenants
    _connected(factory, a, keyring)
    asked: list[str] = []

    def api(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url.params.get("postedAfter")))
        return httpx.Response(200, json={"payload": {"transactions": []}})

    with tenant_session(factory, a) as s:
        enqueue(s, a, SETTLEMENT_FETCH, payload={"posted_after": "2026-01-01T00:00:00Z"})
    _runner(factory, keyring, lwa_handler=_token_ok, api_handler=api).run_once()

    assert asked == ["2026-01-01T00:00:00Z"]


def test_without_a_window_it_looks_back_far_enough_to_catch_restatements(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
    keyring: Keyring,
) -> None:
    a, _ = two_tenants
    _connected(factory, a, keyring)
    asked: list[datetime] = []

    def api(request: httpx.Request) -> httpx.Response:
        raw = str(request.url.params.get("postedAfter"))
        asked.append(datetime.fromisoformat(raw.replace("Z", "+00:00")))
        return httpx.Response(200, json={"payload": {"transactions": []}})

    with tenant_session(factory, a) as s:
        enqueue(s, a, SETTLEMENT_FETCH)
    _runner(factory, keyring, lwa_handler=_token_ok, api_handler=api).run_once()

    assert asked
    assert datetime.now(UTC) - asked[0] >= timedelta(days=13)
