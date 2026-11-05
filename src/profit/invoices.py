"""Buy prices read off the invoices themselves.

The tax archive already holds every supplier invoice as a PDF, and
``taxes/extract/vendors/`` already parses them — but only for the VAT totals a
return needs. The product lines, which are the only record of what a single
unit cost, were being thrown away.

This reads them back.

## Why not trust what was there

``price_bounds.unit_cost`` looked like a cost and was not. When
``repricing/catalogue.py`` has no invoice it derives one backwards from the
Seller Central floor, ``floor x (1 - referral)``, which makes break-even land
on the floor it came from. Twenty-two of twenty-six rows were that. Nothing
downstream could tell, because an invented number and a real one are the same
shape.

An invoice line cannot be invented. It has a quantity, a printed unit price
and a document number to check it against.

## What "landed" means here

The price on the line, plus that line's share of the delivery the same invoice
charged, plus the VAT that §19 makes unrecoverable. Vendors differ in which of
those they have already applied:

================  =====================  ======================================
Vendor            Printed unit price     Landed
================  =====================  ======================================
Müller            gross, VAT included    price + shipping share
Saturn/MediaMarkt net                    (price + shipping share) x 1.19
NICI              net                    (price + shipping share) x 1.19
Qogita            net, 0% reverse charge (price + shipping share) x 1.19
Terrashop         gross, VAT included    price + shipping share
================  =====================  ======================================

Delivery is divided by the number of units on the invoice, not by the number
of lines. A €3.95 Müller delivery across five jars is €0.79 a jar, and leaving
it out is how a €6.74 cost passes for €5.95.

**Prep is not in here.** Prep Prime invoices a batch, not a line, and nothing
in the archive says which units a batch covered. Guessing an allocation would
put an invented number back into the one place that is supposed to be free of
them, so prep stays visible as an unallocated cost instead.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Callable, Iterable

logger = logging.getLogger(__name__)

VAT = Decimal("1.19")
CENT = Decimal("0.01")

#: Vendors whose printed unit price already carries German VAT.
GROSS_PRICED: frozenset[str] = frozenset({"mueller", "terrashop"})


@dataclass(frozen=True)
class InvoiceLine:
    """One product line, exactly as the invoice prints it."""

    vendor: str
    doc_number: str
    doc_date: date
    name: str
    qty: int
    #: The unit price as printed. Whether it includes VAT depends on vendor.
    unit_price: Decimal
    ean: str = ""
    article_no: str = ""

    @property
    def key(self) -> str:
        """What this line can be matched on — an EAN if the vendor prints one."""
        return self.ean or f"{self.vendor}:{self.article_no}"


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


def read_invoice(path: Path, vendor: str, doc_number: str, doc_date: date) -> list[InvoiceLine]:
    """Product lines from one archived invoice PDF."""
    parser = PARSERS.get(vendor)
    if parser is None:
        return []
    text = _text_of(path)
    if not text:
        return []
    try:
        return parser(text, vendor, doc_number, doc_date)
    except Exception as exc:                                     # noqa: BLE001
        logger.warning("invoice %s %s: %s", vendor, doc_number, exc)
        return []


def landed_costs(lines: Iterable[InvoiceLine], *, shipping: Decimal = Decimal("0"),
                 vendor: str = "") -> list[LandedCost]:
    """Turn one invoice's lines into landed unit costs.

    Delivery is spread per unit across the whole invoice, then VAT is applied
    where the vendor priced net.
    """
    items = [ln for ln in lines if ln.qty > 0]
    if not items:
        return []
    units = sum(ln.qty for ln in items)
    per_unit_shipping = (shipping / units) if units else Decimal("0")
    gross_priced = (vendor or items[0].vendor) in GROSS_PRICED

    landed: list[LandedCost] = []
    for line in items:
        base = line.unit_price + per_unit_shipping
        cost = base if gross_priced else base * VAT
        note = (f"{line.vendor} {line.doc_number}: {line.qty} x €{line.unit_price}"
                + (f" + €{per_unit_shipping.quantize(CENT)} delivery"
                   if per_unit_shipping else "")
                + ("" if gross_priced else " + 19% VAT"))
        landed.append(LandedCost(
            key=line.key, name=line.name, unit_cost=cost.quantize(CENT), qty=line.qty,
            vendor=line.vendor, doc_number=line.doc_number, doc_date=line.doc_date,
            detail=note))
    return landed


# -- per-vendor line parsers ---------------------------------------------------------

def _qogita(text: str, vendor: str, number: str, when: date) -> list[InvoiceLine]:
    """``NAME  SELLER_ID  GTIN  €PRICE  QTY  €SUBTOTAL`` on one line."""
    pattern = re.compile(
        r"^(?P<name>.+?)\s{2,}\S+\s+(?P<ean>\d{12,14})\s+€(?P<price>[\d.,]+)\s+"
        r"(?P<qty>\d+)\s+€[\d.,]+\s*$", re.MULTILINE)
    return [InvoiceLine(vendor=vendor, doc_number=number, doc_date=when,
                        name=m.group("name").strip(), ean=m.group("ean"),
                        qty=int(m.group("qty")), unit_price=_money(m.group("price")))
            for m in pattern.finditer(text)]


def _mueller(text: str, vendor: str, number: str, when: date) -> list[InvoiceLine]:
    """``QTY  ARTICLE_NO  NAME  PRICE €  TOTAL €  19%`` — delivery on its own row."""
    pattern = re.compile(
        r"^(?P<qty>\d+)\s+(?P<art>\d+)\s+(?P<name>.+?)\s{2,}"
        r"(?P<price>[\d.,]+)\s*€\s+[\d.,]+\s*€", re.MULTILINE)
    return [InvoiceLine(vendor=vendor, doc_number=number, doc_date=when,
                        name=m.group("name").strip(), article_no=m.group("art"),
                        qty=int(m.group("qty")), unit_price=_money(m.group("price")))
            for m in pattern.finditer(text)]


def _nici(text: str, vendor: str, number: str, when: date) -> list[InvoiceLine]:
    """Two-row lines: article and description, then the EAN underneath."""
    head = re.compile(
        r"^(?P<art>\d{4,6})\s+(?P<name>\D.+?)\s{2,}(?P<qty>\d+)\s+"
        r"(?P<price>[\d.,]+)\s+[\d.,]+\s+\d+\s*$", re.MULTILINE)
    lines: list[InvoiceLine] = []
    for match in head.finditer(text):
        tail = text[match.end(): match.end() + 400]
        ean = re.search(r"\b(\d{13})\b", tail)
        lines.append(InvoiceLine(
            vendor=vendor, doc_number=number, doc_date=when,
            name=match.group("name").strip(), article_no=match.group("art"),
            ean=ean.group(1) if ean else "", qty=int(match.group("qty")),
            unit_price=_money(match.group("price"))))
    return lines


def _mms(text: str, vendor: str, number: str, when: date) -> list[InvoiceLine]:
    """Saturn and MediaMarkt: ``POS ART NAME QTY RATE PRICE EUR TOTAL EUR``.

    A row whose description is blank still counts — the quantity and price are
    there, and a missing name loses the label, not the money.
    """
    pattern = re.compile(
        r"^\s*\d+\s+(?P<art>\d+)\s+(?P<name>.*?)\s{2,}(?P<qty>\d+)\s+"
        r"[\d,]+%\s+(?P<price>[\d.,]+)\s*EUR", re.MULTILINE)
    return [InvoiceLine(vendor=vendor, doc_number=number, doc_date=when,
                        name=(m.group("name") or "").strip(), article_no=m.group("art"),
                        qty=int(m.group("qty")), unit_price=_money(m.group("price")))
            for m in pattern.finditer(text)]


Parser = Callable[[str, str, str, date], list[InvoiceLine]]

PARSERS: dict[str, Parser] = {
    "qogita": _qogita,
    "mueller": _mueller,
    "nici": _nici,
    "saturn": _mms,
    "mediamarkt": _mms,
}

#: Patterns that find the delivery charge on an invoice, per vendor.
SHIPPING_PATTERNS: dict[str, re.Pattern[str]] = {
    "mueller": re.compile(r"Versandkosten\s+([\d.,]+)\s*€", re.IGNORECASE),
    "saturn": re.compile(r"Versandkosten\s+\d+\s+[\d,]+%\s+([\d.,]+)\s*EUR", re.IGNORECASE),
    "mediamarkt": re.compile(r"Versandkosten\s+\d+\s+[\d,]+%\s+([\d.,]+)\s*EUR", re.IGNORECASE),
    "qogita": re.compile(r"^Shipping\s+€([\d.,]+)\s*$", re.MULTILINE),
}


def shipping_on(text: str, vendor: str) -> Decimal:
    pattern = SHIPPING_PATTERNS.get(vendor)
    if pattern is None:
        return Decimal("0")
    found = pattern.search(text)
    return _money(found.group(1)) if found else Decimal("0")


def _money(raw: str) -> Decimal:
    """German or English decimals, both seen in this archive."""
    cleaned = raw.strip()
    if "," in cleaned and "." in cleaned:
        cleaned = (cleaned.replace(".", "").replace(",", ".")
                   if cleaned.rindex(",") > cleaned.rindex(".") else cleaned.replace(",", ""))
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    return Decimal(cleaned)


def _text_of(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        done = subprocess.run(["pdftotext", "-layout", str(path), "-"],
                              capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("pdftotext on %s: %s", path, exc)
        return ""
    return done.stdout if done.returncode == 0 else ""


# -- importing the archive -----------------------------------------------------------

@dataclass
class ImportReport:
    read: int = 0
    skipped: dict[str, str] = None            # type: ignore[assignment]
    costs: list[LandedCost] = None            # type: ignore[assignment]
    unmatched: list[LandedCost] = None        # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.skipped = self.skipped or {}
        self.costs = self.costs or []
        self.unmatched = self.unmatched or []

    @property
    def summary(self) -> str:
        return (f"{self.read} invoices, {len(self.costs)} unit costs matched to a SKU, "
                f"{len(self.unmatched)} lines with no SKU, {len(self.skipped)} skipped")


def import_costs(conn, *, archive_root: Path, by: str = "invoice-import") -> ImportReport:
    """Read every goods invoice in the archive and record what a unit cost.

    Writes into ``sku_costs`` dated at the invoice date, so a restock at a new
    price becomes a second entry and last month's margins keep last month's
    cost. Re-running is safe: an entry for the same SKU and date is replaced,
    never duplicated.

    A line that matches no listed SKU is reported rather than dropped. Most are
    real — the deodorants bought only to support a brand-ungating application
    were never listed — and a silent drop would make the unmatched case
    indistinguishable from a broken parser.
    """
    from .costs import CostBasis, CostEntry                       # noqa: PLC0415

    report = ImportReport()
    by_asin = _asin_index(conn)
    skus_by_asin = _skus_by_asin(conn)
    entries: list[CostEntry] = []

    for vendor, number, when, path in _goods_documents(conn, archive_root):
        if vendor not in PARSERS:
            report.skipped[f"{vendor} {number}"] = "no line parser for this vendor"
            continue
        if not path.is_file():
            report.skipped[f"{vendor} {number}"] = f"not in the archive: {path.name}"
            continue
        text = _text_of(path)
        lines = read_invoice(path, vendor, number, when)
        if not lines:
            report.skipped[f"{vendor} {number}"] = "no product lines found"
            continue
        report.read += 1
        for cost in landed_costs(lines, shipping=shipping_on(text, vendor), vendor=vendor):
            asin = by_asin.get(cost.key)
            matched = skus_by_asin.get(asin or "", [])
            if not matched:
                report.unmatched.append(cost)
                continue
            report.costs.append(cost)
            entries.extend(
                CostEntry(sku=sku, effective_from=cost.doc_date, unit_cost=cost.unit_cost,
                          source=f"{cost.vendor} {cost.doc_number}", note=cost.detail)
                for sku in matched)

    if entries:
        CostBasis(conn).record(entries, by=by)
    logger.info("invoice costs: %s", report.summary)
    return report


def _goods_documents(conn, archive_root: Path) -> list[tuple[str, str, date, Path]]:
    from .sources import GOODS_VENDORS                            # noqa: PLC0415

    found = []
    for vendor, number, when, path in conn.execute(
        "SELECT vendor, doc_number, doc_date, path FROM tax_documents "
        "WHERE kind = 'invoice' ORDER BY doc_date"
    ):
        if vendor not in GOODS_VENDORS:
            continue
        found.append((str(vendor), str(number), date.fromisoformat(str(when)[:10]),
                      archive_root / str(path)))
    return found


def _asin_index(conn) -> dict[str, str]:
    """EAN or ``vendor:article`` to ASIN.

    Barcodes resolve through the catalogues the sourcing bots already fill.
    A vendor that prints no barcode — Müller, Saturn — has no automatic route,
    so those keys are mapped in ``cockpit/pricing/costs.yaml`` by hand and
    read from there.
    """
    index: dict[str, str] = {}
    for table, gtin, asin in (("catalogue_offers", "gtin", "asin"),
                              ("qogita_variants", "gtin", "asin")):
        try:
            for key, value in conn.execute(
                    f"SELECT {gtin}, {asin} FROM {table} WHERE {asin} IS NOT NULL "
                    f"AND {asin} != ''"):
                index.setdefault(str(key), str(value))
        except Exception:                                         # noqa: BLE001, S110
            pass                                                  # table absent in a test db
    from .sources import invoice_costs                            # noqa: PLC0415

    index.update(_manual_keys())
    for asin in invoice_costs():
        index.setdefault(asin, asin)
    return index


def _manual_keys(path: Path | None = None) -> dict[str, str]:
    """``units:`` entries in the cost book whose key is not a barcode.

    The book is keyed by EAN, but a Müller or Saturn line has only the
    retailer's own article number. Writing ``mueller:2904994`` as the key
    there gives those lines a home without inventing a barcode for them.
    """
    from .sources import DEFAULT_COST_BOOK                        # noqa: PLC0415

    book = Path(path) if path else DEFAULT_COST_BOOK
    if not book.is_file():
        return {}
    try:
        import yaml                                               # noqa: PLC0415

        raw = yaml.safe_load(book.read_text("utf-8")) or {}
    except Exception:                                             # noqa: BLE001
        return {}
    return {str(key): str(entry["asin"])
            for key, entry in (raw.get("units") or {}).items()
            if isinstance(entry, dict) and entry.get("asin")}


def _skus_by_asin(conn) -> dict[str, list[str]]:
    """Every SKU that carries a given ASIN — a product can have two.

    ``sales`` is read as well as the two listing tables because a SKU that
    sold out and was delisted still has sales to cost. Matching only against
    what is listed today would leave its margin permanently unknowable.
    """
    found: dict[str, list[str]] = {}
    for table in ("listing_health", "price_bounds", "sales"):
        try:
            for sku, asin in conn.execute(
                    f"SELECT sku, asin FROM {table} WHERE asin IS NOT NULL AND asin != ''"):
                found.setdefault(str(asin), [])
                if str(sku) not in found[str(asin)]:
                    found[str(asin)].append(str(sku))
        except Exception:                                         # noqa: BLE001, S110
            pass
    return found
