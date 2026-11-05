"""Every table in the platform.

Imported for its side effect as much as its names: a model that is never
imported is never registered on the metadata, so Alembic does not see it and
:func:`~biryar.db.base.tenant_scoped_tables` does not know it needs a
row-level-security policy. Add a model here the moment you create it.
"""

from .credentials import AmazonCredential, CredentialStatus, Marketplace
from .tenancy import Role, Tenant, TenantStatus, User, UserSession

__all__ = [
    "AmazonCredential",
    "CredentialStatus",
    "Marketplace",
    "Role",
    "Tenant",
    "TenantStatus",
    "User",
    "UserSession",
]
