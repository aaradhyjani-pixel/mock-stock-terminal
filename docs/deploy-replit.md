# Deploying on Replit

Roughly twenty minutes, most of it waiting. Do it at least a week before the
event, not the night before.

## 1. Import the repository

In Replit: **Create Repl → Import from GitHub**, and paste the repository URL.

Replit reads the `.replit` file in the repo, so the Python version, the run
command and the deployment target are already set.

## 2. Turn on PostgreSQL

In the Repl: **Tools → Database → Create a database**.

This sets `DATABASE_URL` in the environment, and the app adopts it
automatically. You do not need to set `EXCHANGE_DATABASE_URL`.

Use PostgreSQL rather than SQLite here. A Repl's filesystem is replaced on
redeploy, and a redeploy that takes every team's cash balance with it is not a
recoverable situation halfway through a competition.

## 3. Set the secret key

**Tools → Secrets**, add:

| Key | Value |
| --- | --- |
| `EXCHANGE_SECRET_KEY` | a long random string |

Generate one in the Repl shell:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

This signs every session token. While it is left at the built-in default,
anyone who has read this repository can forge a login as any team or any
operator, and the app logs a loud warning at startup saying so.

## 4. Install and seed

In the Repl shell:

```bash
pip install -e .
python -m scripts.seed --reset --demo-teams 8
```

The seed prints the seven operator passwords **once**. They are stored only as
scrypt hashes and cannot be recovered. Write them down before you close the
shell.

## 5. Run it

Press **Run**. Open the preview and check:

- `/healthz` reports the database up and the engine running
- `/login` shows the market clock
- `/console/login` accepts the `director` account

## 6. Publish

**Deploy → Reserved VM.**

> **Not Autoscale.** Autoscale runs more than one instance, and two instances
> means two market engines producing two different prices for the same stock at
> the same moment. It also sleeps between requests, which would stop the market
> clock every time the hall went quiet. The `.replit` file pins
> `deploymentTarget = "vm"`; leave it alone.

The smallest Reserved VM is enough. The measured tick, doing prices, resting
orders, the margin scan and the broadcast, is about 6 ms, and the load is 500
sockets receiving roughly 3 KB once a second.

## 7. Rehearse the connections

This is the step people skip and the one that matters.

The number of concurrent WebSocket connections a platform will hold is not
something anyone can tell you from a laptop. Before you trust this with 500
people:

1. Deploy.
2. Get at least twenty real phones onto the venue Wi-Fi, signed in as practice
   teams.
3. Leave them connected through a full 25-minute trading day.
4. Check `/healthz`: `websockets.connections` should equal the number of open
   devices, and `dropped_messages` should stay near zero.

If connections do not hold, the terminal degrades rather than breaking. After
three failed reconnects it falls back to polling every two seconds, tells the
participant so, and keeps trading working, because the server prices every order
regardless of how the client heard about the last quote. But degraded is not
where you want 500 people to start.

If the rehearsal goes badly, the fallback is a small VPS in Mumbai. Nothing in
the code changes; only `EXCHANGE_DATABASE_URL` and where you run it.

## 8. Before the doors open

- [ ] `python -m scripts.import_teams teams.csv --cards cards.html`, print the cards
- [ ] Console → **Check the books balance** must say every team reconciles
- [ ] Practice session, then console → **Reset the competition** (type RESET)
- [ ] Set the market to PRE_OPEN for day 1
- [ ] Read `docs/runbook.md`

## Things that will bite you on Replit

**The Repl sleeps.** A development Repl stops when you close the tab. That is
fine while building and fatal during an event, which is why the deployment is a
Reserved VM: it stays up.

**A redeploy restarts the app.** The engine handles this correctly. It comes
back `FROZEN` with the remaining session time preserved, and an operator decides
when to resume. It does not silently run the clock on while nobody could trade.
Do not redeploy mid-competition anyway.

**Secrets are not in the repository.** If you fork or re-import the Repl, set
`EXCHANGE_SECRET_KEY` again. Everyone's sessions end when it changes, which is
the correct behaviour but a surprise if you were not expecting it.

**The database survives redeploys, the filesystem does not.** Anything written
to disk by the app is disposable. Everything that matters is in PostgreSQL.
