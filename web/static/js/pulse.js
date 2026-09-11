/* ETERNAL Pulse — personal coaching feedback (client-only).
 *
 * Does NOT affect ranking, cash, fills, or margin. See docs/eternal-pulse.md.
 * Deterministic rules: news window timing + preview slippage + fill outcome.
 */

const STORAGE_KEY = "eternal_pulse_v1";
const DECISION_MS = 12000;
/** After the window closes, fills on mapped symbols still count as CHASED. */
const CHASE_GRACE_MS = 45000;
/** |slippage_pct| at or above this → SIZED_HARD. */
const MATERIAL_SLIP_PCT = 0.5;

const EMPTY_STATS = () => ({
  reacted: 0,
  hesitated: 0,
  chased: 0,
  sized_hard: 0,
  clean_entry: 0,
});

const MOMENT_META = {
  REACTED: { tone: "up", blurb: "You moved inside the decision window." },
  HESITATED: { tone: "warn", blurb: "Window closed without a mapped trade." },
  CHASED: { tone: "warn", blurb: "You traded after the window — late to the story." },
  SIZED_HARD: { tone: "down", blurb: "Size moved the book (material slippage)." },
  CLEAN_ENTRY: { tone: "up", blurb: "Tight entry — low slippage." },
};

function now() {
  return Date.now();
}

function safeParse(raw) {
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

function loadStore(teamId) {
  let raw = null;
  try {
    raw = localStorage.getItem(STORAGE_KEY);
  } catch {
    return { teamId: teamId || null, stats: EMPTY_STATS(), lastMoment: null, history: [] };
  }
  const parsed = raw ? safeParse(raw) : null;
  if (!parsed || typeof parsed !== "object") {
    return { teamId: teamId || null, stats: EMPTY_STATS(), lastMoment: null, history: [] };
  }
  // Light team keying: reset if a different team signs in on this browser.
  if (teamId && parsed.teamId && String(parsed.teamId) !== String(teamId)) {
    return { teamId: String(teamId), stats: EMPTY_STATS(), lastMoment: null, history: [] };
  }
  return {
    teamId: teamId ? String(teamId) : (parsed.teamId || null),
    stats: { ...EMPTY_STATS(), ...(parsed.stats || {}) },
    lastMoment: parsed.lastMoment || null,
    history: Array.isArray(parsed.history) ? parsed.history.slice(-40) : [],
  };
}

function saveStore(store) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({
      teamId: store.teamId,
      stats: store.stats,
      lastMoment: store.lastMoment,
      history: (store.history || []).slice(-40),
    }));
  } catch { /* private browsing / quota */ }
}

/**
 * Create a Pulse controller bound to optional UI hooks.
 * @param {{ teamId?: string|number|null, onUpdate?: (snapshot) => void }} opts
 */
export function createPulse(opts = {}) {
  const store = loadStore(opts.teamId ?? null);
  if (opts.teamId != null) store.teamId = String(opts.teamId);

  /** @type {{ id: string, symbols: Set<string>, openedAt: number, closesAt: number, closed: boolean, traded: boolean, kind: string } | null} */
  let windowState = null;
  /** symbol -> { windowId, openedAt, closesAt, kind } for chase detection after close */
  const recentMapped = new Map();
  let lastToastAt = 0;
  let coalesceTimer = null;
  let pendingMoment = null;

  function snapshot() {
    return {
      stats: { ...store.stats },
      lastMoment: store.lastMoment ? { ...store.lastMoment } : null,
      windowOpen: Boolean(windowState && !windowState.closed && now() < windowState.closesAt),
      activeSymbols: windowState && !windowState.closed
        ? [...windowState.symbols]
        : [],
    };
  }

  function emit() {
    if (typeof opts.onUpdate === "function") opts.onUpdate(snapshot());
  }

  function bump(key) {
    if (key in store.stats) store.stats[key] += 1;
  }

  function recordMoment(moment) {
    store.lastMoment = moment;
    store.history.push(moment);
    saveStore(store);
    pendingMoment = moment;
    clearTimeout(coalesceTimer);
    // Coalesce rapid fills into one premium card.
    coalesceTimer = setTimeout(() => {
      const m = pendingMoment;
      pendingMoment = null;
      if (m) showMomentCard(m);
      emit();
    }, 180);
    emit();
  }

  function showMomentCard(moment) {
    const host = document.getElementById("toasts");
    if (!host) return;
    const gap = now() - lastToastAt;
    if (gap < 900) {
      // Replace the newest pulse card instead of stacking spam.
      const existing = host.querySelector(".toast.pulse-moment");
      if (existing) existing.remove();
    }
    lastToastAt = now();
    const meta = MOMENT_META[moment.label] || { tone: "", blurb: "" };
    const node = document.createElement("div");
    node.className = `toast pulse-moment ${meta.tone || moment.tone || ""}`;
    const extras = (moment.tags || [])
      .filter((t) => t !== moment.label)
      .map((t) => `<span class="pulse-tag">${escape(t)}</span>`)
      .join("");
    const priceLine = moment.price != null
      ? `<div class="pulse-price">${escape(moment.side || "")} ${escape(String(moment.qty ?? ""))} ${escape(moment.symbol || "")} @ ${escape(moment.priceDisplay || String(moment.price))}</div>`
      : "";
    node.innerHTML = `
      <div class="pulse-moment-head">
        <span class="pulse-label">${escape(moment.label)}</span>
        ${extras}
      </div>
      ${priceLine}
      <div class="msg">${escape(moment.blurb || meta.blurb || "")}</div>
    `;
    host.appendChild(node);
    setTimeout(() => node.remove(), 6500);
    while (host.querySelectorAll(".toast").length > 5) {
      host.querySelector(".toast")?.remove();
    }
  }

  function escape(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function closeWindow({ hesitated } = { hesitated: true }) {
    if (!windowState || windowState.closed) return;
    if (windowState._timer) clearTimeout(windowState._timer);
    windowState.closed = true;
    const symbols = [...windowState.symbols];
    const openedAt = windowState.openedAt;
    const closesAt = windowState.closesAt;
    const kind = windowState.kind;
    const id = windowState.id;
    for (const sym of symbols) {
      recentMapped.set(sym, { windowId: id, openedAt, closesAt, kind });
    }
    if (hesitated && !windowState.traded && symbols.length) {
      bump("hesitated");
      recordMoment({
        label: "HESITATED",
        tone: "warn",
        blurb: MOMENT_META.HESITATED.blurb,
        symbols,
        at: now(),
        tags: ["HESITATED"],
      });
    } else {
      saveStore(store);
      emit();
    }
    // Soft-clear chase map later.
    setTimeout(() => {
      for (const [sym, meta] of recentMapped) {
        if (meta.windowId === id && now() > meta.closesAt + CHASE_GRACE_MS) {
          recentMapped.delete(sym);
        }
      }
    }, CHASE_GRACE_MS + 100);
    windowState = null;
    document.documentElement.classList.remove("pulse-action");
    emit();
  }

  return {
    /** Call once team id is known (after /api/auth/me). */
    setTeamId(teamId) {
      if (teamId == null) return;
      const next = String(teamId);
      if (store.teamId && store.teamId !== next) {
        store.stats = EMPTY_STATS();
        store.lastMoment = null;
        store.history = [];
      }
      store.teamId = next;
      saveStore(store);
      emit();
    },

    /**
     * News stage decision window opened for mapped symbols.
     * @param {string[]} symbols
     * @param {{ id?: string|number, kind?: string, durationMs?: number }} meta
     */
    onDecisionStart(symbols, meta = {}) {
      const list = (symbols || []).map(String).filter(Boolean);
      // Closing a prior window without a fill counts as hesitation.
      if (windowState && !windowState.closed) {
        closeWindow({ hesitated: true });
      }
      if (!list.length) {
        document.documentElement.classList.remove("pulse-action");
        emit();
        return;
      }
      const openedAt = now();
      const durationMs = meta.durationMs || DECISION_MS;
      const win = {
        id: meta.id != null ? String(meta.id) : `w-${openedAt}`,
        symbols: new Set(list),
        openedAt,
        closesAt: openedAt + durationMs,
        closed: false,
        traded: false,
        kind: meta.kind || "NEWS",
        _timer: null,
      };
      windowState = win;
      document.documentElement.classList.add("pulse-action");
      // Auto-close → HESITATED if nobody traded mapped names.
      win._timer = setTimeout(() => {
        if (windowState === win && !win.closed) closeWindow({ hesitated: true });
      }, durationMs + 30);
      emit();
    },

    /** Decision meter finished early (e.g. retracted) without forcing hesitation. */
    onDecisionEnd({ countHesitation = true } = {}) {
      if (windowState && !windowState.closed) {
        closeWindow({ hesitated: countHesitation });
      }
    },

    /**
     * Classify a fill. Call with preview slippage when available.
     * @param {{ symbol: string, side: string, qty: number|string, price: number|string, priceDisplay?: string, fees?: any, slippagePct?: number|null }} fill
     */
    onFill(fill) {
      if (!fill || !fill.symbol) return null;
      const symbol = String(fill.symbol);
      const t = now();
      const slip = fill.slippagePct != null ? Math.abs(Number(fill.slippagePct)) : 0;
      const sizedHard = Number.isFinite(slip) && slip >= MATERIAL_SLIP_PCT;
      const tags = [];

      let primary = null;
      const inOpenWindow = windowState
        && !windowState.closed
        && t <= windowState.closesAt
        && windowState.symbols.has(symbol);

      if (inOpenWindow) {
        windowState.traded = true;
        primary = "REACTED";
        tags.push("REACTED");
        bump("reacted");
      } else {
        const mapped = recentMapped.get(symbol);
        if (mapped && t <= mapped.closesAt + CHASE_GRACE_MS && t > mapped.closesAt) {
          primary = "CHASED";
          tags.push("CHASED");
          bump("chased");
        }
      }

      if (sizedHard) {
        tags.push("SIZED_HARD");
        bump("sized_hard");
        if (!primary) primary = "SIZED_HARD";
      } else if (fill.slippagePct != null && Number.isFinite(slip)) {
        tags.push("CLEAN_ENTRY");
        bump("clean_entry");
        if (!primary) primary = "CLEAN_ENTRY";
        // Prefer CLEAN_ENTRY as co-label when we already REACTED.
        if (primary === "REACTED") {
          /* keep REACTED primary; CLEAN_ENTRY stays as tag */
        }
      } else if (!primary) {
        // Unmapped fill with unknown slip — still acknowledge a clean default.
        tags.push("CLEAN_ENTRY");
        bump("clean_entry");
        primary = "CLEAN_ENTRY";
      }

      const moment = {
        label: primary,
        tone: (MOMENT_META[primary] || {}).tone || "",
        blurb: (MOMENT_META[primary] || {}).blurb || "",
        symbol,
        side: fill.side,
        qty: fill.qty,
        price: fill.price,
        priceDisplay: fill.priceDisplay,
        slippagePct: fill.slippagePct,
        tags,
        at: t,
      };
      recordMoment(moment);
      return moment;
    },

    getSnapshot: snapshot,
    MOMENT_META,
  };
}

export { STORAGE_KEY as PULSE_STORAGE_KEY, MATERIAL_SLIP_PCT, DECISION_MS as PULSE_DECISION_MS };
