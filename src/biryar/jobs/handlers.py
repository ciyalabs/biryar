"""The work itself.

Each handler is built by a factory that closes over what it needs, so the
registry still sees a plain ``(session, job)`` callable and a test can hand
over a client that answers without a network.

## What "failed" means here, in three kinds

The Finances fetch can fail three ways, and treating them alike is how a
queue becomes useless:

* **Throttled, unreachable, a 500.** Try again later. Nothing is wrong with
  the tenant.
* **The access token was rejected.** Usually just stale. Refreshing is the
  answer, and only if *that* is refused does it become the next case.
* **The authorisation was revoked.** No number of retries will help. The
  credential is marked, the job stops, and the seller is told on their own
  screen rather than the failure accumulating in a log.

The third is the one worth getting right. A revoked seller whose jobs merely
keep failing looks identical to an outage, and the one person who could fix
it — by reconnecting — is never asked to.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy.orm import Session

from ..amazon.finances import FinancesClient, SpApiError, SpApiRefused, Throttled, to_row
from ..amazon.lwa import AuthorisationRevoked, LwaClient, LwaError
from ..crypto import Keyring, SealingError
from ..db.models.credentials import Marketplace
from ..db.session import SessionFactory, tenant_session
from ..repositories.credentials import (
    active_credential,
    mark_needs_reauth,
    mark_refreshed,
    read_refresh_token,
    reseal_if_stale,
)
from ..repositories.jobs import ClaimedJob
from ..repositories.transactions import record_transactions
from .runner import Handler, Permanent, Retry

logger = logging.getLogger(__name__)

SETTLEMENT_FETCH = "settlement.fetch"

#: How far back to look when a job does not say. Amazon restates
#: transactions for a while after they post, so a window that only covers
#: "since last time" misses corrections; overlapping is free because ingest
#: upserts on the transaction id.
DEFAULT_LOOKBACK = timedelta(days=14)


def settlement_fetch(
    *,
    lwa: LwaClient,
    keyring: Keyring,
    http: httpx.Client,
    factory: SessionFactory,
) -> Handler:
    """Build the handler that pulls the Finances feed for one tenant.

    ``factory`` is here for one reason: marking a credential as needing
    reauthorisation has to survive the failure that caused it. The handler's
    own session rolls back when it raises, so a mark written there would be
    discarded along with everything else, and the seller would never be told
    to reconnect — the exact trap the runner documents for job failures.
    """

    def handle(db: Session, job: ClaimedJob) -> None:
        marketplace = Marketplace(
            job.payload.get("marketplace", Marketplace.DE.value)
        )

        credential = active_credential(db, job.tenant_id, marketplace)
        if credential is None:
            raise Permanent(
                f"no active {marketplace.name} authorisation for this tenant; "
                "it must be connected before anything can be fetched"
            )

        try:
            refresh_token = read_refresh_token(credential, keyring)
            # Opportunistic, and deliberately inside the same guard: a key
            # rotation finishes as tenants are used, and when the key is
            # missing this fails for exactly the reason the read above does.
            # Left outside, it raised a bare SealingError that the runner
            # read as transient and retried forever.
            reseal_if_stale(credential, keyring)
        except SealingError as exc:
            # The key is gone. Not the seller's fault and not fixable by
            # reconnecting, so this must not mark them as needing reauth.
            raise Permanent(f"this tenant's credential cannot be read: {exc}") from exc

        try:
            access = lwa.refresh_access_token(refresh_token)
        except AuthorisationRevoked as exc:
            _mark_needs_reauth_durably(factory, job.tenant_id, marketplace, str(exc))
            raise Permanent(f"the seller must reconnect Amazon: {exc}") from exc
        except LwaError as exc:
            raise Retry(f"could not refresh the access token: {exc}") from exc

        mark_refreshed(credential)

        posted_after = _window_start(job)
        client = FinancesClient(
            access_token=access.token, marketplace=marketplace, client=http,
        )

        try:
            rows = [to_row(raw, marketplace) for raw in client.transactions(posted_after)]
        except Throttled as exc:
            # Amazon said slow down, so say when rather than letting the
            # generic backoff decide.
            raise Retry(str(exc), delay=timedelta(minutes=5)) from exc
        except SpApiError as exc:
            raise Retry(str(exc)) from exc
        except SpApiRefused as exc:
            raise Permanent(str(exc)) from exc

        stored = record_transactions(db, job.tenant_id, marketplace, rows)
        logger.info(
            "settlement: tenant %s, %s, %d transaction(s) since %s",
            job.tenant_id, marketplace.name, stored, posted_after.date(),
        )

    return handle


def _mark_needs_reauth_durably(
    factory: SessionFactory,
    tenant_id: uuid.UUID,
    marketplace: Marketplace,
    reason: str,
) -> None:
    """Record that a seller must reconnect, in a transaction of its own.

    The caller is about to raise, which rolls its own transaction back. A
    mark written there would go with it, leaving the job failed for a reason
    the seller is never shown and the credential still looking healthy.
    """
    try:
        with tenant_session(factory, tenant_id) as db:
            credential = active_credential(db, tenant_id, marketplace)
            if credential is not None:
                mark_needs_reauth(credential, reason)
    except Exception:
        logger.exception(
            "credentials: could not mark tenant %s as needing reauthorisation",
            tenant_id,
        )


def _window_start(job: ClaimedJob) -> datetime:
    """How far back this run should look.

    An explicit ``posted_after`` in the payload wins, which is what a
    backfill uses. Otherwise a fixed lookback, deliberately generous:
    Amazon restates transactions after they post, and re-reading one costs
    nothing because ingest upserts on its id, while missing one costs a
    wrong total that nothing will ever correct.
    """
    given = job.payload.get("posted_after")
    if isinstance(given, str):
        return datetime.fromisoformat(given.replace("Z", "+00:00"))
    return datetime.now(UTC) - DEFAULT_LOOKBACK


def register_all(
    register: Callable[[str], Callable[[Handler], Handler]],
    *,
    lwa: LwaClient,
    keyring: Keyring,
    http: httpx.Client,
    factory: SessionFactory,
) -> None:
    """Attach every handler this worker can run to a registry."""
    register(SETTLEMENT_FETCH)(
        settlement_fetch(lwa=lwa, keyring=keyring, http=http, factory=factory)
    )
