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
* ``SET LOCAL`` is used rather than ``SET``, so the value cannot outlive its
  transaction and leak into whatever the pooled connection serves next.
"""

from __future__ import annotations

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

    The tenant is set with ``SET LOCAL``, so it is bound to this transaction
    and gone when the transaction ends, whether it commits or rolls back. The
    connection can then be returned to the pool without carrying one tenant's
    identity into the next request that borrows it.
    """
    if not isinstance(tenant_id, uuid.UUID):                 # pragma: no cover
        raise TenantContextError(
            f"tenant_id must be a UUID, got {type(tenant_id).__name__}"
        )

    session = factory()
    try:
        # begin() is implicit on first use; issue the SET LOCAL inside it.
        session.execute(
            text(f"SET LOCAL {TENANT_GUC} = :tenant"),
            {"tenant": str(tenant_id)},
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


def _forbid_stray_tenant_setting(engine: Engine) -> None:
    """Refuse a session-lifetime ``SET`` of the tenant GUC.

    ``SET app.tenant_id`` without ``LOCAL`` survives the transaction and rides
    the pooled connection into the next request, which is a cross-tenant read
    waiting to happen. It is easy to write by accident and invisible in
    review, so it is rejected here rather than documented against.
    """

    @event.listens_for(engine, "before_cursor_execute")
    def _check(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        normalised = " ".join(statement.lower().split())
        if (
            TENANT_GUC in normalised
            and normalised.startswith("set ")
            and not normalised.startswith("set local ")
        ):
            raise TenantContextError(
                f"{TENANT_GUC} must be set with SET LOCAL so it cannot "
                "outlive its transaction and leak across pooled "
                "connections. Use tenant_session()."
            )
