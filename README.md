# Mock Exchange Terminal

A simulated NSE trading terminal for a college mock-stock competition. Teams sign
in, trade a basket of NSE stocks whose prices the organisers control, go long or
short with margin, react to news, and are ranked on a live leaderboard. Built for
about 500 participants in roughly 100 teams on one afternoon.

```
                    one process, one clock, one price
  participants ──┐
  operators    ──┼── FastAPI + WebSocket ── market engine ── PostgreSQL
  projector    ──┘                          (tick loop)      (source of truth)
```

## Quick start

```bash
cd terminal
uv venv .venv --python 3.12 && uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m scripts.seed --reset --demo-teams 8
.venv/bin/python -m app.main
```

Then open:

| Page | Path | Who |
| --- | --- | --- |
| Terminal | `/login` then `/` | Participants |
| Operator console | `/console/login` then `/console` | Organisers |
| Projector board | `/projector` | The big screen |
| API docs | `/api/docs` | Developers |

The seed script prints the operator passwords once. They are not recoverable.
Write them down.

## What it does

**Trading.** Market, limit, stop-loss and stop-limit orders. Day validity.
Order-flow impact, a bid/ask spread, and size-based slippage, so a large order
visibly costs more than a small one. Every fill goes through a single code path
whether it came from a participant, a resting order or the risk desk.

**Margin.** Longs are cash-only; shorts get up to 5x leverage. The whole model is
one line — `available = cash − short_mv − 0.20 × short_mv` — and both rules fall
out of it. Positions are marked every second, warned at 15%, force-covered at
12%, and closed out at zero. See [docs/margin.md](docs/margin.md).

**Market structure.** Timed trading days with a pre-open, per-stock halts, ±20%
price bands with upper and lower circuits, a market-wide breaker, and a synthetic
index (CLUB 50) built from the basket.

**Operations.** Open, close, halt and freeze the market. Move prices to a target
over a duration, jump them instantly (with a typed confirmation), raise
volatility, halt a stock, undo the last price action. Publish news and rumours.
Run a scripted scenario like a playlist. See every team's book, the blotter, the
audit log, and export everything as CSV.

**Results.** When the competition ends, the console shows the final table with the
documented tie-breaks (lower drawdown, then fewer trades), five side awards, and a
printable report card for every team with their equity curve, win rate, best and
worst trade and charges paid. Each team can see its own from the terminal.

**Corporate actions.** Dividends and splits, between trading days. Both are
value-neutral by construction and the tests assert it.

**Practice mode.** One button wipes every trade and returns all teams to their
opening balance, keeping the teams, members and audit log, so the rehearsal the
evening before does not contaminate the real event.

**Reliability.** The books are checked by an invariant test that runs thousands of
randomised trades and asserts that cash always equals the ledger. The operator
console runs the same check on live data at the press of a button.

## Layout

```
app/
  config.py        infrastructure settings, and the rulebook loaded from YAML
  results.py       final table, tie-breaks, awards, report cards
  money.py         exact decimal arithmetic and the Money column type
  models.py        database schema
  risk.py          account valuation, buying power, the margin model
  fees.py          the charge schedule
  security.py      passwords, tokens, role guards, rate limits
  ws.py            WebSocket fan-out
  engine/
    market.py      the tick loop and every market state transition
    pricing.py     price process, quotes, bands, slippage
    matching.py    the order path; execute_fill is the only place money moves
    riskdesk.py    margin warnings, forced covers, bust, borrow fees
    valuations.py  bulk valuation for the margin scan and leaderboard
  routers/         auth, public market data, trading, admin, websockets
config/
  rules.yaml       the rulebook: capital, leverage, fees, session lengths
  instruments.yaml the basket of 26 NSE names
  scenarios/       scripted trading days
web/               the three front ends; no build step, no external assets
scripts/           seed, team import, load test
tests/             109 tests
docs/              architecture, margin derivation, the runbook, the rulebook
```

## Configuration

Infrastructure comes from the environment (prefix `EXCHANGE_`):

```bash
EXCHANGE_DATABASE_URL=postgresql+asyncpg://user:pass@host/exchange
EXCHANGE_SECRET_KEY=<a long random string>
EXCHANGE_SECURE_COOKIES=true
EXCHANGE_PORT=8000
```

The rulebook comes from `config/rules.yaml`. It is what the participant rulebook
documents and what the tests assert, so change it before the event and not
during: the console refuses to reload it while the market is open.

## Running the tests

```bash
.venv/bin/python -m pytest -q          # 109 tests, about fifteen seconds
.venv/bin/python -m pytest tests/test_margin.py -v
```

`tests/test_invariants.py` is the important one. It drives thousands of random
trades, price moves and margin calls, then asserts four properties: cash equals
the ledger, orders equal their fills, positions equal their fills, and equity
equals starting capital plus realised plus unrealised P&L minus charges.

## Deploying on Replit

Full walkthrough in [docs/deploy-replit.md](docs/deploy-replit.md). The short
version: import from GitHub, turn on the PostgreSQL module, set
`EXCHANGE_SECRET_KEY` in Secrets, seed, and deploy as a **Reserved VM**.

The repository ships a `.replit` that runs the app directly.

**The deployment target must stay `vm` (Reserved VM).** Autoscale runs more than
one instance, and two instances means two market engines producing two different
prices for the same stock at the same moment. It also has no persistent disk, so
the SQLite database holding every team's cash would be wiped on each deploy.

Before the event:

1. Set `EXCHANGE_SECRET_KEY` in Replit Secrets. Generate it with
   `python -c "import secrets; print(secrets.token_urlsafe(48))"`. The app logs a
   loud warning at startup while the built-in default is still in place, because
   anyone who has read this repository could otherwise forge a login.
2. Enable the PostgreSQL module. Replit then sets `DATABASE_URL` and the app
   adopts it automatically. Do not run the event on SQLite here: a Repl's
   filesystem is replaced on redeploy, and PostgreSQL is not.

**Rehearse the WebSockets.** The one thing that cannot be verified from a laptop
is how many concurrent WebSocket connections the platform will hold. Deploy, open
the terminal on at least twenty real phones on the venue network, leave them
connected for a full 25-minute trading day, and check `/healthz` for dropped
sockets. If the connections do not hold, the fallback is a small VPS and the only
change is the database URL.

The terminal degrades rather than breaking: after three failed reconnects it falls
back to polling every two seconds, says so, and keeps trading working, because the
server prices every order anyway.

## Deployment notes

**One process.** Run with a single worker. Two workers means two engines, two
clocks and two different prices. The concurrency guarantees in this system come
from a per-team `asyncio.Lock` held across the read, the funds check, the write
and the commit; that only holds inside one process. On PostgreSQL a row lock is
taken as well, which protects against a stray `psql` session but is not a licence
to run a second worker.

**PostgreSQL for the event.** SQLite is fine for development and is exact here
(money is stored as a scaled integer), but use PostgreSQL on the day, with
continuous archiving so a restore is minutes rather than hours.

**Restart behaviour.** If the process dies while the market is open, it comes back
`FROZEN` with the remaining time preserved, and an operator decides when to
resume. The clock never silently runs on while nobody could trade.

**No external assets.** The front end loads no fonts, scripts or styles from any
CDN. The chart is hand-drawn on a canvas. If the venue's internet fails but the
local network holds, the terminal still works.

## Before the event

The full sequence is in [docs/runbook.md](docs/runbook.md). The short version:

1. Refresh `config/instruments.yaml` with real prices a day or two before.
2. Agree every value in `config/rules.yaml` and publish
   [docs/rulebook.md](docs/rulebook.md) to participants.
3. Import teams from CSV, print the credential cards.
4. Run `scripts/loadtest.py` at three times expected load.
5. Hold a dress rehearsal on the real network, then a practice session the
   evening before.
