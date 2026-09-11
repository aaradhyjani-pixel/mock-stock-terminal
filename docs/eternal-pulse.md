# ETERNAL Pulse

Personal coaching feedback for the **ETERNAL** participant terminal.

Pulse does **not** affect competition ranking, cash, fills, margin, or
leaderboard standing. It is a client-side experience layer that helps college
teams under a 25-minute clock learn faster from their own decisions.

## Purpose

Under pressure, most learning evaporates between fills. Pulse turns each news
hit and each trade into a named moment so a participant can glance at a strip
of chips and know: *did we react, hesitate, chase, or size too hard?*

This is coaching, not scoring. Honest signals beat fake ML claims.

## How it works (no ML)

Pulse is a deterministic rules engine in the browser
(`web/static/js/pulse.js`), wired from `terminal.js`.

### Signals

1. **News / rumour mapped symbols** — when the news stage opens a ~12s decision
   window, Pulse records which symbols were named and when the window opened.
2. **Trade timing** — whether the team fills a mapped symbol inside that window,
   after it closes, or with no recent mapped headline.
3. **Size vs liquidity** — absolute slippage percent from the last order
   preview (`/api/orders/preview`). Material slippage means the book moved
   against size.
4. **Fill outcome** — side, qty, symbol, and fill price from the fill event
   (or an immediately filled order response).

### Pulse moments

| Moment | Meaning |
|--------|---------|
| `REACTED` | Fill on a mapped symbol while the decision window was still open. |
| `HESITATED` | Decision window closed with no fill on any mapped symbol. |
| `CHASED` | Fill on a mapped symbol after the decision window had already closed (still “recent” — within a short chase grace). |
| `SIZED_HARD` | Fill whose preview slippage met or exceeded the material threshold (~0.5%). |
| `CLEAN_ENTRY` | Fill with low / zero preview slippage; preferred when timing was also clean (`REACTED`). |

A single fill may contribute more than one counter (e.g. `REACTED` +
`CLEAN_ENTRY`, or `CHASED` + `SIZED_HARD`). The UI shows one primary label on
the moment card, with secondary chips in the dock when useful.

### Session Pulse strip

Running personal stats live in a desktop **Pulse dock** (wide screens ≥1100px):
reacted / hesitated / chased / sized hard / clean entry counts, plus the last
moment label.

Persistence key: `eternal_pulse_v1` in `localStorage`, lightly keyed by team id
when available so two teams on one shared laptop do not bleed stats into each
other. Clearing site data resets the strip; that is intentional.

## What Pulse never does

- Change order routing, matching, fees, margin, or rank.
- Call a model, ranking API, or remote analytics endpoint.
- Block trading or replace the fill toast contract for mobile / accessibility.

## Design lineage

Guided by Stanford d.school modes — Empathize (25-minute laptop pressure),
Define (story → bet → meaning), Ideate / Prototype / Test inside the product
itself. Bias to action: ship the dock and moment card, then tune thresholds
from live practice runs.
