"""Alembic environment.

The URL comes from settings rather than from ``alembic.ini`` so that there is
exactly one place a connection string is configured, and it is not a file in
version control.
"""

from __future__ import annotations

import logging
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

import biryar.db.models  # noqa: F401  (registers every model on the metadata)
from biryar.config import settings
from biryar.db.base import Base

config = context.config

# Logging is Alembic's to configure only when nobody else has.
#
# ``fileConfig`` is global: it switches off every existing logger unless told
# otherwise, and it replaces the root handlers with the ones in alembic.ini.
# That is right for `alembic upgrade head` at a shell, where Alembic is the
# whole program. It is wrong in-process — the test fixtures upgrade a real
# database, and an operator may call ``command.upgrade`` from a script — where
# it reaches out of Alembic and reconfigures the host application: `biryar.*`
# loggers go quiet and their output stops reaching the handlers the host
# installed, with nothing to say why.
#
# How this was found: a test asserting a secret was never logged passed while
# the code deliberately logged it, because by then there was nothing left to
# log with. A silenced logger and a correct program look identical from
# outside, so the condition is checked rather than assumed, and
# ``disable_existing_loggers`` is pinned even on the CLI path.
if config.config_file_name is not None and not logging.getLogger().handlers:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

config.set_main_option("sqlalchemy.url", str(settings().database_url))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=str(settings().database_url),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
