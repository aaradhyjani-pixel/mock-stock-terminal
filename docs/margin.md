# The margin model

This is the most dispute-prone part of any trading game, so it is written down
here in full. Every number a participant sees is derivable from this page, and
`tests/test_margin.py` asserts every worked example in it.

## The one formula

```
long_mv      = Σ (long qty × last price)
short_mv     = Σ (short qty × last price)

equity       = cash + long_mv − short_mv          ← what the leaderboard ranks
margin_req   = initial_margin × short_mv          initial_margin = 20%
available    = cash − short_mv − margin_req
             = cash − 1.20 × short_mv
```

A trade is allowed when it would leave `available` at or above zero. That single
rule produces both of the competition's rules without them being written
separately, which is what stops them drifting apart:

**Cash-only longs.** Buying spends cash and adds nothing to `available`, so a
long position must be paid for in full. `available` falls by exactly the cost of
the purchase.

**5x on shorts.** Shorting credits the sale proceeds to cash but locks those
proceeds plus a further 20%, so opening a short costs 20% of its value in
buying power. Short exposure can therefore reach five times account value and
not a rupee more.

Holdings are not collateral. A team that spends its whole balance on stock has
no buying power left and cannot short until it sells something. That is a
deliberate simplification: one margin model means one thing to explain and one
thing to argue about.

## Thresholds

| Level | Value | What happens |
| --- | --- | --- |
| Initial margin | 20% of short value | The cost of opening a short |
| Warning | equity < 15% of short value | Banner, sound, a line in the activity log |
| Maintenance | equity < 12% of short value | The engine covers shorts at market |
| Liquidate to | equity ≥ 20% of short value | Where forced covering stops |
| Bust | equity ≤ 0 | All positions closed, trading over |

At full 5x leverage an adverse move of about 8% triggers the call and about 20%
wipes the account out. That is the lesson the leverage rule exists to teach, so
the terminal shows both the leverage multiple and the distance to a margin call
prominently.

## Worked example

One thousand shares of Reliance shorted at 2,500 on 10 lakh of capital. Charges
are ignored here for clarity; the engine charges them.

| Step | Cash | Short MV | Equity | Available | Leverage |
| --- | ---: | ---: | ---: | ---: | ---: |
| Start | 10,00,000 | 0 | 10,00,000 | 10,00,000 | 0.0x |
| Short 1,000 at 2,500 | 35,00,000 | 25,00,000 | 10,00,000 | 5,00,000 | 2.5x |
| Price → 2,700 | 35,00,000 | 27,00,000 | 8,00,000 | 2,60,000 | 3.4x |
| Short 1,000 more at 2,700 | — | — | — | −2,80,000 | — |

That fourth row is **rejected**: it would need 5,40,000 of buying power (20% of
27 lakh) and only 2,60,000 is available.

| Step | Cash | Short MV | Equity | Maintenance | State |
| --- | ---: | ---: | ---: | ---: | --- |
| Price → 3,150 | 35,00,000 | 31,50,000 | 3,50,000 | 3,78,000 | Margin call |

Equity of 3,50,000 is below the 3,78,000 maintenance requirement, so the risk
desk acts.

## What the risk desk does

It buys back **only as much as it takes** to restore the 20% buffer, worst loser
first, not the whole position. In the example above it covers 445 shares, leaving
555 short:

```
shortfall = 0.20 × 31,50,000 − 3,50,000 = 2,80,000
shares    = ceil(2,80,000 ÷ (0.20 × 3,150)) = 445
```

After covering, equity is unchanged at 3,50,000 — covering is equity-neutral,
because cash falls by exactly the value that leaves the short book, and the loss
was already recognised in the mark. Short value is now 17,48,250, and 3,50,000 is
above 20% of that, so the account is out of trouble with room to spare.

Partial liquidation is what a real risk desk does. It is less punitive than a
full close-out and it leaves the team with a position they chose, while still
stopping the bleeding.

If equity reaches zero the team is marked BUSTED: everything is closed, trading
stops, and they stay on the leaderboard at ₹0. The ledger keeps the true
(possibly negative) number — falsifying it to tidy a leaderboard is how audit
trails stop being trustworthy — but the board shows the floor.

## Borrow fee

At each day's close every open short is charged 0.05% of its market value as a
`BORROW_FEE` ledger row. Small enough not to matter for a trade held ten minutes,
large enough that carrying a big short book across all five days costs something.

## Where the numbers live

All of them are in `config/rules.yaml` under `margin:`. Change them before the
event and publish the change in the rulebook. The console refuses to reload the
rulebook while the market is open, so a rate cannot move under a live position.
