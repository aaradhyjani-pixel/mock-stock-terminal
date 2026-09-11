/* HTTP client.
 *
 * Holds the short-lived access token in memory and mirrors it to sessionStorage
 * so a refresh of the page does not sign you out mid-session. When a request
 * comes back 401 it silently trades the refresh cookie for a new access token
 * and retries once; only if that fails does it send you to the login screen.
 * On a phone that has been in someone's pocket for twenty minutes, that is the
 * difference between "it logged me out" and nothing happening at all.
 */

import { apiUrl, isCrossOrigin } from "./config.js";

const STORE_KEY = "exchange.token";
const OPS_STORE_KEY = "exchange.ops.token";

function read(key) {
  try {
    return sessionStorage.getItem(key) || "";
  } catch {
    return "";
  }
}

function write(key, value) {
  try {
    if (value) sessionStorage.setItem(key, value);
    else sessionStorage.removeItem(key);
  } catch {
    /* Private browsing. The in-memory copy still works for this page view. */
  }
}

export class ApiError extends Error {
  constructor(message, status, body) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

export class Api {
  constructor({ operator = false } = {}) {
    this.operator = operator;
    this.storeKey = operator ? OPS_STORE_KEY : STORE_KEY;
    this.token = read(this.storeKey);
    this.loginPath = operator ? "/console/login" : "/login";
    this.refreshPath = operator ? "/api/auth/ops/refresh" : "/api/auth/refresh";
    this.onUnauthorised = null;
  }

  setToken(token) {
    this.token = token || "";
    write(this.storeKey, this.token);
  }

  clearToken() {
    this.setToken("");
  }

  async request(path, { method = "GET", body, retry = true, raw = false } = {}) {
    const headers = {};
    if (this.token) headers.Authorization = `Bearer ${this.token}`;
    if (body !== undefined) headers["Content-Type"] = "application/json";

    let response;
    try {
      response = await fetch(apiUrl(path), {
        method,
        headers,
        // Cross-origin needs "include" for the refresh cookie to travel at all,
        // and the server must then send SameSite=None; Secure. Same-origin
        // keeps the stricter setting.
        credentials: isCrossOrigin ? "include" : "same-origin",
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch (cause) {
      throw new ApiError("Cannot reach the exchange. Check your connection.", 0, null);
    }

    if (response.status === 401 && retry) {
      const refreshed = await this.refresh();
      if (refreshed) return this.request(path, { method, body, retry: false, raw });
      this.clearToken();
      if (this.onUnauthorised) this.onUnauthorised();
      else window.location.replace(this.loginPath);
      throw new ApiError("Your session expired. Sign in again.", 401, null);
    }

    if (raw) return response;

    let payload = null;
    const text = await response.text();
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch {
        payload = { detail: text };
      }
    }

    if (!response.ok) {
      const detail = payload?.detail;
      const message =
        typeof detail === "string"
          ? detail
          : Array.isArray(detail)
            ? detail.map((d) => d.msg || d).join("; ")
            : `Request failed (${response.status}).`;
      throw new ApiError(message, response.status, payload);
    }
    return payload;
  }

  async refresh() {
    try {
      const response = await fetch(apiUrl(this.refreshPath), {
        method: "POST",
        credentials: isCrossOrigin ? "include" : "same-origin",
      });
      if (!response.ok) return false;
      const data = await response.json();
      if (!data.access_token) return false;
      this.setToken(data.access_token);
      return true;
    } catch {
      return false;
    }
  }

  get(path) { return this.request(path); }
  post(path, body) { return this.request(path, { method: "POST", body }); }
  del(path) { return this.request(path, { method: "DELETE" }); }
}

export const api = new Api();
export const opsApi = new Api({ operator: true });
