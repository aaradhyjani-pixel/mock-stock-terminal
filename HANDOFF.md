# Working on this from another machine or another Claude Code account

Everything is in one private GitHub repository. Anyone with access can clone it,
run it, change it and push back.

```
https://github.com/aaradhyjani-pixel/mock-stock-terminal
```

---

## Step 1 — give the other account access

The repo is **private**, so this step is required. Pick one.

### A. Add them as a collaborator (recommended)

Run this on **your** machine, replacing the username:

```bash
gh repo add-collaborator aaradhyjani-pixel/mock-stock-terminal THEIR_GITHUB_USERNAME --permission push
```

They get an email invite. Once they accept, they have full access.

### B. If it is your own second account

Nothing to do here. Just sign in as yourself in step 2.

### C. Make it public (only after the event)

```bash
gh repo edit aaradhyjani-pixel/mock-stock-terminal --visibility public --accept-visibility-change-consequences
```

**Do not do this before the competition.** `config/scenarios/` contains the
scripted news and price moves for every trading day. Anyone who reads it knows
each headline before it breaks, which ends the competition rather than helps it.

---

## Step 2 — on the other machine, set up

Paste this whole block into a Terminal:

```bash
gh auth login
```

Choose GitHub.com, HTTPS, and authenticate in the browser. Then:

```bash
cd ~
git clone https://github.com/aaradhyjani-pixel/mock-stock-terminal.git
cd mock-stock-terminal
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

The tests should say **109 passed**. If they do, the whole system works on that
machine.

---

## Step 3 — run it

```bash
.venv/bin/python -m scripts.seed --reset --demo-teams 24 --demo-password trade123
.venv/bin/python -m app.main
```

The seed prints the operator passwords once. Write them down. Then open
http://localhost:8000

To put it online for phones, in a second Terminal window:

```bash
cd ~/mock-stock-terminal && ./tunnel.sh
```

Leave that window open. It prints the public link and reconnects whenever the
tunnel drops.

---

## Step 4 — start Claude Code there

```bash
cd ~/mock-stock-terminal
claude
```

Then paste this as the first message so it knows what it is looking at:

> This is a simulated NSE trading terminal for a college mock-stock competition,
> about 500 participants in 100 teams. Read README.md and docs/architecture.md
> first. The rules that matter: one process only, because a second one means a
> second market engine and two different prices for the same stock; money is
> always Decimal and never float; every fill goes through execute_fill in
> app/engine/matching.py; and the margin model is one formula,
> available = cash - 1.2 x short_mv, documented in docs/margin.md. Run
> `.venv/bin/python -m pytest -q` before and after any change; 109 tests must
> pass.

---

## Step 5 — change something and push it back

```bash
git checkout -b my-change
# ... edit ...
.venv/bin/python -m pytest -q          # must stay at 109 passed
git add -A
git commit -m "what changed and why"
git push -u origin my-change
```

Then open a pull request:

```bash
gh pr create --fill
```

Or push straight to main if you are the only one working on it:

```bash
git checkout main && git add -A && git commit -m "..." && git push
```

---

## Pulling those changes back to this machine

```bash
cd "/Users/aaradhymanojkumarjani/MOCK STOCK TERMINAL"
git pull
```

---

## What is where

| Path | What it is |
| --- | --- |
| `app/engine/matching.py` | The order path. `execute_fill` is the only place money moves. |
| `app/engine/market.py` | The tick loop and every market state transition. |
| `app/risk.py` | Account valuation and the margin model. |
| `app/results.py` | Final table, tie-breaks, awards, report cards. |
| `config/rules.yaml` | The rulebook: capital, leverage, fees, session lengths. |
| `config/instruments.yaml` | The 26 NSE stocks. Refresh prices before the event. |
| `config/scenarios/` | Scripted news and price moves per day. Keep private. |
| `web/` | The three front ends. No build step, no external assets. |
| `docs/runbook.md` | What to do on event day, including incidents. |
| `docs/rulebook.md` | The page participants read before signing in. |

## Things that will trip you up

**Do not run more than one process.** No `--workers 2`, no autoscaling. Two
processes means two market engines and two different prices for the same stock
at the same moment.

**Do not change `config/rules.yaml` while the market is open.** The console
refuses to reload it, and the participant rulebook quotes those numbers.

**Reset is super admin only.** Sign in as `director`, not `market`. This has
already cost time once.

**The free tunnels drop.** localhost.run and Cloudflare quick tunnels both hand
out a new URL on reconnect and go down without warning. Fine for practice, not
for the real event. For that, deploy properly: `docs/deploy-replit.md` or the
`fly.toml` in the repo root.
