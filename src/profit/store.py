"""The one table the profit package needs of its own.

:class:`~src.profit.costs.CostBasis` reads dated landed costs out of
``sku_costs``. Nothing else in this package touches a database: the ledger
takes rows you hand it, and the report and growth models are pure functions
over what the ledger produced.

Keeping the DDL here rather than in a platform-wide schema is deliberate. It
means the profit calculation can be lifted into any project, pointed at a
fresh SQLite file, and used on its own.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SKU_COSTS_DDL = """
CREATE TABLE IF NOT EXISTS sku_costs (
    sku             TEXT NOT NULL,
    effective_from  TEXT NOT NULL,      -- ISO date
    unit_cost       TEXT NOT NULL,      -- Decimal as text, positive
    source          TEXT NOT NULL DEFAULT '',
    note            TEXT NOT NULL DEFAULT '',
    entered_by      TEXT NOT NULL,
    entered_at      TIMESTAMP NOT NULL,
    PRIMARY KEY (sku, effective_from)
);
"""


def init_costs_db(path: Path | str | None = None) -> sqlite3.Connection:
    """Open (or create) a database holding the cost table, and return it.

    ``None`` gives an in-memory database, which is what the tests use.
    """
    conn = sqlite3.connect(":memory:" if path is None else str(path))
    conn.executescript(SKU_COSTS_DDL)
    conn.commit()
    return conn
