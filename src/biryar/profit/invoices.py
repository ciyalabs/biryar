"""Buy prices, read from a cost file you control.

What a unit cost is the one number Amazon never knows, and it is the number
every margin depends on. Biryar will not invent it.

## Why this matters more than it looks

A cost derived backwards from a price is not a cost. If the only thing on
record is a Seller Central floor, it is tempting to work the cost out as
``floor x (1 - referral)`` and move on. That makes break-even land exactly on
the floor it came from, which is to say it proves nothing and hides the fact
that it proves nothing. An invented number and a real one are the same shape.

An invoice line cannot be invented. It has a quantity, a printed unit price,
and a document number to check it against.

## What "landed" means here

The price on the line, plus that line's share of the delivery charged on the
same invoice, plus any VAT you cannot reclaim.

Delivery is divided by the number of **units** on the invoice, not by the
number of lines. A 3.95 delivery across five jars is 0.79 a jar, and leaving
it out is how a 6.74 cost passes for 5.95.

The VAT term exists because not every seller can reclaim input VAT. A seller
on a small-business exemption pays the gross and never gets the 19% back, so
for them the gross is the cost. A seller who reclaims should set
``prices_include_vat`` and ``vat_rate`` so that no VAT is added at all.

## The cost file

One CSV, one row per invoice line. Group rows into invoices with
``vendor`` + ``doc_number``.

```
vendor,doc_number,doc_date,name,qty,unit_price,sku,ean,article_no,shipping,prices_include_vat,vat_rate
acme,INV-1001,2026-09-30,Widget Blue,5,8.40,MY-SKU-1,4001234567890,W-100,3.95,false,0.19
acme,INV-1001,2026-09-30,Widget Red,3,9.10,MY-SKU-2,4001234567891,W-101,3.95,false,0.19
```

`shipping`, `prices_include_vat` and `vat_rate` describe the whole invoice, so
repeat them on each of its rows. The first row of an invoice wins; later
disagreements are ignored rather than averaged, because an averaged delivery
charge is another invented number.

`sku` is **your own Amazon SKU**, and it is the column that makes a cost
usable. The Finances feed reports the seller SKU and nothing else: a
supplier's EAN or article number appears nowhere in it, so a cost keyed on
one of those is stored correctly and never found by a sale. It is optional
because an EAN still identifies the product unambiguously, which a later
release can resolve to a SKU — but until then it changes no margin, and
:mod:`biryar.api.v1.costs` reports each one rather than letting a working
import look like a broken ledger.

`ean` and `article_no` are the fallbacks, in that order. Prefer an EAN where
the vendor prints one; a line with neither and no SKU can only be matched by
name, which is fragile.

## What is deliberately not here

**Prep and fulfilment-service invoices.** A prep service bills a batch, not a
line, and nothing on the invoice says which units a batch covered. Allocating
it would put an invented number back into the one place that is supposed to be
free of them, so these stay visible as an unallocated cost instead.
"""

from __future__ import annotations

import csv
import io
import logging
from collections import OrderedDict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

logger = logging.getLogger(__name__)

CENT = Decimal("0.01")

#: Default VAT rate applied when an invoice prices net and the seller cannot
#: reclaim input VAT. Override per invoice in the cost file.
DEFAULT_VAT_RATE = Decimal("0.19")

#: Columns a cost file must carry. Everything else is optional.
REQUIRED_COLUMNS: frozenset[str] = frozenset({
    "vendor", "doc_number", "doc_date", "name", "qty", "unit_price",
})


class CostFileError(ValueError):
    """The cost file could not be read as invoice lines."""


@dataclass(frozen=True)
class InvoiceLine:
    """One product line, exactly as the invoice prints it."""

    vendor: str
    doc_number: str
    doc_date: date
    name: str
    qty: int
    #: The unit price as printed. Whether it includes VAT is stated per
    #: invoice, not guessed from the vendor.
    unit_price: Decimal
    #: Your own Amazon SKU for this product, if you know it. This is the
    #: only identifier a sale can be matched on: the Finances feed reports
    #: the seller SKU, and a supplier's EAN or article number appears
    #: nowhere in it. A cost keyed on anything else is stored and never
    #: found.
    sku: str = ""
    ean: str = ""
    article_no: str = ""

    @property
    def key(self) -> str:
        """What this line is matched on, best identifier first.

        The SKU when given, because that is what a sale carries. The EAN or
        the vendor's article number otherwise — not matchable against a sale
        today, but they identify the product unambiguously, so a later
        release that learns the EAN of each SKU can resolve them without
        asking for the invoices again.
        """
        return self.sku or self.ean or f"{self.vendor}:{self.article_no}"


@dataclass(frozen=True)
class Invoice:
    """One document's lines, with the terms that apply to all of them."""

    vendor: str
    doc_number: str
    doc_date: date
    lines: tuple[InvoiceLine, ...]
    shipping: Decimal = Decimal("0")
    prices_include_vat: bool = False
    vat_rate: Decimal = DEFAULT_VAT_RATE


@dataclass(frozen=True)
class LandedCost:
    """A unit cost that can be traced to a document."""

    key: str
    name: str
    unit_cost: Decimal
    qty: int
    vendor: str
    doc_number: str
    doc_date: date
    detail: str
    #: ``"sku"``, ``"ean"`` or ``"vendor_article"`` — which identifier
    #: :attr:`key` came from. Only ``"sku"`` can match a sale.
    keyed_by: str = "sku"


def landed_costs(
    lines: Iterable[InvoiceLine],
    *,
    shipping: Decimal = Decimal("0"),
    prices_include_vat: bool = False,
    vat_rate: Decimal = DEFAULT_VAT_RATE,
) -> list[LandedCost]:
    """Turn one invoice's lines into landed unit costs.

    Delivery is spread per unit across the whole invoice, then VAT is added
    where the invoice priced net and the seller cannot reclaim it.
    """
    items = [ln for ln in lines if ln.qty > 0]
    if not items:
        return []

    units = sum(ln.qty for ln in items)
    per_unit_shipping = (shipping / units) if units else Decimal("0")
    multiplier = Decimal("1") if prices_include_vat else (Decimal("1") + vat_rate)

    landed: list[LandedCost] = []
    for line in items:
        base = line.unit_price + per_unit_shipping
        cost = base * multiplier
        note = (
            f"{line.vendor} {line.doc_number}: {line.qty} x {line.unit_price}"
            + (f" + {per_unit_shipping.quantize(CENT)} delivery" if per_unit_shipping else "")
            + ("" if prices_include_vat else f" + {(vat_rate * 100).normalize()}% VAT")
        )
        landed.append(LandedCost(
            key=line.key, name=line.name, unit_cost=cost.quantize(CENT), qty=line.qty,
            keyed_by=("sku" if line.sku else "ean" if line.ean else "vendor_article"),
            vendor=line.vendor, doc_number=line.doc_number, doc_date=line.doc_date,
            detail=note))
    return landed


@dataclass(frozen=True)
class SkippedRow:
    """A row that could not be read, and why."""

    line: int
    reason: str


@dataclass(frozen=True)
class CostFile:
    """What a cost file contained, including what could not be read.

    ``skipped`` is part of the result rather than only a log line. One bad
    row should not cost you the other two hundred, which is why parsing
    continues — but a silent skip is worse than a failure: thirty dropped
    lines mean thirty SKUs with no cost, which means a floor below cost on
    each of them and a profit figure that looks fine. Whoever uploaded the
    file is the only person who can fix the row, so they have to be told.
    """

    invoices: list[Invoice]
    skipped: list[SkippedRow]


def read_cost_rows(text: str, *, label: str = "cost file") -> CostFile:
    """Parse cost-file text into invoices, grouped by vendor and document.

    Text rather than a path: an upload arrives in memory and should not have
    to become a file to be read, and a parser that cannot be handed a string
    is awkward to test.

    Raises :class:`CostFileError` if the header is missing a required column
    — that is not one bad row, it is the wrong file.
    """
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames is None:
        raise CostFileError(f"{label}: the file is empty")
    present = {name.strip().lower() for name in reader.fieldnames}
    missing = REQUIRED_COLUMNS - present
    if missing:
        raise CostFileError(
            f"{label}: missing required column(s): {', '.join(sorted(missing))}")

    skipped: list[SkippedRow] = []
    rows = list(_rows(reader, label, skipped))
    return CostFile(invoices=list(_group(rows)), skipped=skipped)


def read_cost_csv(path: Path | str) -> list[Invoice]:
    """Read a cost file from disk into invoices.

    The reporting form is :func:`read_cost_rows`; this keeps the shape a
    command line wants, where a warning in the log is the right channel.
    """
    path = Path(path)
    # utf-8-sig: a BOM from a spreadsheet export otherwise becomes part of
    # the first column name, and the file fails for a missing "vendor".
    parsed = read_cost_rows(path.read_text(encoding="utf-8-sig"), label=path.name)
    for row in parsed.skipped:
        logger.warning("%s line %d skipped: %s", path.name, row.line, row.reason)
    return parsed.invoices


def _rows(
    reader: Iterable[dict[str, str]], label: str, skipped: list[SkippedRow]
) -> Iterator[tuple[InvoiceLine, Decimal, bool, Decimal]]:
    for number, raw in enumerate(reader, start=2):      # row 1 is the header
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        if not any(row.values()):
            continue
        try:
            line = InvoiceLine(
                vendor=row["vendor"],
                doc_number=row["doc_number"],
                doc_date=_as_date(row["doc_date"]),
                name=row["name"],
                qty=int(row["qty"]),
                unit_price=_money(row["unit_price"]),
                sku=row.get("sku", ""),
                ean=row.get("ean", ""),
                article_no=row.get("article_no", ""),
            )
        except (KeyError, ValueError, InvalidOperation) as exc:
            skipped.append(SkippedRow(line=number, reason=str(exc)))
            continue
        try:
            terms = (
                _money(row["shipping"]) if row.get("shipping") else Decimal("0"),
                _as_bool(row.get("prices_include_vat", "")),
                _money(row["vat_rate"]) if row.get("vat_rate") else DEFAULT_VAT_RATE,
            )
        except (ValueError, InvalidOperation) as exc:
            # An unreadable shipping or VAT figure used to raise out of the
            # generator and abandon the whole file, because only the line
            # above was guarded.
            skipped.append(SkippedRow(line=number, reason=str(exc)))
            continue
        yield (line, *terms)


@dataclass
class _Bundle:
    """One document's lines while they are still being collected."""

    doc_date: date
    lines: list[InvoiceLine]
    shipping: Decimal
    prices_include_vat: bool
    vat_rate: Decimal


def _group(rows: Iterable[tuple[InvoiceLine, Decimal, bool, Decimal]]) -> Iterator[Invoice]:
    """Collect lines into invoices. The first row of a document sets its terms."""
    bundles: OrderedDict[tuple[str, str], _Bundle] = OrderedDict()
    for line, shipping, includes_vat, vat_rate in rows:
        key = (line.vendor, line.doc_number)
        bundle = bundles.get(key)
        if bundle is None:
            bundles[key] = _Bundle(
                doc_date=line.doc_date, lines=[line], shipping=shipping,
                prices_include_vat=includes_vat, vat_rate=vat_rate,
            )
        else:
            bundle.lines.append(line)

    for (vendor, doc_number), bundle in bundles.items():
        yield Invoice(
            vendor=vendor, doc_number=doc_number, doc_date=bundle.doc_date,
            lines=tuple(bundle.lines), shipping=bundle.shipping,
            prices_include_vat=bundle.prices_include_vat, vat_rate=bundle.vat_rate)


def costs_from_file(path: Path | str) -> list[LandedCost]:
    """Every landed unit cost in a cost file, across all of its invoices."""
    landed: list[LandedCost] = []
    for invoice in read_cost_csv(path):
        landed.extend(landed_costs(
            invoice.lines,
            shipping=invoice.shipping,
            prices_include_vat=invoice.prices_include_vat,
            vat_rate=invoice.vat_rate,
        ))
    return landed


def _as_date(raw: str) -> date:
    """ISO first, then the common European spellings."""
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date: {raw!r}")


def _as_bool(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "y", "gross"}


def _money(raw: str) -> Decimal:
    """German or English decimals, and a leading currency symbol either way."""
    cleaned = raw.strip().lstrip("€$£").strip()
    if "," in cleaned and "." in cleaned:
        cleaned = (cleaned.replace(".", "").replace(",", ".")
                   if cleaned.rindex(",") > cleaned.rindex(".") else cleaned.replace(",", ""))
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    return Decimal(cleaned)
