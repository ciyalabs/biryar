"""Real profit: what a sale left behind once everything was paid.

Three questions this answers that no single Amazon page can:

* **What did the unit cost me?** Amazon never knows. It comes from
  :mod:`cockpit.src.profit.costs`, the owner's own landed cost per SKU.
* **What did Amazon keep?** Commission, FBA pick & pack, the closing fee —
  itemised per SKU inside the Finances feed, not just as a period total.
* **What did the parcel cost?** A label bought through Amazon is a separate
  transaction hours later. :mod:`cockpit.src.profit.ledger` puts it back on
  the order it shipped.

Subscription and FBA inbound transport are deliberately *not* pushed down
onto units. They are period overhead; spreading them over whichever SKUs
happened to sell that month makes a slow month look like a bad product.
:class:`~cockpit.src.profit.report.PeriodProfit` keeps them separate.
"""

from .costs import CostBasis, CostEntry, invoiced_costs
from .growth import (
    GrowthModel, Projection, SkuPosition, Spread, build_positions, model,
)
from .invoices import ImportReport, InvoiceLine, LandedCost, import_costs, read_invoice
from .ledger import OrderLine, Overhead, Settlement, build_settlement
from .sources import (
    GoodsInvoice, ResolvedCost, goods_invoices, resolve_costs, spend_by_vendor,
)
from .report import (
    ChannelProfit, PeriodProfit, SkuProfit, by_channel, by_sku,
    overhead_by_kind, period, summarise,
)

__all__ = [
    "CostBasis", "CostEntry", "invoiced_costs",
    "OrderLine", "Overhead", "Settlement", "build_settlement",
    "ImportReport", "InvoiceLine", "LandedCost", "import_costs", "read_invoice",
    "GrowthModel", "Projection", "SkuPosition", "Spread", "build_positions", "model",
    "GoodsInvoice", "ResolvedCost", "goods_invoices", "resolve_costs", "spend_by_vendor",
    "ChannelProfit", "PeriodProfit", "SkuProfit",
    "by_channel", "by_sku", "overhead_by_kind", "period", "summarise",
]
