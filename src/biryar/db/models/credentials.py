"""Each tenant's Amazon credentials, sealed at rest.

## What is stored and what is not

A refresh token is a long-lived bearer credential for someone's selling
account. Held in the clear it is worth more than the database around it, so
only ciphertext is stored and the key lives outside the database entirely —
in a secret manager on a hosted install, in a file the operator controls on a
self-hosted one. A database dump is then not a set of working credentials.

Access tokens are **not** stored at all. They live for an hour, they are
derivable from the refresh token at any time, and a short-lived secret
written to disk is a long-lived secret with extra steps.

## Why the rotation columns exist

A refresh token can be revoked by Amazon, by the seller, or by reauthorising
the application. When that happens every job for that tenant begins failing
with an error that looks like a bug. ``last_refreshed_at`` and
``last_error`` exist so the failure reads as "this tenant must reconnect"
rather than sending someone into the scheduler's logs.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Enum, LargeBinary, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base, LongText, ShortText, TenantScoped, TimestampMixin, tenant_fk


class Marketplace(StrEnum):
    """The Amazon marketplaces Biryar understands.

    A short list on purpose. Each one implies a currency, a VAT regime and a
    set of fee paths, and claiming support for a marketplace whose fee
    structure has never been read against real settlement data would be a
    claim the product cannot keep.
    """

    DE = "A1PA6795UKMFR9"
    UK = "A1F83G8C2ARO7P"
    FR = "A13V1IB3VIYZZH"
    IT = "APJ6JRA9NG5V4"
    ES = "A1RKKUPIHCS9HS"
    NL = "A1805IZSGTT6HS"
    US = "ATVPDKIKX0DER"


class CredentialStatus(StrEnum):
    ACTIVE = "active"
    #: Amazon refused the last refresh. The tenant must reauthorise.
    NEEDS_REAUTH = "needs_reauth"
    REVOKED = "revoked"


class AmazonCredential(Base, TenantScoped, TimestampMixin):
    """One tenant's authorisation against one Amazon marketplace."""

    __tablename__ = "amazon_credentials"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[uuid.UUID] = tenant_fk()

    marketplace: Mapped[Marketplace] = mapped_column(
        Enum(Marketplace, name="marketplace", native_enum=True), nullable=False,
    )
    #: Amazon's identifier for the selling account. Not a secret, and useful
    #: in support: it is what a seller can read off their own Seller Central.
    selling_partner_id: Mapped[ShortText] = mapped_column(nullable=False)

    #: The sealed refresh token. Bytes, not text, because the ciphertext is
    #: binary and base64-ing it into a varchar only makes it bigger and
    #: tempts someone to look at it.
    refresh_token_sealed: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    #: Which key sealed it, so the key can be rotated without reauthorising
    #: every tenant: new writes use the current key, old rows are re-sealed
    #: in the background, and this column says which is which.
    key_id: Mapped[str] = mapped_column(String(64), nullable=False)

    status: Mapped[CredentialStatus] = mapped_column(
        Enum(CredentialStatus, name="credential_status", native_enum=True),
        nullable=False,
        default=CredentialStatus.ACTIVE,
    )

    scopes: Mapped[LongText] = mapped_column(nullable=False, default="")
    last_refreshed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(512), nullable=True)

    __table_args__ = (
        # One authorisation per marketplace per tenant. Reauthorising replaces
        # the row rather than accumulating credentials nobody can tell apart.
        UniqueConstraint(
            "tenant_id", "marketplace", name="uq_amazon_credentials_tenant_id_marketplace",
        ),
    )

    def __repr__(self) -> str:
        # Never render the sealed token, even as a length: an object that
        # prints its own secret ends up in a log the first time something
        # unexpected happens.
        return (
            f"<AmazonCredential tenant={self.tenant_id} "
            f"marketplace={self.marketplace.name} status={self.status.value}>"
        )
