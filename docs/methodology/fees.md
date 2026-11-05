# How the fees are measured

**Status: outline. This is the document that has to be right, because it is
the argument for the whole product.**

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

## To be written

- The derivation, with a worked example at three price points showing the
  rate moving as the flat charge is amortised over a larger sale.
- The exact fee paths read out of the Finances feed, and the seven that an
  earlier version of the ledger silently dropped.
- Why the deferred-settlement twin has to be discarded, and what counting it
  twice does to every per-period total.
- How a label covering several SKUs is split across an order, and why
  pro-rata rather than equal.
