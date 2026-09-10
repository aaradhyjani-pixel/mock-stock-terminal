# Putting it on terminal.aaradhyjani.com

Your nameservers stay at Hostinger. Your email and your site are never touched.
The only change to your DNS is adding new records for a subdomain that does not
exist yet, which cannot affect anything that already works.

Total time: about 20 minutes, plus DNS propagation.

---

## Part 1 — get the app running on Replit

You already have a Replit account (`@aaryrobocode`).

**1. Import the repository.** Open:

> https://replit.com/github/aaradhyjani-pixel/mock-stock-terminal

The repo is private, so Replit asks to connect your GitHub account the first
time. Authorise it. Replit reads the `.replit` file already in the repo, so the
Python version, the run command and the deployment target are set for you.

**2. Turn on the database.** In the Repl: **Tools → Database → Create a
database**. This sets `DATABASE_URL`, and the app picks it up automatically.
Nothing to configure.

**3. Set the secret key.** **Tools → Secrets**, add:

| Key | Value |
| --- | --- |
| `EXCHANGE_SECRET_KEY` | paste the output of the command below |

In the Repl shell:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

This signs every session token. Until it is set, anyone who has read the
repository could forge a login as any team or any operator, and the app logs a
warning at startup saying exactly that.

**4. Install and seed.** In the Repl shell:

```bash
pip install -e .
python -m scripts.seed --reset --demo-teams 24 --demo-password trade123
```

It prints seven operator passwords **once**. They are stored only as hashes and
cannot be recovered. Write them down before closing the shell.

**5. Check it.** Press **Run**, open the preview, and confirm `/healthz` shows
the database up and the engine running.

---

## Part 2 — publish it

**Deploy → Reserved VM.** The smallest size is enough; the tick costs about 6 ms.

> **Not Autoscale.** Autoscale runs more than one instance, and two instances
> means two market engines producing two different prices for the same stock at
> the same moment. It also sleeps between requests, which would stop the market
> clock every time the hall went quiet. The `.replit` file already pins
> `deploymentTarget = "vm"`.

Reserved VM is the paid tier, around $7 a month. You can cancel after the event.

When it finishes you get a URL like `https://mock-stock-terminal.replit.app`.
Check it works before going any further.

---

## Part 3 — attach your domain

**In Replit:** open your deployment → **Settings → Custom domain** → enter
`terminal.aaradhyjani.com`.

Replit then shows you **two records**. They look like this, but use the exact
values Replit gives you, not these:

| Type | Name | Value |
| --- | --- | --- |
| A or CNAME | `terminal` | (what Replit shows) |
| TXT | `terminal` | `replit-verify=...` |

**In Hostinger:** hPanel → **Domains → aaradhyjani.com → DNS / Nameservers →
Manage DNS records**.

Add both records exactly as Replit gave them. For the Name field enter
`terminal`, not the full `terminal.aaradhyjani.com` — Hostinger appends the
domain for you.

**Do not touch anything else on that page.** Leave every existing record alone,
in particular:

- the two `A` records and two `AAAA` records on `@`
- the `www` CNAME to `cdn.hstgr.net`
- both `MX` records to `mx1`/`mx2.hostinger.com`
- the `TXT` SPF record

Those are your website and your email. You are only adding new rows.

Propagation is usually 5 to 30 minutes. Replit shows the domain as verified when
it is done.

---

## Part 4 — check it

```bash
dig +short terminal.aaradhyjani.com
curl -s https://terminal.aaradhyjani.com/healthz
```

And confirm nothing else moved:

```bash
dig +short MX aaradhyjani.com          # must still be mx1/mx2.hostinger.com
curl -s -o /dev/null -w "%{http_code}\n" https://aaradhyjani.com   # must be 200
```

Send yourself an email to be certain.

---

## Then

- Import your real teams and print the cards:
  `python -m scripts.import_teams teams.csv --cards cards.html`
- Run a practice session, then console → **Reset the competition** (type RESET)
- Read `docs/runbook.md` before the day
- Open the console at `https://terminal.aaradhyjani.com/console/login`

## If something is wrong

**Domain will not verify.** Check the Name field is `terminal` and not the full
hostname. Hostinger appending the domain to a full hostname produces
`terminal.aaradhyjani.com.aaradhyjani.com`, which is the usual cause.

**Site or email broke.** You edited an existing record. Hostinger keeps DNS
history in hPanel; restore it there. The records to compare against are listed
in Part 3.

**Deployment sleeps or the clock stops.** You are on Autoscale, not Reserved VM.
Change it in deployment settings.
