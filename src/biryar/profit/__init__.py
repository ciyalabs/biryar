"""Real profit: what a sale left behind once everything was paid.

Three questions this answers that no single Amazon page can:

* **What did the unit cost me?** Amazon never knows. It comes from
  :mod:`biryar.profit.costs`, built from your own cost file — see
  :mod:`biryar.profit.invoices`. Nothing in this package reads a database;
  :mod:`biryar.repositories` is what loads these values from one.
* **What did Amazon keep?** Commission, fulfilment, the closing fee, postage
  and fee VAT, itemised per SKU inside the Finances feed rather than stated
  as a period total. :mod:`biryar.profit.ledger` reads them.
* **What did the parcel cost?** A label bought through Amazon arrives as a
  separate transaction hours after the order. The ledger puts it back on the
  order it shipped, split across that order's lines by revenue.

Subscription and inbound transport are deliberately *not* pushed down onto
units. They are period overhead, and spreading them over whichever SKUs
happened to sell that month makes a slow month look like a bad product.
:class:`~biryar.profit.report.PeriodProfit` keeps them separate.
"""

from .costs import CostBasis, CostEntry, ResolvedCost
from .growth import (
    GrowthModel,
    Projection,
    SkuPosition,
    Spread,
    build_positions,
    model,
)
from .invoices import (
    CostFile,
    CostFileError,
    Invoice,
    InvoiceLine,
    LandedCost,
    SkippedRow,
    costs_from_file,
    landed_costs,
    read_cost_csv,
    read_cost_rows,
)
from .ledger import OrderLine, Overhead, Settlement, build_settlement
from .report import (
    ChannelProfit,
    PeriodProfit,
    SkuProfit,
    by_channel,
    by_sku,
    overhead_by_kind,
    period,
    summarise,
)
from .settlement_file import (
    SettlementFile,
    SettlementFileError,
    read_settlement_file,
)

__all__ = [
    "ChannelProfit",
    "CostBasis",
    "CostEntry",
    "CostFile",
    "CostFileError",
    "GrowthModel",
    "Invoice",
    "InvoiceLine",
    "LandedCost",
    "OrderLine",
    "Overhead",
    "PeriodProfit",
    "Projection",
    "ResolvedCost",
    "Settlement",
    "SettlementFile",
    "SettlementFileError",
    "SkippedRow",
    "SkuPosition",
    "SkuProfit",
    "Spread",
    "build_positions",
    "build_settlement",
    "by_channel",
    "by_sku",
    "costs_from_file",
    "invoiced_costs",
    "landed_costs",
    "model",
    "overhead_by_kind",
    "period",
    "read_cost_csv",
    "read_cost_rows",
    "read_settlement_file",
    "summarise",
]
