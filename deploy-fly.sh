#!/bin/bash
# Deploy the exchange to Fly.io.
#
# Run this after `fly auth login`. It waits for authentication, then does the
# whole deploy unattended: creates the app, creates the persistent volume,
# generates and sets the secret key, ships the image, and verifies the result.
#
#   ./deploy-fly.sh
#
# Safe to re-run. Every step checks whether it has already been done.

set -uo pipefail
export PATH="/opt/homebrew/bin:$HOME/.fly/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
cd "$(dirname "$0")" || exit 1

APP=$(grep '^app = ' fly.toml | sed 's/app = "//;s/"//')
REGION=$(grep '^primary_region = ' fly.toml | sed 's/primary_region = "//;s/".*//')
say() { echo "[$(date '+%H:%M:%S')] $*"; }

# ---------------------------------------------------------------- wait for auth
say "waiting for 'fly auth login' to complete (up to 20 minutes)"
for _ in $(seq 1 240); do
  if flyctl auth whoami >/dev/null 2>&1; then
    say "authenticated as $(flyctl auth whoami 2>/dev/null)"
    break
  fi
  sleep 5
done
if ! flyctl auth whoami >/dev/null 2>&1; then
  say "STILL NOT AUTHENTICATED. Run 'fly auth login' in your Terminal, then re-run this script."
  exit 1
fi

# -------------------------------------------------------------------- create app
if flyctl apps list 2>/dev/null | grep -q "^$APP"; then
  say "app $APP already exists"
else
  say "creating app $APP"
  flyctl apps create "$APP" --org personal 2>&1 | tail -3
fi

# ------------------------------------------------------------------ secret key
# Signs every session token. Without a real one, anyone who has read the source
# could forge a login as any team or operator.
if flyctl secrets list -a "$APP" 2>/dev/null | grep -q EXCHANGE_SECRET_KEY; then
  say "secret key already set"
else
  say "generating and setting the secret key"
  KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
  flyctl secrets set EXCHANGE_SECRET_KEY="$KEY" -a "$APP" --stage 2>&1 | tail -2
fi

# ---------------------------------------------------------------------- volume
# The database lives here. It survives deploys and restarts; the rest of the
# filesystem does not.
if flyctl volumes list -a "$APP" 2>/dev/null | grep -q exchange_data; then
  say "volume already exists"
else
  say "creating a 1GB volume in $REGION"
  flyctl volumes create exchange_data --region "$REGION" --size 1 -a "$APP" --yes 2>&1 | tail -3
fi

# ---------------------------------------------------------------------- deploy
say "deploying (this builds the image remotely, usually 2-4 minutes)"
flyctl deploy -a "$APP" --remote-only --wait-timeout 600 2>&1 | tail -25
DEPLOY_STATUS=$?

# ---------------------------------------------------------------------- verify
URL="https://$APP.fly.dev"
say "verifying $URL"
for i in $(seq 1 30); do
  CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 15 "$URL/healthz" || echo 000)
  if [ "$CODE" = "200" ]; then
    say "LIVE at $URL"
    curl -s --max-time 15 "$URL/healthz" | python3 -m json.tool | head -12
    echo
    echo "  ============================================================"
    echo "   PLAYERS      $URL"
    echo "   ORGANISER    $URL/console/login"
    echo "   BIG SCREEN   $URL/projector"
    echo "  ============================================================"
    echo "$URL" > /tmp/fly_url.txt
    exit 0
  fi
  say "  attempt $i: $CODE, waiting"
  sleep 10
done

say "did not come up. Recent logs:"
flyctl logs -a "$APP" --no-tail 2>&1 | tail -30
exit 1
