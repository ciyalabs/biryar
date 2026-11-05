# Release manifest

What has migrated from the private codebase, what has not, and what blocks
the rest. Updated as modules land.

## Migrated

| Path | Source | Notes |
|---|---|---|
| `src/profit/` | `cockpit/src/profit/` | The settlement ledger, cost basis, growth model and period report. Extracts clean: zero imports from sibling cockpit modules. |
| `tests/test_profit.py` | `cockpit/tests/test_profit.py` | Needs import paths rewritten from `cockpit.src.profit` to `src.profit`, and `cockpit.src.db.schema.init_db` supplied or stubbed. |
| `reports/` | `reports/` | All six reports plus the shared citation widget. |

## Blocked before this repository can be made public

| Item | Problem |
|---|---|
| `src/profit/invoices.py` | Contains vendor-specific parsers named `_qogita`, `_mueller`, `_nici` and `_mms`. Publishing these discloses the supplier list of a live business. Must be generalised to a CSV cost importer, or removed along with its three exports in `__init__.py` and the four tests that import it. |
| `reports/sourcing/fig/deal-detail.png` | Shows a real, identifiable product: title, ASIN 3407761694, EAN, cost 3 EUR, sale price 15 EUR, 138% ROI. The retailer is blurred; the product is not. |
| `reports/manufacturers/fig/order-list.png` | Supplier and article identities are blurred, but landed cost, Amazon price, margin, rank, offers and fee per row are legible, as are the aggregates (56,155 EUR to buy the assortment, 11,861 EUR margin, 123 EUR a day). |

Everything else sampled is either blurred or carries no business data. The
report text is clean across all six: no supplier names, no addresses, no bank
details, no tax references.

## Not in release 0.1

Cut because they are specific to one business:

- **Taxes** (ELSTER, ERiC, FinTS bank feed) — German-only and regulated.
- **NICI**, **Manufacturers**, **Accounts vault** — one supplier, one
  outreach funnel, and one person's third-party logins.

Cut because they are a different product:

- **Sourcing**, **Deals**, **Best sellers** — these need Keepa and scraping
  keys that each user would have to supply, and they compete with Keepa and
  SellerAmp rather than with anything in the profit story.

Cut because it serves the builder, not the seller:

- **Models** — shadow evaluation and promotion is ML operations.

## Known structural work

1. **The backend is a monolith.** `cockpit/src/api.py` is 5,678 lines with 65
   routes and imports `accounts` and `qualifications` directly; `jobs.py` is
   2,603 lines. Shipping a subset of pages means splitting these, not copying
   them. This is the largest single task in the release and the one most
   likely to break the 3,014 tests that currently pass.
2. **No settlement flat-file parser exists.** `build_settlement` consumes
   `listFinancialEvents` JSON from the SP-API. The V2 settlement report a
   seller can download from Seller Central is a different shape, and nothing
   in the codebase reads it. A free no-integration entry point needs this
   adapter written from scratch.
3. **Credentials come from `.env`.** A shipped product needs a first-run
   setup wizard. The Fernet vault in `cockpit/src/accounts.py` is the right
   foundation.
4. **No Dockerfile, no packaging.** `pyproject.toml` in the source repo is
   pytest configuration only.
5. **The two-machine assumption** — systemd user units, SSH tunnels, an mDNS
   hostname, a GPU box for Ollama — is correct for the private deployment and
   wrong for a shipped artifact.
6. **GPU jobs fail silently.** Without CUDA, `clip_matcher` and `llm_support`
   write descriptor-only artifacts and record a successful run that trained
   nothing. In a shipped product that is a false statement to a paying user.
   These stay out of 0.1.
