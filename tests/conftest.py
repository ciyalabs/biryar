"""Fixtures.

Most of this suite needs a real PostgreSQL. That is not laziness about
mocking: the thing under test *is* PostgreSQL behaviour. Row-level security,
``current_setting``, ``FORCE ROW LEVEL SECURITY`` and the enum types have no
equivalent in SQLite, so a test that ran against SQLite would prove the
opposite of what it claims — that the isolation works, on an engine that
cannot express it.

Set ``BIRYAR_TEST_DATABASE_URL`` to run them. Without it they skip, and the
pure-logic tests still run anywhere — but skipping is not the intended way
to work, it is the fallback. ``scripts/devdb.py`` starts a PostgreSQL with
no root and no Docker:

    python scripts/devdb.py start
    export BIRYAR_TEST_DATABASE_URL=$(python scripts/devdb.py url)
    pytest

A suite where most tests skip looks green while proving nothing about the
property the product most depends on, and a round trip through CI to learn
whether a policy works is slow enough that it stops being done.
"""

from __future__ import annotations

import os
import pathlib
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

import biryar.db.models  # noqa: F401
from biryar.config import Settings as BiryarSettings
from biryar.config import settings
from biryar.db.session import SessionFactory, session_factory

TEST_URL_ENV = "BIRYAR_TEST_DATABASE_URL"

#: The unprivileged role the application connects as. It must not own the
#: tables and must not hold BYPASSRLS, or the policies are decoration.
APP_ROLE = "biryar_app_test"
APP_PASSWORD = "biryar_app_test"


def _admin_url() -> str | None:
    return os.environ.get(TEST_URL_ENV)


@pytest.fixture(scope="session")
def admin_engine() -> Iterator[Engine]:
    """A connection as the owner, used to build the schema and the app role."""
    url = _admin_url()
    if not url:
        pytest.skip(f"{TEST_URL_ENV} is not set")
    engine = create_engine(url, future=True)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def schema(admin_engine: Engine) -> Iterator[None]:
    """Build the schema by running the real migrations.

    Deliberately ``alembic upgrade head`` rather than
    ``Base.metadata.create_all``. create_all builds what the models say,
    which means the migrations — the thing that will actually run against
    production — are never executed by the suite, and a migration can drift
    from its models for months without anyone noticing. Running them here
    makes every test in this file a test of the migration too.
    """
    from alembic import command
    from alembic.config import Config

    with admin_engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
        conn.execute(text(f"DROP ROLE IF EXISTS {APP_ROLE}"))
        conn.execute(
            text(f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{APP_PASSWORD}' NOBYPASSRLS")
        )
        conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}"))
        # Default privileges rather than a one-off grant, and set *before* the
        # migrations run. A plain GRANT ON ALL TABLES covers the tables that
        # exist at the moment it runs; the downgrade/upgrade round-trip test
        # drops every table and builds them again, and the rebuilt ones came
        # back with no privileges at all. Default privileges attach to tables
        # this role creates from now on, so a later migration's new table is
        # usable without anyone remembering to grant it.
        conn.execute(
            text(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}"
            )
        )
        # The same for functions, and for the same reason. A migration that
        # CREATE OR REPLACEs a SECURITY DEFINER function re-runs its REVOKE
        # FROM PUBLIC, so the round-trip test — which downgrades to base and
        # upgrades again — stripped the grants made after the first upgrade
        # and every later claim failed on permission rather than on anything
        # it was testing.
        conn.execute(
            text(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                f"GRANT EXECUTE ON FUNCTIONS TO {APP_ROLE}"
            )
        )

    root = pathlib.Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "src" / "biryar" / "migrations"))
    os.environ["BIRYAR_DATABASE_URL"] = _admin_url() or ""
    settings.cache_clear()
    command.upgrade(config, "head")

    # Belt and braces for the tables this first upgrade created. Default
    # privileges cover everything after it.
    #
    # The EXECUTE grant is what a real deployment must also do: the session
    # resolver is SECURITY DEFINER and revoked from PUBLIC by its migration,
    # so the application cannot authenticate anyone without it.
    with admin_engine.begin() as conn:
        conn.execute(
            text(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
                f"IN SCHEMA public TO {APP_ROLE}"
            )
        )
        for signature in (
            "public.biryar_resolve_session(text)",
            "public.biryar_claim_job(text, integer, text[])",
            "public.biryar_reclaim_stuck_jobs(interval)",
        ):
            conn.execute(text(f"GRANT EXECUTE ON FUNCTION {signature} TO {APP_ROLE}"))
    yield


@pytest.fixture(scope="session")
def app_engine(schema: None) -> Iterator[Engine]:
    """An engine connected as the unprivileged application role."""
    from sqlalchemy.engine import make_url

    from biryar.config import Settings
    from biryar.db.session import engine_for

    url = make_url(_admin_url()).set(username=APP_ROLE, password=APP_PASSWORD)
    # render_as_string, not str(). SQLAlchemy's URL.__str__ masks the password
    # as literal asterisks so a URL cannot be logged by accident, which means
    # str() of a URL you built yourself silently throws the password away and
    # the connection fails authentication for reasons that look like anything
    # but this.
    engine = engine_for(
        Settings(database_url=url.render_as_string(hide_password=False))
    )
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def factory(app_engine: Engine) -> SessionFactory:
    return session_factory(app_engine)


@pytest.fixture
def two_tenants(admin_engine: Engine) -> Iterator[tuple[uuid.UUID, uuid.UUID]]:
    """Two tenants, created as the owner because ``tenants`` has no policy."""
    a, b = uuid.uuid4(), uuid.uuid4()
    with admin_engine.begin() as conn:
        for tid, slug in ((a, f"a-{a.hex[:8]}"), (b, f"b-{b.hex[:8]}")):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, display_name, status) "
                    "VALUES (:id, :slug, :name, 'active')"
                ),
                {"id": tid, "slug": slug, "name": slug},
            )
    yield a, b
    with admin_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM tenants WHERE id = ANY(:ids)"), {"ids": [a, b]},
        )


def make_user(session: Session, tenant_id: uuid.UUID, email: str) -> uuid.UUID:
    """Insert a user through the application role, under whatever tenant is set."""
    from biryar.db.models import Role, User

    user = User(
        tenant_id=tenant_id,
        email=email,
        display_name=email.split("@")[0],
        role=Role.OWNER,
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user.id


#: An Amazon application that exists only in tests. The ids are shaped like
#: the real ones so that anything parsing them behaves the same.
TEST_AMAZON = {
    "amazon_application_id": "amzn1.sellerapps.app.test",
    "lwa_client_id": "amzn1.application-oa2-client.test",
    "lwa_client_secret": "test-client-secret",
    "amazon_redirect_uri": "https://biryar.test/v1/amazon/callback",
    "amazon_application_is_draft": True,
}


def settings_for_tests(**overrides: object) -> BiryarSettings:
    """Settings for a test app: a real sealing key, a fake Amazon application."""
    from biryar.config import Settings as BiryarSettings
    from biryar.crypto import Keyring

    values: dict[str, object] = {
        "database_url": "postgresql+psycopg://unused:unused@127.0.0.1:1/unused",
        "secret_key": f"v1:{Keyring.generate_key()}",
        **TEST_AMAZON,
        **overrides,
    }
    return BiryarSettings(**values)  # type: ignore[arg-type]


@pytest.fixture
def client(app_engine: Engine) -> Iterator[TestClient]:
    """The API, wired to the throwaway database.

    The engine is injected rather than read from the environment, so the app
    under test is the real one and nothing about it is configured by what
    happens to be exported in the shell.
    """
    from fastapi.testclient import TestClient

    from biryar.api import create_app

    with TestClient(create_app(settings_for_tests(), engine=app_engine)) as test_client:
        yield test_client
