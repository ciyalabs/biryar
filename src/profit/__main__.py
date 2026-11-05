"""``python -m cockpit.src.profit import-costs`` — read buy prices off the invoices.

The tax archive already holds every supplier invoice. This walks the ones that
bought stock, reads their product lines, and records what a unit landed at into
``sku_costs``, dated at the invoice.

Re-running is safe and is the point: a restock at a new price becomes a second
dated entry rather than overwriting the first, so last month's margins keep
last month's cost.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
load_dotenv(_REPO_ROOT / ".env")

from cockpit.src.db.schema import init_db                          # noqa: E402

from .invoices import import_costs                                 # noqa: E402


def _archive_root(config: dict) -> Path:
    raw = str(((config.get("taxes") or {}).get("dropbox_root"))
              or "~/Dropbox/ciya_labs_docs/taxes")
    return Path(raw).expanduser()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cockpit.src.profit", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="verb", required=True)
    imp = sub.add_parser("import-costs", help="read buy prices from the invoice archive")
    imp.add_argument("--root", help="archive root; default comes from taxes config")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    config = yaml.safe_load((_REPO_ROOT / "cockpit" / "config.yaml").read_text("utf-8"))
    taxes_cfg = yaml.safe_load((_REPO_ROOT / "taxes" / "config.yaml").read_text("utf-8"))
    conn = init_db(_REPO_ROOT / str((config.get("platform") or {}).get("db_path")
                                    or "cockpit/data/seller_cockpit.db"))
    conn.execute("PRAGMA busy_timeout = 120000")

    root = Path(args.root).expanduser() if args.root else _archive_root(taxes_cfg)
    report = import_costs(conn, archive_root=root)
    print(report.summary)
    for name, why in sorted(report.skipped.items()):
        print(f"  skipped  {name:<30} {why}")
    for line in report.unmatched:
        print(f"  no SKU   {line.vendor:<11} €{line.unit_cost:>7} x{line.qty:<3} "
              f"{line.name[:44]}")
    return 0 if report.read else 1


if __name__ == "__main__":
    raise SystemExit(main())
