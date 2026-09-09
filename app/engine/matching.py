"""The order path. Every fill in the system goes through ``execute_fill``.

Market orders, resting limit orders, triggered stop-losses and forced margin
covers all converge on one function. There is exactly one place where cash
moves, one place where a position changes, and one place where a ledger row is
written, which is why the invariant tests can make claims about the whole
system from a few hundred lines.

Concurrency: callers must hold the team's lock (``db.locked_team``) around the
whole read-check-write cycle. ``submit_order`` and the engine's sweep both do.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_rules
from ..fees import FeeBreakdown, compute_fees
from ..models import (
    Fill,
    Instrument,
    InstrumentStatus,
    LedgerEntry,
    LedgerKind,
    MarketState,
    Order,
    OrderSide,
    OrderStatus,
    OrderTag,
    OrderType,
    Position,
    Team,
    TeamStatus,
    utcnow,
)
from ..money import ZERO, D, money, paise, round_to_tick
from ..risk import Valuation, project_available, valuate
from . import pricing


class RejectReason:
    MARKET_CLOSED = "market_closed"
    INSTRUMENT_HALTED = "instrument_halted"
    INSTRUMENT_SUSPENDED = "instrument_suspended"
    CIRCUIT = "circuit_limit"
    TEAM_INACTIVE = "team_inactive"
    BAD_QUANTITY = "bad_quantity"
    BAD_PRICE = "bad_price"
    INSUFFICIENT_FUNDS = "insufficient_funds"
    RATE_LIMITED = "rate_limited"
    SLIPPAGE = "slippage_tolerance_exceeded"
    NO_POSITION = "no_position"
    UNKNOWN_SYMBOL = "unknown_symbol"
    DAY_END = "day_end"


class OrderRejected(Exception):
    """A business rejection. Carries a machine code and a sentence for a human."""

    def __init__(self, reason: str, message: str, http_status: int = 400):
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.http_status = http_status


@dataclass
class FillResult:
    fill: Fill
    realised_pnl: Decimal
    fees: FeeBreakdown
    position_after: int


# ------------------------------------------------------------------ primitives


def apply_position_delta(
    position: Position, side: OrderSide, qty: int, price: Decimal
) -> Decimal:
    """Net a fill into a position and return the realised P&L it crystallises.

    Handles the three cases a signed position can be in: growing, shrinking, and
    flipping through zero. A flip realises the whole old position and reopens
    the remainder at the fill price.
    """
    old = position.qty
    delta = side.sign * qty
    new = old + delta
    realised = ZERO

    if old == 0 or (old > 0) == (delta > 0):
        # Opening or adding. Weighted average cost.
        total_qty = abs(old) + qty
        position.avg_cost = money(
            (position.avg_cost * abs(old) + price * qty) / total_qty
        ) if total_qty else ZERO
    elif abs(delta) <= abs(old):
        # Reducing. Average cost is untouched; the difference is realised.
        closed = qty
        realised = (price - position.avg_cost) * closed if old > 0 else (position.avg_cost - price) * closed
    else:
        # Flipping. Close everything, then open the remainder at this price.
        closed = abs(old)
        realised = (price - position.avg_cost) * closed if old > 0 else (position.avg_cost - price) * closed
        position.avg_cost = price

    position.qty = new
    if new == 0:
        position.avg_cost = ZERO
    position.realised_pnl = money(position.realised_pnl + realised)
    position.updated_at = utcnow()
    return money(realised)


def post_ledger(
    session: AsyncSession,
    team: Team,
    kind: LedgerKind,
    amount: Decimal,
    *,
    note: str | None = None,
    ref_type: str | None = None,
    ref_id: int | None = None,
    day_no: int = 0,
    ts: datetime | None = None,
) -> LedgerEntry:
    """Move cash. The only place ``team.cash`` is ever written."""
    amount = paise(amount)
    team.cash = paise(team.cash + amount)
    entry = LedgerEntry(
        team_id=team.id,
        kind=kind,
        amount=amount,
        balance_after=team.cash,
        note=note,
        ref_type=ref_type,
        ref_id=ref_id,
        day_no=day_no,
        ts=ts or utcnow(),
    )
    session.add(entry)
    return entry


async def get_position(session: AsyncSession, team_id: int, symbol: str) -> Position:
    position = (
        await session.execute(
            select(Position).where(Position.team_id == team_id, Position.symbol == symbol)
        )
    ).scalar_one_or_none()
    if position is None:
        position = Position(team_id=team_id, symbol=symbol, qty=0, avg_cost=ZERO, realised_pnl=ZERO)
        session.add(position)
        await session.flush()
    return position


async def load_positions(session: AsyncSession, team_id: int) -> list[Position]:
    return list(
        (await session.execute(select(Position).where(Position.team_id == team_id))).scalars()
    )


async def load_marks(session: AsyncSession, symbols: set[str] | None = None) -> dict[str, Decimal]:
    stmt = select(Instrument.symbol, Instrument.last_price)
    if symbols:
        stmt = stmt.where(Instrument.symbol.in_(symbols))
    return {row.symbol: row.last_price for row in (await session.execute(stmt))}


async def valuate_team(
    session: AsyncSession, team: Team, marks: dict[str, Decimal] | None = None
) -> tuple[Valuation, list[Position]]:
    positions = await load_positions(session, team.id)
    if marks is None:
        marks = await load_marks(session, {p.symbol for p in positions if p.qty != 0})
    return valuate(team.cash, positions, marks), positions


# ----------------------------------------------------------------- the one path


async def execute_fill(
    session: AsyncSession,
    *,
    team: Team,
    order: Order,
    instrument: Instrument,
    qty: int,
    price: Decimal,
    day_no: int,
    now: datetime | None = None,
) -> FillResult:
    """Book a fill. Cash, position, ledger and order state move together.

    The caller has already decided that this fill is allowed. This function does
    not re-check funds; it records what was decided, atomically.
    """
    now = now or utcnow()
    price = round_to_tick(price, instrument.tick_size)
    gross = paise(price * qty)
    fees = compute_fees(gross, order.side)

    position = await get_position(session, team.id, instrument.symbol)
    realised = apply_position_delta(position, order.side, qty, price)
    position.fees_paid = paise(position.fees_paid + fees.total)

    fill = Fill(
        order_id=order.id,
        team_id=team.id,
        symbol=instrument.symbol,
        side=order.side,
        qty=qty,
        price=price,
        gross=gross,
        fees_total=fees.total,
        fees=fees.as_json(),
        realised_pnl=realised,
        ts=now,
        day_no=day_no,
    )
    session.add(fill)

    cash_delta = -gross if order.side is OrderSide.BUY else gross
    post_ledger(
        session,
        team,
        LedgerKind.TRADE,
        cash_delta,
        note=f"{order.side.value} {qty} {instrument.symbol} @ {price}",
        ref_type="order",
        ref_id=order.id,
        day_no=day_no,
        ts=now,
    )
    if fees.total > 0:
        post_ledger(
            session,
            team,
            LedgerKind.FEE,
            -fees.total,
            note=f"Charges on {order.side.value} {qty} {instrument.symbol}",
            ref_type="order",
            ref_id=order.id,
            day_no=day_no,
            ts=now,
        )

    # Weighted average across partial fills.
    previous_value = (order.avg_price or ZERO) * order.filled_qty
    order.filled_qty += qty
    order.avg_price = money((previous_value + price * qty) / order.filled_qty)
    order.fees_total = paise(order.fees_total + fees.total)
    order.updated_at = now
    if order.filled_qty >= order.qty:
        order.status = OrderStatus.FILLED

    instrument.day_volume += qty
    await session.flush()

    return FillResult(fill=fill, realised_pnl=realised, fees=fees, position_after=position.qty)


# ------------------------------------------------------------------ validation


def max_qty_within_limit(
    instrument: Instrument, side: OrderSide, limit_price: Decimal, quote: pricing.Quote
) -> int:
    """How much of a limit order can fill at or better than its limit.

    Slippage walks the price away from the touch in half-spread steps. This
    returns the quantity reachable before the walk crosses the limit, so a large
    marketable limit fills what it can and rests the remainder. That is the real
    behaviour, and it stops a limit order at the touch being used to sidestep
    slippage on an enormous size.
    """
    rules = get_rules().market
    base = quote.touch(side)
    if base <= 0:
        return 0
    if not rules.slippage_enabled or instrument.liquidity_notional <= 0:
        return 10**9 if _limit_is_marketable(side, limit_price, base) else 0

    half_spread = D(instrument.spread_bps) / Decimal("20000")
    slice_qty = max(1, int(instrument.liquidity_notional / base))

    if half_spread <= 0:
        return 10**9 if _limit_is_marketable(side, limit_price, base) else 0

    if side is OrderSide.BUY:
        room = (limit_price - base) / base
    else:
        room = (base - limit_price) / base
    if room < 0:
        return 0
    steps = int((room / half_spread).to_integral_value(rounding=ROUND_FLOOR))
    steps = min(steps, int((pricing.MAX_SLIPPAGE_FRACTION / half_spread)))
    return (steps + 1) * slice_qty


def _limit_is_marketable(side: OrderSide, limit_price: Decimal, touch: Decimal) -> bool:
    return limit_price >= touch if side is OrderSide.BUY else limit_price <= touch


def stop_triggered(order: Order, last: Decimal) -> bool:
    """A stop for a buy triggers when price rises to it; for a sell, when it falls."""
    if order.trigger_price is None:
        return False
    if order.side is OrderSide.BUY:
        return last >= order.trigger_price
    return last <= order.trigger_price


def check_tradable(
    instrument: Instrument, side: OrderSide, market_state: MarketState
) -> None:
    if instrument.status is InstrumentStatus.SUSPENDED:
        raise OrderRejected(
            RejectReason.INSTRUMENT_SUSPENDED,
            f"{instrument.symbol} is suspended and cannot be traded.",
        )
    if instrument.status is InstrumentStatus.HALTED:
        reason = instrument.halt_reason or "pending an announcement"
        raise OrderRejected(
            RejectReason.INSTRUMENT_HALTED,
            f"{instrument.symbol} is halted ({reason}). Orders will be accepted when it resumes.",
        )
    if market_state is MarketState.OPEN and not pricing.side_allowed(instrument.status, side):
        limit = "upper" if instrument.status is InstrumentStatus.UPPER_CIRCUIT else "lower"
        raise OrderRejected(
            RejectReason.CIRCUIT,
            f"{instrument.symbol} is at its {limit} circuit limit. "
            f"{'Buy' if side is OrderSide.BUY else 'Sell'} orders rest until the price comes off the band.",
        )


def validate_request(
    *,
    instrument: Instrument,
    side: OrderSide,
    order_type: OrderType,
    qty: int,
    limit_price: Decimal | None,
    trigger_price: Decimal | None,
) -> None:
    rules = get_rules().market
    if qty <= 0:
        raise OrderRejected(RejectReason.BAD_QUANTITY, "Quantity must be a positive whole number.")
    if qty > rules.max_order_qty:
        raise OrderRejected(
            RejectReason.BAD_QUANTITY,
            f"Maximum {rules.max_order_qty:,} shares per order. Split larger trades.",
        )
    if instrument.lot_size > 1 and qty % instrument.lot_size:
        raise OrderRejected(
            RejectReason.BAD_QUANTITY,
            f"{instrument.symbol} trades in lots of {instrument.lot_size}.",
        )

    needs_limit = order_type in (OrderType.LIMIT, OrderType.SL_L)
    if needs_limit:
        if limit_price is None or limit_price <= 0:
            raise OrderRejected(RejectReason.BAD_PRICE, "A limit price is required for this order type.")
        if round_to_tick(limit_price, instrument.tick_size) != limit_price:
            raise OrderRejected(
                RejectReason.BAD_PRICE,
                f"Limit price must be a multiple of {instrument.tick_size}.",
            )
    if order_type.is_stop:
        if trigger_price is None or trigger_price <= 0:
            raise OrderRejected(RejectReason.BAD_PRICE, "A trigger price is required for a stop-loss order.")
        if round_to_tick(trigger_price, instrument.tick_size) != trigger_price:
            raise OrderRejected(
                RejectReason.BAD_PRICE,
                f"Trigger price must be a multiple of {instrument.tick_size}.",
            )


async def check_funds(
    session: AsyncSession,
    team: Team,
    instrument: Instrument,
    side: OrderSide,
    qty: int,
    price: Decimal,
    *,
    valuation: Valuation | None = None,
    positions: list[Position] | None = None,
) -> None:
    """Reject unless the trade leaves available funds at or above zero."""
    if valuation is None or positions is None:
        valuation, positions = await valuate_team(session, team)
    current = next((p.qty for p in positions if p.symbol == instrument.symbol), 0)
    gross = paise(price * qty)
    fees = compute_fees(gross, side).total
    projected = project_available(
        valuation,
        current_qty=current,
        side=side,
        qty=qty,
        fill_price=price,
        mark=instrument.last_price,
        fees=fees,
    )
    if projected < 0:
        shortfall = paise(-projected)
        raise OrderRejected(
            RejectReason.INSUFFICIENT_FUNDS,
            f"Short by Rs {shortfall} of available funds. "
            f"You have Rs {paise(valuation.available)} available; this order needs that much more.",
        )


# --------------------------------------------------------------------- entry


@dataclass
class OrderOutcome:
    order: Order
    fills: list[FillResult]
    replayed: bool = False

    @property
    def filled(self) -> bool:
        return self.order.status is OrderStatus.FILLED


async def try_execute(
    session: AsyncSession,
    *,
    team: Team,
    order: Order,
    instrument: Instrument,
    market_state: MarketState,
    day_no: int,
    now: datetime | None = None,
    enforce_slippage: bool = True,
    enforce_funds: bool = True,
) -> list[FillResult]:
    """Attempt to fill a working order against the current quote.

    Returns the fills produced, which may be empty when the order is not
    marketable. Used identically by fresh market orders, resting limits and
    triggered stops.

    ``enforce_funds`` is set false only by the risk desk. A forced cover must
    always go through: if a gap has left a team unable to afford buying its own
    short back, refusing the cover would leave the position open and the loss
    growing, which is the opposite of what a risk desk is for.
    """
    now = now or utcnow()
    results: list[FillResult] = []
    remaining = order.qty - order.filled_qty
    if remaining <= 0 or market_state is not MarketState.OPEN:
        return results
    if not pricing.side_allowed(instrument.status, order.side):
        return results

    quote = pricing.quote_for(instrument)
    working_type = order.order_type
    if working_type is OrderType.SL_M:
        working_type = OrderType.MARKET
    elif working_type is OrderType.SL_L:
        working_type = OrderType.LIMIT

    if working_type is OrderType.MARKET:
        fillable = remaining
    else:
        if order.limit_price is None:
            return results
        if not _limit_is_marketable(order.side, order.limit_price, quote.touch(order.side)):
            return results
        fillable = min(remaining, max_qty_within_limit(instrument, order.side, order.limit_price, quote))
        if fillable <= 0:
            return results

    fq = pricing.fill_quote(instrument, order.side, fillable, quote)
    price = fq.price
    if working_type is OrderType.LIMIT and order.limit_price is not None:
        # Never fill worse than the limit the participant set.
        price = min(price, order.limit_price) if order.side is OrderSide.BUY else max(price, order.limit_price)

    if enforce_slippage and working_type is OrderType.MARKET:
        tolerance = order.slippage_tolerance_pct
        if tolerance is not None and tolerance >= 0:
            reference = quote.touch(order.side)
            drift = abs((price - reference) / reference * Decimal("100")) if reference > 0 else ZERO
            if drift > tolerance:
                raise OrderRejected(
                    RejectReason.SLIPPAGE,
                    f"This order would fill about {drift.quantize(Decimal('0.01'))}% away from the quote, "
                    f"beyond your {tolerance}% tolerance. Reduce the size or raise the tolerance.",
                )

    if enforce_funds:
        await check_funds(session, team, instrument, order.side, fillable, price)
    result = await execute_fill(
        session,
        team=team,
        order=order,
        instrument=instrument,
        qty=fillable,
        price=price,
        day_no=day_no,
        now=now,
    )
    results.append(result)
    return results


async def submit_order(
    session: AsyncSession,
    *,
    team: Team,
    member_id: int | None,
    symbol: str,
    side: OrderSide,
    order_type: OrderType,
    qty: int,
    market_state: MarketState,
    day_no: int,
    limit_price: Decimal | None = None,
    trigger_price: Decimal | None = None,
    slippage_tolerance_pct: Decimal | None = None,
    client_order_id: str | None = None,
    tag: OrderTag = OrderTag.NORMAL,
    now: datetime | None = None,
) -> OrderOutcome:
    """Validate, record and (if marketable) fill an order.

    The caller holds the team lock. Rejections are recorded as REJECTED order
    rows so that the participant's order list and the operator's blotter both
    explain what happened and why.
    """
    now = now or utcnow()

    if client_order_id:
        existing = (
            await session.execute(
                select(Order).where(
                    Order.team_id == team.id, Order.client_order_id == client_order_id
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            # A retry after a dropped connection returns the original order
            # rather than placing a second one.
            return OrderOutcome(order=existing, fills=[], replayed=True)

    instrument = (
        await session.execute(select(Instrument).where(Instrument.symbol == symbol))
    ).scalar_one_or_none()

    order = Order(
        team_id=team.id,
        member_id=member_id,
        client_order_id=client_order_id,
        symbol=symbol,
        side=side,
        order_type=order_type,
        qty=qty,
        limit_price=limit_price,
        trigger_price=trigger_price,
        slippage_tolerance_pct=slippage_tolerance_pct,
        status=OrderStatus.PENDING,
        tag=tag,
        day_no=day_no,
        created_at=now,
        updated_at=now,
    )

    try:
        if instrument is None or not instrument.listed:
            raise OrderRejected(RejectReason.UNKNOWN_SYMBOL, f"{symbol} is not listed in this competition.")
        if team.status is TeamStatus.BUSTED:
            raise OrderRejected(
                RejectReason.TEAM_INACTIVE,
                "Your account value reached zero, so trading is closed for your team.",
            )
        if team.status is not TeamStatus.ACTIVE:
            raise OrderRejected(
                RejectReason.TEAM_INACTIVE,
                "Your team cannot trade right now. Please speak to the help desk.",
            )
        if not market_state.accepts_orders:
            raise OrderRejected(
                RejectReason.MARKET_CLOSED,
                {
                    MarketState.CLOSED: "The market is closed. Trading resumes at the start of the next day.",
                    MarketState.FROZEN: "The market is frozen by the organisers. Please hold.",
                    MarketState.HALTED: "Trading is halted market-wide. Please hold.",
                    MarketState.FINAL: "The competition has ended. Final positions are locked.",
                }.get(market_state, "The market is not accepting orders."),
            )
        if market_state is MarketState.PRE_OPEN and order_type is OrderType.MARKET:
            raise OrderRejected(
                RejectReason.MARKET_CLOSED,
                "Only limit and stop orders can be queued before the open.",
            )

        validate_request(
            instrument=instrument,
            side=side,
            order_type=order_type,
            qty=qty,
            limit_price=limit_price,
            trigger_price=trigger_price,
        )
        check_tradable(instrument, side, market_state)

        session.add(order)
        await session.flush()

        if order_type.is_stop:
            # A stop sits dormant until its trigger is crossed, even if it would
            # be marketable right now. That is what makes it a stop.
            last = instrument.last_price
            if not stop_triggered(order, last):
                return OrderOutcome(order=order, fills=[])
            order.status = OrderStatus.TRIGGERED

        fills = await try_execute(
            session,
            team=team,
            order=order,
            instrument=instrument,
            market_state=market_state,
            day_no=day_no,
            now=now,
        )
        if not fills and order_type is OrderType.MARKET:
            raise OrderRejected(
                RejectReason.MARKET_CLOSED,
                "No price is available for this stock right now. Try again in a moment.",
            )
        return OrderOutcome(order=order, fills=fills)

    except OrderRejected as exc:
        order.status = OrderStatus.REJECTED
        order.reason = exc.message[:200]
        if order.id is None and instrument is not None:
            # Rejected before the row was written. Record it anyway so the
            # participant's order list and the operator's blotter both explain
            # what happened. An unknown symbol cannot be recorded (the foreign
            # key would fail), so that one is raised to the caller instead.
            session.add(order)
            await session.flush()
        elif order.id is None:
            raise
        return OrderOutcome(order=order, fills=[])


async def cancel_order(
    session: AsyncSession, order: Order, reason: str = "Cancelled by team"
) -> Order:
    if order.status.is_terminal:
        raise OrderRejected("not_open", "That order is no longer open.")
    order.status = OrderStatus.CANCELLED
    order.reason = reason[:200]
    order.updated_at = utcnow()
    return order


async def working_orders(
    session: AsyncSession, symbol: str | None = None, team_id: int | None = None
) -> list[Order]:
    stmt = select(Order).where(
        Order.status.in_([OrderStatus.PENDING, OrderStatus.TRIGGERED])
    ).order_by(Order.created_at)
    if symbol:
        stmt = stmt.where(Order.symbol == symbol)
    if team_id:
        stmt = stmt.where(Order.team_id == team_id)
    return list((await session.execute(stmt)).scalars())
