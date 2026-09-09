# Event-day runbook

Print this. The one rule that matters: **freeze first, diagnose second.**

## T minus 7 days

- [ ] Code freeze. Only config and news change from here.
- [ ] Final scenario scripts reviewed by two people.
- [ ] `config/rules.yaml` agreed and locked. Publish `docs/rulebook.md` to teams.
- [ ] Refresh `config/instruments.yaml` with real prices, or prepare a
      `prices.csv` for `scripts/seed.py --prices`.
- [ ] Team CSV final. Run the import, print the credential cards.
- [ ] Confirm the venue network: SSID, client capacity, and whether the ops desk
      can get a wired port. Ask what the public IP is and whether the AP has a
      client limit.
- [ ] `python -m scripts.loadtest --clients 1500 --orders-per-second 200`.

## T minus 1 day

- [ ] Practice session, 45 minutes, real teams, throwaway leaderboard.
- [ ] Afterwards, console → **Reset the competition** (type RESET). This wipes
      every practice trade and returns all teams to ten lakh. Teams, members and
      the audit log survive, so nobody needs a new card. Then run **Check the
      books balance** to confirm the slate is clean.
- [ ] Restore last night's backup onto the spare host and boot it. Time it.
- [ ] Charge the hotspots. Print this runbook and the credential cards.

## T minus 60 minutes

- [ ] `/healthz` green: database, engine running, tick latency under 100 ms.
- [ ] Console → **Check the books balance**. Must say all teams reconcile.
- [ ] Market in PRE_OPEN for day 1, prices at reference, index at 20,000.
- [ ] Projector signed in and showing the countdown.
- [ ] Help desk signed in. Broadcast: "Doors open, sign in and check your funds."

## Each trading day

1. Market operator loads the day's script and presses **Play** on the MC's cue.
2. News desk follows the script. Improvise only with the market operator's nod;
   the two desks are separate hands on purpose.
3. Help desk watches the blotter for rejects clustering on one reason. Several
   teams hitting the same rejection usually means a rule nobody understood, and
   is worth a broadcast rather than fifty individual conversations.
4. At close: confirm the snapshot was written, borrow fees charged, and announce
   the day's top three.
5. In the break: run **Check the books balance** again.

## Incidents

| What you see | What you do |
| --- | --- |
| Anything unexpected | **Freeze.** Then diagnose. The clock stops, so no team loses time and nobody with a working connection gains an edge. |
| Engine not ticking | Check `/healthz`. Supervisor restarts it within seconds. If it is not back in 60 seconds, the tech lead takes over; at 5 minutes, fail over to the spare. |
| A restart happened | The market comes back FROZEN with its remaining time preserved. Check the state, then Resume. Never assume it resumed itself. |
| Wrong price pushed | **Undo the last price action.** The price reverts and you are told how many fills happened at the bad price. Those fills stand. Decide on adjustments in the break, not live. |
| A team says they were cheated | Help desk logs team, time, and a screenshot. Adjudicate after the close. Any correction is a cash adjustment with a reason, approved by a second operator. |
| Venue Wi-Fi collapses | Freeze. Do not let the half of the room with a hotspot trade against the half without one. |
| One team cannot sign in | Help desk resets their password from the console. It signs out their old sessions immediately. |
| Live prices stop updating for some people | The terminal falls back to polling on its own after three failed reconnects and says so. Trading still works; prices are a couple of seconds behind. Check `/healthz` for dropped sockets. |

## Corporate actions

Dividends and splits happen **between trading days**, never during one. Both
change prices and positions at the same time, so:

1. **Freeze the market first.** A closed market advances to the next day on its
   own timer; a frozen one does not move at all.
2. Console → price desk → pay the dividend or run the split. Both make you type
   the symbol back.
3. Check one affected team's book. Their account value must be unchanged: a
   dividend moves value from the price into cash, and a split only changes the
   units.
4. Resume.

A split cancels every resting order in that stock and tells the teams why.

## Close

- [ ] Blackout the leaderboard for the last 10 minutes of the final day.
- [ ] After the last close: **End the competition and lock results** (type FINAL).
      The Final results panel appears in the console with the podium, the tie-break
      columns and the five awards.
- [ ] Open **Print report cards** and print them, one per team. Everyone gets one.
- [ ] Export orders, fills, ledger, snapshots and audit as CSV.
- [ ] Take a database backup before anyone touches anything.
- [ ] Reveal the standings on the projector. Announce the main and side awards.
- [ ] Leave the server up, read-only, for 48 hours so teams can review their
      trades.

## Who does what

| Role | Console login | Can |
| --- | --- | --- |
| Super admin (×2) | `director`, `deputy` | Everything, including approving adjustments and ending the competition |
| Market operator | `market` | Market state, prices, halts, scenarios, freeze |
| News desk | `news` | Publish and retract news. Cannot touch prices. |
| Help desk | `helpdesk` | View any team, reset passwords, request adjustments |
| Judges | `judge` | Read-only: every book, the blotter, exports |
| Projector | `projector` | Read-only kiosk account for the big screen |

A cash adjustment needs two different operators: one to request, another to
approve. The system refuses to let one person do both.
