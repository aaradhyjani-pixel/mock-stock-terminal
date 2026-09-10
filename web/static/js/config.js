/* Where the API lives.
 *
 * The front end and the backend do not have to be served from the same place.
 * On a single host they are, and this stays empty, which makes every request
 * same-origin. Split across hosts - the static pages on Vercel, the engine
 * somewhere that can run a process - this points at the engine.
 *
 * Set it before the module scripts load, in each HTML page:
 *
 *     <script>window.EXCHANGE_API_BASE = "https://engine.example.com";</script>
 *
 * No trailing slash. Leave it empty for same-origin, which is the default and
 * the simpler deployment.
 */

const configured = (window.EXCHANGE_API_BASE || "").replace(/\/+$/, "");

/** Absolute URL for an API path, or the path itself when same-origin. */
export function apiUrl(path) {
  return configured ? configured + path : path;
}

/** WebSocket URL for a path, following the API origin. */
export function wsUrl(path) {
  if (configured) {
    return configured.replace(/^http/, "ws") + path;
  }
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  return `${scheme}://${window.location.host}${path}`;
}

/** True when the API is on a different origin, which changes how cookies work. */
export const isCrossOrigin = Boolean(configured);

export const API_BASE = configured;
