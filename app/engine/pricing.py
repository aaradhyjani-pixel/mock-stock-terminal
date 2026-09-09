"""How a price moves, what it is quoted at, and what you actually get filled at.

This is the only module in the system that uses floating point, and it is
confined to the random-walk multiplier. The moment a price exists it is a
``Decimal`` snapped to the instrument's tick grid, and every downstream number
is exact.

The price process has three inputs:

* **drift** - the operator's intent. A ``MOVE`` sets a target and a duration and
  the engine walks there geometrically, so the tape looks like a market trending
  rather than a number jumping. Nobody can front-run a step change that never
  happens.
* **noise** - a random walk scaled to the instrument's daily volatility, so a
  quiet stock stays quiet and a volatile one twitches.
* **impact** - net participant order flow nudges the price. Without this a team
  can buy forty lakh of a mid-cap without moving it, which teaches the wrong
  lesson.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from ..config import MarketRules, SessionRules, get_rules
from ..models import Instrument, InstrumentStatus, OrderSide, PriceActionKind
from ..money import ZERO, D, money, round_to_tick

# A single fill is never allowed to walk further than this from the touch, no
# matter how large the order. Beyond it the order is rejected as too large for
# the book, which is kinder than filling it at an absurd price.
MAX_SLIPPAGE_FRACTION = Decimal("0.10")


@dataclass(frozen=True)
class Quote:
    symbol: str
    last: Decimal
    bid: Decimal
    ask: Decimal
    status: InstrumentStatus

    def touch(self, side: OrderSide) -> Decimal:
        """The price a market order of that side starts from."""
        return self.ask if side is OrderSide.BUY else self.bid


@dataclass(frozen=True)
class FillQuote:
    """The result of walking an order through the available liquidity."""

    price: Decimal  # volume-weighted average
    slices: int
    slippage_pct: Decimal


def quote_for(instrument: Instrument) -> Quote:
    """Bid and ask around the last price, from the instrument's spread."""
    half = D(instrument.spread_bps) / Decimal("20000")  # bps -> fraction, halved
    last = instrument.last_price
    tick = instrument.tick_size
    bid = round_to_tick(last * (Decimal("1") - half), tick)
    ask = round_to_tick(last * (Decimal("1") + half), tick)
    # A configured spread narrower than one tick rounds away to nothing, and a
    # zero spread is a legitimate setting for a friction-free practice session.
    # What is never allowed is a crossed book, where the ask is below the bid.
    if ask < bid:
        ask = bid
    return Quote(instrument.symbol, last, bid, ask, instrument.status)


def ticks_per_day(session: SessionRules | None = None) -> Decimal:
    session = session or get_rules().session
    return Decimal(session.open_seconds) / session.tick_seconds


def tick_sigma(instrument: Instrument, session: SessionRules | None = None) -> float:
    """Per-tick standard deviation of log return."""
    daily = float(instrument.daily_vol_pct) / 100.0
    n = float(ticks_per_day(session))
    if n <= 0:
        return 0.0
    return daily / math.sqrt(n) * float(instrument.vol_multiplier)


def compute_drift(
    instrument: Instrument,
    target: Decimal | None,
    seconds_remaining: float,
    tick_seconds: float,
) -> float:
    """Log-drift per tick needed to arrive at ``target`` when the move ends."""
    if target is None or target <= 0 or instrument.last_price <= 0:
        return 0.0
    ticks_left = max(1.0, seconds_remaining / max(tick_seconds, 0.001))
    return math.log(float(target) / float(instrument.last_price)) / ticks_left


def compute_impact(
    net_notional: Decimal,
    instrument: Instrument,
    market: MarketRules | None = None,
    session: SessionRules | None = None,
) -> float:
    """Log-impact from recent net buying or selling pressure.

    ``net_notional`` is the sum of signed trade values still inside the impact
    window, so the same trade is counted by every tick in that window. The
    result is therefore divided by the number of ticks in the window: a trade
    contributes its full impact exactly once, spread smoothly over the window
    rather than applied five times over.

    Getting this wrong is not subtle. Without the division, a routine 1.6 lakh
    buy in a large cap moved the price a full per cent, which would have taught
    participants something false about how markets absorb size.
    """
    market = market or get_rules().market
    session = session or get_rules().session
    if not market.impact_enabled or instrument.liquidity_notional <= 0:
        return 0.0

    ticks_in_window = max(1.0, market.impact_window_seconds / max(float(session.tick_seconds), 0.001))
    ratio = float(net_notional) / float(instrument.liquidity_notional)
    # Clamp so a single enormous print cannot dislocate the tape in one tick.
    # The price band would catch it anyway; this keeps the shape sane.
    ratio = max(-3.0, min(3.0, ratio))
    return float(market.impact_coefficient) * ratio / ticks_in_window


def next_price(
    instrument: Instrument,
    rng: random.Random,
    target: Decimal | None = None,
    seconds_remaining: float = 0.0,
    net_notional: Decimal = ZERO,
    session: SessionRules | None = None,
    market: MarketRules | None = None,
    noise: bool = True,
) -> tuple[Decimal, InstrumentStatus]:
    """One tick. Returns the new price and the resulting circuit status."""
    session = session or get_rules().session
    market = market or get_rules().market
    tick_seconds = float(session.tick_seconds)

    drift = compute_drift(instrument, target, seconds_remaining, tick_seconds)
    shock = rng.gauss(0.0, tick_sigma(instrument, session)) if noise else 0.0
    impact = compute_impact(net_notional, instrument, market, session)

    multiplier = math.exp(drift + shock + impact)
    raw = D(instrument.last_price) * D(multiplier)
    priced = round_to_tick(raw, instrument.tick_size)

    return apply_band(instrument, priced)


def apply_band(instrument: Instrument, price: Decimal) -> tuple[Decimal, InstrumentStatus]:
    """Clamp to the day's price band and derive the circuit status.

    A stock that touches its band is not broken; it is on circuit, exactly as on
    the real exchange. One side of the book keeps trading and the other rests.
    """
    low = round_to_tick(instrument.band_low, instrument.tick_size)
    high = round_to_tick(instrument.band_high, instrument.tick_size)
    floor = instrument.tick_size

    if instrument.status in (InstrumentStatus.HALTED, InstrumentStatus.SUSPENDED):
        return money(instrument.last_price), instrument.status

    if price >= high:
        return max(high, floor), InstrumentStatus.UPPER_CIRCUIT
    if price <= low:
        return max(low, floor), InstrumentStatus.LOWER_CIRCUIT
    return max(price, floor), InstrumentStatus.ACTIVE


def side_allowed(status: InstrumentStatus, side: OrderSide) -> bool:
    """Which side of the book may still execute given a circuit state."""
    if status is InstrumentStatus.UPPER_CIRCUIT:
        return side is OrderSide.SELL
    if status is InstrumentStatus.LOWER_CIRCUIT:
        return side is OrderSide.BUY
    return status.tradable


def fill_quote(
    instrument: Instrument,
    side: OrderSide,
    qty: int,
    quote: Quote | None = None,
) -> FillQuote:
    """Walk ``qty`` through the book and return the volume-weighted price.

    Liquidity is consumed in slices. The first slice trades at the touch; each
    slice after it is a further half-spread worse. A large order therefore pays
    for the space it takes, and the participant sees this in the preview before
    they confirm.
    """
    market = get_rules().market
    quote = quote or quote_for(instrument)
    base = quote.touch(side)
    tick = instrument.tick_size

    if qty <= 0 or base <= 0:
        return FillQuote(price=base, slices=0, slippage_pct=ZERO)

    if not market.slippage_enabled or instrument.liquidity_notional <= 0:
        return FillQuote(price=base, slices=1, slippage_pct=ZERO)

    slice_qty = max(1, int(instrument.liquidity_notional / base))
    half_spread = D(instrument.spread_bps) / Decimal("20000")

    remaining = qty
    total = ZERO
    index = 0
    while remaining > 0:
        take = min(remaining, slice_qty)
        step = half_spread * index
        if step > MAX_SLIPPAGE_FRACTION:
            step = MAX_SLIPPAGE_FRACTION
        factor = (Decimal("1") + step) if side is OrderSide.BUY else (Decimal("1") - step)
        price = max(base * factor, tick)
        total += price * take
        remaining -= take
        index += 1

    vwap = round_to_tick(total / qty, tick)
    slippage = ((vwap - base) / base * Decimal("100")) if base > 0 else ZERO
    if side is OrderSide.SELL:
        slippage = -slippage
    return FillQuote(price=vwap, slices=index, slippage_pct=money(slippage))


def synth_depth(instrument: Instrument, levels: int = 5) -> dict:
    """A plausible depth ladder around the quote, for display only.

    The engine is a dealer, not an order book, so there is no real depth. The
    ladder is generated from the instrument's own liquidity so that a liquid
    stock visibly shows more size than an illiquid one, and the numbers move
    with the price rather than sitting frozen.
    """
    quote = quote_for(instrument)
    tick = instrument.tick_size
    base_size = max(1, int(instrument.liquidity_notional / max(quote.last, tick) / 4))

    bids, asks = [], []
    for level in range(levels):
        decay = Decimal(10 - level) / Decimal("10")
        size = max(1, int(base_size * float(decay)))
        bids.append({"price": str(money(quote.bid - tick * level)), "qty": size})
        asks.append({"price": str(money(quote.ask + tick * level)), "qty": size})
    return {"bids": bids, "asks": asks}


def target_from_params(instrument: Instrument, params: dict) -> Decimal | None:
    """Resolve a MOVE or JUMP payload into an absolute target price.

    Operators think in percentages ("INFY down 7") and occasionally in absolute
    prices. Both are accepted; the percentage is applied to the price at the
    moment the action was created, which is stored on the action, so a long move
    lands where the operator aimed rather than compounding.
    """
    if "target_price" in params and params["target_price"] is not None:
        return round_to_tick(D(params["target_price"]), instrument.tick_size)
    if "pct" in params and params["pct"] is not None:
        anchor = D(params.get("anchor_price") or instrument.last_price)
        return round_to_tick(anchor * (Decimal("1") + D(params["pct"]) / Decimal("100")), instrument.tick_size)
    return None


def move_seconds_remaining(ends_at: datetime | None, now: datetime) -> float:
    if ends_at is None:
        return 0.0
    return max(0.0, (ends_at - now).total_seconds())


PRICE_ACTION_KINDS_WITH_TARGET = {PriceActionKind.MOVE, PriceActionKind.JUMP}
