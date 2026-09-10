#!/bin/bash
# Keeps the exchange running for a practice session.
#
# Restarts the server if it dies, keeps the Mac awake, and prints the address
# to hand out. Run it in a Terminal window and leave that window open:
#
#   ./keepalive.sh
#
# Stop it with Ctrl-C.

cd "$(dirname "$0")" || exit 1
export PATH="/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

# Stop the Mac sleeping. A sleeping laptop takes the whole market with it.
pkill -f "caffeinate -dimsu" 2>/dev/null
caffeinate -dimsu &
CAFFEINE=$!
trap 'kill $CAFFEINE 2>/dev/null; exit 0' INT TERM

last_ip=""
while true; do
  if ! pgrep -f "app.main" > /dev/null; then
    echo "$(date '+%H:%M:%S')  server is down, restarting"
    (.venv/bin/python -m app.main > /tmp/exchange.log 2>&1 &)
    sleep 6
  fi

  ip=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null)
  if [ "$ip" != "$last_ip" ]; then
    # The address changes when the network hands out a new lease. Anyone
    # already signed in keeps trading; new joiners need the new address.
    echo
    echo "  ============================================================"
    echo "   PLAYERS      http://$ip:8000"
    echo "   ORGANISER    http://$ip:8000/console/login"
    echo "   BIG SCREEN   http://$ip:8000/projector"
    echo "  ============================================================"
    echo
    last_ip="$ip"
  fi
  sleep 10
done
