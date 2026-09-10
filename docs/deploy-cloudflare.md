# Cloudflare

Two Cloudflare routes exist. One is broken right now, one works but needs a
domain. A third looks tempting and cannot work at all.

## What does not work: Workers or Pages

Cloudflare Workers and Pages run short-lived request handlers. This system needs
a process that stays alive between requests, because the market engine ticks
once a second: it moves prices, sweeps resting orders, runs the margin desk and
broadcasts quotes whether or not anyone made a request.

There is nothing to configure here. Serverless has no place to put a clock, and
porting it would mean rewriting the engine in JavaScript on Durable Objects and
throwing away the tested one. Do not spend time on it.

## What is broken: quick tunnels

`cloudflared tunnel --url http://localhost:8000` creates a tunnel with no
account and no card. As of today it connects fine but Cloudflare never publishes
DNS for the hostname it hands out:

```
$ nslookup fixtures-know-extras-cat.trycloudflare.com 1.1.1.1
** server can't find ...: NXDOMAIN
```

The tunnel registers, reports zero errors, and is simply unreachable. That is
Cloudflare's side; retrying and reinstalling do not help. It worked earlier the
same day and then stopped, so it may come back on its own.

## What works: a named tunnel on your own domain

A named tunnel gives a **stable** hostname such as `terminal.yourdomain.com`,
free, that keeps working across reconnects. The catch is that it needs a domain
whose nameservers point at Cloudflare.

`aaradhyjani.com` currently uses `ns1.dns-parking.com` (Hostinger), so this needs
a nameserver change first.

### Moving the domain to Cloudflare

1. Sign up free at https://dash.cloudflare.com
2. **Add a site**, enter `aaradhyjani.com`, choose the Free plan.
3. Cloudflare imports your existing DNS records. **Check them against Hostinger
   before continuing** — anything missing here goes offline when you switch.
4. Cloudflare shows you two nameservers. Set those at your registrar, replacing
   the Hostinger ones.
5. Propagation takes anywhere from a few minutes to a few hours.

Your existing site keeps working throughout, provided step 3's records are
right. That check is the whole risk of this move, and it is worth doing slowly.

### Then, the tunnel

```bash
cloudflared tunnel login
cloudflared tunnel create mock-stock
cloudflared tunnel route dns mock-stock terminal.aaradhyjani.com
cloudflared tunnel run --url http://localhost:8000 mock-stock
```

Now `https://terminal.aaradhyjani.com` reaches the terminal on your laptop, over
a stable name that survives reconnects.

### Honest limits

It is still your laptop serving 500 people. The machine must stay awake, stay on
the network, and not be closed. Cloudflare only solves the address, not the
hosting.

For the real competition, run it somewhere that stays up on its own:
`docs/deploy-replit.md`, or the `fly.toml` in the repo root.
