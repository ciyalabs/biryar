# Release manifest

What has migrated from the private codebase, what has not, and what still
has to be built. Updated as modules land.

## Migrated

| Path | Source | Notes |
|---|---|---|
| `src/profit/` | `cockpit/src/profit/` | The settlement ledger, cost basis, growth model and period report. Extracts clean: zero imports from sibling cockpit modules. |
| `tests/test_profit.py` | `cockpit/tests/test_profit.py` | Needs import paths rewritten from `cockpit.src.profit` to `src.profit`, and `cockpit.src.db.schema.init_db` supplied or stubbed. |
| `reports/` | `reports/` | All six reports plus the shared citation widget. |

## Resolved

**Supplier disclosure — fixed.** `src/profit/invoices.py` carried PDF parsers
named after four named suppliers. It is now a vendor-agnostic CSV cost
importer: `read_cost_csv`, `costs_from_file`, and a `landed_costs` that takes
`prices_include_vat` and `vat_rate` as parameters instead of matching a vendor
against a hardcoded set. `sources.py` and `__main__.py` were dropped, both
being coupled to a private tax archive and naming nine vendors between them;
`ResolvedCost` moved to `costs.py`, which is the only thing that depended on
them. Docstrings referencing one seller's VAT status, an internal memory key
and private module paths were generalised. `store.py` was added so the
package carries its own `sku_costs` table and no longer needs a
platform-wide schema. 34 tests pass.

The private repository is unchanged and keeps its PDF parsers.

## Published knowingly

This repository went public on 2026-11-05. Two report figures carry live
business data and were kept anyway, as a deliberate decision rather than an
oversight:

| Item | What is visible |
|---|---|
| `reports/sourcing/fig/deal-detail.png` | A real, identifiable product: title, ASIN 3407761694, EAN, cost 3 EUR, sale price 15 EUR, 138% ROI. The retailer is blurred; the product is not. |
| `reports/manufacturers/fig/order-list.png` | Supplier and article identities are blurred, but landed cost, Amazon price, margin, rank, offers and fee per row are legible, as are the aggregates (56,155 EUR to buy the assortment, 11,861 EUR margin, 123 EUR a day). |

Everything else sampled is either blurred or carries no business data. The
report text is clean across all six: no supplier names, no addresses, no bank
details, no tax references. A pre-publication scan also caught one supplier
named in this file's own cut list, in the sentence explaining why that page
was removed.

## Not in release 0.1

Cut because they are specific to one business:

- **Taxes** (ELSTER, ERiC, FinTS bank feed) — German-only and regulated.
- **The single-supplier assortment page**, **Manufacturers**, **Accounts
  vault** — one supplier's catalogue, one outreach funnel, and one person's
  third-party logins.

Cut because they are a different product:

- **Sourcing**, **Deals**, **Best sellers** — these need Keepa and scraping
  keys that each user would have to supply, and they compete with Keepa and
  SellerAmp rather than with anything in the profit story.

Cut because it serves the builder, not the seller:

- **Models** — shadow evaluation and promotion is ML operations.

## Architecture

Settled 2026-11-05. Biryar is a **multi-tenant platform**; a self-hosted
install is the same program serving one tenant, not a different product.
The reasoning is in [docs/architecture/tenancy.md](docs/architecture/tenancy.md).

| Decision | Choice |
|---|---|
| Tenancy | Multi-tenant, isolated by PostgreSQL row-level security |
| Database | PostgreSQL. No SQLite path: RLS has no equivalent there |
| Frontend | Vite + React + TypeScript against a versioned HTTP API |
| Migration order | Backend first, with Streamlit running against it, then React |

## Done

- **The tenant boundary.** `tenant_id` plus a forced RLS policy on every
  tenant-scoped table, an application role that owns nothing and cannot
  bypass, and `SET LOCAL` enforced by an engine listener. Proven by
  `tests/test_tenancy.py` against a real PostgreSQL in CI.
- **Schema foundations.** Money as `NUMERIC` rather than the `REAL` the old
  schema used, every timestamp `TIMESTAMPTZ`, a constraint naming convention
  so downgrades are not one-way.
- **Tenancy, users, sessions, per-tenant sealed Amazon credentials.**
- **Migrations that the tests actually run**, plus a test asserting the
  migrations and the models still describe the same database.
- **The profit domain**, moved under `biryar.profit`, 34 tests, mypy strict.

## Next, in order

1. **The finance tables** — settlement lines, SKU costs, overheads — tenant
   scoped, with the profit ledger reading from them instead of from a cursor
   the caller supplies.
2. **Identity and the API skeleton** — Argon2 password hashing, session
   issuing, and a versioned `/v1` surface. The API becomes a public contract
   the moment React consumes it, so versioning comes before the first route,
   not after.
3. **SP-API client and OAuth** — the authorisation flow that writes
   `amazon_credentials`, and token refresh that marks a tenant
   `needs_reauth` rather than failing jobs obscurely.
4. **Jobs** — per-tenant scheduling with isolation, retries and backpressure.
   APScheduler in-process does not survive multi-tenancy.
5. **React** — once the API above is stable, so it is written once.

## Still carried over from the private codebase

The old platform's `api.py` is 5,678 lines across 65 routes and imports
`accounts` and `qualifications` directly; `jobs.py` is 2,603. Neither is
being ported. The routes that matter are being rewritten against the schema
above, which is why the order starts with data rather than with endpoints.

**No settlement flat-file parser exists.** `build_settlement` consumes
`listFinancialEvents` JSON from the SP-API. The V2 settlement report a seller
can download from Seller Central is a different shape, and nothing reads it.
A free no-integration entry point needs that adapter written from scratch.

**GPU jobs fail silently.** Without CUDA, `clip_matcher` and `llm_support`
write descriptor-only artifacts and record a successful run that trained
nothing. In a shipped product that is a false statement to a paying user.
They stay out until that is fixed at the source.
