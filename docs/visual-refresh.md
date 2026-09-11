# Visual refresh (Blade-inspired)

Participant terminal chrome only — no engine, math, or branding changes.
Product remains the mock trading terminal (not ETERNAL; ETERNAL is a ticker).

## Principles

1. **4/8pt rhythm** — spacing tokens `--s1`…`--s6` (4–32px). Panels and stacks use 8/12/16.
2. **Consistent radii** — controls 6–8px (`--radius-sm` / `--radius`); cards 10–16px (`--radius-md` / `--radius-lg` / `--radius-xl`).
3. **Hierarchy** — display titles (stage headline, account/rank), uppercase micro-labels, mono tabular prices.
4. **Calm elevation** — near-black surfaces with soft separators (`--line` / `--line-soft`), 1–2 shadow levels; fewer harsh chalk borders.
5. **Sparse accent** — teal for primary CTA, selection rail, focus ring, and live stage only.
6. **Decisive trade** — Buy solid green / Sell solid red; watchlist selected row = left rail + soft glow.
7. **Login atmosphere** — mesh/gradient + subtle grid; sharp glass card; live market clock; mark **M**; tagline on judgment under pressure.
8. **Terminal hero** — glass topbar; Events stage as the visual centre; denser readable panels with gutters instead of 1px column seams.

## Constraints (unchanged)

- No CDN, no web fonts, no frontend build.
- Light theme + `prefers-reduced-motion` + mobile nav must work.
- IDs and `terminal.js` wiring preserved.

## Files

- `web/static/css/terminal.css` — token + component system
- `web/login.html` — cinematic entry structure
- `web/index.html` — Events stage / watchlist label polish
