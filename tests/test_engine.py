"""The tick loop, price bands, session transitions and recovery."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db import session_scope
from app.engine import pricing
from app.engine.market import MarketEngine
from app.models import (
    Candle,
    EquitySnapshot,
    Instrument,
    InstrumentStatus,
    MarketState,
    MarketStateRow,
    Order,
    OrderStatus,
    OrderType,
    PriceAction,
    PriceActionKind,
    Tick,
    utcnow,
)
from tests.conftest import make_instrument, make_team, set_market


async def open_market(seconds: int = 1500) -> None:
    await set_market(MarketState.OPEN)
    async with session_scope() as session:
        state = (await session.execute(select(MarketStateRow))).scalar_one()
        state.session_ends_at = utcnow() + timedelta(seconds=seconds)


async def price_of(symbol: str) -> Decimal:
    async with session_scope() as session:
        return (
            await session.execute(select(Instrument.last_price).where(Instrument.symbol == symbol))
        ).scalar_one()


async def status_of(symbol: str) -> InstrumentStatus:
    async with session_scope() as session:
        return (
            await session.execute(select(Instrument.status).where(Instrument.symbol == symbol))
        ).scalar_one()


# ------------------------------------------------------------- price process


async def test_a_move_walks_the_price_to_its_target(deterministic_engine: MarketEngine):
    """A MOVE drifts toward the target and lands on it when time runs out."""
    await open_market()
    await make_instrument("TESTCO", "100", daily_vol_pct="0")
    now = utcnow()

    async with session_scope() as session:
        session.add(
            PriceAction(
                symbol="TESTCO",
                kind=PriceActionKind.MOVE,
                params={"pct": "10", "anchor_price": "100"},
                price_before=Decimal("100"),
                started_at=now,
                ends_at=now + timedelta(seconds=10),
            )
        )

    prices = []
    for _ in range(6):
        await deterministic_engine.tick()
        prices.append(await price_of("TESTCO"))

    assert prices == sorted(prices), "the walk is monotonic when there is no noise"
    assert Decimal("100") < prices[-1] < Decimal("110")

    # Once the action expires the engine lands exactly on the target.
    await deterministic_engine.tick(now=now + timedelta(seconds=11))
    assert await price_of("TESTCO") == Decimal("110")


async def test_noise_moves_a_price_that_has_no_operator_intent(
    deterministic_engine: MarketEngine,
):
    await open_market()
    await make_instrument("TESTCO", "1000", daily_vol_pct="3")
    start = await price_of("TESTCO")
    for _ in range(40):
        await deterministic_engine.tick()
    assert await price_of("TESTCO") != start, "a live tape does not sit still"


async def test_order_flow_impact_is_applied_once_not_once_per_tick():
    """A trade's impact is spread over the window, not repeated across it.

    The window sums every trade still inside it, so without dividing by the
    number of ticks in the window a single order would push the price five
    times over. A 1.6 lakh buy in a 2 crore book should move a large cap by
    a few hundredths of a per cent per tick, not by a full per cent.
    """
    from app.engine.pricing import compute_impact

    instrument = await make_instrument("TESTCO", "1640", liquidity="20000000")
    per_tick = compute_impact(Decimal("163670"), instrument)

    # Five ticks in a five second window at one second a tick.
    total = per_tick * 5
    assert 0.001 < total < 0.0015, "a 1.6 lakh order moves a 2 crore book about 0.12%"
    assert per_tick * 5 == pytest.approx(
        0.15 * (163670 / 20000000), rel=1e-9
    ), "the whole window delivers exactly the configured coefficient, once"


async def test_a_very_large_order_moves_the_price_meaningfully():
    """Size must still cost something, or the lesson is lost."""
    from app.engine.pricing import compute_impact

    instrument = await make_instrument("TESTCO", "100", liquidity="20000000")
    total = compute_impact(Decimal("4000000"), instrument) * 5
    assert 0.025 < total < 0.035, "a 40 lakh order moves the price about 3%"


async def test_the_band_clamps_the_price_and_sets_the_circuit(
    deterministic_engine: MarketEngine,
):
    await open_market()
    await make_instrument("TESTCO", "100", daily_vol_pct="0", band_pct="10")
    now = utcnow()

    async with session_scope() as session:
        session.add(
            PriceAction(
                symbol="TESTCO",
                kind=PriceActionKind.JUMP,
                params={"pct": "50", "anchor_price": "100"},
                price_before=Decimal("100"),
                started_at=now,
                ends_at=now,
            )
        )
    await deterministic_engine.tick()

    assert await price_of("TESTCO") == Decimal("110"), "clamped to the 10% band"
    assert await status_of("TESTCO") is InstrumentStatus.UPPER_CIRCUIT


async def test_a_lower_circuit_blocks_sells_and_allows_buys():
    instrument = await make_instrument("TESTCO", "100", band_pct="10")
    instrument.status = InstrumentStatus.LOWER_CIRCUIT
    from app.models import OrderSide

    assert pricing.side_allowed(InstrumentStatus.LOWER_CIRCUIT, OrderSide.BUY)
    assert not pricing.side_allowed(InstrumentStatus.LOWER_CIRCUIT, OrderSide.SELL)
    assert pricing.side_allowed(InstrumentStatus.UPPER_CIRCUIT, OrderSide.SELL)
    assert not pricing.side_allowed(InstrumentStatus.UPPER_CIRCUIT, OrderSide.BUY)


async def test_a_halted_stock_does_not_tick(deterministic_engine: MarketEngine):
    await open_market()
    await make_instrument("TESTCO", "100", daily_vol_pct="5")
    async with session_scope() as session:
        instrument = (
            await session.execute(select(Instrument).where(Instrument.symbol == "TESTCO"))
        ).scalar_one()
        instrument.status = InstrumentStatus.HALTED

    for _ in range(10):
        await deterministic_engine.tick()
    assert await price_of("TESTCO") == Decimal("100")


async def test_the_same_seed_produces_the_same_tape():
    """A rehearsal can be replayed exactly, which is what makes it a rehearsal."""
    await open_market()
    await make_instrument("TESTCO", "500", daily_vol_pct="2")

    first = []
    engine = MarketEngine(seed=7)
    for _ in range(15):
        await engine.tick()
        first.append(await price_of("TESTCO"))

    async with session_scope() as session:
        instrument = (
            await session.execute(select(Instrument).where(Instrument.symbol == "TESTCO"))
        ).scalar_one()
        instrument.last_price = Decimal("500")

    second = []
    engine = MarketEngine(seed=7)
    for _ in range(15):
        await engine.tick()
        second.append(await price_of("TESTCO"))

    assert first == second


# ------------------------------------------------------------- persistence


async def test_ticks_and_candles_are_recorded(deterministic_engine: MarketEngine):
    await open_market()
    await make_instrument("TESTCO", "100")
    for _ in range(5):
        await deterministic_engine.tick()

    async with session_scope() as session:
        ticks = list((await session.execute(select(Tick))).scalars())
        candles = list((await session.execute(select(Candle))).scalars())

    assert len(ticks) == 5
    intervals = {c.interval for c in candles}
    assert intervals == {"1m", "5m"}
    for candle in candles:
        assert candle.low <= candle.o <= candle.h
        assert candle.low <= candle.c <= candle.h


async def test_the_index_tracks_the_basket(deterministic_engine: MarketEngine):
    await open_market()
    await make_instrument("BIG", "100", daily_vol_pct="0")
    await make_instrument("SMALL", "100", daily_vol_pct="0")
    async with session_scope() as session:
        for symbol, weight in (("BIG", "9"), ("SMALL", "1")):
            instrument = (
                await session.execute(select(Instrument).where(Instrument.symbol == symbol))
            ).scalar_one()
            instrument.index_weight = Decimal(weight)

    await deterministic_engine.tick()
    async with session_scope() as session:
        base = (await session.execute(select(MarketStateRow.index_value))).scalar_one()
    assert base == Decimal("20000")

    # A 10% rise in the 90%-weighted name should move the index about 9%.
    async with session_scope() as session:
        instrument = (
            await session.execute(select(Instrument).where(Instrument.symbol == "BIG"))
        ).scalar_one()
        instrument.last_price = Decimal("110")
    await deterministic_engine.tick()

    async with session_scope() as session:
        value = (await session.execute(select(MarketStateRow.index_value))).scalar_one()
    assert Decimal("21750") < value < Decimal("21850")


# --------------------------------------------------------- session lifecycle


async def test_closing_a_day_cancels_working_orders_and_charges_borrow(
    deterministic_engine: MarketEngine,
):
    from app.engine import matching
    from app.models import MarketState as MS
    from app.models import OrderSide, Team

    await open_market()
    await make_instrument("TESTCO", "100")
    team_id = await make_team()

    async with session_scope() as session:
        team = (await session.execute(select(Team).where(Team.id == team_id))).scalar_one()
        await matching.submit_order(
            session,
            team=team,
            member_id=None,
            symbol="TESTCO",
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            qty=1000,
            market_state=MS.OPEN,
            day_no=1,
        )
        await matching.submit_order(
            session,
            team=team,
            member_id=None,
            symbol="TESTCO",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            qty=10,
            limit_price=Decimal("50"),
            market_state=MS.OPEN,
            day_no=1,
        )

    async with session_scope() as session:
        state = (await session.execute(select(MarketStateRow))).scalar_one()
        cash_before = (await session.execute(select(Team.cash).where(Team.id == team_id))).scalar_one()
        await deterministic_engine.close_day(session, state)

    async with session_scope() as session:
        resting = list(
            (
                await session.execute(
                    select(Order).where(Order.order_type == OrderType.LIMIT)
                )
            ).scalars()
        )
        cash_after = (await session.execute(select(Team.cash).where(Team.id == team_id))).scalar_one()
        snapshots = list(
            (await session.execute(select(EquitySnapshot).where(EquitySnapshot.is_close.is_(True)))).scalars()
        )
        state = (await session.execute(select(MarketStateRow))).scalar_one()

    assert resting[0].status is OrderStatus.CANCELLED
    assert "end of the trading day" in resting[0].reason
    assert cash_after < cash_before, "the borrow fee was charged"
    assert snapshots and snapshots[0].rank == 1
    assert state.state is MarketState.CLOSED


async def test_starting_a_day_resets_the_band_reference(deterministic_engine: MarketEngine):
    await make_instrument("TESTCO", "100", band_pct="10")
    async with session_scope() as session:
        instrument = (
            await session.execute(select(Instrument).where(Instrument.symbol == "TESTCO"))
        ).scalar_one()
        instrument.last_price = Decimal("109")
        instrument.status = InstrumentStatus.UPPER_CIRCUIT

    async with session_scope() as session:
        state = await deterministic_engine.get_state(session)
        await deterministic_engine.start_day(session, state, 2)

    async with session_scope() as session:
        instrument = (
            await session.execute(select(Instrument).where(Instrument.symbol == "TESTCO"))
        ).scalar_one()
        state = (await session.execute(select(MarketStateRow))).scalar_one()

    assert instrument.day_reference == Decimal("109"), "today's band is measured from yesterday's close"
    assert instrument.status is InstrumentStatus.ACTIVE, "circuits clear overnight"
    assert instrument.day_volume == 0
    assert state.state is MarketState.PRE_OPEN
    assert state.day_no == 2


async def test_freeze_stops_the_clock_and_resume_restores_it(
    deterministic_engine: MarketEngine,
):
    await open_market(seconds=600)
    async with session_scope() as session:
        state = (await session.execute(select(MarketStateRow))).scalar_one()
        await deterministic_engine.freeze(session, state, "Wi-Fi has gone down.")

    async with session_scope() as session:
        state = (await session.execute(select(MarketStateRow))).scalar_one()
        assert state.state is MarketState.FROZEN
        assert state.session_ends_at is None
        assert 590 <= state.frozen_remaining_seconds <= 600

    # A frozen market does not tick.
    await make_instrument("TESTCO", "100", daily_vol_pct="5")
    for _ in range(5):
        await deterministic_engine.tick()
    assert await price_of("TESTCO") == Decimal("100")

    async with session_scope() as session:
        state = (await session.execute(select(MarketStateRow))).scalar_one()
        await deterministic_engine.resume(session, state, MarketState.OPEN)

    async with session_scope() as session:
        state = (await session.execute(select(MarketStateRow))).scalar_one()
    assert state.state is MarketState.OPEN
    assert state.session_ends_at is not None


async def test_recovery_freezes_a_market_that_was_open_when_the_process_died(
    deterministic_engine: MarketEngine,
):
    """A restart must not silently run the clock on while nobody could trade."""
    await open_market(seconds=400)
    await deterministic_engine.recover()

    async with session_scope() as session:
        state = (await session.execute(select(MarketStateRow))).scalar_one()
    assert state.state is MarketState.FROZEN
    assert state.frozen_remaining_seconds is not None
    assert "restart" in state.banner


@pytest.mark.usefixtures("no_fees")
async def test_resting_orders_are_swept_by_the_tick(deterministic_engine: MarketEngine):
    """A limit order left resting fills on a later tick, through the same path."""
    from app.engine import matching
    from app.models import MarketState as MS
    from app.models import OrderSide, Team

    await open_market()
    await make_instrument("TESTCO", "100", daily_vol_pct="0")
    team_id = await make_team()

    async with session_scope() as session:
        team = (await session.execute(select(Team).where(Team.id == team_id))).scalar_one()
        outcome = await matching.submit_order(
            session,
            team=team,
            member_id=None,
            symbol="TESTCO",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            qty=10,
            limit_price=Decimal("95"),
            market_state=MS.OPEN,
            day_no=1,
        )
        await session.flush()
        order_id = outcome.order.id

    now = utcnow()
    async with session_scope() as session:
        session.add(
            PriceAction(
                symbol="TESTCO",
                kind=PriceActionKind.JUMP,
                params={"pct": "-8", "anchor_price": "100"},
                price_before=Decimal("100"),
                started_at=now,
                ends_at=now,
            )
        )

    await deterministic_engine.tick()

    async with session_scope() as session:
        order = (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()
    assert order.status is OrderStatus.FILLED
    assert order.avg_price <= Decimal("95")
