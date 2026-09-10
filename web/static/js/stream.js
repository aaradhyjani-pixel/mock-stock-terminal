/* WebSocket connection with reconnect.
 *
 * The venue network will drop connections. What matters is that the terminal
 * says so plainly, keeps trying, and repaints itself completely on the way back
 * rather than resuming from a stale picture. Every reconnect receives a fresh
 * snapshot as its first message, so there is no partial state to reconcile.
 *
 * While disconnected the terminal disables its confirm buttons: sending an
 * order priced against a quote that stopped updating is worse than being told
 * to wait a moment.
 */

import { apiUrl, isCrossOrigin, wsUrl } from "./config.js";

const MAX_BACKOFF_MS = 8000;

// After this many failed reconnects in a row, stop assuming WebSockets work
// here and start polling instead. Some hosts and some captive portals cap or
// forbid long-lived connections; a terminal that shows a stale price forever
// is worse than one that updates every two seconds and says so.
const FALLBACK_AFTER_ATTEMPTS = 3;
const POLL_INTERVAL_MS = 2000;

export class Stream {
  constructor(path = "/ws") {
    this.path = path;
    this.socket = null;
    this.handlers = new Map();
    this.token = "";
    this.attempts = 0;
    this.connected = false;
    this.closedByUs = false;
    this.keepAlive = null;
    this.polling = false;
    this.pollTimer = null;
    // Set by the app so the fallback knows which endpoints to poll and how to
    // shape their answers into the same events the socket would have sent.
    this.pollSources = [];
  }

  /**
   * Register the REST fallback.
   *
   * Each source is { url, event, transform } - the poller fetches the url and
   * emits `event` with `transform(body)`, so every handler downstream is the
   * one that already exists for the live socket. Nothing else in the app has
   * to know whether the data arrived over a socket or a poll.
   */
  usePolling(sources) {
    this.pollSources = sources || [];
    return this;
  }

  on(event, handler) {
    if (!this.handlers.has(event)) this.handlers.set(event, []);
    this.handlers.get(event).push(handler);
    return this;
  }

  emit(event, data) {
    for (const handler of this.handlers.get(event) || []) {
      try {
        handler(data);
      } catch (error) {
        console.error(`stream handler for "${event}" failed`, error);
      }
    }
  }

  connect(token) {
    this.token = token || this.token;
    this.closedByUs = false;
    this.open();
  }

  open() {
    if (this.socket && this.socket.readyState <= WebSocket.OPEN) return;
    const url = `${wsUrl(this.path)}?token=${encodeURIComponent(this.token)}`;

    let socket;
    try {
      socket = new WebSocket(url);
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.socket = socket;

    socket.onopen = () => {
      this.attempts = 0;
      this.connected = true;
      this.stopPolling();
      this.emit("status", { connected: true, degraded: false });
      // Some proxies drop an idle socket. A short client-side beat keeps it up
      // and gives us an early signal when it has gone away.
      clearInterval(this.keepAlive);
      this.keepAlive = setInterval(() => {
        if (socket.readyState === WebSocket.OPEN) socket.send("ping");
      }, 15000);
    };

    socket.onmessage = (event) => {
      let message;
      try {
        message = JSON.parse(event.data);
      } catch {
        return;
      }
      if (!message || !message.event) return;
      if (message.event === "ping") return;
      this.emit(message.event, message.data);
      this.emit("*", message);
    };

    socket.onclose = (event) => {
      clearInterval(this.keepAlive);
      this.connected = false;
      this.emit("status", { connected: false, code: event.code, degraded: this.polling });
      if (event.code === 4401) {
        this.emit("unauthorised", {});
        return;
      }
      if (!this.closedByUs) this.scheduleReconnect();
    };

    socket.onerror = () => {
      /* onclose always follows; reconnect is handled there. */
    };
  }

  scheduleReconnect() {
    this.attempts += 1;
    if (this.attempts >= FALLBACK_AFTER_ATTEMPTS) this.startPolling();
    // Exponential backoff with jitter: 500 phones reconnecting after an access
    // point blip must not arrive in one synchronised wave.
    const base = Math.min(MAX_BACKOFF_MS, 400 * 2 ** Math.min(this.attempts, 5));
    const delay = base / 2 + Math.random() * (base / 2);
    setTimeout(() => this.open(), delay);
  }

  // ------------------------------------------------------------- fallback

  startPolling() {
    if (this.polling || !this.pollSources.length) return;
    this.polling = true;
    this.emit("status", { connected: false, degraded: true });
    const tick = async () => {
      if (!this.polling) return;
      for (const source of this.pollSources) {
        try {
          const response = await fetch(apiUrl(source.url), {
            headers: this.token ? { Authorization: `Bearer ${this.token}` } : {},
            credentials: isCrossOrigin ? "include" : "same-origin",
          });
          if (!response.ok) continue;
          const body = await response.json();
          this.emit(source.event, source.transform ? source.transform(body) : body);
        } catch {
          /* Keep polling. The next pass may succeed. */
        }
      }
    };
    tick();
    this.pollTimer = setInterval(tick, POLL_INTERVAL_MS);
  }

  stopPolling() {
    this.polling = false;
    clearInterval(this.pollTimer);
    this.pollTimer = null;
  }

  close() {
    this.closedByUs = true;
    clearInterval(this.keepAlive);
    this.stopPolling();
    if (this.socket) this.socket.close();
  }
}
