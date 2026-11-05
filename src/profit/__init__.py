"""Real profit: what a sale left behind once everything was paid.

Three questions this answers that no single Amazon page can:

* **What did the unit cost me?** Amazon never knows. It comes from
  :mod:`src.profit.costs`, built from your own cost file — see
  :mod:`src.profit.invoices`.
* **What did Amazon keep?** Commission, fulfilment, the closing fee, postage
  and fee VAT, itemised per SKU inside the Finances feed rather than stated
  as a period total. :mod:`src.profit.ledger` reads them.
* **What did the parcel cost?** A label bought through Amazon arrives as a
  separate transaction hours after the order. The ledger puts it back on the
  order it shipped, split across that order's lines by revenue.

Subscription and inbound transport are deliberately *not* pushed down onto
units. They are period overhead, and spreading them over whichever SKUs
happened to sell that month makes a slow month look like a bad product.
:class:`~src.profit.report.PeriodProfit` keeps them separate.
"""

from .costs import CostBasis, CostEntry, ResolvedCost, invoiced_costs
from .growth import (
    GrowthModel, Projection, SkuPosition, Spread, build_positions, model,
)
from .invoices import (
    CostFileError, Invoice, InvoiceLine, LandedCost,
    costs_from_file, landed_costs, read_cost_csv,
)
from .ledger import OrderLine, Overhead, Settlement, build_settlement
from .report import (
    ChannelProfit, PeriodProfit, SkuProfit, by_channel, by_sku,
    overhead_by_kind, period, summarise,
)

__all__ = [
    "CostBasis", "CostEntry", "ResolvedCost", "invoiced_costs",
    "OrderLine", "Overhead", "Settlement", "build_settlement",
    "CostFileError", "Invoice", "InvoiceLine", "LandedCost",
    "costs_from_file", "landed_costs", "read_cost_csv",
    "GrowthModel", "Projection", "SkuPosition", "Spread", "build_positions", "model",
    "ChannelProfit", "PeriodProfit", "SkuProfit",
    "by_channel", "by_sku", "overhead_by_kind", "period", "summarise",
]
