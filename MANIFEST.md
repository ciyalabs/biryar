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

Each of these is covered by tests that run against a real PostgreSQL in CI.

**The tenant boundary.** `tenant_id` plus a forced row-level-security policy
on every tenant-scoped table, an application role that owns nothing and
cannot bypass, and the tenant setting made transaction-local and enforced by
an engine listener. A query that forgets to filter returns one tenant's rows;
a query with no tenant returns none. See
[docs/architecture/tenancy.md](docs/architecture/tenancy.md).

**Schema foundations.** Money as `NUMERIC` rather than the `REAL` the old
schema used, every timestamp `TIMESTAMPTZ`, a constraint naming convention,
and migrations the test suite actually runs rather than approximating with
`create_all`.

**Finance.** `amazon_transactions` holds the Finances feed verbatim, keyed on
Amazon's own transaction id so re-fetching an overlapping window is free.
`sku_costs` holds dated landed costs. Nothing derived is stored, so a fix to
how a fee is classified corrects history instead of needing a backfill.

**The profit domain.** `biryar.profit` imports nothing that knows what a
database is, which is why its 34 tests run anywhere in a fraction of a
second. `biryar.repositories` is the only module that imports both sides.

**Identity.** Argon2id passwords, SHA-256 session tokens, and one
`SECURITY DEFINER` function — the single sanctioned way to read a session
before a tenant is known, since the table that answers "whose token is this"
is itself tenant-scoped.

**The API.** `/v1`, versioned before the first route because the shape
becomes a contract the moment React consumes it. Health, sign-in, sign-out,
me, and the Amazon connection routes.

**Amazon authorisation.** The consent round trip with signed, short-lived
state; LWA token exchange that tells a revoked authorisation apart from a bad
day; and refresh tokens sealed at rest under a keyring, so a key can be
rotated incrementally rather than in one transaction that holds every row.

**Jobs.** A queue in PostgreSQL, so work and the reason for it commit
together. Claiming is fair across tenants — one seller's backfill cannot
occupy every worker — and a job carries its own retry policy, so a throttle
is tried again and a revoked authorisation is not. `python -m biryar.worker`
runs it; `--schedule` queues what is due and is safe to run twice.

**The settlement fetch.** Consent to sealed credential to queued job to the
Finances feed to rows the ledger reads. Verified end to end through the real
worker with Amazon stubbed.

**The front end.** Vite, React, TypeScript, in `web/`. Its types are
generated from the API's own OpenAPI document rather than written by hand,
and CI regenerates that document and fails if it differs from what is
committed — so a route that changes shape breaks the type check instead of
breaking a page in front of someone.

**Packaging.** One image carries the API, the worker and the built front
end; `docker compose up` plus one migration is the whole install. A third CI
job builds that image and runs it, asserting that health reports the database
*unreachable* rather than `ok` — the answer that proves it looked — and that
the front end is served from inside the image. `tests/test_packaging.py`
closes the two ways this broke: a dependency declared only under the dev
extras (present on every development machine, absent from the image, first
noticed as a container exiting with empty logs), and a licence file the wheel
build reads but the build context did not carry.

## Next

Two items, both about the first ten minutes a stranger spends with this:

1. **A first-run setup screen.** Credentials still come from the
   environment, which is fine for a self-host and wrong for a hosted tenant.
2. **The SP-API guide written properly.** `docs/setup/sp-api.md` is an
   outline. It is the step most likely to lose someone, and it is the one
   place where getting a detail wrong wastes a stranger's afternoon.

See *Known structural work* below for what is deliberately deferred.

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
