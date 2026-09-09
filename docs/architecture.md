# Architecture

## The shape

```
 participants ─┐
 operators   ──┼─→ FastAPI (uvicorn, one worker)
 projector   ──┘      ├── REST API
                      ├── WebSocket hub  ──→ fan-out to every client
                      └── market engine  ──→ asyncio task, one tick a second
                                 │
                                 └──→ PostgreSQL (source of truth)
```

One process. That is the central decision and everything else follows from it.

## Why one process

A trading simulation has to answer one question consistently: *what is the price
right now?* With two workers there are two engines, two random walks and two
answers, and a fill becomes indefensible the moment a team asks why they got
what they got.

One process also makes the concurrency story small enough to prove. Two members
of the same team tapping Buy at the same instant are serialised by an
`asyncio.Lock` held across the read, the funds check, the write and the commit.
`tests/test_invariants.py` fires five simultaneous orders that each fit
individually but not together, and asserts that exactly two fill.

On PostgreSQL the order path also takes a row lock on the team, which protects
against a stray `psql` session or a migration script. That is belt and braces,
not permission to run a second worker. **Run with `workers=1`.**

The load this has to carry is small: 500 sockets, a broadcast of about 3 KB once
a second, and a peak of perhaps 50 orders a second in the ten seconds after a big
headline. A single core handles that with room to spare. The measured tick, doing
prices, resting orders, the margin scan and the publish, is around 6 ms.

## The tick

Once a second while the market is open:

1. Fire any scenario steps that have come due.
2. Move every price: operator intent (drift toward a target), noise scaled to the
   instrument's daily volatility, and order-flow impact. Snap to the tick grid,
   clamp to the price band, set the circuit status.
3. Persist ticks and roll 1-minute and 5-minute candles.
4. Sweep resting limit and stop orders through the same fill path as live orders.
5. Run the risk desk over every team holding a short.
6. Publish quotes; every 10 seconds the leaderboard, every 30 the snapshots.
7. Advance the session clock.

A tick that raises is logged and skipped. The next one continues from the last
persisted state, because all of that state is in the database rather than in
memory.

## Data integrity

**Money is exact.** Every rupee amount is a `Decimal`. The only floating point in
the system is the random-walk multiplier in `pricing.py`, and its output is
snapped to the tick grid before it becomes a price. The `Money` column type
stores six decimal places as a native `NUMERIC` on PostgreSQL and as a scaled
integer on SQLite, so the two backends agree exactly and a test that passes
locally means something about production.

**The ledger is the truth.** `ledger` is append-only. `teams.cash` is a cache of
its sum. Nothing writes to `teams.cash` without writing the matching ledger row
in the same transaction, and `POST /api/admin/invariants/check` recomputes and
compares on demand.

**One fill path.** `engine/matching.execute_fill` is the only function that moves
cash, changes a position or writes a ledger row. Market orders, resting limits,
triggered stops and forced margin covers all converge on it.

**Idempotent orders.** Every submission carries a `client_order_id`. A retry after
a dropped connection returns the original order rather than placing a second one,
which is the exact failure mode of a phone on venue Wi-Fi.

## Why a dealer, not an order book

Participants trade against the house at a quoted spread, with slippage for size,
rather than against each other. With 100 teams and 26 stocks, a real matching
engine would leave most stocks with no resting orders most of the time:
participants would see empty books and unfillable orders. The dealer model gives
a fill every time at a defensible price, which is what trading on a retail app
actually feels like. A synthesised depth ladder is shown for realism.

## Three prices per instrument

Worth knowing, because the difference matters exactly once:

- `last_price` is what it trades at now.
- `start_price` is what the index is measured against. A split scales it, so
  that changing the units of a stock does not move the index.
- `seed_price` is what the instrument was seeded at, and nothing ever changes it.
  A competition reset restores from here, so a split during the practice session
  does not leave the stock permanently at half price.

## The front end

No build step and no external assets. No web fonts, no CDN scripts, no charting
library; the candlestick chart is drawn by hand on a canvas in about 250 lines.
At a venue where the local network works but the internet does not, every
external request is a way for the terminal to look broken to 500 people at once.

Money crosses the wire as strings and is never parsed into a JavaScript number
for arithmetic. The client never computes a fill price, a charge or a margin
figure; it asks `POST /api/orders/preview` and displays the answer. Two
implementations of the margin formula is one too many.

## Failure modes

| Failure | Effect | Recovery |
| --- | --- | --- |
| Engine task raises | One tick skipped | Logged; the next tick continues from persisted state |
| Process dies | Everything stops | Restarts and comes back **FROZEN** with the remaining time preserved, so an operator decides when to resume |
| Database unavailable | Orders rejected | The engine cannot persist, so it stops advancing; freeze and wait |
| Host dies | Everything down | Spare host with a restored backup; the market was frozen by definition |
| Slow client | Nothing | Its queue drops the oldest quote; durable messages (fills, margin calls) evict quotes to fit |
| Client reconnects | Brief gap | First message on a new socket is a full snapshot, so there is no partial state to reconcile |
| WebSockets unavailable entirely | Degraded, not broken | After three failed reconnects the client polls REST every two seconds and says so. Trading still works: the server prices every order regardless |

## What is deliberately not here

- **Long leverage.** Cash-only longs, one margin model, fewer arguments.
- **Good-till-cancelled orders.** Day validity only; GTC adds state for little
  value in a five-day competition.
- **Partial fills on market orders.** Slippage is capped at 10%, so a market
  order always fills completely. Only limit orders fill partially.
- **Reversing trades.** Operators can undo a price action and adjust cash with a
  second operator's approval. A fill that happened, happened.
