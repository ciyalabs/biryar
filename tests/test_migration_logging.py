"""Running a migration must not switch the application's logging off.

Alembic's generated ``env.py`` calls ``fileConfig(config.config_file_name)``,
and ``disable_existing_loggers`` defaults to **True**. Alembic runs
in-process here — the test fixtures upgrade a real database, and an operator
may call ``command.upgrade`` from their own script — so that default does
not stay inside Alembic. It reaches across and disables every logger that
already exists, which in this package means `biryar.worker`,
`biryar.jobs.runner` and everything else that reports what it is doing.

How it was found: a test asserting a secret was never logged passed while
the code deliberately logged it, because by then there was nothing left to
log with. A silenced logger and a correct program look identical from the
outside, which is what makes this worth a test of its own.
"""

from __future__ import annotations

import logging
import os
import pathlib

import pytest

from biryar.config import settings

pytestmark = pytest.mark.postgres

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_upgrading_leaves_existing_loggers_alone(admin_engine, caplog) -> None:
    """A logger made before the migration still works after it."""
    from alembic import command
    from alembic.config import Config

    logger = logging.getLogger("biryar.test.survivor")
    assert not logger.disabled, "precondition: the logger starts enabled"

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option(
        "script_location", str(ROOT / "src" / "biryar" / "migrations")
    )
    # env.py reads the URL from settings, as it does in production.
    os.environ["BIRYAR_DATABASE_URL"] = admin_engine.url.render_as_string(
        hide_password=False
    )
    settings.cache_clear()
    # Already at head from the fixture; this is a no-op upgrade whose only
    # job is to run env.py, which is where the logging call lives.
    command.upgrade(config, "head")

    assert not logger.disabled, (
        "running a migration disabled an existing logger — env.py is calling "
        "fileConfig without disable_existing_loggers=False"
    )

    with caplog.at_level("INFO", logger="biryar.test"):
        logger.info("still here")
    assert "still here" in caplog.text, (
        "the logger survived but its output no longer reaches a handler"
    )
