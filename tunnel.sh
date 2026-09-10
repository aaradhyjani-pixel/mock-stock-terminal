#!/bin/bash
# Keeps a public HTTPS link alive for a practice session.
#
#   ./tunnel.sh
#
# Leave the window open. It restarts the server and the tunnel whenever either
# drops, keeps the Mac awake, and prints the link. localhost.run hands out a new
# subdomain on each reconnect, so watch this window for the current one.
#
# Needs no account, no card, no signup.

set -u
export PATH="/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
cd "$(dirname "$0")" || exit 1

pkill -f "caffeinate -dimsu" 2>/dev/null
caffeinate -dimsu & CAFFEINE=$!
trap 'kill $CAFFEINE 2>/dev/null; pkill -f "localhost.run"; exit 0' INT TERM

current=""
while true; do
  if ! pgrep -f "app.main" > /dev/null; then
    echo "$(date '+%H:%M:%S')  server down, restarting"
    (.venv/bin/python -m app.main > /tmp/exchange.log 2>&1 &)
    sleep 6
  fi

  alive=0
  if [ -n "$current" ]; then
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 15 "$current/healthz")
    [ "$code" = "200" ] && alive=1
  fi

  if [ "$alive" = "0" ]; then
    echo "$(date '+%H:%M:%S')  tunnel down, reconnecting"
    pkill -f "localhost.run" 2>/dev/null; sleep 3
    : > /tmp/lhr.log
    ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o ServerAliveInterval=20 -o ServerAliveCountMax=3 \
        -R 80:localhost:8000 nokey@localhost.run > /tmp/lhr.log 2>&1 &
    for _ in $(seq 1 12); do
      sleep 4
      current=$(grep -oE 'https://[a-z0-9-]+\.lhr\.life' /tmp/lhr.log | head -1)
      [ -n "$current" ] && break
    done
    if [ -n "$current" ]; then
      echo "$current" > /tmp/tunnel_url.txt
      lan=$(ipconfig getifaddr en0 2>/dev/null)
      echo
      echo "  ================================================================"
      echo "   PLAYERS     $current"
      echo "   ORGANISER   $current/console/login    director / admin123"
      echo "   BIG SCREEN  $current/projector"
      echo "   ON WI-FI    http://$lan:8000"
      echo "  ================================================================"
      echo
    else
      echo "$(date '+%H:%M:%S')  could not get a link, retrying"
    fi
  fi
  sleep 20
done
