"""The migrations and the models must describe the same database.

They drift the moment someone edits a model and forgets the migration, and
the drift is invisible until a deployment runs against a schema that does not
have the column the code reads. This asserts they agree, so the gap is caught
in CI rather than by production.
"""

from __future__ import annotations

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, text

import biryar.db.models  # noqa: F401
from biryar.db.base import Base

pytestmark = pytest.mark.postgres


def test_the_migrations_reproduce_the_models(admin_engine: Engine, schema: None) -> None:
    """``alembic upgrade head`` must leave exactly what the models declare."""
    with admin_engine.connect() as conn:
        context = MigrationContext.configure(
            conn, opts={"compare_type": True, "compare_server_default": True},
        )
        diff = compare_metadata(context, Base.metadata)

    # Alembic reports tables it sees but the models do not define. Its own
    # bookkeeping table is one of those, and is not drift.
    drift = [
        entry for entry in diff
        if not (
            isinstance(entry, tuple)
            and len(entry) == 2
            and entry[0] == "remove_table"
            and getattr(entry[1], "name", None) == "alembic_version"
        )
    ]

    assert drift == [], f"models and migrations disagree:\n{drift}"


def test_every_revision_can_be_undone(
    admin_engine: Engine, app_engine: Engine, schema: None,
) -> None:
    """A migration that cannot be reversed is a migration you cannot deploy twice.

    Downgrades are the thing nobody exercises until the night they are
    needed, which is the worst possible moment to discover that one of them
    drops a table a policy still depends on.
    """
    import os
    import pathlib

    from alembic import command
    from alembic.config import Config

    root = pathlib.Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "src" / "biryar" / "migrations"))
    os.environ["BIRYAR_DATABASE_URL"] = str(admin_engine.url)

    command.downgrade(config, "base")
    command.upgrade(config, "head")

    # Rebuilding the schema changes types that pooled connections have
    # already compiled prepared statements against, and PostgreSQL refuses
    # those with "cached plan must not change result type". Nothing in
    # production downgrades under a live pool, so this is the test's mess to
    # clean up rather than a reason to weaken the application.
    app_engine.dispose()


def test_check_constraints_are_named_as_the_models_name_them(
    admin_engine: Engine, schema: None,
) -> None:
    """Alembic does not compare CHECK constraints, so nothing else would.

    The migrations passed names that already carried the convention's
    prefix, and the convention added it again: the live constraint was
    called ck_user_sessions_ck_user_sessions_expires_after_issue. It
    enforced the right rule, so nothing failed — but no code deriving the
    name from the models could find it, which is what a later ALTER or a
    downgrade does.

    The drift test above cannot see this because compare_metadata ignores
    CHECK constraints entirely. This reads the catalogue instead.
    """
    expected = {
        constraint.name
        for table in Base.metadata.tables.values()
        for constraint in table.constraints
        if isinstance(getattr(constraint, "name", None), str)
        and str(constraint.name).startswith("ck_")
    }

    with admin_engine.connect() as conn:
        live = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT conname FROM pg_constraint c "
                    "JOIN pg_namespace n ON n.oid = c.connamespace "
                    "WHERE c.contype = 'c' AND n.nspname = 'public' "
                    "AND conname LIKE 'ck\\_%'"
                )
            )
        }

    missing = expected - live
    assert not missing, (
        "the database has no constraint by these names, so the migrations "
        f"and the models disagree: {sorted(missing)}"
    )
