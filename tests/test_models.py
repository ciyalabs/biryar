"""Checks on the mapping itself, which need no database.

A SQLAlchemy relationship is not validated when it is declared. The mappers
configure lazily, on first ORM use, and a relationship with no usable join
condition raises only then. The profit tests never touch the ORM, so a broken
mapping passed the whole local suite and failed in CI against PostgreSQL —
for a reason that had nothing to do with PostgreSQL.

Forcing configuration here moves that class of error back to where it can be
caught in a second, anywhere.
"""

from __future__ import annotations

from sqlalchemy.orm import configure_mappers

import biryar.db.models  # noqa: F401
from biryar.db.base import Base, tenant_scoped_tables
from biryar.migrations import declared_tenant_tables


def test_every_mapping_configures() -> None:
    """Catches a relationship with no join condition, among other things."""
    configure_mappers()


def test_every_tenant_scoped_table_actually_carries_a_tenant_column() -> None:
    """The mixin marks a table; it does not add the column. Both are needed."""
    for name in tenant_scoped_tables():
        table = Base.metadata.tables[name]
        assert "tenant_id" in table.c, f"{name} is marked tenant-scoped but has no tenant_id"
        assert not table.c.tenant_id.nullable, f"{name}.tenant_id is nullable"
        assert table.c.tenant_id.foreign_keys, f"{name}.tenant_id has no foreign key"


def test_the_models_and_the_migrations_agree_on_which_tables_are_scoped() -> None:
    """Catches a new tenant-scoped model whose policy was never written.

    The models say which tables hold tenant data; the migrations say which
    ones were given a policy. They must be the same set, and this needs no
    database, so it fails in a second rather than after a container starts.
    """
    assert set(tenant_scoped_tables()) == declared_tenant_tables()


def test_every_relationship_has_a_resolvable_join() -> None:
    """Walk the relationships rather than trusting configure_mappers alone."""
    configure_mappers()
    for mapper in Base.registry.mappers:
        for rel in mapper.relationships:
            assert rel.primaryjoin is not None, (
                f"{mapper.class_.__name__}.{rel.key} has no join condition"
            )

