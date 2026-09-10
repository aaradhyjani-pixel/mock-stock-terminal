#!/bin/bash
# Holds a public link open and reconnects whenever it drops.
#
# Run by launchd, so it survives this terminal, a logout and a reboot.
# The current link is always in /tmp/tunnel_url.txt and printed to
# /tmp/tunnel-agent.log.
#
# localhost.run hands out a new subdomain on each reconnect, so read the file
# rather than remembering the address.

export PATH="/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
cd "/Users/aaradhymanojkumarjani/MOCK STOCK TERMINAL" || exit 1

current=""
while true; do
  ok=0
  if [ -n "$current" ]; then
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 15 "$current/healthz" 2>/dev/null)
    [ "$code" = "200" ] && ok=1
  fi

  if [ "$ok" = "0" ]; then
    pkill -f "nokey@localhost.run" 2>/dev/null
    sleep 2
    : > /tmp/lhr.log
    ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o ServerAliveInterval=20 -o ServerAliveCountMax=3 \
        -R 80:localhost:8000 nokey@localhost.run > /tmp/lhr.log 2>&1 &
    for _ in $(seq 1 15); do
      sleep 3
      current=$(grep -oE 'https://[a-z0-9-]+\.lhr\.life' /tmp/lhr.log 2>/dev/null | head -1)
      [ -n "$current" ] && break
    done
    if [ -n "$current" ]; then
      echo "$current" > /tmp/tunnel_url.txt
      echo "$(date '+%Y-%m-%d %H:%M:%S')  LINK: $current"
    fi
  fi
  sleep 25
done
