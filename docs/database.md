# The database

The app runs on PostgreSQL or SQLite and the two agree to the paisa. Money is a
`Decimal` in Python and is stored as `NUMERIC(24,6)` on PostgreSQL and as a
scaled integer on SQLite, so a test that passes on one means something about the
other. That is why the suite can run on either.

    .venv/bin/python -m pytest -q                       # SQLite, ~13s

    TEST_DATABASE_URL=postgresql+asyncpg://exchange:exchange@127.0.0.1/exchange \
      .venv/bin/python -m pytest -q                     # PostgreSQL, ~21s

Run the PostgreSQL pass before the event. One code path only executes there: the
order path takes `SELECT ... FOR UPDATE` on the team row, and on SQLite that is
a no-op.

## Which to use

**PostgreSQL for the event.** It is what Supabase and every managed host give
you, it survives a redeploy, and the row lock is real.

**SQLite for development**, and for a practice run on a laptop where there is
one process and nothing to coordinate.

## Local PostgreSQL

```bash
brew install postgresql@16
brew services start postgresql@16
createdb exchange
psql -d postgres -c "CREATE ROLE exchange LOGIN PASSWORD 'exchange';"
psql -d postgres -c "GRANT ALL PRIVILEGES ON DATABASE exchange TO exchange;"
psql -d exchange -c "GRANT ALL ON SCHEMA public TO exchange;"

export EXCHANGE_DATABASE_URL="postgresql+asyncpg://exchange:exchange@127.0.0.1:5432/exchange"
.venv/bin/python -m scripts.seed --reset --demo-teams 24 --demo-password trade123
.venv/bin/python -m app.main
```

There are no migrations. The schema is created on first boot.

## Supabase

Paste the **session pooler** URI from Project Settings → Database, port 5432, as
`EXCHANGE_DATABASE_URL`. The app converts it to the async driver and strips the
`sslmode` parameter asyncpg will not accept.

Do not use the transaction pooler. The order path holds a transaction open
across a row lock, and a transaction pooler will not keep the session that
requires.

## What was verified on real PostgreSQL

- All 112 tests pass, the same as on SQLite
- `teams.cash` is `numeric(24,6)`; an opening balance stores as
  `1000000.000000`, not a float
- The row lock holds: six simultaneous orders from one team, sized so only two
  fit inside ten lakh, produced exactly two fills and available funds never went
  negative
- A load test left 931 fills and 1886 ledger rows, and every team still
  reconciled, checked by PostgreSQL itself:

```sql
SELECT count(*) FROM teams t
JOIN (SELECT team_id, sum(amount) s FROM ledger GROUP BY team_id) l ON l.team_id = t.id
WHERE round(t.cash, 2) <> round(l.s, 2);   -- must be 0
```

Run that query during any break. It is the same check the console's
**Check the books balance** button performs.

## Backups

```bash
pg_dump "$EXCHANGE_DATABASE_URL" > backup-$(date +%Y%m%d-%H%M).sql
```

Take one at every day's close and before announcing results. Supabase and most
managed hosts also take their own, but a dump you hold is a dump you can restore
without logging into anything.
