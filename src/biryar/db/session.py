"""The engine, the session, and the only sanctioned way to touch tenant data.

## The one rule

Every read or write of tenant data happens inside :func:`tenant_session`.
There is no second way in, and that is deliberate.

Application-level scoping — remembering ``.where(Model.tenant_id == me)`` on
every query — fails the way all discipline-based safety fails. It works until
someone writes the two hundredth query, or a join, or a raw ``text()`` for a
report, and the failure is silent: the query succeeds and returns another
seller's revenue. For a platform whose entire value is that the numbers are
trustworthy, a silent cross-tenant read is the worst available bug.

So the boundary lives in PostgreSQL. Every tenant-scoped table carries a
row-level-security policy that compares ``tenant_id`` against
``current_setting('app.tenant_id')``. :func:`tenant_session` sets that for
the transaction and clears it at the end. A query that forgets to filter
returns nothing; a query run with no tenant set returns nothing, because
``current_setting`` of an unset key is NULL and NULL never equals a uuid.

Two things keep it honest, both checked by the tests:

* The application connects as a role that is **not** the table owner and has
  **no** ``BYPASSRLS``. PostgreSQL exempts a table's owner from its own
  policies unless ``FORCE ROW LEVEL SECURITY`` is set, so the migration sets
  that too, and belt-and-braces beats either alone.
* The setting is made transaction-local, so it cannot outlive its
  transaction and leak into whatever the pooled connection serves next.

``set_config(name, value, is_local)`` is used rather than ``SET LOCAL``.
``SET`` is a utility statement: PostgreSQL does not plan it, so it cannot
take a bind parameter, and ``SET LOCAL app.tenant_id = $1`` is a syntax
error. Interpolating the uuid into the string instead would work and would
also be the one place in the codebase building SQL by concatenation, which
is not a habit worth starting for the sake of one statement. ``set_config``
is an ordinary function, takes parameters, and its third argument is exactly
the ``LOCAL`` flag.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings, settings

type SessionFactory = sessionmaker[Session]

#: The GUC the row-level-security policies read. Namespaced because PostgreSQL
#: only permits custom settings that carry a prefix.
TENANT_GUC = "app.tenant_id"


class TenantContextError(RuntimeError):
    """Raised when tenant data is reached without a tenant in scope."""


def engine_for(config: Settings | None = None) -> Engine:
    """Build an engine for ``config``, defaulting to the process settings."""
    config = config or settings()
    engine = create_engine(
        str(config.database_url),
        echo=config.echo_sql,
        pool_size=config.pool_size,
        max_overflow=config.max_overflow,
        pool_pre_ping=True,
        future=True,
    )
    _forbid_stray_tenant_setting(engine)
    return engine


def session_factory(engine: Engine) -> SessionFactory:
    """A session factory bound to ``engine``.

    Sessions are created with ``expire_on_commit=False`` so that a caller can
    still read an object's attributes after the transaction that loaded it has
    closed. The alternative issues a fresh SELECT outside the tenant
    transaction, which under row-level security finds nothing and raises a
    confusing ``ObjectDeletedError`` rather than the real problem.
    """
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def tenant_session(
    factory: SessionFactory,
    tenant_id: uuid.UUID,
) -> Iterator[Session]:
    """Open a transaction scoped to one tenant.

    The tenant is set transaction-locally, so it is gone when the transaction
    ends, whether it commits or rolls back. The connection can then be
    returned to the pool without carrying one tenant's identity into the next
    request that borrows it.
    """
    if not isinstance(tenant_id, uuid.UUID):                 # pragma: no cover
        raise TenantContextError(
            f"tenant_id must be a UUID, got {type(tenant_id).__name__}"
        )

    session = factory()
    try:
        # begin() is implicit on first use, so this lands inside the
        # transaction. The third argument is the LOCAL flag.
        session.execute(
            text("SELECT set_config(:guc, :tenant, true)"),
            {"guc": TENANT_GUC, "tenant": str(tenant_id)},
        )
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def admin_session(factory: SessionFactory) -> Iterator[Session]:
    """A session with no tenant set, for platform-level tables only.

    Tenant-scoped tables are invisible here, which is the point: provisioning
    a tenant, reading the tenant list, or running a migration has no business
    seeing anyone's sales. If a query through this session returns nothing
    when you expected rows, the table is tenant-scoped and you want
    :func:`tenant_session`.
    """
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


#: A false third argument to ``set_config``, which makes the setting last for
#: the whole session rather than the transaction.
#:
#: Matched as "a falsey final argument before a closing paren" rather than by
#: walking the call's own parentheses. The first attempt used ``[^)]*`` to
#: scan the arguments, which stops dead at the first ``)`` — and a driver
#: renders a named parameter as ``%(guc)s``, so the pattern never reached the
#: argument it was looking for and the guard silently passed everything.
_FALSEY_LAST_ARGUMENT = re.compile(r",\s*(?:false|'f'|0)\s*\)", re.IGNORECASE)


def _forbid_stray_tenant_setting(engine: Engine) -> None:
    """Refuse any session-lifetime setting of the tenant GUC.

    A tenant id that survives its transaction rides the pooled connection
    into whatever request borrows it next, which is a cross-tenant read
    waiting to happen. Both spellings of the mistake are easy to write and
    invisible in review, so both are rejected here rather than documented
    against:

    * ``SET app.tenant_id = ...`` without ``LOCAL``
    * ``set_config('app.tenant_id', ..., false)``
    """

    @event.listens_for(engine, "before_cursor_execute")
    def _check(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        normalised = " ".join(statement.lower().split())
        mentions_tenant = TENANT_GUC in normalised or (
            parameters is not None and TENANT_GUC in str(parameters)
        )
        if not mentions_tenant:
            return

        if normalised.startswith("set ") and not normalised.startswith("set local "):
            raise TenantContextError(
                f"{TENANT_GUC} must not be set for the whole session: it would "
                "outlive its transaction and leak across pooled connections. "
                "Use tenant_session()."
            )
        if "set_config" in normalised and _FALSEY_LAST_ARGUMENT.search(normalised):
            raise TenantContextError(
                f"set_config for {TENANT_GUC} must pass true as its third "
                "argument so the setting is transaction-local. "
                "Use tenant_session()."
            )
