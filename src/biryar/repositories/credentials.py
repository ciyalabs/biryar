"""Storing and reading a tenant's Amazon authorisation."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..crypto import Keyring, Sealed
from ..db.models.credentials import AmazonCredential, CredentialStatus, Marketplace

logger = logging.getLogger(__name__)


def store_credential(
    session: Session,
    tenant_id: uuid.UUID,
    *,
    marketplace: Marketplace,
    selling_partner_id: str,
    refresh_token: str,
    keyring: Keyring,
    scopes: str = "",
) -> AmazonCredential:
    """Record an authorisation, replacing any this tenant already had.

    Reauthorising overwrites rather than accumulating. Two live credentials
    for one marketplace is a state where which one is used depends on row
    order, and the one that stops working is unpredictable.
    """
    sealed = keyring.seal(refresh_token)

    existing = session.execute(
        select(AmazonCredential).where(
            AmazonCredential.tenant_id == tenant_id,
            AmazonCredential.marketplace == marketplace,
        )
    ).scalar_one_or_none()

    if existing is None:
        existing = AmazonCredential(tenant_id=tenant_id, marketplace=marketplace)
        session.add(existing)

    existing.selling_partner_id = selling_partner_id
    existing.refresh_token_sealed = sealed.ciphertext
    existing.key_id = sealed.key_id
    existing.scopes = scopes
    existing.status = CredentialStatus.ACTIVE
    existing.last_error = None
    session.flush()

    logger.info(
        "credentials: tenant %s authorised for %s", tenant_id, marketplace.name,
    )
    return existing


def read_refresh_token(credential: AmazonCredential, keyring: Keyring) -> str:
    """The plaintext refresh token. Held as briefly as possible."""
    return keyring.unseal(
        Sealed(ciphertext=credential.refresh_token_sealed, key_id=credential.key_id)
    )


def active_credential(
    session: Session, tenant_id: uuid.UUID, marketplace: Marketplace,
) -> AmazonCredential | None:
    """This tenant's usable authorisation, if it has one."""
    return session.execute(
        select(AmazonCredential).where(
            AmazonCredential.tenant_id == tenant_id,
            AmazonCredential.marketplace == marketplace,
            AmazonCredential.status == CredentialStatus.ACTIVE,
        )
    ).scalar_one_or_none()


def mark_refreshed(credential: AmazonCredential) -> None:
    """Record a successful token refresh."""
    credential.last_refreshed_at = datetime.now(UTC)
    credential.last_error = None


def mark_needs_reauth(credential: AmazonCredential, reason: str) -> None:
    """Record that Amazon refused, and why.

    The status exists so that the failure reads as "this seller must
    reconnect" on their own screen, rather than as a job that keeps failing
    with an authorisation error in a log nobody is watching. A credential
    that has been revoked at Amazon's end will never recover on its own, so
    retrying it forever is just noise plus rate limiting.
    """
    credential.status = CredentialStatus.NEEDS_REAUTH
    credential.last_error = reason[:512]
    logger.warning(
        "credentials: tenant %s must reauthorise %s: %s",
        credential.tenant_id, credential.marketplace.name, reason[:200],
    )


def reseal_if_stale(credential: AmazonCredential, keyring: Keyring) -> bool:
    """Move a credential onto the current key. Returns whether it moved.

    Called opportunistically whenever a credential is read, so that a key
    rotation completes by itself as tenants are used rather than needing a
    migration that holds every row at once.
    """
    sealed = Sealed(
        ciphertext=credential.refresh_token_sealed, key_id=credential.key_id,
    )
    if not keyring.needs_resealing(sealed):
        return False

    resealed = keyring.seal(keyring.unseal(sealed))
    credential.refresh_token_sealed = resealed.ciphertext
    credential.key_id = resealed.key_id
    return True
