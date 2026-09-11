/* Formatting helpers.
 *
 * The server sends every rupee amount as a string with two decimals. Nothing in
 * this file parses money into a JavaScript number for arithmetic: the grouping
 * is done on the digit string itself. A binary float cannot represent 0.1, and a
 * team's cash balance is not the place to find that out.
 */

/** Group digits the Indian way: 12,34,567.89 */
export function inr(value, { decimals = 2, sign = false } = {}) {
  if (value === null || value === undefined || value === "") return "-";
  let text = String(value).trim();
  let negative = text.startsWith("-");
  if (negative) text = text.slice(1);

  let [whole, frac = ""] = text.split(".");
  frac = decimals > 0 ? (frac + "000000").slice(0, decimals) : "";

  if (whole.length > 3) {
    const tail = whole.slice(-3);
    let head = whole.slice(0, -3);
    const groups = [];
    while (head.length > 2) {
      groups.unshift(head.slice(-2));
      head = head.slice(0, -2);
    }
    if (head) groups.unshift(head);
    whole = groups.concat(tail).join(",");
  }

  let out = decimals > 0 ? `${whole}.${frac}` : whole;
  if (negative) return `-${out}`;
  return sign && Number(value) > 0 ? `+${out}` : out;
}

/** Short form for wide numbers: 12.3L, 4.56Cr. Display only. */
export function inrShort(value) {
  const n = Number(value);
  if (!isFinite(n)) return "-";
  const abs = Math.abs(n);
  const sign = n < 0 ? "-" : "";
  if (abs >= 1e7) return `${sign}${(abs / 1e7).toFixed(2)}Cr`;
  if (abs >= 1e5) return `${sign}${(abs / 1e5).toFixed(2)}L`;
  if (abs >= 1e3) return `${sign}${(abs / 1e3).toFixed(1)}k`;
  return `${sign}${abs.toFixed(0)}`;
}

export function pct(value, { sign = true } = {}) {
  if (value === null || value === undefined || value === "") return "-";
  const n = Number(value);
  if (!isFinite(n)) return "-";
  const prefix = sign && n > 0 ? "+" : "";
  return `${prefix}${n.toFixed(2)}%`;
}

export function signClass(value) {
  const n = Number(value);
  if (!isFinite(n) || n === 0) return "";
  return n > 0 ? "up" : "down";
}

export function qty(value) {
  return new Intl.NumberFormat("en-IN").format(Number(value) || 0);
}

/** mm:ss, or h:mm:ss past an hour. */
export function duration(seconds) {
  const total = Math.max(0, Math.floor(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

export function clockTime(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  return d.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
}

export function shortTime(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  return d.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: false });
}

/** Compact relative clock for the Events desk (e.g. "12s ago", "3m ago"). */
export function relativeTime(iso) {
  if (!iso) return "-";
  const then = new Date(iso).getTime();
  if (!Number.isFinite(then)) return "-";
  const sec = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (sec < 5) return "just now";
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  return shortTime(iso);
}

export function escapeHtml(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/** Read a CSS custom property, so canvas drawing follows the theme. */
export function cssVar(name, fallback = "#888") {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name);
  return value ? value.trim() : fallback;
}

export const STATE_TEXT = {
  PRE_OPEN: "Pre-open",
  OPEN: "Open",
  HALTED: "Halted",
  CLOSED: "Closed",
  FROZEN: "Frozen",
  FINAL: "Ended",
};
