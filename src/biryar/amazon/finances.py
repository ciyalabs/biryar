"""Reading the Finances feed.

The endpoint, its parameters and the field names here are taken from a
system that has been reading this feed against the live API, not from
guesswork. The one thing worth restating is what this deliberately does
*not* do: it does not interpret anything.

It fetches transactions and hands them over whole. Classifying a fee,
deciding what counts as revenue, spotting the payout twin of a sale — all of
that belongs to :mod:`biryar.profit.ledger`, reading rows back out of the
database. Doing it here would freeze today's understanding into the stored
data, and the understanding has been wrong before: seven fee paths dropped,
every deferred sale counted twice. Both were fixed by changing the reading.

## Regional hosts

A refresh token is bound to a region, so the host follows the marketplace
rather than being configured. Sending a European seller's token to the North
American host fails in a way that reads like a credential problem.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from ..db.models.credentials import Marketplace

logger = logging.getLogger(__name__)

PATH = "/finances/2024-06-19/transactions"

#: Which regional endpoint a marketplace's tokens work against.
ENDPOINT: dict[Marketplace, str] = {
    Marketplace.DE: "https://sellingpartnerapi-eu.amazon.com",
    Marketplace.UK: "https://sellingpartnerapi-eu.amazon.com",
    Marketplace.FR: "https://sellingpartnerapi-eu.amazon.com",
    Marketplace.IT: "https://sellingpartnerapi-eu.amazon.com",
    Marketplace.ES: "https://sellingpartnerapi-eu.amazon.com",
    Marketplace.NL: "https://sellingpartnerapi-eu.amazon.com",
    Marketplace.US: "https://sellingpartnerapi-na.amazon.com",
}

#: The calendar a marketplace's money counts on. Amazon posts timestamps in
#: UTC, and a sale posted either side of local midnight otherwise lands in
#: the wrong day's totals — which is the whole reason ``local_date`` is a
#: column rather than something derived at read time.
TIMEZONE: dict[Marketplace, str] = {
    Marketplace.DE: "Europe/Berlin",
    Marketplace.UK: "Europe/London",
    Marketplace.FR: "Europe/Paris",
    Marketplace.IT: "Europe/Rome",
    Marketplace.ES: "Europe/Madrid",
    Marketplace.NL: "Europe/Amsterdam",
    Marketplace.US: "America/Los_Angeles",
}

#: Stop after this many pages. A loop that follows nextToken forever is one
#: malformed response away from running until someone notices.
MAX_PAGES = 200


class SpApiError(RuntimeError):
    """The call failed in a way that may well work if tried again."""


class Throttled(SpApiError):
    """Amazon asked us to slow down. Explicitly retryable."""


class SpApiRefused(RuntimeError):
    """The call will not succeed as made. Do not retry it unchanged."""


@dataclass(frozen=True)
class FinancesClient:
    """Reads transactions for one tenant and one marketplace."""

    access_token: str
    marketplace: Marketplace
    client: httpx.Client
    max_pages: int = MAX_PAGES

    @property
    def base_url(self) -> str:
        return ENDPOINT[self.marketplace]

    def transactions(self, posted_after: datetime) -> Iterator[dict[str, Any]]:
        """Every transaction posted since ``posted_after``, following pages.

        A generator so a long window does not have to be held in memory at
        once, and so a caller can stop early.
        """
        token: str | None = None
        for page in range(self.max_pages):
            params: dict[str, str] = {
                "postedAfter": posted_after.astimezone(UTC).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                "marketplaceId": self.marketplace.value,
            }
            if token:
                params["nextToken"] = token

            payload = self._get(params)
            transactions = payload.get("transactions") or []
            yield from transactions

            token = payload.get("nextToken")
            if not token:
                return

            logger.debug(
                "finances: page %d returned %d transactions, following nextToken",
                page + 1, len(transactions),
            )

        raise SpApiError(
            f"stopped after {self.max_pages} pages; the feed is still offering "
            "more, which is more likely a bug than a real result"
        )

    def _get(self, params: dict[str, str]) -> dict[str, Any]:
        try:
            response = self.client.get(
                f"{self.base_url}{PATH}",
                params=params,
                headers={"x-amz-access-token": self.access_token},
            )
        except httpx.HTTPError as exc:
            raise SpApiError(f"could not reach the Selling Partner API: {exc}") from exc

        if response.status_code == 429:
            raise Throttled("Amazon is throttling this application")
        if response.status_code in (401, 403):
            # The access token was rejected. That is usually a stale token
            # rather than a revoked authorisation, and the caller decides
            # which by trying to refresh it.
            raise SpApiRefused(f"the Selling Partner API refused: {response.status_code}")
        if response.status_code >= 500:
            raise SpApiError(f"the Selling Partner API returned {response.status_code}")
        if response.status_code >= 400:
            raise SpApiRefused(
                f"the Selling Partner API rejected the request: "
                f"{response.status_code} {response.text[:200]}"
            )

        try:
            body: dict[str, Any] = response.json()
        except ValueError as exc:
            raise SpApiError("the Selling Partner API returned something that is not JSON") from exc

        payload: dict[str, Any] = body.get("payload") or {}
        return payload


def to_row(raw: dict[str, Any], marketplace: Marketplace) -> dict[str, Any]:
    """One transaction, in the shape ``record_transactions`` stores.

    Only the handful of fields worth having as columns are lifted out; the
    rest stays in the payload. Nothing is interpreted.

    ``local_date`` is the marketplace's own calendar day, not the UTC one.
    Amazon posts in UTC; a sale at 23:30 in Berlin is already the next day
    there, and using the UTC date would file it under the wrong day's
    totals — quietly, and only for sales near midnight, which is the worst
    way to be wrong.
    """
    related = {
        identifier["relatedIdentifierName"]: identifier["relatedIdentifierValue"]
        for identifier in raw.get("relatedIdentifiers") or []
    }
    posted_at = datetime.fromisoformat(raw["postedDate"].replace("Z", "+00:00"))

    return {
        "transaction_id": raw["transactionId"],
        "transaction_type": raw["transactionType"],
        "status": raw.get("transactionStatus", "") or "",
        "posted_at": posted_at,
        "local_date": posted_at.astimezone(ZoneInfo(TIMEZONE[marketplace])).date(),
        "order_id": related.get("ORDER_ID", "") or "",
        "payload": raw,
    }
