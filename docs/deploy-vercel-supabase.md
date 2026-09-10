# Vercel and Supabase

This works, with one qualification you need before you start.

## What goes where

| Piece | Host | Why |
| --- | --- | --- |
| The pages | **Vercel** | 180 KB of static HTML, CSS and JS. No build step. A perfect fit. |
| The database | **Supabase** | Managed PostgreSQL. The app speaks it already. |
| The engine and API | **not Vercel** | See below. |

## Why the engine cannot go on Vercel

Vercel is serverless: a function wakes on a request and dies when it answers.
This system needs a process that keeps running whether or not anyone is
clicking. Once a second it moves every price, fills resting limit and stop
orders, runs the margin desk, and advances the market clock.

Put that on serverless and the market only moves when somebody happens to load a
page. A team watching their phone gets liquidated; a team who stepped out does
not. Same for a stop-loss: it fills for whoever refreshed and not for whoever
did not. That is not a slow competition, it is an unfair one, and no amount of
configuration fixes it.

Supabase cannot host it either. Supabase is Postgres plus edge functions; the
edge functions are Deno and equally short-lived.

So the engine needs somewhere that runs a process. Any of these work, and all of
them cost about the same:

- **Replit Reserved VM** — the repo is already configured, see `docs/deploy-replit.md`
- **Fly.io** — `fly.toml` is in the repo root
- **Render** starter — `render.yaml` is in the repo root
- **Any small VPS**, including Hostinger's — `Dockerfile` is in the repo root

The free tiers of most of these sleep when idle, which stops the market clock.
Whatever you pick must stay awake.

---

## Setting it up

### 1. Supabase, for the database

1. Create a project at https://supabase.com. Pick the Mumbai region.
2. **Project Settings → Database → Connection string → URI**. Copy it.
3. Give it to the engine host as `EXCHANGE_DATABASE_URL`, or as `DATABASE_URL`
   and let the app adopt it.

The app converts the connection string to the async driver and strips the
`sslmode` parameter that asyncpg does not accept, so paste it exactly as
Supabase gives it. Use the **session pooler** string, port 5432, not the
transaction pooler: the order path holds a transaction open across a row lock
and the transaction pooler will not do that.

There are no migrations to run. The schema is created on first boot.

### 2. The engine, wherever you chose

Set:

```
EXCHANGE_DATABASE_URL   the Supabase URI
EXCHANGE_SECRET_KEY     python -c "import secrets; print(secrets.token_urlsafe(48))"
EXCHANGE_SECURE_COOKIES true
EXCHANGE_CORS_ORIGINS   ["https://your-project.vercel.app"]
```

That last one matters. Without it the browser blocks every call from the Vercel
pages, and with the wrong value the sign-in works but the session dies half an
hour later when the silent refresh fails.

Then seed it once:

```bash
python -m scripts.seed --reset --demo-teams 24 --demo-password trade123
```

### 3. Vercel, for the pages

```bash
npx vercel login
npx vercel --prod
```

`vercel.json` already sets `web/` as the output directory and maps `/login`,
`/console`, `/console/login` and `/projector` to their pages.

Then tell the pages where the engine is. In each file in `web/`:

```html
<script>window.EXCHANGE_API_BASE = "https://your-engine-host.example.com";</script>
```

No trailing slash. Empty means same-origin, which is what a single-host
deployment uses. Redeploy after changing it.

### 4. Check it

```bash
curl -s https://your-engine-host/healthz

curl -s -X OPTIONS https://your-engine-host/api/auth/login \
  -H "Origin: https://your-project.vercel.app" \
  -H "Access-Control-Request-Method: POST" -i | grep -i access-control
```

You want `access-control-allow-origin` naming your Vercel URL and
`access-control-allow-credentials: true`. Then open the Vercel URL, sign in as a
team, and place a trade.

---

## What was verified

The split was run end to end locally, pages on one origin and the API on
another:

- CORS preflight returns the allowed origin and credentials, and an origin that
  is not on the list gets no allow-origin header at all
- Sign-in issues cookies as `SameSite=none; Secure`, which is the only way they
  travel cross-origin
- Order preview, order placement and portfolio all work across origins
- The WebSocket connects cross-origin and delivers its opening snapshot of all
  26 instruments

## The failure that will not announce itself

If `EXCHANGE_CORS_ORIGINS` is missing or wrong, sign-in still appears to work,
because the access token goes in a header. The refresh cookie is what breaks,
and it breaks silently: every participant is thrown out roughly half an hour in,
mid-trade, with nothing in any log to explain it.

Check the preflight above before the event, not during it.
