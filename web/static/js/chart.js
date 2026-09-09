/* Candlestick chart on a canvas.
 *
 * Written by hand rather than pulled from a charting library, for one reason:
 * the terminal must work when the venue's internet does not. A CDN script tag
 * is a single point of failure between 500 participants and a working screen,
 * and vendoring a charting bundle to avoid that costs more than the 250 lines
 * it would replace.
 *
 * Draws candles, a volume strip, a last-price line, news markers on the time
 * axis, and a crosshair with a readout. Colours come from CSS custom
 * properties, so it follows the theme without being told.
 */

import { cssVar, inr, shortTime } from "./format.js";

const PAD = { top: 10, right: 62, bottom: 22, left: 8 };
const VOLUME_FRACTION = 0.18;

export class CandleChart {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.candles = [];
    this.markers = [];
    this.hover = null;
    this.symbol = "";
    this.tooltip = null;

    this._onMove = this._onMove.bind(this);
    this._onLeave = this._onLeave.bind(this);
    canvas.addEventListener("mousemove", this._onMove);
    canvas.addEventListener("mouseleave", this._onLeave);
    canvas.addEventListener("touchstart", this._onMove, { passive: true });
    canvas.addEventListener("touchmove", this._onMove, { passive: true });
    canvas.addEventListener("touchend", this._onLeave);

    this.resizeObserver = new ResizeObserver(() => this.draw());
    this.resizeObserver.observe(canvas.parentElement || canvas);
  }

  destroy() {
    this.resizeObserver.disconnect();
    this.canvas.removeEventListener("mousemove", this._onMove);
    this.canvas.removeEventListener("mouseleave", this._onLeave);
    this.tooltip?.remove();
  }

  setData(symbol, candles, markers = []) {
    this.symbol = symbol;
    this.candles = candles || [];
    this.markers = markers || [];
    this.draw();
  }

  /** Update the newest candle in place, for live ticks between fetches. */
  pushPrice(price) {
    const value = Number(price);
    if (!isFinite(value) || !this.candles.length) return;
    const last = this.candles[this.candles.length - 1];
    last.c = value;
    if (value > last.h) last.h = value;
    if (value < last.l) last.l = value;
    this.draw();
  }

  _geometry() {
    const rect = this.canvas.getBoundingClientRect();
    const width = Math.max(120, rect.width);
    const height = Math.max(120, rect.height);
    const plotW = width - PAD.left - PAD.right;
    const volumeH = Math.round((height - PAD.top - PAD.bottom) * VOLUME_FRACTION);
    const plotH = height - PAD.top - PAD.bottom - volumeH - 6;
    return { width, height, plotW, plotH, volumeH };
  }

  _scales() {
    const { plotW, plotH } = this._geometry();
    const data = this.candles;
    if (!data.length) return null;

    let high = -Infinity;
    let low = Infinity;
    let maxVolume = 0;
    for (const candle of data) {
      if (candle.h > high) high = candle.h;
      if (candle.l < low) low = candle.l;
      if (candle.v > maxVolume) maxVolume = candle.v;
    }
    if (!isFinite(high) || !isFinite(low)) return null;
    if (high === low) {
      high += high * 0.005 || 1;
      low -= low * 0.005 || 1;
    }
    const padding = (high - low) * 0.08;
    high += padding;
    low -= padding;

    const slot = plotW / data.length;
    return {
      high,
      low,
      maxVolume,
      slot,
      x: (i) => PAD.left + slot * (i + 0.5),
      y: (price) => PAD.top + plotH - ((price - low) / (high - low)) * plotH,
      plotH,
    };
  }

  draw() {
    const { width, height, plotH, volumeH } = this._geometry();
    const dpr = window.devicePixelRatio || 1;
    const canvas = this.canvas;
    if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) {
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
    }
    const ctx = this.ctx;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const colors = {
      grid: cssVar("--line-soft", "#1b2523"),
      axis: cssVar("--dim", "#64756f"),
      text: cssVar("--text", "#e8efec"),
      up: cssVar("--up", "#34d399"),
      down: cssVar("--down", "#f2555a"),
      accent: cssVar("--accent", "#2dd4a7"),
      panel: cssVar("--panel", "#111817"),
      warn: cssVar("--warn", "#f5a524"),
    };

    if (!this.candles.length) {
      ctx.fillStyle = colors.axis;
      ctx.font = `12px ${getComputedStyle(document.body).fontFamily}`;
      ctx.textAlign = "center";
      ctx.fillText("Waiting for the market to open", width / 2, height / 2);
      return;
    }

    const scale = this._scales();
    if (!scale) return;
    const mono = `10px ui-monospace, SFMono-Regular, Menlo, monospace`;

    // Horizontal grid and the price axis on the right.
    ctx.font = mono;
    ctx.textBaseline = "middle";
    const gridLines = 5;
    for (let i = 0; i <= gridLines; i += 1) {
      const price = scale.low + ((scale.high - scale.low) * i) / gridLines;
      const y = Math.round(scale.y(price)) + 0.5;
      ctx.strokeStyle = colors.grid;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(PAD.left, y);
      ctx.lineTo(width - PAD.right, y);
      ctx.stroke();

      ctx.fillStyle = colors.axis;
      ctx.textAlign = "left";
      ctx.fillText(inr(price.toFixed(2)), width - PAD.right + 6, y);
    }

    // Candles.
    const bodyW = Math.max(1, Math.min(11, scale.slot * 0.66));
    for (let i = 0; i < this.candles.length; i += 1) {
      const candle = this.candles[i];
      const x = scale.x(i);
      const rising = candle.c >= candle.o;
      const color = rising ? colors.up : colors.down;

      ctx.strokeStyle = color;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(Math.round(x) + 0.5, scale.y(candle.h));
      ctx.lineTo(Math.round(x) + 0.5, scale.y(candle.l));
      ctx.stroke();

      const yOpen = scale.y(candle.o);
      const yClose = scale.y(candle.c);
      const top = Math.min(yOpen, yClose);
      const bodyH = Math.max(1, Math.abs(yClose - yOpen));
      ctx.fillStyle = color;
      ctx.fillRect(x - bodyW / 2, top, bodyW, bodyH);
    }

    // Volume strip beneath the price plot.
    if (scale.maxVolume > 0) {
      const volumeTop = PAD.top + plotH + 6;
      for (let i = 0; i < this.candles.length; i += 1) {
        const candle = this.candles[i];
        const h = (candle.v / scale.maxVolume) * volumeH;
        const rising = candle.c >= candle.o;
        ctx.fillStyle = rising ? colors.up : colors.down;
        ctx.globalAlpha = 0.32;
        ctx.fillRect(scale.x(i) - bodyW / 2, volumeTop + volumeH - h, bodyW, h);
        ctx.globalAlpha = 1;
      }
    }

    // Last price: dashed line plus a tag on the axis.
    const last = this.candles[this.candles.length - 1];
    const lastY = scale.y(last.c);
    const rising = last.c >= last.o;
    ctx.strokeStyle = rising ? colors.up : colors.down;
    ctx.setLineDash([3, 3]);
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(PAD.left, Math.round(lastY) + 0.5);
    ctx.lineTo(width - PAD.right, Math.round(lastY) + 0.5);
    ctx.stroke();
    ctx.setLineDash([]);

    ctx.fillStyle = rising ? colors.up : colors.down;
    ctx.fillRect(width - PAD.right + 1, lastY - 8, PAD.right - 2, 16);
    ctx.fillStyle = colors.panel;
    ctx.textAlign = "left";
    ctx.font = `600 ${mono}`;
    ctx.fillText(inr(last.c.toFixed(2)), width - PAD.right + 5, lastY);

    // Time axis: a handful of labels, never a crowded row.
    ctx.font = mono;
    ctx.fillStyle = colors.axis;
    ctx.textAlign = "center";
    const stride = Math.max(1, Math.floor(this.candles.length / 6));
    for (let i = 0; i < this.candles.length; i += stride) {
      ctx.fillText(shortTime(this.candles[i].ts), scale.x(i), height - PAD.bottom / 2);
    }

    // News markers sit on the time axis at the candle they landed in.
    for (const marker of this.markers) {
      const index = this._indexForTime(marker.ts);
      if (index < 0) continue;
      const x = scale.x(index);
      const y = PAD.top + plotH + 2;
      ctx.fillStyle = colors.warn;
      ctx.beginPath();
      ctx.moveTo(x, y - 6);
      ctx.lineTo(x - 4, y);
      ctx.lineTo(x + 4, y);
      ctx.closePath();
      ctx.fill();
    }

    if (this.hover !== null) this._drawCrosshair(scale, colors, width, height);
  }

  _drawCrosshair(scale, colors, width, height) {
    const index = this.hover;
    const candle = this.candles[index];
    if (!candle) return;
    const ctx = this.ctx;
    const x = Math.round(scale.x(index)) + 0.5;

    ctx.strokeStyle = colors.axis;
    ctx.globalAlpha = 0.55;
    ctx.setLineDash([2, 3]);
    ctx.beginPath();
    ctx.moveTo(x, PAD.top);
    ctx.lineTo(x, height - PAD.bottom);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.globalAlpha = 1;

    if (!this.tooltip) {
      this.tooltip = document.createElement("div");
      this.tooltip.className = "chart-tip";
      (this.canvas.parentElement || document.body).appendChild(this.tooltip);
    }
    const change = candle.c - candle.o;
    const changePct = candle.o ? (change / candle.o) * 100 : 0;
    this.tooltip.textContent =
      `${shortTime(candle.ts)}\n` +
      `O ${inr(candle.o.toFixed(2))}  H ${inr(candle.h.toFixed(2))}\n` +
      `L ${inr(candle.l.toFixed(2))}  C ${inr(candle.c.toFixed(2))}\n` +
      `${change >= 0 ? "+" : ""}${changePct.toFixed(2)}%`;
    const left = Math.min(width - 130, Math.max(4, x + 10));
    this.tooltip.style.left = `${left}px`;
    this.tooltip.style.top = `${PAD.top + 4}px`;
    this.tooltip.style.display = "block";
  }

  _indexForTime(iso) {
    const target = new Date(iso).getTime();
    for (let i = 0; i < this.candles.length; i += 1) {
      if (new Date(this.candles[i].ts).getTime() >= target) return i;
    }
    return -1;
  }

  _onMove(event) {
    if (!this.candles.length) return;
    const rect = this.canvas.getBoundingClientRect();
    const point = event.touches ? event.touches[0] : event;
    if (!point) return;
    const x = point.clientX - rect.left;
    const { plotW } = this._geometry();
    const slot = plotW / this.candles.length;
    const index = Math.floor((x - PAD.left) / slot);
    this.hover = Math.max(0, Math.min(this.candles.length - 1, index));
    this.draw();
  }

  _onLeave() {
    this.hover = null;
    if (this.tooltip) this.tooltip.style.display = "none";
    this.draw();
  }
}

/** Convert the API's string candles into the numeric form the canvas wants. */
export function toCandles(rows) {
  return (rows || []).map((row) => ({
    ts: row.ts,
    o: Number(row.o),
    h: Number(row.h),
    l: Number(row.l),
    c: Number(row.c),
    v: Number(row.v) || 0,
  }));
}
