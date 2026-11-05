"""The seam between the database and the domain.

Everything under :mod:`biryar.profit` is pure: it takes values and returns
values, and imports nothing that knows what a database is. That is what lets
the margin rules be tested in milliseconds on a machine with no PostgreSQL,
and it is worth protecting.

These modules are where rows become those values. They are the only place
that imports both.
"""

from .auth import (
    IssuedSession,
    ResolvedSession,
    authenticate,
    create_user,
    issue_session,
    normalise_email,
    resolve_session,
    revoke_all_sessions,
    revoke_session,
    sole_tenant,
    tenant_by_slug,
)
from .costs import forget_cost, load_cost_basis, record_costs
from .transactions import record_transactions, settlement_rows

__all__ = [
    "IssuedSession",
    "ResolvedSession",
    "authenticate",
    "create_user",
    "forget_cost",
    "issue_session",
    "load_cost_basis",
    "normalise_email",
    "record_costs",
    "record_transactions",
    "resolve_session",
    "revoke_all_sessions",
    "revoke_session",
    "settlement_rows",
    "sole_tenant",
    "tenant_by_slug",
]
