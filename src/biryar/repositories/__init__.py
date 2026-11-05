"""The seam between the database and the domain.

Everything under :mod:`biryar.profit` is pure: it takes values and returns
values, and imports nothing that knows what a database is. That is what lets
the margin rules be tested in milliseconds on a machine with no PostgreSQL,
and it is worth protecting.

These modules are where rows become those values. They are the only place
that imports both.
"""

from .costs import forget_cost, load_cost_basis, record_costs
from .transactions import record_transactions, settlement_rows

__all__ = [
    "forget_cost",
    "load_cost_basis",
    "record_costs",
    "record_transactions",
    "settlement_rows",
]
