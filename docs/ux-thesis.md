# Participant terminal UX thesis

Internal design brief for the college NSE mock-stock competition terminal.

## Diagnosis

The market engine is excellent. The participant UI wrongly mimics a full OMS /
Bloomberg: five equal-weight right-rail tabs, every order type on the ticket at
once, news buried beside Funds and Board. Under time pressure that layout
competes with the game instead of serving it.

Participants are not desk traders. They need to read a market story, place a
clear bet, feel the consequence, and learn for the next day.

## Game loop

1. **Read** — a headline or rumour lands; the stage lights up.
2. **Decide** — a short decision window; related symbols pulse on the watchlist.
3. **Bet** — Buy / Sell, Market / Limit, quantity, one big confirm.
4. **Feel** — fill toast with side, qty, symbol, price (and slippage when the
   preview had it); Book updates; rank may move.
5. **Learn** — review Book / Funds before the next day opens.

Anything that does not support this loop is demoted or hidden behind disclosure.

## North-star layout

```
[ Clock · Day ]  [ ACCOUNT ₹ · RANK ]  [ team ]
[ NEWS / RUMOUR STAGE — live ]
[ Watch compact | Chart + simple ticket | Book ]
         └ advanced order drawer
```

- **Topbar hierarchy:** account value + live rank are primary; Available,
  Total P&L, and index are secondary chips. Market state + countdown stay
  prominent. Theme toggle and logout remain.
- **News as stage:** persistent strip above the three columns. Rumours are
  visually distinct from news. Archive lives in a secondary tab.
- **Book as primary rail:** positions and open orders in one pane (two
  sections). Funds, Board, and News archive are secondary.
- **Ticket progressive disclosure:** Stop / Stop-limit sit behind
  “Protect this trade”; default surface is Market | Limit only.

## Progressive disclosure rules

- Default ticket: side, Market | Limit, qty + presets, concise preview
  (est. price, value, charges total, available after), big confirm.
- Advanced drawer reveals trigger / limit fields for SL_M and SL_L only.
- Never reimplement margin or charges client-side; always call
  `/api/orders/preview`.
- Sell copy reflects position state: closing a long vs opening / adding a short.
- Compact leverage / margin meter appears in Book (and Funds) when short
  exposure exists; hide the scare when there is no short risk.

## Experiential feedback principles

- **Fills:** toast must name side, qty, symbol, and fill price; mention
  slippage when the last preview reported non-zero slippage.
- **Rank:** surface live rank in the topbar from leaderboard `you` / `is_you`
  data. Throttle rank-change toasts so portfolio chatter does not spam.
- **News stage:** on each news / rumour event, emphasise the stage, pulse
  mapped watchlist rows, and run an optional ~12s decision-window meter.
  Respect `prefers-reduced-motion`.
- **First-run coach:** three dismissible steps (pick a stock → tiny buy → see
  Book), keyed in `localStorage` as `mst_coach_v1`. Never block trading.

## Mobile rules

- Bottom nav is four items: **Watch / Trade / Book / More**.
- More opens Funds, Board, and News archive — not a fifth peer tab.
- News stage stays visible above the active pane when viewport height allows.
- Selecting a watchlist row still deep-links into Trade on narrow screens.

## What not to regress

- No CDN, no build step, no external fonts or scripts.
- All existing REST and WebSocket contracts stay intact.
- Server remains the sole source of prices, charges, margin, and fills.
- Theme toggle and logout stay reachable.
- Trading must work while the coach is visible and while the socket is only
  degraded (REST polling).
- Login, projector, and console pages are out of scope except light CSS
  variable alignment; do not break them by renaming shared primitives carelessly.
