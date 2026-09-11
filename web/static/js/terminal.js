/* The participant terminal.
 *
 * One page, driven by a single WebSocket. The design rules it follows:
 *
 * - The server prices everything. This file never computes a fill price, a
 *   charge or a margin figure; it asks /api/orders/preview and displays what
 *   comes back. Two implementations of the margin formula is one too many.
 * - Money stays a string from the server to the screen.
 * - When the socket is down, trading controls are disabled and say why. An
 *   order priced against a frozen quote is worse than a short wait.
 *
 * UX north star: read the market story → place a clear bet → feel consequence.
 */

import { api } from "./api.js";
import { CandleChart, toCandles } from "./chart.js";
import { STATE_TEXT, duration, escapeHtml, inr, pct, qty as fmtQty, relativeTime, shortTime, signClass } from "./format.js";
import { Stream } from "./stream.js";

const el = (id) => document.getElementById(id);

const COACH_KEY = "mst_coach_v1";
const DECISION_MS = 12000;
const RANK_TOAST_COOLDOWN_MS = 8000;

const state = {
  me: null,
  market: null,
  instruments: new Map(),
  order: new Map(),          // display order of symbols
  selected: null,
  funds: null,
  positions: [],
  orders: [],
  news: [],
  leaderboard: { rows: [], you: null, blackout: false },
  unreadNews: 0,
  connected: false,
  ticket: { side: "BUY", type: "MARKET", qty: 1 },
  preview: null,
  lastPreviewSlippage: null, // carried into fill toasts when non-zero
  sort: { key: "symbol", dir: 1 },
  filter: "",
  chart: null,
  interval: "1m",
  lastRank: null,
  lastRankToastAt: 0,
  newsHighlights: new Map(),   // symbol -> { until, kind }
  decisionTimer: null,
  decisionTick: null,
  slamTimer: null,
};

const stream = new Stream("/ws");

/* ------------------------------------------------------------------ startup */

async function boot() {
  applyStoredTheme();

  try {
    state.me = await api.get("/api/auth/me");
  } catch {
    window.location.replace("/login");
    return;
  }

  el("teamName").textContent = state.me.team.name;
  el("memberName").textContent = state.me.member.name;
  const badge = el("teamBadge");
  if (badge) badge.textContent = (state.me.team.name || "T").trim().charAt(0).toUpperCase() || "T";

  wireChrome();
  wireTicket();
  wireTabs();
  wireMobileNav();
  wireMoreSheet();
  wireCoach();

  state.chart = new CandleChart(el("chart"));

  await Promise.all([loadInstruments(), loadPortfolio(), loadOrders(), loadNews(), loadLeaderboard()]);

  // Seed the news stage with the newest item if any.
  if (state.news.length) showOnStage(state.news[0], { decision: false });

  wireStream();
  // If WebSockets turn out to be unavailable here, the terminal keeps working
  // on REST polling. Each source is reshaped into the same event the socket
  // would have sent, so no handler below needs to know which path it came by.
  stream.usePolling([
    {
      url: "/api/market",
      event: "market_state",
    },
    {
      url: "/api/instruments",
      event: "quotes",
      transform: (body) => ({ quotes: body.instruments }),
    },
    {
      url: "/api/portfolio",
      event: "portfolio",
      transform: (body) => ({ funds: body.funds, positions: body.positions }),
    },
  ]);
  stream.connect(api.token);

  setInterval(tickClock, 250);
  setInterval(() => { if (state.connected) loadLeaderboard(); }, 20000);


  // Phones treat a swipe-back as history.back(). Keep them on the terminal
  // while signed in instead of dumping them on /login.
  guardParticipantHistory();

  maybeShowCoach();
  measureStageHeight();
  window.addEventListener("resize", measureStageHeight);
  document.title = "Trading Terminal";
}


function guardParticipantHistory() {
  try {
    history.replaceState({ mst: "terminal" }, "", "/");
  } catch { /* ignore */ }
  window.addEventListener("popstate", () => {
    try {
      history.pushState({ mst: "terminal" }, "", "/");
    } catch { /* ignore */ }
  });
}

function measureStageHeight() {
  const stage = el("newsStage");
  if (!stage) return;
  document.documentElement.style.setProperty("--stage-h", `${stage.offsetHeight}px`);
}

function applyStoredTheme() {
  let theme = null;
  try {
    theme = localStorage.getItem("exchange.theme");
  } catch { /* private browsing */ }
  if (theme) document.documentElement.setAttribute("data-theme", theme);
}

function wireChrome() {
  el("themeToggle").addEventListener("click", () => {
    const current = document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", current);
    try { localStorage.setItem("exchange.theme", current); } catch { /* ignore */ }
    state.chart?.draw();
  });

  el("logout").addEventListener("click", async () => {
    stream.close();
    try { await api.post("/api/auth/logout"); } catch { /* signing out anyway */ }
    api.clearToken();
    window.location.replace("/login");
  });

  el("search").addEventListener("input", (event) => {
    state.filter = event.target.value.trim().toUpperCase();
    renderWatchlist();
  });

  document.querySelectorAll("#watchTable th.sortable").forEach((th) => {
    th.addEventListener("click", () => {
      const key = th.dataset.key;
      state.sort = state.sort.key === key
        ? { key, dir: -state.sort.dir }
        : { key, dir: key === "symbol" ? 1 : -1 };
      renderWatchlist();
    });
  });

  document.querySelectorAll("[data-interval]").forEach((button) => {
    button.addEventListener("click", async () => {
      state.interval = button.dataset.interval;
      document.querySelectorAll("[data-interval]").forEach((b) =>
        b.setAttribute("aria-pressed", String(b === button)));
      await loadCandles();
    });
  });

  // Keyboard shortcuts, desktop only. B and S are what a trader reaches for.
  document.addEventListener("keydown", (event) => {
    if (event.target.matches("input, textarea, select")) {
      if (event.key === "Escape") event.target.blur();
      return;
    }
    if (event.key === "b" || event.key === "B") setSide("BUY");
    if (event.key === "s" || event.key === "S") setSide("SELL");
    if (event.key === "/") { event.preventDefault(); el("search").focus(); }
  });
}

/* ------------------------------------------------------------------ loading */

async function loadInstruments() {
  const data = await api.get("/api/instruments");
  data.instruments.forEach((row, index) => {
    state.instruments.set(row.symbol, row);
    state.order.set(row.symbol, index);
  });
  if (!state.selected && data.instruments.length) {
    select(data.instruments[0].symbol, { focus: false });
  }
  renderWatchlist();
}

async function loadPortfolio() {
  const data = await api.get("/api/portfolio");
  state.funds = data.funds;
  state.positions = data.positions;
  state.limits = data.limits;
  state.pnl = data.pnl;
  renderFunds();
  renderPositions();
  updateSideIntent();
}

async function loadOrders() {
  const data = await api.get("/api/orders");
  state.orders = data.orders;
  renderOrders();
}

async function loadNews() {
  const data = await api.get("/api/news");
  state.news = data.news;
  renderNews();
}

async function loadLeaderboard() {
  try {
    state.leaderboard = await api.get("/api/leaderboard");
    renderLeaderboard();
  } catch { /* the board is not worth an error message */ }
}

async function loadCandles() {
  if (!state.selected) return;
  try {
    const data = await api.get(`/api/instruments/${state.selected}/candles?interval=${state.interval}&limit=180`);
    const markers = state.news
      .filter((item) => (item.symbols || []).includes(state.selected))
      .map((item) => ({ ts: item.published_at }));
    state.chart.setData(state.selected, toCandles(data.candles), markers);
  } catch { /* chart stays as it was */ }
}

/* ------------------------------------------------------------------ streams */

function wireStream() {
  stream.on("status", ({ connected, degraded }) => {
    // Degraded means the live tape is gone but REST still answers, so prices
    // are a couple of seconds old and trading is still safe: the server prices
    // every order anyway. Only a total loss of contact disables the ticket.
    state.connected = connected || Boolean(degraded);
    state.degraded = Boolean(degraded) && !connected;
    const banner = el("offline");
    banner.hidden = connected;
    banner.textContent = state.degraded
      ? "Live updates are unavailable, so prices refresh every couple of seconds. Trading still works."
      : "Reconnecting to the exchange. Trading is paused until the connection is back.";
    banner.style.background = state.degraded ? "var(--panel-3)" : "";
    banner.style.color = state.degraded ? "var(--muted)" : "";
    updateTicketAvailability();
  });

  stream.on("unauthorised", async () => {
    if (await api.refresh()) stream.connect(api.token);
    else window.location.replace("/login");
  });

  stream.on("snapshot", (data) => {
    applyMarket(data.market);
    data.instruments.forEach((row) => state.instruments.set(row.symbol, row));
    if (data.portfolio) {
      state.funds = data.portfolio.funds;
      state.positions = data.portfolio.positions;
      renderFunds();
      renderPositions();
      updateSideIntent();
    }
    renderWatchlist();
    renderTicketQuote();
  });

  stream.on("quotes", (data) => {
    for (const quote of data.quotes) {
      const existing = state.instruments.get(quote.symbol);
      if (!existing) continue;
      quote.name = existing.name;
      quote.sector = existing.sector;
      quote.tick_size = existing.tick_size;
      quote._prev = existing.last;
      state.instruments.set(quote.symbol, { ...existing, ...quote });
    }
    if (data.index) renderIndex(data.index);
    renderWatchlist();
    renderTicketQuote();
    renderPositions();
    const selected = state.instruments.get(state.selected);
    if (selected) state.chart?.pushPrice(selected.last);
  });

  stream.on("market_state", (data) => {
    applyMarket(data);
    renderWatchlist();
  });

  stream.on("instrument_status", (data) => {
    const instrument = state.instruments.get(data.symbol);
    if (instrument) {
      instrument.status = data.status;
      instrument.halt_reason = data.reason || null;
      state.instruments.set(data.symbol, instrument);
    }
    renderWatchlist();
    renderTicketQuote();
    toast(
      data.status === "HALTED" ? "warn" : "",
      `${data.symbol} ${data.status === "HALTED" ? "halted" : "resumed"}`,
      data.reason || "",
    );
  });

  stream.on("portfolio", (data) => {
    state.funds = data.funds;
    state.positions = data.positions;
    renderFunds();
    renderPositions();
    updateSideIntent();
    // Rank may move after fills / marks; refresh board on the next poll, and
    // also nudge a quieter check so the header stays honest.
    maybeRefreshRankSoon();
  });

  stream.on("order_update", (order) => {
    upsertOrder(order);
    renderOrders();
  });

  stream.on("fill", (fill) => {
    const slip = state.lastPreviewSlippage;
    const slipNote = slip !== null && slip !== undefined && Number(slip) !== 0
      ? ` Slippage ${pct(slip)}.`
      : "";
      toast(
        fill.side === "BUY" ? "up" : "down",
        `${fill.side} ${fmtQty(fill.qty)} ${fill.symbol} @ ${inr(fill.price)}`,
        `Filled.${slipNote} Charges ${inr(fill.fees)}.`,
      );
    state.lastPreviewSlippage = null;
    loadOrders();
  });

  stream.on("news", (item) => {
    state.news.unshift(item);
    state.unreadNews += 1;
    renderNews();
    showOnStage(item, { decision: true });
    highlightNewsSymbols(item.symbols || [], item.kind);
    toast("warn", item.kind === "RUMOUR" ? "Rumour" : "News", item.headline);
    if ((item.symbols || []).includes(state.selected)) loadCandles();
    measureStageHeight();
  });

  stream.on("news_retracted", ({ id }) => {
    const item = state.news.find((n) => n.id === id);
    if (item) {
      item.retracted = true;
      renderNews();
      // If the stage is showing this item, mark it.
      const stage = el("newsStage");
      if (stage?.dataset.newsId === String(id)) {
        el("stageHeadline").classList.add("retracted");
        el("stageKindPill").textContent = "Retracted";
        el("stageKindPill").className = "pill down";
      }
    }
  });

  stream.on("announcement", (data) => {
    showBanner(data.message, data.severity || "info");
    toast(data.severity === "critical" ? "down" : "warn", "Announcement", data.message);
  });

  stream.on("leaderboard", (data) => {
    if (data.blackout) {
      state.leaderboard = { ...state.leaderboard, blackout: true, rows: [] };
    } else {
      state.leaderboard = {
        ...state.leaderboard,
        blackout: false,
        rows: data.rows.map((row) => ({ ...row, is_you: row.team_id === state.me.team.id })),
      };
    }
    renderLeaderboard();
  });

  stream.on("margin_warning", (data) => {
    toast("warn", "Margin warning", data.message);
    flashFunds("warn");
  });
  stream.on("margin_call", (data) => {
    toast("down", "Margin call", data.message);
    flashFunds("danger");
    loadOrders();
  });
  stream.on("busted", (data) => {
    toast("down", "Out of the competition", data.message);
    showBanner(data.message, "critical");
    updateTicketAvailability();
  });
}

let rankRefreshTimer = null;
function maybeRefreshRankSoon() {
  clearTimeout(rankRefreshTimer);
  rankRefreshTimer = setTimeout(() => {
    if (state.connected) loadLeaderboard();
  }, 1500);
}

function applyMarket(market) {
  state.market = market;
  const stateEl = el("marketState");
  stateEl.textContent = STATE_TEXT[market.state] || market.state;
  stateEl.className = `state ${market.state}`;
  el("dayNo").textContent = market.day_no ? `Day ${market.day_no}/${market.total_days}` : "Not started";
  if (market.banner) showBanner(market.banner, market.banner_severity);
  else hideBanner();
  if (market.index) renderIndex(market.index);
  updateTicketAvailability();
}

function renderIndex(index) {
  el("indexName").textContent = index.name;
  el("indexValue").textContent = inr(index.value);
  const change = el("indexChange");
  if (index.change_pct !== undefined) {
    change.textContent = pct(index.change_pct);
    change.className = signClass(index.change_pct);
  }
}

/* ------------------------------------------------------------------ banners */

function showBanner(message, severity = "info") {
  const banner = el("banner");
  banner.textContent = message;
  banner.dataset.severity = severity;
  banner.hidden = false;
  el("app").classList.add("has-banner");
}

function hideBanner() {
  el("banner").hidden = true;
  el("app").classList.remove("has-banner");
}

/* --------------------------------------------------------------- news stage */

function showOnStage(item, { decision = false } = {}) {
  const stage = el("newsStage");
  if (!stage || !item) return;

  const isRumour = item.kind === "RUMOUR";
  const mode = item.retracted ? "retracted" : isRumour ? "rumour" : "breaking";
  stage.dataset.newsId = item.id != null ? String(item.id) : "";
  stage.dataset.mode = mode;
  stage.classList.toggle("rumour", isRumour && !item.retracted);
  stage.classList.add("live");

  // Replay slam / flash with distinct timing for BREAKING vs RUMOUR.
  stage.classList.remove("slam", "flash", "slam-breaking", "slam-rumour");
  void stage.offsetWidth;
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (!reduceMotion) {
    const slamKind = isRumour ? "slam-rumour" : "slam-breaking";
    stage.classList.add("slam", "flash", slamKind);
    clearTimeout(state.slamTimer);
    state.slamTimer = setTimeout(() => {
      stage.classList.remove("slam", "flash", "slam-breaking", "slam-rumour");
    }, 650);
  }

  const pill = el("stageKindPill");
  if (item.retracted) {
    pill.textContent = "Retracted";
    pill.className = "pill down";
  } else if (isRumour) {
    pill.textContent = "Rumour";
    pill.className = "pill warn";
  } else {
    pill.textContent = "Breaking";
    pill.className = "pill accent";
  }

  const kicker = el("stageKicker");
  if (kicker) {
    kicker.textContent = item.retracted
      ? "Retracted — do not trade on this"
      : isRumour
        ? "Unverified — trade carefully"
        : "Live from the events desk";
  }

  const headline = el("stageHeadline");
  headline.textContent = item.headline || "";
  headline.classList.toggle("retracted", Boolean(item.retracted));

  el("stageTime").textContent = item.published_at ? relativeTime(item.published_at) : "";

  const symBox = el("stageSymbols");
  const symbols = item.symbols || [];
  symBox.innerHTML = symbols
    .map((s) => `<button type="button" class="sym-chip" data-stage-sym="${escapeHtml(s)}">${escapeHtml(s)}</button>`)
    .join("");
  symBox.querySelectorAll("[data-stage-sym]").forEach((tag) => {
    tag.addEventListener("click", () => {
      if (state.instruments.has(tag.dataset.stageSym)) {
        select(tag.dataset.stageSym, { focus: true });
      }
    });
  });

  // Auto-select first related symbol when nothing is selected, or on a live break.
  const first = symbols.find((s) => state.instruments.has(s));
  if (first && !item.retracted && (decision || !state.selected)) {
    select(first, { focus: Boolean(decision) });
  }

  const windowEl = el("stageWindow");
  const bar = el("stageWindowBar");
  const labelEl = el("stageWindowLabel");
  clearTimeout(state.decisionTimer);
  if (state.decisionTick) {
    clearInterval(state.decisionTick);
    state.decisionTick = null;
  }
  document.documentElement.classList.remove("news-action", "news-action-rumour");

  if (decision && !item.retracted) {
    windowEl.hidden = false;
    windowEl.dataset.kind = isRumour ? "rumour" : "breaking";
    document.documentElement.classList.add("news-action");
    if (isRumour) document.documentElement.classList.add("news-action-rumour");

    const ends = Date.now() + DECISION_MS;
    if (bar) {
      bar.style.animation = "none";
      bar.style.transform = "";
      void bar.offsetWidth;
      if (reduceMotion) {
        bar.style.animation = "none";
      } else {
        bar.style.animation = `decision-drain ${DECISION_MS}ms linear forwards`;
      }
    }
    const tick = () => {
      const left = Math.max(0, ends - Date.now());
      const secs = Math.ceil(left / 1000);
      if (labelEl) labelEl.textContent = `Decide · ${secs}s`;
      if (reduceMotion && bar) {
        bar.style.transform = `scaleX(${left / DECISION_MS})`;
      }
      if (left <= 0 && state.decisionTick) {
        clearInterval(state.decisionTick);
        state.decisionTick = null;
      }
    };
    tick();
    state.decisionTick = setInterval(tick, 100);
    state.decisionTimer = setTimeout(() => {
      windowEl.hidden = true;
      stage.classList.remove("live", "slam", "flash", "slam-breaking", "slam-rumour");
      stage.dataset.mode = "idle";
      if (labelEl) labelEl.textContent = "Decide";
      document.documentElement.classList.remove("news-action", "news-action-rumour");
      measureStageHeight();
    }, DECISION_MS);
  } else {
    windowEl.hidden = true;
    if (labelEl) labelEl.textContent = "Decide";
  }
  measureStageHeight();
}

function highlightNewsSymbols(symbols, kind) {
  const until = Date.now() + DECISION_MS;
  const hitKind = kind === "RUMOUR" ? "rumour" : "news";
  for (const symbol of symbols) {
    if (!state.instruments.has(symbol)) continue;
    state.newsHighlights.set(symbol, { until, kind: hitKind });
  }
  renderWatchlist();
  setTimeout(() => {
    const now = Date.now();
    for (const [sym, meta] of state.newsHighlights) {
      if (meta.until <= now) state.newsHighlights.delete(sym);
    }
    renderWatchlist();
  }, DECISION_MS + 50);
}

/* --------------------------------------------------------------- watchlist */

function sortedInstruments() {
  const rows = [...state.instruments.values()];
  const { key, dir } = state.sort;
  const filtered = state.filter
    ? rows.filter((r) => r.symbol.includes(state.filter) || r.name.toUpperCase().includes(state.filter))
    : rows;
  return filtered.sort((a, b) => {
    if (key === "symbol") return dir * a.symbol.localeCompare(b.symbol);
    return dir * (Number(a[key]) - Number(b[key]));
  });
}

function renderWatchlist() {
  const body = el("watchBody");
  const rows = sortedInstruments();
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="4"><div class="empty">No stocks match "${escapeHtml(state.filter)}".</div></td></tr>`;
    return;
  }

  const previous = new Map();
  body.querySelectorAll("tr[data-symbol]").forEach((tr) => previous.set(tr.dataset.symbol, tr.dataset.last));

  const now = Date.now();
  body.innerHTML = rows.map((row) => {
    const cls = signClass(row.change_pct);
    const badge = statusBadge(row);
    const hit = state.newsHighlights.get(row.symbol);
    const hitClass = hit && hit.until > now
      ? (hit.kind === "rumour" ? "rumour-hit" : "news-hit")
      : "";
    return `<tr data-symbol="${row.symbol}" data-last="${row.last}" class="${hitClass}" aria-selected="${row.symbol === state.selected}">
      <td><div class="sym">${row.symbol}${badge}</div><div class="co">${escapeHtml(row.name)}</div></td>
      <td class="num price">${inr(row.last)}</td>
      <td class="num ${cls}">${pct(row.change_pct)}</td>
    </tr>`;
  }).join("");

  body.querySelectorAll("tr[data-symbol]").forEach((tr) => {
    const symbol = tr.dataset.symbol;
    const before = previous.get(symbol);
    if (before !== undefined && before !== tr.dataset.last) {
      const cell = tr.querySelector(".price");
      cell.classList.add(Number(tr.dataset.last) > Number(before) ? "tick-up" : "tick-down");
      setTimeout(() => cell.classList.remove("tick-up", "tick-down"), 600);
    }
    tr.addEventListener("click", () => select(symbol));
  });
}

function statusBadge(row) {
  if (row.status === "HALTED") return ' <span class="pill warn">Halted</span>';
  if (row.status === "SUSPENDED") return ' <span class="pill">Suspended</span>';
  if (row.status === "UPPER_CIRCUIT") return ' <span class="pill up">UC</span>';
  if (row.status === "LOWER_CIRCUIT") return ' <span class="pill down">LC</span>';
  return "";
}

async function select(symbol, { focus = true } = {}) {
  state.selected = symbol;
  renderWatchlist();
  renderTicketQuote();
  await loadCandles();
  // On a phone, tapping a stock should take you to it. Selecting one during
  // start-up should not: landing on the ticket for a stock nobody chose is a
  // confusing first screen. Start on the market list.
  if (focus && window.innerWidth <= 780) showPane("centre");
  advanceCoachIf(0);
}

/* ------------------------------------------------------------------ ticket */

function wireTicket() {
  document.querySelectorAll("[data-side]").forEach((button) => {
    button.addEventListener("click", () => setSide(button.dataset.side));
  });

  document.querySelectorAll("[data-otype]").forEach((button) => {
    button.addEventListener("click", () => setOrderType(button.dataset.otype));
  });

  el("qty").addEventListener("input", schedulePreview);
  el("limitPrice").addEventListener("input", schedulePreview);
  el("triggerPrice").addEventListener("input", schedulePreview);

  document.querySelectorAll("[data-qty]").forEach((button) => {
    button.addEventListener("click", () => {
      const preset = button.dataset.qty;
      if (preset === "max" && state.preview) el("qty").value = state.preview.max_qty || 1;
      else el("qty").value = preset;
      schedulePreview();
    });
  });

  // Closing the advanced drawer while a stop type is selected snaps back to Market.
  el("advancedOrders").addEventListener("toggle", () => {
    const open = el("advancedOrders").open;
    if (!open && state.ticket.type.startsWith("SL")) {
      setOrderType("MARKET");
    }
  });

  el("orderForm").addEventListener("submit", submitOrder);
}

function setOrderType(type) {
  state.ticket.type = type;
  document.querySelectorAll("[data-otype]").forEach((b) =>
    b.setAttribute("aria-pressed", String(b.dataset.otype === type)));

  const needsLimit = type === "LIMIT" || type === "SL_L";
  const needsTrigger = type.startsWith("SL");
  el("limitField").hidden = !needsLimit;
  el("triggerField").hidden = !needsTrigger;

  // Keep the advanced drawer open when a stop type is chosen.
  if (needsTrigger) el("advancedOrders").open = true;

  schedulePreview();
  updateTicketAvailability();
}

function setSide(side) {
  state.ticket.side = side;
  document.querySelectorAll("[data-side]").forEach((b) =>
    b.setAttribute("aria-pressed", String(b.dataset.side === side)));
  const submit = el("submitOrder");
  submit.className = `btn block ${side === "BUY" ? "buy" : "sell"}`;
  updateSideIntent();
  schedulePreview();
}

function updateSideIntent() {
  const note = el("sideIntent");
  const sellBtn = el("sellSideBtn");
  if (!note) return;

  const position = state.positions.find((p) => p.symbol === state.selected);
  const side = state.ticket.side;

  if (side === "BUY") {
    if (position && position.qty < 0) {
      note.textContent = `Closes / reduces your short of ${fmtQty(Math.abs(position.qty))}.`;
    } else if (position && position.qty > 0) {
      note.textContent = "Adds to your long.";
    } else {
      note.textContent = "Opens a long.";
    }
    if (sellBtn) sellBtn.textContent = position && position.qty > 0 ? "Sell" : "Sell / Short";
    return;
  }

  // SELL
  if (position && position.qty > 0) {
    note.textContent = `Closes / reduces your long of ${fmtQty(position.qty)}.`;
    if (sellBtn) sellBtn.textContent = "Sell";
  } else if (position && position.qty < 0) {
    note.textContent = "Adds to your short.";
    if (sellBtn) sellBtn.textContent = "Sell / Short";
  } else {
    note.textContent = "Opens a short (margin applies).";
    if (sellBtn) sellBtn.textContent = "Sell / Short";
  }
}

function renderTicketQuote() {
  const instrument = state.instruments.get(state.selected);
  if (!instrument) return;
  el("ticketSymbol").textContent = instrument.symbol;
  el("ticketName").textContent = instrument.name;
  el("bidPrice").textContent = inr(instrument.bid);
  el("askPrice").textContent = inr(instrument.ask);
  el("ticketLast").textContent = inr(instrument.last);
  el("ticketChange").textContent = `${inr(instrument.change, { sign: true })} (${pct(instrument.change_pct)})`;
  el("ticketChange").className = `v ${signClass(instrument.change_pct)}`;
  el("dayRange").textContent = `${inr(instrument.low)} - ${inr(instrument.high)}`;
  el("bandRange").textContent = `${inr(instrument.band_low)} - ${inr(instrument.band_high)}`;

  const position = state.positions.find((p) => p.symbol === instrument.symbol);
  el("ticketPosition").innerHTML = position
    ? `<span class="pill ${position.side.toLowerCase()}">${position.side}</span> ${fmtQty(Math.abs(position.qty))} at ${inr(position.avg_cost)}`
    : '<span class="dim">No position</span>';

  updateSideIntent();
  updateTicketAvailability();
}

let previewTimer = null;
function schedulePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(runPreview, 180);
}

async function runPreview() {
  const quantity = parseInt(el("qty").value, 10);
  if (!state.selected || !quantity || quantity <= 0) {
    el("previewBox").innerHTML = '<div class="dim">Enter a quantity to see the cost.</div>';
    state.preview = null;
    return;
  }
  try {
    const body = {
      symbol: state.selected,
      side: state.ticket.side,
      qty: quantity,
      // Stop-market previews as market (same as pre-redesign). SL_L keeps its type.
      order_type: state.ticket.type === "SL_M" ? "MARKET" : state.ticket.type,
    };
    if (state.ticket.type === "LIMIT" || state.ticket.type === "SL_L") {
      const limit = el("limitPrice").value;
      if (limit) body.limit_price = limit;
    }
    state.preview = await api.post("/api/orders/preview", body);
    if (state.preview?.slippage_pct !== undefined) {
      state.lastPreviewSlippage = state.preview.slippage_pct;
    }
    renderPreview();
  } catch (error) {
    el("previewBox").innerHTML = `<div class="down">${escapeHtml(error.message)}</div>`;
  }
}

function renderPreview() {
  const preview = state.preview;
  if (!preview) return;
  const charges = Object.entries(preview.charges?.items || {});
  el("previewBox").innerHTML = `
    <div class="line"><span class="k">Estimated price</span><span class="v">${inr(preview.estimated_price)}</span></div>
    ${Number(preview.slippage_pct) !== 0
      ? `<div class="line"><span class="k">Slippage</span><span class="v">${pct(preview.slippage_pct)}</span></div>` : ""}
    <div class="line"><span class="k">Value</span><span class="v">${inr(preview.gross)}</span></div>
    <div class="line"><span class="k">Charges</span><span class="v">${inr(preview.charges?.total)}</span></div>
    ${charges.length ? `<details>
      <summary>Charge breakdown</summary>
      ${charges.map(([k, v]) => `<div class="line"><span class="k">${escapeHtml(k.replace(/_/g, " "))}</span><span class="v">${inr(v)}</span></div>`).join("")}
    </details>` : ""}
    ${Number(preview.margin_locked) > 0
      ? `<div class="line"><span class="k">Margin locked</span><span class="v">${inr(preview.margin_locked)}</span></div>` : ""}
    <div class="line total ${preview.affordable ? "" : "bad"}">
      <span class="k">Available after</span><span class="v">${inr(preview.available_after)}</span>
    </div>
    ${preview.affordable ? "" : '<div class="down" style="font-size:11px">Not enough available funds for this order.</div>'}
  `;
  updateTicketAvailability();
}

function updateTicketAvailability() {
  const submit = el("submitOrder");
  const instrument = state.instruments.get(state.selected);
  const marketState = state.market?.state;
  const busted = state.me?.team?.status === "BUSTED";

  let reason = "";
  if (!state.connected) reason = "Reconnecting to the exchange...";
  else if (state.degraded && !instrument) reason = "Waiting for prices...";
  else if (busted) reason = "Your team is out of the competition";
  else if (marketState === "CLOSED") reason = "Market closed";
  else if (marketState === "FROZEN") reason = "Market frozen by the organisers";
  else if (marketState === "HALTED") reason = "Trading halted";
  else if (marketState === "FINAL") reason = "The competition has ended";
  else if (marketState === "PRE_OPEN" && state.ticket.type === "MARKET") reason = "Pre-open: limit orders only";
  else if (instrument?.status === "HALTED") reason = `${instrument.symbol} is halted`;
  else if (instrument?.status === "SUSPENDED") reason = `${instrument.symbol} is suspended`;

  submit.disabled = Boolean(reason);
  submit.textContent = reason || `${state.ticket.side} ${state.selected || ""}`;
  el("ticketNote").textContent = reason;
}

async function submitOrder(event) {
  event.preventDefault();
  const submit = el("submitOrder");
  const quantity = parseInt(el("qty").value, 10);
  if (!quantity || quantity <= 0) return;

  const body = {
    symbol: state.selected,
    side: state.ticket.side,
    order_type: state.ticket.type,
    qty: quantity,
    // A fresh identifier per submission. A retry of the same tap reuses it, so
    // a flaky connection cannot turn one order into two.
    client_order_id: `${state.me.team.id}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
  };
  const limit = el("limitPrice").value;
  const trigger = el("triggerPrice").value;
  if ((state.ticket.type === "LIMIT" || state.ticket.type === "SL_L") && limit) body.limit_price = limit;
  if (state.ticket.type.startsWith("SL") && trigger) body.trigger_price = trigger;

  // Remember preview slippage for the fill toast that may follow.
  if (state.preview?.slippage_pct !== undefined) {
    state.lastPreviewSlippage = state.preview.slippage_pct;
  }

  submit.disabled = true;
  submit.textContent = "Sending...";
  try {
    const order = await api.post("/api/orders", body);
    upsertOrder(order);
    renderOrders();
    if (order.status === "REJECTED") {
      toast("down", "Order rejected", order.reason || "");
    } else if (order.status === "FILLED") {
      const slip = state.lastPreviewSlippage;
      if (!moment) {
        const slipNote = slip !== null && slip !== undefined && Number(slip) !== 0
          ? ` Slippage ${pct(slip)}.`
          : "";
        toast(
          "up",
          `${order.side} ${fmtQty(order.filled_qty)} ${order.symbol} @ ${inr(order.avg_price)}`,
          `Filled.${slipNote}`,
        );
      }
      state.lastPreviewSlippage = null;
    } else {
      toast("", "Order placed", `${order.type} resting at ${inr(order.limit_price || order.trigger_price)}`);
    }
    await Promise.all([loadPortfolio(), runPreview()]);
    advanceCoachIf(1);
  } catch (error) {
    toast("down", "Could not place the order", error.message);
  } finally {
    updateTicketAvailability();
  }
}

/* ---------------------------------------------------------------- portfolio */

function renderFunds() {
  if (!state.funds) return;
  const funds = state.funds;
  el("hdrEquity").textContent = inr(funds.equity);
  el("hdrAvailable").textContent = inr(funds.available);
  const pnl = state.pnl;
  if (pnl) {
    el("hdrPnl").textContent = `${inr(pnl.total, { sign: true })} (${pct(pnl.total_pct)})`;
    el("hdrPnl").className = `v ${signClass(pnl.total)}`;
  }

  el("fundsBox").innerHTML = `
    <dl class="kv">
      <dt>Account value</dt><dd class="big">${inr(funds.equity)}</dd>
      <dt>Cash</dt><dd>${inr(funds.cash)}</dd>
      <dt>Holdings</dt><dd>${inr(funds.long_mv)}</dd>
      <dt>Short exposure</dt><dd>${inr(funds.short_mv)}</dd>
      <dt>Margin locked</dt><dd>${inr(funds.margin_required)}</dd>
      <dt>Available to trade</dt><dd class="big ${Number(funds.available) <= 0 ? "down" : ""}">${inr(funds.available)}</dd>
      <dt>Unrealised P&amp;L</dt><dd class="${signClass(funds.unrealised_pnl)}">${inr(funds.unrealised_pnl, { sign: true })}</dd>
      <dt>Realised P&amp;L</dt><dd class="${signClass(funds.realised_pnl)}">${inr(funds.realised_pnl, { sign: true })}</dd>
    </dl>
    ${renderLeverageGauge(funds)}
  `;

  renderBookMargin(funds);
}

function renderBookMargin(funds) {
  const box = el("bookMargin");
  if (!box) return;
  const shortMv = Number(funds.short_mv || 0);
  if (!shortMv) {
    box.hidden = true;
    box.innerHTML = "";
    return;
  }
  box.hidden = false;
  box.innerHTML = renderLeverageGauge(funds);
}

function renderLeverageGauge(funds) {
  const maxLeverage = Number(state.limits?.max_leverage || 5);
  const leverage = Number(funds.leverage || 0);
  const used = Math.min(100, (leverage / maxLeverage) * 100);
  const distance = funds.distance_to_call_pct;

  let tone = "";
  if (funds.margin_state === "WARNING") tone = "warn";
  if (funds.margin_state === "CALL" || funds.margin_state === "BUST") tone = "danger";

  return `
    <div>
      <div class="row" style="justify-content:space-between;margin-bottom:4px">
        <span class="label">Leverage</span>
        <span class="mono" style="flex:0">${leverage.toFixed(2)}x of ${maxLeverage}x</span>
      </div>
      <div class="meter ${tone}"><span style="width:${used}%"></span></div>
      ${distance !== null && distance !== undefined
        ? `<div class="muted" style="margin-top:6px;font-size:11.5px">
             Your shorts can move <b class="${Number(distance) < 3 ? "down" : ""}">${pct(distance, { sign: false })}</b>
             against you before the exchange covers them.
           </div>`
        : '<div class="dim" style="margin-top:6px;font-size:11.5px">No short positions, so no margin risk.</div>'}
    </div>`;
}

function flashFunds(tone) {
  const box = el("fundsBox");
  box.style.transition = "background 0.2s";
  box.style.background = tone === "danger" ? "var(--down-bg)" : "var(--warn-bg)";
  setTimeout(() => { box.style.background = ""; }, 1400);
}

function renderPositions() {
  const body = el("positionsBody");
  if (!state.positions.length) {
    body.innerHTML = '<tr><td colspan="6"><div class="empty">No open positions. Pick a stock and place your first trade.</div></td></tr>';
    el("positionsCount").textContent = "";
    updateBookCount();
    return;
  }
  el("positionsCount").textContent = state.positions.length;

  body.innerHTML = state.positions.map((position) => {
    const live = state.instruments.get(position.symbol);
    const last = live ? live.last : position.last;
    return `<tr data-symbol="${position.symbol}">
      <td><div class="sym">${position.symbol}</div>
          <span class="pill ${position.side.toLowerCase()}">${position.side}</span></td>
      <td class="num">${fmtQty(Math.abs(position.qty))}</td>
      <td class="num">${inr(position.avg_cost)}</td>
      <td class="num">${inr(last)}</td>
      <td class="num ${signClass(position.unrealised_pnl)}">${inr(position.unrealised_pnl, { sign: true })}<br>
          <span style="font-size:10px">${pct(position.unrealised_pct)}</span></td>
      <td class="num"><button class="btn sm ghost" data-close="${position.symbol}" data-qty="${Math.abs(position.qty)}"
          data-side="${position.qty > 0 ? "SELL" : "BUY"}">Close</button></td>
    </tr>`;
  }).join("");

  body.querySelectorAll("[data-close]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      select(button.dataset.close);
      setSide(button.dataset.side);
      el("qty").value = button.dataset.qty;
      setOrderType("MARKET");
      schedulePreview();
      if (window.innerWidth <= 780) showPane("centre");
    });
  });
  body.querySelectorAll("tr[data-symbol]").forEach((tr) => {
    tr.addEventListener("click", () => select(tr.dataset.symbol));
  });
  updateBookCount();
}

function upsertOrder(order) {
  const index = state.orders.findIndex((o) => o.id === order.id);
  if (index >= 0) state.orders[index] = { ...state.orders[index], ...order };
  else state.orders.unshift(order);
}

function renderOrders() {
  const body = el("ordersBody");
  const open = state.orders.filter((o) => o.status === "PENDING" || o.status === "TRIGGERED");
  el("ordersCount").textContent = open.length || "";
  updateBookCount();

  // Book shows open orders; if none, surface a few recent for context.
  const rows = open.length ? open : state.orders.filter((o) => o.status === "FILLED" || o.status === "CANCELLED" || o.status === "REJECTED").slice(0, 8);
  const showingRecent = !open.length && rows.length;

  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="5"><div class="empty">No open orders.</div></td></tr>';
    return;
  }

  body.innerHTML = (showingRecent
    ? `<tr><td colspan="5"><div class="dim" style="padding:6px 8px;font-size:11px">No open orders — recent activity:</div></td></tr>`
    : "") + rows.map((order) => {
    const isOpen = order.status === "PENDING" || order.status === "TRIGGERED";
    const price = order.avg_price || order.limit_price || order.trigger_price;
    const tone = order.status === "FILLED" ? "up" : order.status === "REJECTED" ? "down" : "";
    return `<tr>
      <td><div class="sym">${order.symbol}</div>
          <span class="pill ${order.side === "BUY" ? "up" : "down"}">${order.side}</span>
          ${order.tag && order.tag !== "NORMAL" ? `<span class="pill warn">${order.tag}</span>` : ""}</td>
      <td class="num">${fmtQty(order.qty)}${order.filled_qty && order.filled_qty < order.qty ? `<br><span class="dim" style="font-size:10px">${order.filled_qty} done</span>` : ""}</td>
      <td class="num">${price ? inr(price) : "-"}<br><span class="dim" style="font-size:10px">${order.type}</span></td>
      <td><span class="pill ${tone}">${order.status}</span>
          ${order.reason ? `<div class="dim" style="font-size:10px;white-space:normal;max-width:170px">${escapeHtml(order.reason)}</div>` : ""}</td>
      <td class="num">${isOpen ? `<button class="btn sm ghost" data-cancel="${order.id}">Cancel</button>` : `<span class="dim" style="font-size:10px">${shortTime(order.created_at)}</span>`}</td>
    </tr>`;
  }).join("");

  body.querySelectorAll("[data-cancel]").forEach((button) => {
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const order = await api.del(`/api/orders/${button.dataset.cancel}`);
        upsertOrder(order);
        renderOrders();
        toast("", "Order cancelled", `${order.symbol} ${order.side}`);
      } catch (error) {
        toast("down", "Could not cancel", error.message);
        button.disabled = false;
      }
    });
  });
}

function updateBookCount() {
  const badge = el("bookCount");
  if (!badge) return;
  const open = state.orders.filter((o) => o.status === "PENDING" || o.status === "TRIGGERED").length;
  const n = state.positions.length + open;
  badge.textContent = n || "";
}

/* --------------------------------------------------------------------- news */

function renderNews() {
  const box = el("newsList");
  if (!state.news.length) {
    box.innerHTML = `<div class="empty events-empty">
      <div class="events-empty-title">Events desk is quiet</div>
      <div>Headlines and rumours will land here — and slam onto the stage above. Tap a stock chip to jump into Trade.</div>
    </div>`;
    return;
  }
  el("newsCount").textContent = state.unreadNews || "";

  box.innerHTML = state.news.slice(0, 80).map((item, index) => {
    const kind = item.retracted ? "Retracted"
      : item.kind === "RUMOUR" ? "Rumour"
        : item.kind === "RESULTS" ? "Results" : "Breaking";
    const kindClass = item.retracted ? "down"
      : item.kind === "RUMOUR" ? "warn" : "accent";
    return `
    <article class="news-item event-card ${item.retracted ? "retracted" : ""} ${item.kind === "RUMOUR" ? "is-rumour" : ""} ${index === 0 ? "open" : ""}" data-news="${item.id}">
      <div class="event-rail" aria-hidden="true"></div>
      <div class="event-main">
        <div class="top">
          <span class="pill ${kindClass}">${kind}</span>
          <span class="time" title="${escapeHtml(shortTime(item.published_at))}">${relativeTime(item.published_at)}</span>
        </div>
        <div class="headline">${escapeHtml(item.headline)}</div>
        <div class="body">${escapeHtml(item.body || "")}</div>
        ${(item.symbols || []).length
          ? `<div class="tags">${item.symbols.map((s) => `<button type="button" class="sym-chip" data-sym="${escapeHtml(s)}">${escapeHtml(s)}</button>`).join("")}</div>`
          : ""}
      </div>
    </article>`;
  }).join("");

  box.querySelectorAll(".news-item").forEach((node) => {
    node.addEventListener("click", (event) => {
      if (event.target.closest("[data-sym]")) return;
      node.classList.toggle("open");
      const id = Number(node.dataset.news);
      const item = state.news.find((n) => n.id === id);
      if (item) showOnStage(item, { decision: false });
    });
  });
  box.querySelectorAll("[data-sym]").forEach((tag) => {
    tag.addEventListener("click", (event) => {
      event.stopPropagation();
      if (state.instruments.has(tag.dataset.sym)) {
        // Jump into Trade/Watch for that name (mobile focuses the ticket).
        select(tag.dataset.sym, { focus: true });
      }
    });
  });
}

/* -------------------------------------------------------------- leaderboard */

function yourRankInfo() {
  const board = state.leaderboard;
  if (board.you && board.you.rank != null) return board.you;
  const row = (board.rows || []).find((r) => r.is_you || (state.me && r.team_id === state.me.team.id));
  return row || null;
}

function renderHeaderRank() {
  const info = yourRankInfo();
  const rankEl = el("hdrRank");
  if (!rankEl) return;

  if (state.leaderboard.blackout && info?.rank != null) {
    rankEl.textContent = `#${info.rank}`;
    rankEl.title = "Leaderboard blackout — your rank still shown";
  } else if (info?.rank != null) {
    rankEl.textContent = `#${info.rank}`;
    rankEl.title = "";
  } else if (state.leaderboard.blackout) {
    rankEl.textContent = "—";
    rankEl.title = "Leaderboard blackout";
  } else {
    rankEl.textContent = "—";
    rankEl.title = "";
  }

  // Throttled rank-change feedback.
  if (info?.rank != null) {
    const prev = state.lastRank;
    if (prev != null && prev !== info.rank) {
      const now = Date.now();
      if (now - state.lastRankToastAt > RANK_TOAST_COOLDOWN_MS) {
        const up = info.rank < prev;
        toast(
          up ? "up" : "down",
          up ? `Rank up → #${info.rank}` : `Rank down → #${info.rank}`,
          `Was #${prev}`,
        );
        state.lastRankToastAt = now;
      }
    }
    state.lastRank = info.rank;
  }
}

function renderLeaderboard() {
  const box = el("leaderList");
  const board = state.leaderboard;
  renderHeaderRank();

  if (board.blackout) {
    box.innerHTML = `<div class="empty">
      <div style="font-size:15px;font-weight:600;margin-bottom:6px">Leaderboard blackout</div>
      Standings are hidden for the closing minutes. Your own position is still shown below.
    </div>${board.you ? youRow(board.you) : ""}`;
    return;
  }

  if (!board.rows?.length) {
    box.innerHTML = '<div class="empty">The board appears once trading starts.</div>';
    return;
  }

  box.innerHTML = `
    <table class="grid">
      <thead><tr><th style="width:34px">#</th><th>Team</th><th class="num">Account value</th></tr></thead>
      <tbody>
        ${board.rows.map((row) => `
          <tr ${row.is_you ? 'aria-selected="true"' : ""}>
            <td class="num dim">${row.rank}</td>
            <td>${escapeHtml(row.team)}${row.is_you ? ' <span class="pill accent">You</span>' : ""}
                ${row.status === "BUSTED" ? ' <span class="pill down">Out</span>' : ""}</td>
            <td class="num">${inr(row.equity)}</td>
          </tr>`).join("")}
      </tbody>
    </table>
    ${board.you && !board.rows.some((r) => r.is_you) ? youRow(board.you) : ""}`;
}

function youRow(you) {
  return `<div style="border-top:1px solid var(--line);padding:9px 10px;display:flex;justify-content:space-between;align-items:center;background:var(--accent-ghost)">
    <span><b>${you.rank}</b> &middot; ${escapeHtml(you.team)} <span class="pill accent">You</span></span>
    <span class="mono">${inr(you.equity)}</span>
  </div>`;
}

/* ---------------------------------------------------------------- chrome UI */

function wireTabs() {
  document.querySelectorAll("#rightTabs .tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll("#rightTabs .tab").forEach((t) =>
        t.setAttribute("aria-selected", String(t === tab)));
      // Scope to right-column panes only — never touch mobile-nav / other nodes.
      document.querySelectorAll(".col.right [data-pane]").forEach((pane) => {
        pane.hidden = pane.dataset.pane !== tab.dataset.tab;
      });
      if (tab.dataset.tab === "news") {
        state.unreadNews = 0;
        el("newsCount").textContent = "";
      }
      if (tab.dataset.tab === "book") advanceCoachIf(2);
    });
  });
}

function wireMobileNav() {
  document.querySelectorAll(".mobile-nav button").forEach((button) => {
    button.addEventListener("click", () => {
      const target = button.dataset.mobile;
      if (target === "more") {
        openMoreSheet();
        document.querySelectorAll(".mobile-nav button").forEach((b) =>
          b.setAttribute("aria-pressed", String(b === button)));
        return;
      }
      closeMoreSheet();
      showPane(target, button.dataset.tab);
    });
  });
}

function wireMoreSheet() {
  const sheet = el("moreSheet");
  el("moreClose").addEventListener("click", closeMoreSheet);
  sheet.addEventListener("click", (event) => {
    if (event.target === sheet) closeMoreSheet();
  });
  sheet.querySelectorAll("[data-more-tab]").forEach((btn) => {
    btn.addEventListener("click", () => {
      closeMoreSheet();
      showPane("right", btn.dataset.moreTab);
    });
  });
}

function openMoreSheet() {
  el("moreSheet").hidden = false;
}

function closeMoreSheet() {
  el("moreSheet").hidden = true;
}

function showPane(pane, tab) {
  document.querySelectorAll(".mobile-nav button").forEach((b) => {
    const match = b.dataset.mobile === pane && (!tab || b.dataset.tab === tab || b.dataset.mobile === "more");
    // Prefer exact Book match when tab is book; More stays pressed only when sheet open.
    if (pane === "right" && tab && tab !== "book") {
      b.setAttribute("aria-pressed", String(b.dataset.mobile === "more"));
    } else {
      b.setAttribute("aria-pressed", String(b.dataset.mobile === pane && (pane !== "right" || !tab || b.dataset.tab === tab)));
    }
  });
  document.querySelectorAll(".col").forEach((col) => {
    col.classList.toggle("mobile-active", col.dataset.col === pane);
  });
  if (tab) document.querySelector(`#rightTabs .tab[data-tab="${tab}"]`)?.click();
  if (pane === "centre") state.chart?.draw();
  if (pane === "right" && tab === "book") advanceCoachIf(2);
}

/* -------------------------------------------------------------------- coach */

const COACH_STEPS = [
  "Pick a stock from the watchlist.",
  "Place a tiny buy (qty 1 is fine) to feel a fill.",
  "Open Book to see your position and any open orders.",
];

function wireCoach() {
  el("coachSkip").addEventListener("click", dismissCoach);
  el("coachNext").addEventListener("click", () => {
    const step = Number(el("coach").dataset.step || 0);
    if (step >= COACH_STEPS.length - 1) dismissCoach();
    else showCoachStep(step + 1);
  });
}

function maybeShowCoach() {
  let seen = null;
  try { seen = localStorage.getItem(COACH_KEY); } catch { /* ignore */ }
  if (seen) return;
  showCoachStep(0);
}

function showCoachStep(step) {
  const coach = el("coach");
  coach.hidden = false;
  coach.dataset.step = String(step);
  el("coachStepLabel").textContent = `${step + 1} / ${COACH_STEPS.length}`;
  el("coachText").textContent = COACH_STEPS[step];
  el("coachNext").textContent = step >= COACH_STEPS.length - 1 ? "Done" : "Next";
}

function dismissCoach() {
  el("coach").hidden = true;
  try { localStorage.setItem(COACH_KEY, "1"); } catch { /* ignore */ }
}

function advanceCoachIf(step) {
  const coach = el("coach");
  if (!coach || coach.hidden) return;
  const current = Number(coach.dataset.step || 0);
  if (current === step) {
    if (step >= COACH_STEPS.length - 1) dismissCoach();
    else showCoachStep(step + 1);
  }
}

/* ------------------------------------------------------------------- clock */

function tickClock() {
  const market = state.market;
  const countdown = el("countdown");
  if (!market || !market.ends_at) {
    countdown.textContent = "--:--";
    return;
  }
  const remaining = (new Date(market.ends_at).getTime() - Date.now()) / 1000;
  countdown.textContent = duration(remaining);
  countdown.className = `countdown ${remaining < 60 && market.state === "OPEN" ? "down" : ""}`;
}

/* ------------------------------------------------------------------- toasts */

let toastId = 0;
function toast(tone, title, message) {
  const host = el("toasts");
  const node = document.createElement("div");
  node.className = `toast ${tone}`;
  node.innerHTML = `<div class="title">${escapeHtml(title)}</div>${message ? `<div class="msg">${escapeHtml(message)}</div>` : ""}`;
  const id = ++toastId;
  node.dataset.id = id;
  host.appendChild(node);
  setTimeout(() => node.remove(), tone === "down" ? 9000 : 5000);
  while (host.children.length > 5) host.firstChild.remove();
}

boot();
