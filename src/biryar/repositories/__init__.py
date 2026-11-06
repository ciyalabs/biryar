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
    role_of,
    sole_tenant,
    tenant_by_slug,
)
from .costs import forget_cost, load_cost_basis, record_costs
from .credentials import (
    active_credential,
    mark_needs_reauth,
    mark_refreshed,
    read_refresh_token,
    reseal_if_stale,
    store_credential,
)
from .jobs import (
    ClaimedJob,
    backoff,
    claim,
    complete,
    enqueue,
    fail,
    reclaim_stuck,
)
from .tenancy import SlugError, create_tenant, slugify, tenant_count
from .transactions import record_transactions, settlement_rows

__all__ = [
    "ClaimedJob",
    "IssuedSession",
    "ResolvedSession",
    "SlugError",
    "active_credential",
    "authenticate",
    "backoff",
    "claim",
    "complete",
    "create_tenant",
    "create_user",
    "enqueue",
    "fail",
    "forget_cost",
    "issue_session",
    "load_cost_basis",
    "mark_needs_reauth",
    "mark_refreshed",
    "normalise_email",
    "read_refresh_token",
    "reclaim_stuck",
    "record_costs",
    "record_transactions",
    "reseal_if_stale",
    "resolve_session",
    "revoke_all_sessions",
    "revoke_session",
    "role_of",
    "settlement_rows",
    "slugify",
    "sole_tenant",
    "store_credential",
    "tenant_by_slug",
    "tenant_count",
]
