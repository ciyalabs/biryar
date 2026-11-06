# How the fees are measured

This is the document that has to be right, because it is the argument for
the whole product. The derivation below is arithmetic and you can check it
on paper. What it is *not* is a claim about anyone's particular fee
schedule: every number in the worked example is illustrative, chosen to be
round. Your own rate and flat charge come out of your own Finances feed,
which is the whole point.

## The short version

The Amazon fee schedule states a referral rate. Sellers model margin from it.
Measured against the Finances feed instead, the real cut on a cheap unit
lands between 33% and 43% of the sale price rather than the ~17.85% the
schedule implies.

The difference is not fraud and not an error. It is made of charges the
schedule either states separately or does not mention:

- the per-item closing fee, flat rather than proportional, which dominates on
  a cheap unit and disappears on an expensive one
- postage bought through Buy Shipping, which arrives as its own transaction
  hours after the order and belongs to the order rather than to the month
- fee VAT
- fulfilment charges that vary by unit, not by price

## The derivation

Amazon's cut on a unit is not one number. It is a proportional part and a
flat part:

```
cut(p) = r·p + f
```

where `p` is the sale price, `r` the proportional rate (referral, plus VAT
charged on the fee itself) and `f` everything charged per unit regardless of
price. As a *share* of the price, which is how a seller thinks about it:

```
cut(p) / p = r + f/p
```

That second term is the entire story. It is not constant — it falls as the
price rises, because a flat charge is amortised over a larger sale. A single
"Amazon takes about X%" is therefore not a property of the marketplace or of
the category. It is a property of **the price of the thing you sold.**

With an illustrative `r = 17.85%` and `f = €1.00` — the same two charges
throughout, nothing varying but the price:

| Sale price | Amazon takes | As a share |
|---|---|---|
| €3.00 | €1.54 | **51.2%** |
| €5.00 | €1.89 | **37.9%** |
| €10.00 | €2.79 | 27.9% |
| €15.00 | €3.68 | 24.5% |
| €30.00 | €6.35 | 21.2% |
| €50.00 | €9.92 | 19.8% |

Two things to take from that table. The share converges on `r` from above
but never reaches it, so the schedule's rate is a floor on the real cut, not
an estimate of it. And on a cheap unit the gap is not a rounding error — at
€5 the real cut is more than twice the rate.

This is why the measured range on cheap units sits between 33% and 43% while
the schedule implies ~17.85%. Nothing is hidden: the flat charge is stated,
it is simply not a percentage, and a model built out of percentages has
nowhere to put it.

## Why this changes a price floor

A floor built from a modelled rate is wrong in the direction that costs
money: it is too low, so the repricer is willing to sell below break-even and
reports a profit while doing it.

Measured, the floor is:

```
break-even price = (landed cost + flat charges) / (1 - measured rate)
```

Both terms matter. A rate alone cannot express a flat charge, and a flat
charge alone cannot express a rate. Collapsing them into one number is the
most common way a seller's floor ends up below their actual cost.

Continuing the illustration, with a landed cost of €3.00:

| | |
|---|---|
| Measured floor, `(3.00 + 1.00) / (1 − 0.1785)` | **€4.87** |
| At €4.87, what actually arrives | €3.00 — exactly the cost |
| Floor from the rate alone, `3.00 / (1 − 0.1785)` | **€3.65** |
| At €3.65, what actually arrives | €2.00 |
| Loss per unit | **€1.00** |

The loss is exactly `f`. That is not a coincidence and it is the useful part:
**a floor that ignores the flat charge is wrong by the flat charge, per
unit, every time.** It is also wrong in the direction that costs money — too low, so
a repricer built on it will undercut willingly, sell below break-even, and
report a profit while doing it, because the same wrong rate is used to book
the margin.

### The inverse, and what it cannot tell you

The formula has an inverse:

```
cost(p) = p·(1 − r) − f
```

It is useful for one thing and dangerously useless for another, and the two
are easy to confuse.

**What it is for:** checking that the code computing a floor and the code
booking a margin agree. Both need the same `(r, f)` pair. If the floor uses
a rate and a flat charge while the margin uses a rate alone, the round trip
stops closing, and that mismatch is otherwise silent — both halves look
reasonable in isolation and the loss only appears in the bank.

**What it cannot do is validate a cost.** If you obtained a cost by working
backwards from a Seller Central floor, then feeding it through the inverse
returns that floor *by construction*. It would return it just as happily if
the cost were nonsense. A round trip that cannot fail is not evidence, and
the fact that it passes is the misleading part — it looks like a check.

This is why Biryar will not derive a cost from a price. It reads unit costs
from a cost file you maintain — one CSV row per invoice line, with the
vendor, document number, quantity and printed unit price, so every cost in
the system traces to a document you can hold up against it. A unit cost is
the one number Amazon never reports, every
margin depends on it, and the only thing that can confirm it is a document
with a quantity and a printed unit price on it. An invented number and a
real one are the same shape.

## Still to be written

These need figures from a real Finances feed rather than arithmetic, so they
are written once the ledger has enough history to state them without
rounding a guess into a fact:

- The exact fee paths read out of the Finances feed, and the seven that an
  earlier version of the ledger silently dropped.
- Why the deferred-settlement twin has to be discarded, and what counting it
  twice does to every per-period total.
- How a label covering several SKUs is split across an order, and why
  pro-rata rather than equal.
