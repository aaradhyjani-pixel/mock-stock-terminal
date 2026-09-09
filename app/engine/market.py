"""The market engine.

One process owns the clock, the prices and the resting-order book. Everything
else in the system reads what this loop decides. That is a deliberate constraint
rather than a limitation: a single authoritative tick means there is exactly one
answer to "what is the price right now", which is what makes fills defensible
when a team asks why they got what they got.

The loop, once per second while the market is open:

1. fire any scenario steps that have come due
2. move every price (operator intent, noise, order-flow impact), clamp to bands
3. persist ticks and roll candles
4. sweep resting limit and stop orders through the same fill path as live orders
5. run the risk desk over every team with a short
6. publish quotes, then periodically snapshots and the leaderboard
7. advance the session clock

A tick that throws is logged and skipped; the next tick continues from the last
persisted state. On restart the engine reloads the last tick, the active price
actions and every working order, so a crash costs seconds, not the competition.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_rules, get_settings
from ..db import locked_team, session_scope
from ..models import (
    Candle,
    EquitySnapshot,
    Instrument,
    InstrumentStatus,
    MarketState,
    MarketStateRow,
    NewsItem,
    Order,
    OrderStatus,
    PriceAction,
    PriceActionKind,
    Scenario,
    ScenarioStep,
    Team,
    TeamStatus,
    Tick,
    utcnow,
)
from ..money import ZERO, D, money
from ..ws import hub
from . import matching, pricing, riskdesk
from .valuations import rank, valuate_all

log = logging.getLogger("exchange.engine")

UTC = timezone.utc


def _floor_minute(ts: datetime, minutes: int) -> datetime:
    discard = timedelta(
        minutes=ts.minute % minutes, seconds=ts.second, microseconds=ts.microsecond
    )
    return ts - discard


class MarketEngine:
    """Owns the tick loop and every transition of market state."""

    def __init__(self, seed: int | None = None) -> None:
        self.rng = random.Random(seed)
        self.running = False
        self._task: asyncio.Task | None = None
        # Recent signed traded notional per symbol, for the impact term.
        self._flow: dict[str, deque[tuple[float, Decimal]]] = defaultdict(deque)
        self._warned_teams: set[int] = set()
        self._last_snapshot = 0.0
        self._last_leaderboard = 0.0
        self._tick_durations: deque[float] = deque(maxlen=120)
        self._last_tick_at: datetime | None = None
        self.errors = 0

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if self.running:
            return
        self.running = True
        await self.recover()
        self._task = asyncio.create_task(self._run(), name="market-engine")
        log.info("market engine started")

    async def stop(self) -> None:
        self.running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        log.info("market engine stopped")

    async def recover(self) -> None:
        """Reload state after a restart.

        Prices, active moves and working orders all live in the database, so
        there is nothing to rebuild by hand. What this does is make the restart
        safe: if the process died while the market was open, the market comes
        back FROZEN with its remaining time preserved, so an operator decides
        when to resume rather than the clock silently having run on without
        anyone trading.
        """
        async with session_scope() as session:
            state = await self.get_state(session)
            if state.state is MarketState.OPEN:
                remaining = self._remaining_seconds(state, utcnow())
                state.state = MarketState.FROZEN
                state.frozen_remaining_seconds = int(remaining)
                state.banner = "Market frozen after a restart. The organisers will resume shortly."
                state.banner_severity = "warning"
                log.warning("recovered mid-session; market frozen with %ss remaining", int(remaining))

    async def _run(self) -> None:
        rules = get_rules().session
        interval = float(rules.tick_seconds)
        while self.running:
            started = time.perf_counter()
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.errors += 1
                log.exception("tick failed; continuing from last persisted state")
            elapsed = time.perf_counter() - started
            self._tick_durations.append(elapsed)
            await asyncio.sleep(max(0.0, interval - elapsed))

    # ----------------------------------------------------------------- state

    async def get_state(self, session: AsyncSession) -> MarketStateRow:
        state = (await session.execute(select(MarketStateRow).where(MarketStateRow.id == 1))).scalar_one_or_none()
        if state is None:
            rules = get_rules()
            state = MarketStateRow(
                id=1,
                state=MarketState.CLOSED,
                day_no=0,
                total_days=rules.session.trading_days,
                index_value=rules.market.index_base,
                index_prev_close=rules.market.index_base,
            )
            session.add(state)
            await session.flush()
        return state

    @staticmethod
    def _remaining_seconds(state: MarketStateRow, now: datetime) -> float:
        if state.session_ends_at is None:
            return 0.0
        ends = state.session_ends_at
        if ends.tzinfo is None:
            ends = ends.replace(tzinfo=UTC)
        return max(0.0, (ends - now).total_seconds())

    # ------------------------------------------------------------------ tick

    async def tick(self, now: datetime | None = None) -> None:
        now = now or utcnow()
        async with session_scope() as session:
            state = await self.get_state(session)

            if state.state in (MarketState.FROZEN, MarketState.FINAL):
                return

            if state.state is MarketState.CLOSED:
                await self._maybe_start_next_day(session, state, now)
                return

            await self._fire_scenarios(session, state, now)

            if state.state is MarketState.PRE_OPEN:
                # Only instant actions apply before the open: an operator can
                # gap a stock, but the tape does not move on its own yet.
                await self._apply_price_actions(session, state, now, ticking=False)
                await self._maybe_open(session, state, now)
                await self._publish_quotes(session, state)
                return

            if state.state is MarketState.HALTED:
                await self._publish_quotes(session, state)
                await self._advance_clock(session, state, now)
                return

            # OPEN
            instruments = await self._apply_price_actions(session, state, now, ticking=True)
            await self._persist_ticks(session, state, instruments, now)
            await self._update_index(session, state, instruments)
            self._last_tick_at = now

        await self._sweep_resting_orders(now)
        await self._run_risk_desk(now)

        async with session_scope() as session:
            state = await self.get_state(session)
            await self._publish_quotes(session, state)
            await self._maybe_snapshot(session, state, now)
            await self._advance_clock(session, state, now)

    # ---------------------------------------------------------------- prices

    async def _apply_price_actions(
        self, session: AsyncSession, state: MarketStateRow, now: datetime, *, ticking: bool
    ) -> list[Instrument]:
        instruments = list(
            (await session.execute(select(Instrument).where(Instrument.listed.is_(True)))).scalars()
        )
        by_symbol = {i.symbol: i for i in instruments}

        actions = list(
            (
                await session.execute(
                    select(PriceAction).where(
                        PriceAction.undone_at.is_(None),
                        PriceAction.cancelled_at.is_(None),
                    )
                )
            ).scalars()
        )

        targets: dict[str, tuple[Decimal, float]] = {}
        for action in actions:
            ends_at = action.ends_at
            if ends_at is not None and ends_at.tzinfo is None:
                ends_at = ends_at.replace(tzinfo=UTC)

            affected = self._action_symbols(action, instruments)

            if action.kind is PriceActionKind.JUMP:
                for instrument in affected:
                    target = pricing.target_from_params(instrument, action.params)
                    if target is None:
                        continue
                    instrument.last_price, instrument.status = pricing.apply_band(instrument, target)
                action.cancelled_at = now  # one-shot; keeps the row for undo
                continue

            if action.kind is PriceActionKind.VOLATILITY:
                multiplier = D(action.params.get("multiplier", 1))
                if ends_at is not None and now >= ends_at:
                    for instrument in affected:
                        instrument.vol_multiplier = Decimal("1")
                    action.cancelled_at = now
                else:
                    for instrument in affected:
                        instrument.vol_multiplier = multiplier
                continue

            if action.kind is PriceActionKind.MOVE:
                if ends_at is not None and now >= ends_at:
                    # Land exactly on the target, then retire the action.
                    for instrument in affected:
                        target = pricing.target_from_params(instrument, action.params)
                        if target is not None:
                            instrument.last_price, instrument.status = pricing.apply_band(instrument, target)
                    action.cancelled_at = now
                    continue
                remaining = pricing.move_seconds_remaining(ends_at, now)
                for instrument in affected:
                    target = pricing.target_from_params(instrument, action.params)
                    if target is not None:
                        targets[instrument.symbol] = (target, remaining)

        if not ticking:
            return instruments

        window = get_rules().market.impact_window_seconds
        for instrument in instruments:
            if instrument.status in (InstrumentStatus.HALTED, InstrumentStatus.SUSPENDED):
                continue
            target, remaining = targets.get(instrument.symbol, (None, 0.0))
            price, status = pricing.next_price(
                instrument,
                self.rng,
                target=target,
                seconds_remaining=remaining,
                net_notional=self._net_flow(instrument.symbol, window),
                noise=True,
            )
            instrument.last_price = price
            instrument.status = status
            instrument.day_high = max(instrument.day_high, price)
            instrument.day_low = min(instrument.day_low, price) if instrument.day_low > 0 else price

        _ = by_symbol
        return instruments

    @staticmethod
    def _action_symbols(action: PriceAction, instruments: list[Instrument]) -> list[Instrument]:
        if action.symbol:
            return [i for i in instruments if i.symbol == action.symbol]
        if action.sector:
            return [i for i in instruments if i.sector == action.sector]
        return list(instruments)

    def record_flow(self, symbol: str, signed_notional: Decimal) -> None:
        """Called after every fill so the next tick feels the order flow."""
        self._flow[symbol].append((time.monotonic(), signed_notional))

    def _net_flow(self, symbol: str, window_seconds: int) -> Decimal:
        queue = self._flow.get(symbol)
        if not queue:
            return ZERO
        cutoff = time.monotonic() - window_seconds
        while queue and queue[0][0] < cutoff:
            queue.popleft()
        return sum((n for _, n in queue), ZERO)

    async def _persist_ticks(
        self, session: AsyncSession, state: MarketStateRow, instruments: list[Instrument], now: datetime
    ) -> None:
        for instrument in instruments:
            quote = pricing.quote_for(instrument)
            session.add(
                Tick(
                    symbol=instrument.symbol,
                    ts=now,
                    last=instrument.last_price,
                    bid=quote.bid,
                    ask=quote.ask,
                    volume=instrument.day_volume,
                    day_no=state.day_no,
                )
            )
            await self._roll_candle(session, instrument, now, state.day_no, "1m", 1)
            await self._roll_candle(session, instrument, now, state.day_no, "5m", 5)

    async def _roll_candle(
        self,
        session: AsyncSession,
        instrument: Instrument,
        now: datetime,
        day_no: int,
        interval: str,
        minutes: int,
    ) -> None:
        bucket = _floor_minute(now, minutes)
        candle = (
            await session.execute(
                select(Candle).where(
                    Candle.symbol == instrument.symbol,
                    Candle.interval == interval,
                    Candle.ts == bucket,
                )
            )
        ).scalar_one_or_none()
        price = instrument.last_price
        if candle is None:
            session.add(
                Candle(
                    symbol=instrument.symbol,
                    interval=interval,
                    ts=bucket,
                    o=price,
                    h=price,
                    low=price,
                    c=price,
                    v=instrument.day_volume,
                    day_no=day_no,
                )
            )
        else:
            candle.h = max(candle.h, price)
            candle.low = min(candle.low, price)
            candle.c = price
            candle.v = instrument.day_volume

    async def _update_index(
        self, session: AsyncSession, state: MarketStateRow, instruments: list[Instrument]
    ) -> None:
        rules = get_rules().market
        weighted_now = ZERO
        weighted_base = ZERO
        for instrument in instruments:
            weighted_now += instrument.index_weight * instrument.last_price
            weighted_base += instrument.index_weight * instrument.start_price
        if weighted_base <= 0:
            return
        state.index_value = money(rules.index_base * weighted_now / weighted_base)

        if rules.market_breaker_pct > 0 and state.index_prev_close > 0:
            change = (state.index_value - state.index_prev_close) / state.index_prev_close * Decimal("100")
            if abs(change) >= rules.market_breaker_pct and state.breaker_until is None:
                state.state = MarketState.HALTED
                state.breaker_until = utcnow() + timedelta(seconds=rules.market_breaker_halt_seconds)
                state.banner = (
                    f"Market-wide circuit breaker: the index moved "
                    f"{change.quantize(Decimal('0.01'))}%. Trading halts for "
                    f"{rules.market_breaker_halt_seconds // 60} minutes."
                )
                state.banner_severity = "critical"
                await hub.broadcast_public(
                    "announcement",
                    {"message": state.banner, "severity": "critical"},
                    durable=True,
                )
                log.warning("market-wide breaker tripped at %s", state.index_value)

    # -------------------------------------------------------- resting orders

    async def _sweep_resting_orders(self, now: datetime) -> None:
        """Evaluate every working order against the new prices.

        Each team's fills happen in their own transaction under their own lock,
        so an API request for a different team is never blocked, and a fill here
        is never interleaved with a fill there.
        """
        async with session_scope() as session:
            state = await self.get_state(session)
            if state.state is not MarketState.OPEN:
                return
            orders = await matching.working_orders(session)
            marks = await matching.load_marks(session)
            day_no = state.day_no
            market_state = state.state

        by_team: dict[int, list[Order]] = defaultdict(list)
        for order in orders:
            by_team[order.team_id].append(order)

        for team_id in sorted(by_team):
            await self._sweep_team_orders(team_id, [o.id for o in by_team[team_id]], marks, market_state, day_no, now)

    async def _sweep_team_orders(
        self,
        team_id: int,
        order_ids: list[int],
        marks: dict[str, Decimal],
        market_state: MarketState,
        day_no: int,
        now: datetime,
    ) -> None:
        events: list[tuple[str, dict]] = []
        async with locked_team(team_id):
            async with session_scope() as session:
                team = (await session.execute(select(Team).where(Team.id == team_id))).scalar_one_or_none()
                if team is None or team.status is not TeamStatus.ACTIVE:
                    return
                for order_id in order_ids:
                    order = (await session.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
                    if order is None or not order.status.is_open:
                        continue
                    instrument = (
                        await session.execute(select(Instrument).where(Instrument.symbol == order.symbol))
                    ).scalar_one_or_none()
                    if instrument is None or not instrument.status.tradable:
                        continue

                    if order.order_type.is_stop and order.status is OrderStatus.PENDING:
                        if not matching.stop_triggered(order, instrument.last_price):
                            continue
                        order.status = OrderStatus.TRIGGERED
                        events.append(
                            (
                                "order_update",
                                {
                                    "id": order.id,
                                    "status": order.status.value,
                                    "symbol": order.symbol,
                                    "message": f"Stop triggered at {instrument.last_price}.",
                                },
                            )
                        )

                    try:
                        fills = await matching.try_execute(
                            session,
                            team=team,
                            order=order,
                            instrument=instrument,
                            market_state=market_state,
                            day_no=day_no,
                            now=now,
                            enforce_slippage=False,
                        )
                    except matching.OrderRejected as exc:
                        order.status = OrderStatus.CANCELLED
                        order.reason = exc.message[:200]
                        events.append(
                            ("order_update", {"id": order.id, "status": "CANCELLED", "reason": exc.message})
                        )
                        continue

                    for result in fills:
                        self.record_flow(
                            order.symbol,
                            result.fill.gross * (1 if order.side.value == "BUY" else -1),
                        )
                        events.append(
                            (
                                "fill",
                                {
                                    "order_id": order.id,
                                    "symbol": order.symbol,
                                    "side": order.side.value,
                                    "qty": result.fill.qty,
                                    "price": str(result.fill.price),
                                    "fees": str(result.fill.fees_total),
                                    "status": order.status.value,
                                },
                            )
                        )

        for event, payload in events:
            await hub.to_team(team_id, event, payload)
        if events:
            await self._publish_portfolio(team_id, marks)

    # ------------------------------------------------------------ risk desk

    async def _run_risk_desk(self, now: datetime) -> None:
        async with session_scope() as session:
            state = await self.get_state(session)
            if state.state is not MarketState.OPEN:
                return
            marks = await matching.load_marks(session)
            valuations = await valuate_all(session, marks, only_with_shorts=True)
            day_no = state.day_no
            market_state = state.state

        if not valuations:
            return

        events = await riskdesk.sweep(valuations, marks, market_state, day_no, warned=self._warned_teams)
        for event in events:
            await hub.to_team(event.team_id, event.kind, {"message": event.message, **event.payload})
            await hub.to_ops(
                "risk_event",
                {"team_id": event.team_id, "kind": event.kind, "message": event.message},
            )
            await self._publish_portfolio(event.team_id, marks)

    # ------------------------------------------------------------ publishing

    async def _publish_quotes(self, session: AsyncSession, state: MarketStateRow) -> None:
        instruments = list(
            (await session.execute(select(Instrument).where(Instrument.listed.is_(True)))).scalars()
        )
        payload = []
        for instrument in instruments:
            quote = pricing.quote_for(instrument)
            change = instrument.last_price - instrument.prev_close
            change_pct = (
                change / instrument.prev_close * Decimal("100") if instrument.prev_close > 0 else ZERO
            )
            payload.append(
                {
                    "symbol": instrument.symbol,
                    "last": str(instrument.last_price),
                    "bid": str(quote.bid),
                    "ask": str(quote.ask),
                    "change": str(money(change)),
                    "change_pct": str(change_pct.quantize(Decimal("0.01"))),
                    "open": str(instrument.day_open),
                    "high": str(instrument.day_high),
                    "low": str(instrument.day_low),
                    "volume": instrument.day_volume,
                    "status": instrument.status.value,
                }
            )

        index_change = state.index_value - state.index_prev_close
        index_pct = (
            index_change / state.index_prev_close * Decimal("100") if state.index_prev_close > 0 else ZERO
        )
        await hub.broadcast_public(
            "quotes",
            {
                "ts": utcnow().isoformat(),
                "quotes": payload,
                "index": {
                    "name": get_rules().market.index_name,
                    "value": str(state.index_value),
                    "change": str(money(index_change)),
                    "change_pct": str(index_pct.quantize(Decimal("0.01"))),
                },
            },
        )

    async def _publish_portfolio(self, team_id: int, marks: dict[str, Decimal]) -> None:
        async with session_scope() as session:
            team = (await session.execute(select(Team).where(Team.id == team_id))).scalar_one_or_none()
            if team is None:
                return
            valuation, positions = await matching.valuate_team(session, team, marks)
            await hub.to_team(
                team_id,
                "portfolio",
                {
                    "funds": valuation.as_dict(),
                    "positions": [
                        {
                            "symbol": p.symbol,
                            "qty": p.qty,
                            "avg_cost": str(p.avg_cost),
                            "last": str(marks.get(p.symbol, p.avg_cost)),
                        }
                        for p in positions
                        if p.qty != 0
                    ],
                },
            )

    async def _maybe_snapshot(self, session: AsyncSession, state: MarketStateRow, now: datetime) -> None:
        rules = get_rules().session
        clock = time.monotonic()

        if clock - self._last_leaderboard >= rules.leaderboard_seconds:
            self._last_leaderboard = clock
            await self._publish_leaderboard(session, state)

        if clock - self._last_snapshot >= rules.snapshot_seconds:
            self._last_snapshot = clock
            await self.snapshot_equities(session, state, now, is_close=False)

    async def _publish_leaderboard(self, session: AsyncSession, state: MarketStateRow) -> None:
        marks = await matching.load_marks(session)
        valuations = await valuate_all(session, marks)
        ranked = rank(valuations)

        full = [
            {
                "rank": position,
                "team_id": tv.team_id,
                "team": tv.team_name,
                "code": tv.code,
                "equity": str(tv.rankable_equity),
                "status": tv.status.value,
            }
            for position, tv in ranked
        ]
        await hub.to_ops("leaderboard", {"rows": full, "blackout": state.leaderboard_blackout})

        if state.leaderboard_blackout:
            await hub.broadcast_public("leaderboard", {"rows": [], "blackout": True})
        else:
            size = get_rules().session.public_leaderboard_size
            await hub.broadcast_public("leaderboard", {"rows": full[:size], "blackout": False})

    async def snapshot_equities(
        self, session: AsyncSession, state: MarketStateRow, now: datetime, *, is_close: bool
    ) -> None:
        marks = await matching.load_marks(session)
        valuations = await valuate_all(session, marks)
        for position, tv in rank(valuations):
            session.add(
                EquitySnapshot(
                    team_id=tv.team_id,
                    ts=now,
                    cash=tv.valuation.cash,
                    long_mv=tv.valuation.long_mv,
                    short_mv=tv.valuation.short_mv,
                    equity=tv.rankable_equity,
                    rank=position,
                    day_no=state.day_no,
                    is_close=is_close,
                )
            )

    # ----------------------------------------------------------- the clock

    async def _advance_clock(self, session: AsyncSession, state: MarketStateRow, now: datetime) -> None:
        rules = get_rules().session

        if state.state is MarketState.HALTED and state.breaker_until is not None:
            until = state.breaker_until
            if until.tzinfo is None:
                until = until.replace(tzinfo=UTC)
            if now >= until:
                state.state = MarketState.OPEN
                state.breaker_until = None
                state.banner = "Trading resumes."
                state.banner_severity = "info"
                await self._publish_state(state)
            return

        if state.state is not MarketState.OPEN or not rules.auto_advance:
            return

        remaining = self._remaining_seconds(state, now)
        if remaining <= 0:
            await self.close_day(session, state, now)
        elif remaining <= rules.blackout_last_seconds and state.day_no >= state.total_days:
            if not state.leaderboard_blackout:
                state.leaderboard_blackout = True
                await hub.broadcast_public(
                    "announcement",
                    {
                        "message": "Leaderboard blackout. Final standings are revealed at the close.",
                        "severity": "info",
                    },
                    durable=True,
                )

    async def _maybe_open(self, session: AsyncSession, state: MarketStateRow, now: datetime) -> None:
        if not get_rules().session.auto_advance:
            return
        if self._remaining_seconds(state, now) <= 0:
            await self.open_trading(session, state, now)

    async def _maybe_start_next_day(
        self, session: AsyncSession, state: MarketStateRow, now: datetime
    ) -> None:
        rules = get_rules().session
        if not rules.auto_advance or state.day_no == 0:
            return
        if self._remaining_seconds(state, now) > 0:
            return
        if state.day_no >= state.total_days:
            await self.finalise(session, state, now)
        else:
            await self.start_day(session, state, state.day_no + 1, now)

    async def start_day(
        self, session: AsyncSession, state: MarketStateRow, day_no: int, now: datetime | None = None
    ) -> None:
        """Begin a trading day in PRE_OPEN.

        Each day gets a fresh reference price, which is what the price band is
        measured from, and fresh day open/high/low. This is why a band is a
        per-day construct here exactly as it is on the real exchange.
        """
        now = now or utcnow()
        rules = get_rules().session
        instruments = list((await session.execute(select(Instrument))).scalars())
        for instrument in instruments:
            instrument.day_reference = instrument.last_price
            instrument.prev_close = instrument.last_price
            instrument.day_open = instrument.last_price
            instrument.day_high = instrument.last_price
            instrument.day_low = instrument.last_price
            instrument.day_volume = 0
            if instrument.status in (InstrumentStatus.UPPER_CIRCUIT, InstrumentStatus.LOWER_CIRCUIT):
                instrument.status = InstrumentStatus.ACTIVE

        state.index_prev_close = state.index_value
        state.day_no = day_no
        state.state = MarketState.PRE_OPEN
        state.session_ends_at = now + timedelta(seconds=rules.pre_open_seconds)
        state.leaderboard_blackout = False
        state.breaker_until = None
        state.banner = f"Day {day_no} pre-open. Limit orders can be queued now."
        state.banner_severity = "info"
        state.changed_at = now
        await self._publish_state(state)
        log.info("day %s pre-open", day_no)

    async def open_trading(
        self, session: AsyncSession, state: MarketStateRow, now: datetime | None = None
    ) -> None:
        now = now or utcnow()
        rules = get_rules().session
        state.state = MarketState.OPEN
        state.session_ends_at = now + timedelta(seconds=rules.open_seconds)
        state.banner = None
        state.changed_at = now
        await self._publish_state(state)
        log.info("day %s open", state.day_no)

    async def close_day(
        self, session: AsyncSession, state: MarketStateRow, now: datetime | None = None
    ) -> None:
        """Close a trading day: cancel day orders, charge borrow, snapshot."""
        now = now or utcnow()
        rules = get_rules().session

        cancelled = await session.execute(
            update(Order)
            .where(Order.status.in_([OrderStatus.PENDING, OrderStatus.TRIGGERED]))
            .values(status=OrderStatus.CANCELLED, reason="Cancelled at the end of the trading day")
        )
        await riskdesk.charge_borrow_fees(session, state.day_no)

        instruments = list((await session.execute(select(Instrument))).scalars())
        for instrument in instruments:
            instrument.prev_close = instrument.last_price

        state.state = MarketState.CLOSED
        state.session_ends_at = now + timedelta(seconds=rules.break_seconds)
        state.banner = (
            f"Day {state.day_no} is closed."
            + ("" if state.day_no >= state.total_days else " The next day opens shortly.")
        )
        state.banner_severity = "info"
        state.changed_at = now

        await self.snapshot_equities(session, state, now, is_close=True)
        await self._publish_state(state)
        await self._publish_leaderboard(session, state)
        log.info("day %s closed (%s working orders cancelled)", state.day_no, cancelled.rowcount or 0)

    async def finalise(
        self, session: AsyncSession, state: MarketStateRow, now: datetime | None = None
    ) -> None:
        now = now or utcnow()
        state.state = MarketState.FINAL
        state.session_ends_at = None
        state.leaderboard_blackout = False
        state.banner = "The competition has ended. Final standings are locked."
        state.banner_severity = "info"
        state.changed_at = now
        await self.snapshot_equities(session, state, now, is_close=True)
        await self._publish_state(state)
        await self._publish_leaderboard(session, state)
        log.info("competition finalised")

    async def freeze(self, session: AsyncSession, state: MarketStateRow, message: str) -> None:
        """Stop everything, including the clock."""
        now = utcnow()
        if state.state is MarketState.FROZEN:
            return
        state.frozen_remaining_seconds = int(self._remaining_seconds(state, now))
        state.state = MarketState.FROZEN
        state.session_ends_at = None
        state.banner = message
        state.banner_severity = "critical"
        state.changed_at = now
        await self._publish_state(state)
        log.warning("market frozen: %s", message)

    async def resume(self, session: AsyncSession, state: MarketStateRow, resume_to: MarketState) -> None:
        """Pick the clock up exactly where it stopped."""
        now = utcnow()
        remaining = state.frozen_remaining_seconds or 0
        state.state = resume_to
        state.session_ends_at = now + timedelta(seconds=remaining) if remaining else None
        state.frozen_remaining_seconds = None
        state.banner = "Trading resumes."
        state.banner_severity = "info"
        state.changed_at = now
        await self._publish_state(state)
        log.info("market resumed to %s with %ss remaining", resume_to.value, remaining)

    async def _publish_state(self, state: MarketStateRow) -> None:
        await hub.broadcast_public("market_state", self.state_payload(state), durable=True)

    @staticmethod
    def state_payload(state: MarketStateRow) -> dict:
        ends = state.session_ends_at
        if ends is not None and ends.tzinfo is None:
            ends = ends.replace(tzinfo=UTC)
        return {
            "state": state.state.value,
            "day_no": state.day_no,
            "total_days": state.total_days,
            "ends_at": ends.isoformat() if ends else None,
            "server_time": utcnow().isoformat(),
            "banner": state.banner,
            "banner_severity": state.banner_severity,
            "blackout": state.leaderboard_blackout,
            "index": {
                "name": get_rules().market.index_name,
                "value": str(state.index_value),
                "prev_close": str(state.index_prev_close),
            },
        }

    # ------------------------------------------------------------- scenarios

    async def _fire_scenarios(self, session: AsyncSession, state: MarketStateRow, now: datetime) -> None:
        scenarios = list(
            (await session.execute(select(Scenario).where(Scenario.status == "PLAYING"))).scalars()
        )
        for scenario in scenarios:
            if scenario.started_at is None:
                continue
            started = scenario.started_at
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            offset = (now - started).total_seconds() - scenario.paused_offset_seconds

            steps = list(
                (
                    await session.execute(
                        select(ScenarioStep).where(
                            ScenarioStep.scenario_id == scenario.id,
                            ScenarioStep.fired_at.is_(None),
                            ScenarioStep.skipped.is_(False),
                        ).order_by(ScenarioStep.at_offset)
                    )
                ).scalars()
            )
            due = [s for s in steps if s.at_offset <= offset]
            for step in due:
                await self.fire_step(session, state, step, now)
            if not steps:
                scenario.status = "DONE"

    async def fire_step(
        self, session: AsyncSession, state: MarketStateRow, step: ScenarioStep, now: datetime | None = None
    ) -> None:
        """Execute one scenario step. Every effect is one an operator could do by hand."""
        now = now or utcnow()
        payload = step.payload or {}
        step.fired_at = now

        for news in payload.get("news", []) or []:
            item = NewsItem(
                headline=news["headline"],
                body=news.get("body", ""),
                kind=news.get("kind", "NEWS"),
                symbols=news.get("symbols", []),
                sectors=news.get("sectors", []),
                sentiment=news.get("sentiment"),
                published_at=now,
                day_no=state.day_no,
            )
            session.add(item)
            await session.flush()
            await hub.broadcast_public(
                "news",
                {
                    "id": item.id,
                    "headline": item.headline,
                    "body": item.body,
                    "kind": item.kind.value if hasattr(item.kind, "value") else item.kind,
                    "symbols": item.symbols,
                    "published_at": now.isoformat(),
                },
                durable=True,
            )

        for move in payload.get("moves", []) or []:
            duration = int(move.get("over_seconds", 60))
            instruments = await self._resolve_targets(session, move)
            for instrument in instruments:
                session.add(
                    PriceAction(
                        symbol=instrument.symbol,
                        kind=PriceActionKind.MOVE if duration > 0 else PriceActionKind.JUMP,
                        params={
                            "pct": str(move["pct"]) if "pct" in move else None,
                            "target_price": move.get("target_price"),
                            "anchor_price": str(instrument.last_price),
                        },
                        price_before=instrument.last_price,
                        started_at=now,
                        ends_at=now + timedelta(seconds=duration) if duration > 0 else now,
                        note=f"scenario step {step.id}",
                    )
                )

        for symbol in payload.get("halt", []) or []:
            instrument = (
                await session.execute(select(Instrument).where(Instrument.symbol == symbol))
            ).scalar_one_or_none()
            if instrument:
                instrument.status = InstrumentStatus.HALTED
                instrument.halt_reason = payload.get("halt_reason", "pending an announcement")

        for symbol in payload.get("resume", []) or []:
            instrument = (
                await session.execute(select(Instrument).where(Instrument.symbol == symbol))
            ).scalar_one_or_none()
            if instrument:
                instrument.status = InstrumentStatus.ACTIVE
                instrument.halt_reason = None

        if announcement := payload.get("announce"):
            state.banner = announcement
            state.banner_severity = payload.get("severity", "info")
            await hub.broadcast_public(
                "announcement", {"message": announcement, "severity": state.banner_severity}, durable=True
            )

        action = payload.get("action")
        if action == "open":
            await self.open_trading(session, state, now)
        elif action == "close":
            await self.close_day(session, state, now)
        elif action == "pre_open":
            await self.start_day(session, state, state.day_no + 1, now)

    async def _resolve_targets(self, session: AsyncSession, move: dict) -> list[Instrument]:
        if symbol := move.get("symbol"):
            rows = await session.execute(select(Instrument).where(Instrument.symbol == symbol))
        elif sector := move.get("sector"):
            rows = await session.execute(select(Instrument).where(Instrument.sector == sector))
        else:
            rows = await session.execute(select(Instrument).where(Instrument.listed.is_(True)))
        return list(rows.scalars())

    # ---------------------------------------------------------------- health

    @property
    def health(self) -> dict:
        durations = list(self._tick_durations)
        durations.sort()
        p95 = durations[int(len(durations) * 0.95)] if durations else 0.0
        return {
            "running": self.running,
            "errors": self.errors,
            "last_tick_at": self._last_tick_at.isoformat() if self._last_tick_at else None,
            "tick_ms_avg": round(sum(durations) / len(durations) * 1000, 2) if durations else 0.0,
            "tick_ms_p95": round(p95 * 1000, 2),
            "websockets": hub.stats,
        }


engine = MarketEngine(seed=None if get_settings().run_engine else 42)


def get_engine() -> MarketEngine:
    return engine
