"""The tenant boundary.

If one test in this repository has to keep working, it is this file. Every
other failure costs someone an afternoon; a failure here shows one seller
another seller's figures.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import ProgrammingError

from biryar.db.base import tenant_scoped_tables
from biryar.db.session import TENANT_GUC, SessionFactory, TenantContextError, tenant_session
from biryar.migrations import declared_tenant_tables

from .conftest import make_user

pytestmark = pytest.mark.postgres


def test_a_tenant_sees_only_its_own_rows(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """The whole product rests on this sentence being true."""
    a, b = two_tenants

    with tenant_session(factory, a) as s:
        make_user(s, a, "owner@a.example")
    with tenant_session(factory, b) as s:
        make_user(s, b, "owner@b.example")

    with tenant_session(factory, a) as s:
        emails = {row[0] for row in s.execute(text("SELECT email FROM users"))}

    assert emails == {"owner@a.example"}


def test_an_unfiltered_query_is_still_safe(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """A query with no WHERE clause is the realistic mistake.

    Application-level scoping fails exactly here: ``SELECT * FROM users``
    reads fine in review and returns the world. Under the policy it returns
    one tenant's rows, so forgetting the filter costs nothing.
    """
    a, b = two_tenants
    with tenant_session(factory, a) as s:
        make_user(s, a, "one@a.example")
        make_user(s, a, "two@a.example")
    with tenant_session(factory, b) as s:
        make_user(s, b, "one@b.example")

    with tenant_session(factory, b) as s:
        count = s.execute(text("SELECT count(*) FROM users")).scalar_one()

    assert count == 1


def test_a_session_with_no_tenant_sees_nothing(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Default-deny. An unset tenant is NULL, and NULL never equals a uuid."""
    a, _ = two_tenants
    with tenant_session(factory, a) as s:
        make_user(s, a, "owner@a.example")

    session = factory()
    try:
        count = session.execute(text("SELECT count(*) FROM users")).scalar_one()
    finally:
        session.close()

    assert count == 0


def test_a_tenant_cannot_write_into_another(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """WITH CHECK stops the write, not just the read.

    A policy with only USING lets a caller insert rows attributed to someone
    else and then be unable to see what they did, which is worse than either
    failure alone.
    """
    a, b = two_tenants

    with pytest.raises(ProgrammingError) as caught, tenant_session(factory, a) as s:
        make_user(s, b, "smuggled@b.example")

    assert "row-level security" in str(caught.value).lower()


def test_the_tenant_setting_does_not_outlive_its_transaction(
    factory: SessionFactory, two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Otherwise a pooled connection carries one tenant into the next request."""
    a, _ = two_tenants
    with tenant_session(factory, a) as s:
        make_user(s, a, "owner@a.example")

    session = factory()
    try:
        current = session.execute(
            text(f"SELECT current_setting('{TENANT_GUC}', true)")
        ).scalar_one()
    finally:
        session.close()

    assert current in (None, "")


def test_setting_the_tenant_for_the_whole_session_is_refused(
    factory: SessionFactory,
) -> None:
    """A bare SET outlives its transaction, so it is rejected."""
    session = factory()
    try:
        with pytest.raises(TenantContextError, match="whole session"):
            session.execute(text(f"SET {TENANT_GUC} = '{uuid.uuid4()}'"))
    finally:
        session.rollback()
        session.close()


def test_set_config_without_the_local_flag_is_refused(factory: SessionFactory) -> None:
    """The same mistake in the function spelling, which is easier to make.

    ``set_config`` takes the LOCAL flag as a third positional argument. It is
    one token, it is easy to get wrong, and getting it wrong is invisible
    until two tenants share a pooled connection.
    """
    session = factory()
    try:
        with pytest.raises(TenantContextError, match="third"):
            session.execute(
                text("SELECT set_config(:guc, :tenant, false)"),
                {"guc": TENANT_GUC, "tenant": str(uuid.uuid4())},
            )
    finally:
        session.rollback()
        session.close()


def test_every_tenant_scoped_model_has_a_live_policy(admin_engine: Engine) -> None:
    """Catches the model that was added without its policy.

    The migration freezes its own table list, so this is what notices when a
    new tenant-scoped model lands and nobody wrote the matching migration.
    """
    with admin_engine.connect() as conn:
        protected = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT tablename FROM pg_policies "
                    "WHERE schemaname = 'public' AND policyname = 'tenant_isolation'"
                )
            )
        }
        forced = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT relname FROM pg_class "
                    "WHERE relrowsecurity AND relforcerowsecurity"
                )
            )
        }

    expected = set(tenant_scoped_tables())

    # An empty expectation satisfies every assertion below it. That is not
    # hypothetical: the models were briefly excluded from the repository by a
    # stray gitignore rule, and a check written only as a subset test would
    # have reported success against a database with no policies at all.
    assert expected, "no tenant-scoped models are registered; check the imports"
    declared = declared_tenant_tables()
    assert expected == declared, (
        "the migrations' frozen table lists and the models disagree: "
        f"models={sorted(expected)} migrations={sorted(declared)}"
    )

    assert expected <= protected, f"no policy on: {sorted(expected - protected)}"
    assert expected <= forced, f"RLS not forced on: {sorted(expected - forced)}"


def test_the_application_role_cannot_bypass_policies(app_engine: Engine) -> None:
    """A role with BYPASSRLS, or one that owns the tables, makes all of this theatre."""
    with app_engine.connect() as conn:
        bypasses, is_super = conn.execute(
            text(
                "SELECT rolbypassrls, rolsuper FROM pg_roles "
                "WHERE rolname = current_user"
            )
        ).one()
        owns = conn.execute(
            text(
                "SELECT count(*) FROM pg_tables "
                "WHERE schemaname = 'public' AND tableowner = current_user"
            )
        ).scalar_one()

    assert not bypasses, "the application role holds BYPASSRLS"
    assert not is_super, "the application role is a superuser"
    assert owns == 0, "the application role owns tables, so it is exempt from their policies"
