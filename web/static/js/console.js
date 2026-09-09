/* The operator console.
 *
 * Design rules, which are different from the participant terminal's:
 *
 * - Anything irreversible asks for confirmation by making you type the thing.
 *   A mistyped jump or an accidental freeze is paid for by every team at once,
 *   and a dialog you can dismiss with the space bar is not a confirmation.
 * - Every control says what it will do, in the button, before you press it.
 * - The console shows the field before it shows the tools: who is in margin
 *   trouble, what the field is holding. An operator about to fire a crash
 *   should be able to see who it lands on.
 */

import { opsApi as api } from "./api.js";
import { STATE_TEXT, duration, escapeHtml, inr, inrShort, pct, qty as fmtQty, shortTime, signClass } from "./format.js";
import { Stream } from "./stream.js";

const el = (id) => document.getElementById(id);

const state = {
  me: null,
  market: null,
  instruments: new Map(),
  teams: [],
  scenarios: [],
  news: [],
  blotter: [],
  analytics: null,
  connected: false,
};

const stream = new Stream("/ws/ops");

async function boot() {
  try {
    const theme = localStorage.getItem("exchange.theme");
    if (theme) document.documentElement.setAttribute("data-theme", theme);
  } catch { /* ignore */ }

  try {
    state.me = await api.get("/api/auth/ops/me");
  } catch {
    window.location.href = "/console/login";
    return;
  }
  el("operatorName").textContent = state.me.operator.name;
  el("operatorRole").textContent = state.me.operator.role.replace(/_/g, " ");
  applyRolePermissions();

  wireMarketControls();
  wirePriceDesk();
  wireNewsDesk();
  wireChrome();

  await refreshAll();
  wireStream();
  stream.connect(api.token);

  setInterval(tickClock, 250);
  setInterval(refreshSlow, 8000);
}

async function refreshAll() {
  await Promise.all([
    loadMarket(), loadInstruments(), loadTeams(), loadScenarios(),
    loadNews(), loadBlotter(), loadAnalytics(), loadHealth(), loadAudit(), loadAdjustments(),
    loadResults(),
  ]);
}

async function refreshSlow() {
  if (!state.connected) return;
  await Promise.all([loadTeams(), loadAnalytics(), loadHealth(), loadScenarios()]);
  if (state.market?.state === "FINAL") await loadResults();
}

/* ---------------------------------------------------------------- loaders */

async function loadMarket() {
  state.market = await api.get("/api/market");
  renderMarket();
}

async function loadInstruments() {
  const data = await api.get("/api/instruments");
  data.instruments.forEach((row) => state.instruments.set(row.symbol, row));
  renderPriceDesk();
  fillSymbolPickers();
}

async function loadTeams() {
  try {
    const data = await api.get("/api/admin/teams");
    state.teams = data.teams;
    renderTeams();
    renderRisk();
  } catch { /* judges and projector may lack access */ }
}

async function loadScenarios() {
  try {
    state.scenarios = (await api.get("/api/admin/scenarios")).scenarios;
    renderScenarios();
  } catch { /* ignore */ }
}

async function loadNews() {
  try {
    state.news = (await api.get("/api/admin/news")).news;
    renderNewsList();
  } catch { /* ignore */ }
}

async function loadBlotter() {
  try {
    state.blotter = (await api.get("/api/admin/blotter?limit=60")).fills;
    renderBlotter();
  } catch { /* ignore */ }
}

async function loadAnalytics() {
  try {
    state.analytics = await api.get("/api/admin/analytics");
    renderAnalytics();
  } catch { /* ignore */ }
}

async function loadHealth() {
  try {
    const health = await api.get("/api/admin/health");
    const engine = health.engine;
    el("healthBox").innerHTML = `
      <dl class="kv">
        <dt>Database</dt><dd class="${health.database ? "up" : "down"}">${health.database ? "OK" : "DOWN"}</dd>
        <dt>Engine</dt><dd class="${engine.running ? "up" : "down"}">${engine.running ? "running" : "stopped"}</dd>
        <dt>Tick latency (avg)</dt><dd>${engine.tick_ms_avg} ms</dd>
        <dt>Tick latency (p95)</dt><dd class="${engine.tick_ms_p95 > 300 ? "down" : ""}">${engine.tick_ms_p95} ms</dd>
        <dt>Tick errors</dt><dd class="${engine.errors ? "down" : ""}">${engine.errors}</dd>
        <dt>Connected clients</dt><dd>${engine.websockets.connections}</dd>
        <dt>Dropped messages</dt><dd>${engine.websockets.dropped_messages}</dd>
        <dt>Last tick</dt><dd>${engine.last_tick_at ? shortTime(engine.last_tick_at) : "-"}</dd>
      </dl>`;
  } catch { /* ignore */ }
}

async function loadAudit() {
  try {
    const data = await api.get("/api/admin/audit?limit=40");
    el("auditBox").innerHTML = data.entries.length
      ? data.entries.map((row) => `
        <div style="padding:5px 9px;border-bottom:1px solid var(--line-soft);font-size:11.5px">
          <span class="mono dim">${shortTime(row.ts)}</span>
          <b>${escapeHtml(row.actor)}</b>
          <span class="pill">${escapeHtml(row.action)}</span>
          ${row.target ? `<span class="muted">${escapeHtml(row.target)}</span>` : ""}
        </div>`).join("")
      : '<div class="empty">No operator actions yet.</div>';
  } catch { /* ignore */ }
}

async function loadAdjustments() {
  try {
    const data = await api.get("/api/admin/adjustments");
    const pending = data.adjustments.filter((a) => !a.resolved);
    el("adjustmentsBox").innerHTML = pending.length
      ? pending.map((a) => `
        <div style="padding:7px 9px;border-bottom:1px solid var(--line-soft);display:flex;gap:8px;align-items:center">
          <span class="mono">${inr(a.amount, { sign: true })}</span>
          <span class="muted" style="flex:1;font-size:11.5px">${escapeHtml(a.reason)}</span>
          <button class="btn sm primary" data-approve="${a.id}">Approve</button>
        </div>`).join("")
      : '<div class="empty">No adjustments waiting for approval.</div>';
    el("adjustmentsBox").querySelectorAll("[data-approve]").forEach((button) => {
      button.addEventListener("click", async () => {
        button.disabled = true;
        try {
          await api.post(`/api/admin/adjustments/${button.dataset.approve}/approve`);
          toast("Adjustment approved");
          await Promise.all([loadAdjustments(), loadTeams()]);
        } catch (error) {
          toast(error.message, "down");
          button.disabled = false;
        }
      });
    });
  } catch { /* ignore */ }
}

async function loadResults() {
  try {
    const data = await api.get("/api/admin/results");
    state.results = data;
    renderResults();
  } catch { /* judges and the projector may not have access */ }
}

function renderResults() {
  const data = state.results;
  const panel = el("resultsPanel");
  if (!panel) return;
  // The table is only meaningful once the competition is locked, so the panel
  // stays out of the way until then rather than showing a half-finished result.
  panel.hidden = !data || !data.final;
  if (panel.hidden) return;

  const podium = data.results.slice(0, 3);
  el("resultsBox").innerHTML = `
    <div style="padding:10px;display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px">
      ${podium.map((row, index) => `
        <div class="panel" style="padding:10px;border-color:${index === 0 ? "var(--accent)" : "var(--line)"}">
          <div class="label">${["Winner", "Runner-up", "Third"][index]}</div>
          <div style="font-size:15px;font-weight:700;margin:3px 0">${escapeHtml(row.team)}</div>
          <div class="mono" style="font-size:14px">${inr(row.final_equity)}</div>
          <div class="mono ${signClass(row.pnl)}" style="font-size:11px">
            ${inr(row.pnl, { sign: true })} (${pct(row.pnl_pct)})
          </div>
        </div>`).join("")}
    </div>

    <div style="padding:0 10px 10px">
      <div class="label" style="margin-bottom:6px">Awards</div>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:8px">
        ${data.awards.map((award) => `
          <div style="border:1px solid var(--line);border-radius:var(--radius);padding:8px 10px">
            <div style="font-weight:600;font-size:12px">${escapeHtml(award.title)}</div>
            <div class="dim" style="font-size:10.5px;margin:2px 0 4px">${escapeHtml(award.description)}</div>
            ${award.team
              ? `<div><b>${escapeHtml(award.team)}</b> <span class="mono dim">${escapeHtml(award.value || "")}</span></div>`
              : '<div class="dim">Nobody qualified.</div>'}
          </div>`).join("")}
      </div>
    </div>

    <table class="grid">
      <thead><tr>
        <th style="width:34px">#</th><th>Team</th><th class="num">Final value</th>
        <th class="num">P&amp;L</th><th class="num">Drawdown</th><th class="num">Trades</th>
        <th class="num">Win rate</th><th class="num">Charges</th>
      </tr></thead>
      <tbody>
        ${data.results.map((row) => `
          <tr>
            <td class="num dim">${row.rank}</td>
            <td>${escapeHtml(row.team)}${row.status === "BUSTED" ? ' <span class="pill down">Out</span>' : ""}</td>
            <td class="num">${inr(row.final_equity)}</td>
            <td class="num ${signClass(row.pnl)}">${inr(row.pnl, { sign: true })}</td>
            <td class="num">${pct(row.max_drawdown_pct, { sign: false })}</td>
            <td class="num">${row.trades}</td>
            <td class="num">${row.closed_trades ? pct(row.win_rate, { sign: false }) : "-"}</td>
            <td class="num dim">${inr(row.charges)}</td>
          </tr>`).join("")}
      </tbody>
    </table>`;
}

/**
 * Grey out what this operator cannot do, and say who can.
 *
 * The alternative is a button that looks live, gets a 403 from the server and
 * reads as broken software. During an event that costs somebody several minutes
 * of trying the same thing harder. Everything is still enforced server-side;
 * this only stops the console offering an action it knows will be refused.
 */
function applyRolePermissions() {
  const role = state.me.operator.role;
  const superAdmin = role === "SUPER_ADMIN";

  const restricted = [
    ["resetBtn", "Only the Event Director or Deputy can reset the competition."],
    ["finaliseBtn", "Only the Event Director or Deputy can end the competition."],
  ];
  for (const [id, why] of restricted) {
    const button = el(id);
    if (!button || superAdmin) continue;
    button.disabled = true;
    button.title = why;
    button.style.opacity = "0.45";
    button.style.cursor = "not-allowed";
    const note = document.createElement("p");
    note.className = "dim";
    note.style.cssText = "margin:0;font-size:11px";
    note.textContent = `${why} You are signed in as ${role.replace(/_/g, " ").toLowerCase()}.`;
    button.insertAdjacentElement("afterend", note);
  }
}

/* -------------------------------------------------------- market controls */

function renderMarket() {
  const market = state.market;
  const stateEl = el("marketState");
  stateEl.textContent = STATE_TEXT[market.state] || market.state;
  stateEl.className = `state ${market.state}`;
  el("dayNo").textContent = market.day_no ? `Day ${market.day_no}/${market.total_days}` : "Not started";
  el("indexValue").textContent = inr(market.index.value);
  el("blackoutBtn").textContent = market.blackout ? "End blackout" : "Start blackout";

  const frozen = market.state === "FROZEN";
  el("freezeBtn").hidden = frozen;
  el("resumeBtn").hidden = !frozen;
  el("marketBanner").textContent = market.banner || "";
}

function wireMarketControls() {
  const action = async (path, options = {}) => {
    try {
      await api.post(path, options.body);
      toast(options.success || "Done");
      await loadMarket();
    } catch (error) {
      toast(error.message, "down");
    }
  };

  el("preOpenBtn").addEventListener("click", () =>
    action("/api/admin/market/pre-open", { success: "Pre-open started" }));
  el("openBtn").addEventListener("click", () =>
    action("/api/admin/market/open", { success: "Market open" }));
  el("closeBtn").addEventListener("click", () =>
    action("/api/admin/market/close", { success: "Day closed" }));

  el("haltBtn").addEventListener("click", () =>
    action("/api/admin/market/halt", {
      body: { message: "Trading is halted market-wide. Please hold.", severity: "warning" },
      success: "Market halted",
    }));

  el("freezeBtn").addEventListener("click", async () => {
    const confirmation = await confirmByTyping(
      "Freeze the market",
      "This stops trading and the clock for every team. Use it first in any incident, then diagnose.",
      "FREEZE",
    );
    if (!confirmation) return;
    await action("/api/admin/market/freeze", {
      body: { confirm: "FREEZE", message: el("freezeMessage").value || "Market frozen by the organisers. Please hold." },
      success: "Market frozen",
    });
  });

  el("resumeBtn").addEventListener("click", () =>
    action("/api/admin/market/resume?to=OPEN", { success: "Market resumed" }));

  el("blackoutBtn").addEventListener("click", () =>
    action(`/api/admin/market/blackout?on=${!state.market.blackout}`, { success: "Leaderboard visibility changed" }));

  el("resetBtn").addEventListener("click", async () => {
    const confirmation = await confirmByTyping(
      "Reset the competition",
      "This deletes every trade and returns all teams to their opening balance. "
      + "Teams, members and the audit log are kept. Use it after the practice session.",
      "RESET",
    );
    if (!confirmation) return;
    try {
      const result = await api.post("/api/admin/reset-trading?confirm=RESET");
      toast(`Reset. ${result.teams_reset} teams are back to their opening balance.`);
      await refreshAll();
    } catch (error) {
      toast(error.message, "down");
    }
  });

  el("finaliseBtn").addEventListener("click", async () => {
    const confirmation = await confirmByTyping(
      "End the competition",
      "This locks the results permanently. Do it after the last close, not before.",
      "FINAL",
    );
    if (!confirmation) return;
    await action("/api/admin/market/finalise?confirm=FINAL", { success: "Competition finalised" });
    await loadResults();
  });

  el("broadcastForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const message = el("broadcastMessage").value.trim();
    if (!message) return;
    await action("/api/admin/market/broadcast", {
      body: { message, severity: el("broadcastSeverity").value },
      success: "Broadcast sent",
    });
    el("broadcastMessage").value = "";
  });

  el("invariantsBtn").addEventListener("click", async () => {
    el("invariantsBtn").disabled = true;
    try {
      const result = await api.post("/api/admin/invariants/check");
      el("invariantResult").innerHTML = result.ok
        ? `<span class="up">All ${result.teams_checked} teams reconcile. Cash matches the ledger exactly.</span>`
        : `<span class="down">${result.mismatches.length} of ${result.teams_checked} teams do not reconcile.</span>
           <pre style="white-space:pre-wrap;font-size:11px">${escapeHtml(JSON.stringify(result.mismatches, null, 1))}</pre>`;
    } catch (error) {
      el("invariantResult").innerHTML = `<span class="down">${escapeHtml(error.message)}</span>`;
    } finally {
      el("invariantsBtn").disabled = false;
    }
  });
}

/* ------------------------------------------------------------- price desk */

function fillSymbolPickers() {
  const symbols = [...state.instruments.keys()].sort();
  const sectors = [...new Set([...state.instruments.values()].map((i) => i.sector))].sort();
  document.querySelectorAll("select[data-symbols]").forEach((select) => {
    const current = select.value;
    select.innerHTML =
      (select.dataset.symbols === "optional" ? '<option value="">- whole market -</option>' : "") +
      symbols.map((s) => `<option value="${s}">${s}</option>`).join("");
    if (current) select.value = current;
  });
  document.querySelectorAll("select[data-sectors]").forEach((select) => {
    select.innerHTML =
      '<option value="">- pick a sector -</option>' +
      sectors.map((s) => `<option value="${escapeHtml(s)}">${escapeHtml(s)}</option>`).join("");
  });
}

function renderPriceDesk() {
  const rows = [...state.instruments.values()].sort((a, b) => a.symbol.localeCompare(b.symbol));
  el("priceBody").innerHTML = rows.map((row) => `
    <tr>
      <td><div class="sym">${row.symbol}</div><div class="co">${escapeHtml(row.sector)}</div></td>
      <td class="num">${inr(row.last)}</td>
      <td class="num ${signClass(row.change_pct)}">${pct(row.change_pct)}</td>
      <td>${statusPill(row)}</td>
      <td class="num">
        <button class="btn sm ghost" data-halt="${row.symbol}">${row.status === "HALTED" ? "Resume" : "Halt"}</button>
      </td>
    </tr>`).join("");

  el("priceBody").querySelectorAll("[data-halt]").forEach((button) => {
    button.addEventListener("click", async () => {
      const symbol = button.dataset.halt;
      const instrument = state.instruments.get(symbol);
      const halting = instrument.status !== "HALTED";
      button.disabled = true;
      try {
        await api.post(halting ? "/api/admin/prices/halt" : "/api/admin/prices/resume", {
          symbol,
          reason: halting ? el("haltReason").value || "pending an announcement" : null,
        });
        toast(`${symbol} ${halting ? "halted" : "resumed"}`);
        await loadInstruments();
      } catch (error) {
        toast(error.message, "down");
        button.disabled = false;
      }
    });
  });
}

function statusPill(row) {
  const map = {
    HALTED: '<span class="pill warn">Halted</span>',
    SUSPENDED: '<span class="pill">Suspended</span>',
    UPPER_CIRCUIT: '<span class="pill up">Upper circuit</span>',
    LOWER_CIRCUIT: '<span class="pill down">Lower circuit</span>',
  };
  return map[row.status] || '<span class="pill">Active</span>';
}

function wirePriceDesk() {
  el("moveForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const scope = el("moveScope").value;
    const body = {
      pct: el("movePct").value,
      over_seconds: parseInt(el("moveSeconds").value, 10) || 0,
      note: el("moveNote").value || null,
    };
    if (scope === "symbol") body.symbol = el("moveSymbol").value;
    else if (scope === "sector") body.sector = el("moveSector").value;

    try {
      const result = await api.post("/api/admin/prices/move", body);
      toast(`Moving ${result.symbols.length} stock${result.symbols.length === 1 ? "" : "s"} by ${body.pct}% over ${body.over_seconds}s`);
      el("moveNote").value = "";
    } catch (error) {
      toast(error.message, "down");
    }
  });

  el("moveScope").addEventListener("change", () => {
    const scope = el("moveScope").value;
    el("moveSymbolField").hidden = scope !== "symbol";
    el("moveSectorField").hidden = scope !== "sector";
  });

  el("jumpForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const symbol = el("jumpSymbol").value;
    const percentage = el("jumpPct").value;
    const confirmation = await confirmByTyping(
      `Jump ${symbol} by ${percentage}%`,
      "An instant price change creates an arbitrage window for anyone watching. Use it for scripted shocks and opening gaps only.",
      symbol,
    );
    if (!confirmation) return;
    try {
      await api.post("/api/admin/prices/jump", { symbol, pct: percentage, confirm: symbol });
      toast(`${symbol} jumped ${percentage}%`);
    } catch (error) {
      toast(error.message, "down");
    }
  });

  el("volForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const body = {
      multiplier: el("volMultiplier").value,
      over_seconds: parseInt(el("volSeconds").value, 10) || 300,
    };
    const symbol = el("volSymbol").value;
    if (symbol) body.symbol = symbol;
    try {
      await api.post("/api/admin/prices/volatility", body);
      toast(`Volatility set to ${body.multiplier}x`);
    } catch (error) {
      toast(error.message, "down");
    }
  });

  el("dividendForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const symbol = el("dividendSymbol").value;
    const amount = el("dividendAmount").value;
    const confirmation = await confirmByTyping(
      `Pay Rs ${amount} per share on ${symbol}`,
      "Holders are credited and short sellers are debited, and the price drops by the "
      + "dividend. No team's account value changes.",
      symbol,
    );
    if (!confirmation) return;
    try {
      const result = await api.post("/api/admin/prices/dividend", {
        symbol, amount_per_share: amount, confirm: symbol,
      });
      toast(`${symbol} went ex-dividend at ${inr(result.ex_price)}. ${result.teams_paid} teams settled.`);
      await loadInstruments();
    } catch (error) {
      toast(error.message, "down");
    }
  });

  el("splitForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const symbol = el("splitSymbol").value;
    const ratio = el("splitRatio").value;
    const confirmation = await confirmByTyping(
      `Split ${symbol} ${ratio}-for-1`,
      "Holdings and the price are both scaled, so nobody is richer or poorer. "
      + "Resting orders in this stock will be cancelled.",
      symbol,
    );
    if (!confirmation) return;
    try {
      const result = await api.post("/api/admin/prices/split", { symbol, ratio, confirm: symbol });
      toast(
        `${symbol} split. New price ${inr(result.new_price)}, ${result.positions_adjusted} positions `
        + `adjusted, ${result.orders_cancelled} orders cancelled.`,
      );
      await loadInstruments();
    } catch (error) {
      toast(error.message, "down");
    }
  });

  el("undoBtn").addEventListener("click", async () => {
    try {
      const result = await api.post("/api/admin/prices/undo");
      const count = result.affected_fills.length;
      toast(
        `${result.symbol || "Last action"} restored to ${inr(result.restored_price)}. ` +
        `${count} fill${count === 1 ? "" : "s"} happened at the old price and stand.`,
        count ? "warn" : "",
      );
      await loadInstruments();
    } catch (error) {
      toast(error.message, "down");
    }
  });
}

/* ------------------------------------------------------------- news desk */

function wireNewsDesk() {
  el("newsForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const symbols = el("newsSymbols").value.split(",").map((s) => s.trim()).filter(Boolean);
    try {
      await api.post("/api/admin/news", {
        headline: el("newsHeadline").value.trim(),
        body: el("newsBody").value.trim(),
        kind: el("newsKind").value,
        symbols,
        sentiment: el("newsSentiment").value || null,
      });
      toast("Published to every terminal");
      el("newsHeadline").value = "";
      el("newsBody").value = "";
      el("newsSymbols").value = "";
      await loadNews();
    } catch (error) {
      toast(error.message, "down");
    }
  });
}

function renderNewsList() {
  el("newsHistory").innerHTML = state.news.length
    ? state.news.slice(0, 30).map((item) => `
      <div class="news-item ${item.retracted ? "retracted" : ""}">
        <div class="top">
          <span class="pill ${item.kind === "RUMOUR" ? "warn" : ""}">${item.kind}</span>
          ${item.sentiment ? `<span class="pill accent">${escapeHtml(item.sentiment)}</span>` : ""}
          <span class="time">${item.published_at ? shortTime(item.published_at) : "scheduled"}</span>
        </div>
        <div class="headline">${escapeHtml(item.headline)}</div>
        ${!item.retracted ? `<button class="btn sm ghost" data-retract="${item.id}" style="margin-top:5px">Retract</button>` : ""}
      </div>`).join("")
    : '<div class="empty">Nothing published yet.</div>';

  el("newsHistory").querySelectorAll("[data-retract]").forEach((button) => {
    button.addEventListener("click", async () => {
      try {
        await api.post(`/api/admin/news/${button.dataset.retract}/retract`);
        toast("Retracted. The headline stays visible, struck through.");
        await loadNews();
      } catch (error) {
        toast(error.message, "down");
      }
    });
  });
}

/* -------------------------------------------------------------- scenarios */

function renderScenarios() {
  el("scenarioBox").innerHTML = state.scenarios.length
    ? state.scenarios.map((scenario) => {
        const fired = scenario.steps.filter((s) => s.fired_at).length;
        return `
        <div style="border-bottom:1px solid var(--line);padding-bottom:8px;margin-bottom:8px">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
            <b>${escapeHtml(scenario.name)}</b>
            <span class="pill ${scenario.status === "PLAYING" ? "up" : ""}">${scenario.status}</span>
            <span class="dim" style="font-size:11px">${fired}/${scenario.steps.length} fired</span>
            <div style="flex:1"></div>
            <button class="btn sm ${scenario.status === "PLAYING" ? "" : "primary"}"
                    data-scenario="${scenario.id}"
                    data-act="${scenario.status === "PLAYING" ? "pause" : "play"}">
              ${scenario.status === "PLAYING" ? "Pause" : "Play"}
            </button>
          </div>
          ${scenario.steps.map((step) => `
            <div class="step ${step.fired_at ? "fired" : ""}">
              <span class="at">${formatOffset(step.at_offset)}</span>
              <span>${escapeHtml(step.label || "step")}</span>
              <span>${step.fired_at
                ? `<span class="dim mono" style="font-size:10px">${shortTime(step.fired_at)}</span>`
                : `<button class="btn sm ghost" data-fire="${step.id}">Fire now</button>
                   <button class="btn sm ghost" data-skip="${step.id}">Skip</button>`}</span>
            </div>`).join("")}
        </div>`;
      }).join("")
    : '<div class="empty">No scenario scripts loaded. Add YAML files to config/scenarios and re-seed.</div>';

  el("scenarioBox").querySelectorAll("[data-scenario]").forEach((button) => {
    button.addEventListener("click", async () => {
      try {
        await api.post(`/api/admin/scenarios/${button.dataset.scenario}/${button.dataset.act}`);
        await loadScenarios();
      } catch (error) {
        toast(error.message, "down");
      }
    });
  });
  el("scenarioBox").querySelectorAll("[data-fire]").forEach((button) => {
    button.addEventListener("click", async () => {
      try {
        await api.post(`/api/admin/scenarios/steps/${button.dataset.fire}/fire`);
        toast("Step fired");
        await Promise.all([loadScenarios(), loadInstruments()]);
      } catch (error) {
        toast(error.message, "down");
      }
    });
  });
  el("scenarioBox").querySelectorAll("[data-skip]").forEach((button) => {
    button.addEventListener("click", async () => {
      await api.post(`/api/admin/scenarios/steps/${button.dataset.skip}/skip`);
      await loadScenarios();
    });
  });
}

function formatOffset(seconds) {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

/* ------------------------------------------------------------------ teams */

function renderTeams() {
  const rows = state.teams;
  el("teamsBody").innerHTML = rows.length
    ? rows.map((team) => `
      <tr data-team="${team.team_id}">
        <td class="num dim">${team.rank}</td>
        <td><div class="sym">${escapeHtml(team.name)}</div><div class="co">${team.code}</div></td>
        <td class="num">${inr(team.equity)}</td>
        <td class="num">${inrShort(team.short_mv)}</td>
        <td class="num ${Number(team.leverage) >= 4 ? "down" : ""}">${team.leverage}x</td>
        <td>${marginPill(team)}</td>
        <td class="num dim">${team.orders}</td>
      </tr>`).join("")
    : '<div class="empty">No teams yet.</div>';

  el("teamsBody").querySelectorAll("tr[data-team]").forEach((tr) => {
    tr.addEventListener("click", () => openTeam(tr.dataset.team));
  });
}

function marginPill(team) {
  const map = {
    OK: '<span class="pill up">OK</span>',
    WARNING: '<span class="pill warn">Warning</span>',
    CALL: '<span class="pill down">Margin call</span>',
    BUST: '<span class="pill down">Bust</span>',
  };
  if (team.status === "BUSTED") return '<span class="pill down">Out</span>';
  return map[team.margin_state] || "";
}

function renderRisk() {
  const trouble = state.teams.filter(
    (t) => t.status === "ACTIVE" && (t.margin_state === "WARNING" || t.margin_state === "CALL"),
  );
  el("riskCount").textContent = trouble.length || "";
  el("riskBox").innerHTML = trouble.length
    ? trouble.map((team) => `
      <div style="padding:7px 9px;border-bottom:1px solid var(--line-soft);display:flex;gap:8px;align-items:center">
        ${marginPill(team)}
        <b style="flex:1">${escapeHtml(team.name)}</b>
        <span class="mono">${inr(team.equity)}</span>
        <span class="mono dim">${team.leverage}x</span>
      </div>`).join("")
    : '<div class="empty">Nobody is close to a margin call.</div>';
}

async function openTeam(teamId) {
  try {
    const detail = await api.get(`/api/admin/teams/${teamId}`);
    showModal(
      `${detail.team.name} (${detail.team.code})`,
      `
      <dl class="kv">
        <dt>Account value</dt><dd class="big">${inr(detail.funds.equity)}</dd>
        <dt>Cash</dt><dd>${inr(detail.funds.cash)}</dd>
        <dt>Holdings</dt><dd>${inr(detail.funds.long_mv)}</dd>
        <dt>Short exposure</dt><dd>${inr(detail.funds.short_mv)}</dd>
        <dt>Available</dt><dd>${inr(detail.funds.available)}</dd>
        <dt>Leverage</dt><dd>${detail.funds.leverage}x</dd>
        <dt>Status</dt><dd>${detail.team.status}</dd>
      </dl>
      <h3>Members</h3>
      <table class="grid"><tbody>
        ${detail.members.map((m) => `<tr>
          <td>${escapeHtml(m.name)} ${m.role === "CAPTAIN" ? '<span class="pill accent">Captain</span>' : ""}</td>
          <td class="mono">${m.login}</td>
          <td class="dim">${m.last_seen_at ? shortTime(m.last_seen_at) : "never signed in"}</td>
          <td class="num"><button class="btn sm ghost" data-reset="${m.id}" data-team="${teamId}">Reset password</button></td>
        </tr>`).join("")}
      </tbody></table>
      <h3>Positions</h3>
      ${detail.positions.length ? `<table class="grid">
        <thead><tr><th>Stock</th><th class="num">Qty</th><th class="num">Avg</th><th class="num">P&amp;L</th></tr></thead>
        <tbody>${detail.positions.map((p) => `<tr>
          <td>${p.symbol} <span class="pill ${p.side.toLowerCase()}">${p.side}</span></td>
          <td class="num">${fmtQty(Math.abs(p.qty))}</td>
          <td class="num">${inr(p.avg_cost)}</td>
          <td class="num ${signClass(p.unrealised_pnl)}">${inr(p.unrealised_pnl, { sign: true })}</td>
        </tr>`).join("")}</tbody></table>` : '<div class="empty">No open positions.</div>'}
      <h3>Recent orders</h3>
      <table class="grid"><tbody>
        ${detail.orders.slice(0, 12).map((o) => `<tr>
          <td class="dim mono">${shortTime(o.created_at)}</td>
          <td>${o.symbol} <span class="pill ${o.side === "BUY" ? "up" : "down"}">${o.side}</span></td>
          <td class="num">${fmtQty(o.qty)}</td>
          <td class="num">${o.avg_price ? inr(o.avg_price) : "-"}</td>
          <td><span class="pill">${o.status}</span></td>
        </tr>`).join("")}
      </tbody></table>
      <h3>Cash adjustment</h3>
      <p class="dim" style="font-size:11.5px;margin:0">
        Requires a second operator to approve before it posts. Trades are never reversed;
        an adjustment is how a platform fault is put right.
      </p>
      <div class="row">
        <input type="number" id="adjAmount" placeholder="Amount, minus for a debit" step="0.01">
        <input type="text" id="adjReason" placeholder="Reason (recorded)">
      </div>
      <button class="btn" id="adjSubmit" data-team="${teamId}">Request adjustment</button>
      `,
    );

    document.querySelectorAll("[data-reset]").forEach((button) => {
      button.addEventListener("click", async () => {
        try {
          const result = await api.post(
            `/api/admin/teams/${button.dataset.team}/reset-password?member_id=${button.dataset.reset}`,
          );
          showModal(
            "New password issued",
            `<p>Write this down and hand it over. It is not recoverable and it is not shown again.</p>
             <div class="preview"><div class="line"><span class="k">Login</span><span class="v">${result.login}</span></div>
             <div class="line"><span class="k">Password</span><span class="v" style="font-size:16px">${result.password}</span></div></div>
             <p class="dim" style="font-size:11.5px">Any session that member had open has been signed out.</p>`,
          );
        } catch (error) {
          toast(error.message, "down");
        }
      });
    });

    el("adjSubmit")?.addEventListener("click", async () => {
      const amount = el("adjAmount").value;
      const reason = el("adjReason").value.trim();
      if (!amount || reason.length < 5) {
        toast("Give an amount and a reason of at least five characters.", "down");
        return;
      }
      try {
        await api.post("/api/admin/adjustments", { team_id: Number(teamId), amount, reason });
        toast("Requested. A second operator must approve it.");
        closeModal();
        await loadAdjustments();
      } catch (error) {
        toast(error.message, "down");
      }
    });
  } catch (error) {
    toast(error.message, "down");
  }
}

/* -------------------------------------------------------------- analytics */

function renderAnalytics() {
  const data = state.analytics;
  if (!data) return;
  el("fieldBox").innerHTML = `
    <dl class="kv">
      <dt>Teams</dt><dd>${data.field.teams}</dd>
      <dt>Still trading</dt><dd>${data.field.active}</dd>
      <dt>Out</dt><dd class="${data.field.busted ? "down" : ""}">${data.field.busted}</dd>
      <dt>In margin trouble</dt><dd class="${data.field.in_margin_trouble ? "warn" : ""}">${data.field.in_margin_trouble}</dd>
      <dt>Median account value</dt><dd>${inr(data.field.median_equity)}</dd>
      <dt>Total short exposure</dt><dd>${inr(data.field.total_short_mv)}</dd>
    </dl>`;

  const rows = data.exposure.filter((row) => row.long_qty || row.short_qty);
  el("exposureBody").innerHTML = rows.length
    ? rows.map((row) => `<tr>
        <td class="sym">${row.symbol}</td>
        <td class="num up">${fmtQty(row.long_qty)}<br><span class="dim" style="font-size:10px">${row.teams_long} teams</span></td>
        <td class="num down">${fmtQty(row.short_qty)}<br><span class="dim" style="font-size:10px">${row.teams_short} teams</span></td>
        <td class="num">${inrShort(row.turnover)}</td>
      </tr>`).join("")
    : '<tr><td colspan="4"><div class="empty">Nobody is holding anything yet.</div></td></tr>';
}

function renderBlotter() {
  el("blotterBody").innerHTML = state.blotter.length
    ? state.blotter.map((fill) => `<tr>
        <td class="dim mono">${shortTime(fill.ts)}</td>
        <td>${escapeHtml(fill.team)}</td>
        <td class="sym">${fill.symbol}</td>
        <td><span class="pill ${fill.side === "BUY" ? "up" : "down"}">${fill.side}</span></td>
        <td class="num">${fmtQty(fill.qty)}</td>
        <td class="num">${inr(fill.price)}</td>
      </tr>`).join("")
    : '<tr><td colspan="6"><div class="empty">No trades yet.</div></td></tr>';
}

/* ------------------------------------------------------------------ stream */

function wireStream() {
  stream.on("status", ({ connected }) => {
    state.connected = connected;
    el("offline").hidden = connected;
  });
  stream.on("unauthorised", async () => {
    if (await api.refresh()) stream.connect(api.token);
    else window.location.href = "/console/login";
  });
  stream.on("market_state", (data) => { state.market = { ...state.market, ...data }; renderMarket(); });
  stream.on("quotes", (data) => {
    for (const quote of data.quotes) {
      const existing = state.instruments.get(quote.symbol);
      if (existing) state.instruments.set(quote.symbol, { ...existing, ...quote });
    }
    if (data.index) el("indexValue").textContent = inr(data.index.value);
    renderPriceDesk();
  });
  stream.on("blotter", () => loadBlotter());
  stream.on("risk_event", (event) => {
    toast(`${event.kind.replace(/_/g, " ")}: team ${event.team_id}`, event.kind === "busted" ? "down" : "warn");
    loadTeams();
  });
  stream.on("leaderboard", (data) => {
    if (data.rows) {
      state.teams = data.rows.map((row) => {
        const existing = state.teams.find((t) => t.team_id === row.team_id) || {};
        return { ...existing, ...row, name: row.team };
      });
      renderTeams();
    }
  });
}

/* -------------------------------------------------------------- chrome, UI */

function wireChrome() {
  el("themeToggle").addEventListener("click", () => {
    const next = document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem("exchange.theme", next); } catch { /* ignore */ }
  });
  el("logout").addEventListener("click", async () => {
    stream.close();
    try { await api.post("/api/auth/ops/logout"); } catch { /* ignore */ }
    api.clearToken();
    window.location.href = "/console/login";
  });
  el("refreshBtn").addEventListener("click", refreshAll);
}

function tickClock() {
  const market = state.market;
  el("countdown").textContent = market?.ends_at
    ? duration((new Date(market.ends_at).getTime() - Date.now()) / 1000)
    : "--:--";
}

/* --------------------------------------------------------------- dialogs */

function showModal(title, contentHtml, footerHtml = "") {
  closeModal();
  const backdrop = document.createElement("div");
  backdrop.className = "modal-backdrop";
  backdrop.id = "modal";
  backdrop.innerHTML = `
    <div class="modal" role="dialog" aria-modal="true">
      <header><h2>${escapeHtml(title)}</h2><div style="flex:1"></div>
        <button class="iconbtn" data-close>Close</button></header>
      <div class="content">${contentHtml}</div>
      ${footerHtml ? `<footer>${footerHtml}</footer>` : ""}
    </div>`;
  document.body.appendChild(backdrop);
  backdrop.addEventListener("click", (event) => {
    if (event.target === backdrop || event.target.hasAttribute("data-close")) closeModal();
  });
  document.addEventListener("keydown", escClose);
  return backdrop;
}

function escClose(event) {
  if (event.key === "Escape") closeModal();
}

function closeModal() {
  document.getElementById("modal")?.remove();
  document.removeEventListener("keydown", escClose);
}

/**
 * Confirmation that cannot be dismissed by reflex.
 *
 * The operator has to type the exact word. For a freeze or a large jump, the
 * cost of a mis-click is paid by every team simultaneously, and a plain OK
 * button is not enough friction to be worth anything.
 */
function confirmByTyping(title, explanation, word) {
  return new Promise((resolve) => {
    const backdrop = showModal(
      title,
      `<p>${escapeHtml(explanation)}</p>
       <div class="field">
         <label for="confirmWord">Type <b>${escapeHtml(word)}</b> to continue</label>
         <input type="text" id="confirmWord" class="confirm-input" autocomplete="off" spellcheck="false">
       </div>`,
      `<button class="btn ghost" data-close>Cancel</button>
       <button class="btn danger" id="confirmGo" disabled>${escapeHtml(title)}</button>`,
    );
    const input = backdrop.querySelector("#confirmWord");
    const go = backdrop.querySelector("#confirmGo");
    input.focus();
    input.addEventListener("input", () => {
      go.disabled = input.value.trim().toUpperCase() !== word.toUpperCase();
    });
    go.addEventListener("click", () => { closeModal(); resolve(true); });
    backdrop.addEventListener("click", (event) => {
      if (event.target === backdrop || event.target.hasAttribute("data-close")) resolve(false);
    });
  });
}

let toastCount = 0;
function toast(message, tone = "") {
  const host = el("toasts");
  const node = document.createElement("div");
  node.className = `toast ${tone}`;
  node.innerHTML = `<div class="msg">${escapeHtml(message)}</div>`;
  host.appendChild(node);
  toastCount += 1;
  setTimeout(() => node.remove(), 6000);
  while (host.children.length > 4) host.firstChild.remove();
}

boot();
