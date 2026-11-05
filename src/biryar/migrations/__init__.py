"""Alembic migrations.

:func:`declared_tenant_tables` lets the tests check the models against what
the migrations actually did, without anyone maintaining a list by hand.
"""

from __future__ import annotations

import importlib
import pkgutil


def declared_tenant_tables() -> set[str]:
    """Every tenant-scoped table any revision declares.

    Each revision freezes its own ``TENANT_TABLES`` — a migration describes
    one step in history and must not change meaning when the models change.
    Collecting them here gives the current total without re-reading the
    models, so a test can compare the two and notice a model that landed
    without its policy.
    """
    from . import versions

    declared: set[str] = set()
    for info in pkgutil.iter_modules(versions.__path__):
        module = importlib.import_module(f"{versions.__name__}.{info.name}")
        declared.update(getattr(module, "TENANT_TABLES", ()))
    return declared
