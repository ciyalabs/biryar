# Biryar

[![CI](https://github.com/ciyalabs/biryar/actions/workflows/ci.yml/badge.svg)](https://github.com/ciyalabs/biryar/actions/workflows/ci.yml)

**Amazon operations, one decision at a time.**

Biryar is a self-hosted operations cockpit for Amazon sellers. It reads your
own seller account through the Selling Partner API, works out what each
product actually earns after every cost Amazon charged, and tells you where
the next euro should go.

A product of [Ciya Labs](https://ciyalabs.com).

---

## The claim this is built on

Most sellers price against a modelled fee. They take the referral rate from
the fee schedule, add an estimate for fulfilment, and call the difference
margin.

Measured against the Finances feed rather than the fee schedule, the real cut
on a cheap unit lands between **33% and 43% of the sale price**, not the
~17.85% the schedule implies. The gap is made of closing fees, per-unit flat
charges, postage bought through Buy Shipping, and fee VAT, all of which the
schedule either omits or states separately from the rate.

Biryar does not model the fee. It reads what Amazon actually took, per SKU,
per order, and derives the break-even from it:

```
break-even price = (landed cost + flat charges) / (1 - measured rate)
```

A floor built on a modelled rate is a guess wearing a number's clothes. The
whole point of this codebase is to stop guessing.

---

## Status

**Pre-release.** This repository is being assembled from a private codebase
that has been running a live Amazon business since September 2026. See
[MANIFEST.md](MANIFEST.md) for exactly what has migrated and what has not.

Release 0.1 is planned as a read-only, self-hosted application. It never
writes to your Amazon account.

---

## What release 0.1 will contain

| Page | What it answers |
|---|---|
| **Portfolio** | What each SKU really earns after every cost, and where the next euro goes |
| **Metrics** | Revenue, net profit, true margin, cash on hand, 90-day forecast |
| **Reimbursements** | FBA units Amazon owes you money for, as drafted cases |
| **Repricing** | Price history, and what your break-even floor actually is (advisory only) |
| **Forecast** | Per-SKU demand, stockout risk, units to buy |
| **Home / Settings / Help** | Status, configuration, and what every number means |

Design constraints, all deliberate:

- **Read-only.** No price writes, no listing edits, no messages sent. The
  configuration default `dry_run: true` stays true.
- **Your keys, your machine.** You register your own SP-API application
  against your own seller account. Nothing is sent to Ciya Labs, and there is
  no Biryar server to send it to.
- **One container.** No GPU, no second machine, no background cloud workers.

---

## Reports

Long-form technical writing on how each part of the system works. These were
written against the live system and are readable without any of the code.

| Report | Subject |
|---|---|
| [Sourcing](reports/sourcing/) | Harvest, watchlist, screening, and the markdown classifier |
| [Portfolio](reports/portfolio/) | Landed cost, measured fees, and profit per unit |
| [Repricing](reports/repricing/) | The envelope policy, the Buy Box model, and price floors |
| [Reimbursements](reports/reimbursements/) | FBA reconciliation and drafted cases |
| [Upstream](reports/manufacturers/) | Finding manufacturers worth writing to |
| [Ungating](reports/ungating/) | Brand applications and wholesale qualification |

Several describe capabilities that are **not** in release 0.1 — taxes,
sourcing, and manufacturer outreach among them. They are published anyway, so
you can see where the system is going before deciding whether to adopt it.

---

## Licence

[Business Source License 1.1](LICENSE). You may run Biryar in production for
your own business. You may not offer it to third parties as a hosted or
managed service. On 2030-11-05 each released version converts to Apache 2.0.

For commercial licensing, contact Ciya Labs.
