"""The order path: netting, order types, circuits, idempotency and slippage."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db import session_scope
from app.engine import matching, pricing
from app.models import (
    Instrument,
    InstrumentStatus,
    MarketState,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Team,
)
from tests.conftest import make_instrument, make_team, set_market


async def order(team_id: int, symbol: str, side: OrderSide, qty: int, **kwargs):
    async with session_scope() as session:
        team = (await session.execute(select(Team).where(Team.id == team_id))).scalar_one()
        outcome = await matching.submit_order(
            session,
            team=team,
            member_id=None,
            symbol=symbol,
            side=side,
            order_type=kwargs.pop("order_type", OrderType.MARKET),
            qty=qty,
            market_state=kwargs.pop("market_state", MarketState.OPEN),
            day_no=1,
            **kwargs,
        )
        await session.flush()
        return outcome.order.id, outcome.order.status, outcome.order.reason


async def position_of(team_id: int, symbol: str) -> Position | None:
    async with session_scope() as session:
        return (
            await session.execute(
                select(Position).where(Position.team_id == team_id, Position.symbol == symbol)
            )
        ).scalar_one_or_none()


async def set_price(symbol: str, price: str) -> None:
    async with session_scope() as session:
        instrument = (
            await session.execute(select(Instrument).where(Instrument.symbol == symbol))
        ).scalar_one()
        instrument.last_price = Decimal(price)


# ------------------------------------------------------------------- netting


@pytest.mark.usefixtures("no_fees", "no_slippage")
async def test_position_netting_through_zero():
    """Opening, adding, reducing and flipping a position."""
    await set_market()
    await make_instrument("TESTCO", "100")
    team_id = await make_team(capital="10000000")

    await order(team_id, "TESTCO", OrderSide.BUY, 100)
    position = await position_of(team_id, "TESTCO")
    assert position.qty == 100
    assert position.avg_cost == Decimal("100")

    # Add at a higher price: the average moves.
    await set_price("TESTCO", "200")
    await order(team_id, "TESTCO", OrderSide.BUY, 100)
    position = await position_of(team_id, "TESTCO")
    assert position.qty == 200
    assert position.avg_cost == Decimal("150")

    # Reduce: realises P&L, leaves the average alone.
    await set_price("TESTCO", "250")
    await order(team_id, "TESTCO", OrderSide.SELL, 50)
    position = await position_of(team_id, "TESTCO")
    assert position.qty == 150
    assert position.avg_cost == Decimal("150")
    assert position.realised_pnl == Decimal("5000")  # (250 - 150) x 50

    # Flip through zero: closes 150 long, opens 50 short at the fill price.
    await order(team_id, "TESTCO", OrderSide.SELL, 200)
    position = await position_of(team_id, "TESTCO")
    assert position.qty == -50
    assert position.avg_cost == Decimal("250")
    assert position.realised_pnl == Decimal("5000") + Decimal("100") * 150


@pytest.mark.usefixtures("no_fees", "no_slippage")
async def test_a_sell_with_no_position_opens_a_short():
    await set_market()
    await make_instrument("TESTCO", "100")
    team_id = await make_team()

    await order(team_id, "TESTCO", OrderSide.SELL, 500)
    position = await position_of(team_id, "TESTCO")
    assert position.qty == -500
    assert position.is_short


@pytest.mark.usefixtures("no_fees", "no_slippage")
async def test_a_buy_covers_a_short_before_going_long():
    await set_market()
    await make_instrument("TESTCO", "100")
    team_id = await make_team()

    await order(team_id, "TESTCO", OrderSide.SELL, 100)
    await order(team_id, "TESTCO", OrderSide.BUY, 150)
    position = await position_of(team_id, "TESTCO")
    assert position.qty == 50, "covered 100, then went long 50"


# --------------------------------------------------------------- order types


@pytest.mark.usefixtures("no_fees")
async def test_a_limit_order_rests_then_fills_when_the_price_comes_to_it():
    await set_market()
    await make_instrument("TESTCO", "100")
    team_id = await make_team()

    order_id, status, _ = await order(
        team_id, "TESTCO", OrderSide.BUY, 10, order_type=OrderType.LIMIT, limit_price=Decimal("95")
    )
    assert status is OrderStatus.PENDING, "not marketable at 100"

    await set_price("TESTCO", "94")
    async with session_scope() as session:
        team = (await session.execute(select(Team).where(Team.id == team_id))).scalar_one()
        working = (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()
        instrument = (
            await session.execute(select(Instrument).where(Instrument.symbol == "TESTCO"))
        ).scalar_one()
        fills = await matching.try_execute(
            session,
            team=team,
            order=working,
            instrument=instrument,
            market_state=MarketState.OPEN,
            day_no=1,
        )
        assert fills
        assert working.status is OrderStatus.FILLED
        assert working.avg_price <= Decimal("95")


@pytest.mark.usefixtures("no_fees")
async def test_a_marketable_limit_never_fills_worse_than_its_limit():
    await set_market()
    await make_instrument("TESTCO", "100", spread_bps="100")  # 1% wide book
    team_id = await make_team()

    _, status, reason = await order(
        team_id, "TESTCO", OrderSide.BUY, 10, order_type=OrderType.LIMIT, limit_price=Decimal("100.50")
    )
    assert status is OrderStatus.FILLED, reason
    async with session_scope() as session:
        filled = (await session.execute(select(Order))).scalars().first()
        assert filled.avg_price <= Decimal("100.50")


@pytest.mark.usefixtures("no_fees")
async def test_a_stop_loss_stays_dormant_until_its_trigger_is_crossed():
    await set_market()
    await make_instrument("TESTCO", "100")
    team_id = await make_team()
    await order(team_id, "TESTCO", OrderSide.BUY, 100)

    order_id, status, _ = await order(
        team_id,
        "TESTCO",
        OrderSide.SELL,
        100,
        order_type=OrderType.SL_M,
        trigger_price=Decimal("90"),
    )
    assert status is OrderStatus.PENDING, "dormant even though a sell would be marketable now"

    await set_price("TESTCO", "89")
    async with session_scope() as session:
        team = (await session.execute(select(Team).where(Team.id == team_id))).scalar_one()
        working = (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()
        instrument = (
            await session.execute(select(Instrument).where(Instrument.symbol == "TESTCO"))
        ).scalar_one()
        assert matching.stop_triggered(working, instrument.last_price)
        working.status = OrderStatus.TRIGGERED
        fills = await matching.try_execute(
            session,
            team=team,
            order=working,
            instrument=instrument,
            market_state=MarketState.OPEN,
            day_no=1,
        )
        assert fills
        assert working.status is OrderStatus.FILLED


# ------------------------------------------------------------------ slippage


@pytest.mark.usefixtures("no_fees")
async def test_a_large_market_order_pays_for_the_space_it_takes():
    """Slippage must make a huge order visibly worse than a small one."""
    await set_market()
    # A 50 bps spread puts the ask at 100.25. Liquidity of 1,00,250 is therefore
    # exactly 1000 shares per slice, which keeps the arithmetic below exact.
    await make_instrument("TESTCO", "100", spread_bps="50", liquidity="100250")
    await make_team(capital="100000000")

    async with session_scope() as session:
        instrument = (
            await session.execute(select(Instrument).where(Instrument.symbol == "TESTCO"))
        ).scalar_one()
        quote = pricing.quote_for(instrument)
        small = pricing.fill_quote(instrument, OrderSide.BUY, 100)
        large = pricing.fill_quote(instrument, OrderSide.BUY, 10_000)

    assert quote.ask == Decimal("100.25")
    assert small.slices == 1
    assert small.price == quote.ask, "a small order fills at the touch"
    assert large.slices == 10
    assert large.price > small.price
    assert large.slippage_pct > 0
    # The walk is capped, so even an absurd order cannot fill at an absurd price.
    absurd = pricing.fill_quote(instrument, OrderSide.BUY, 10_000_000)
    assert absurd.slippage_pct <= Decimal("10.5")


@pytest.mark.usefixtures("no_fees")
async def test_slippage_tolerance_rejects_rather_than_filling_badly():
    await set_market()
    await make_instrument("TESTCO", "100", spread_bps="50", liquidity="100000")
    team_id = await make_team(capital="100000000")

    _, status, reason = await order(
        team_id, "TESTCO", OrderSide.BUY, 10_000, slippage_tolerance_pct=Decimal("0.1")
    )
    assert status is OrderStatus.REJECTED
    assert "tolerance" in reason


# ------------------------------------------------------- halts and circuits


@pytest.mark.usefixtures("no_fees")
async def test_a_halted_stock_rejects_both_sides():
    await set_market()
    await make_instrument("TESTCO", "100")
    team_id = await make_team()
    async with session_scope() as session:
        instrument = (
            await session.execute(select(Instrument).where(Instrument.symbol == "TESTCO"))
        ).scalar_one()
        instrument.status = InstrumentStatus.HALTED
        instrument.halt_reason = "pending an announcement"

    for side in (OrderSide.BUY, OrderSide.SELL):
        _, status, reason = await order(team_id, "TESTCO", side, 10)
        assert status is OrderStatus.REJECTED
        assert "halted" in reason


@pytest.mark.usefixtures("no_fees")
async def test_upper_circuit_blocks_buys_and_allows_sells():
    await set_market()
    await make_instrument("TESTCO", "100")
    team_id = await make_team()
    async with session_scope() as session:
        instrument = (
            await session.execute(select(Instrument).where(Instrument.symbol == "TESTCO"))
        ).scalar_one()
        instrument.status = InstrumentStatus.UPPER_CIRCUIT

    _, status, reason = await order(team_id, "TESTCO", OrderSide.BUY, 10)
    assert status is OrderStatus.REJECTED
    assert "circuit" in reason.lower()

    _, status, reason = await order(team_id, "TESTCO", OrderSide.SELL, 10)
    assert status is OrderStatus.FILLED, reason


# --------------------------------------------------- validation and replay


@pytest.mark.usefixtures("no_fees")
async def test_the_market_being_closed_rejects_orders():
    await set_market(MarketState.CLOSED)
    await make_instrument("TESTCO", "100")
    team_id = await make_team()
    _, status, reason = await order(
        team_id, "TESTCO", OrderSide.BUY, 10, market_state=MarketState.CLOSED
    )
    assert status is OrderStatus.REJECTED
    assert "closed" in reason.lower()


@pytest.mark.usefixtures("no_fees")
async def test_pre_open_queues_limits_but_refuses_market_orders():
    await set_market(MarketState.PRE_OPEN)
    await make_instrument("TESTCO", "100")
    team_id = await make_team()

    _, status, _ = await order(
        team_id, "TESTCO", OrderSide.BUY, 10, market_state=MarketState.PRE_OPEN
    )
    assert status is OrderStatus.REJECTED

    _, status, reason = await order(
        team_id,
        "TESTCO",
        OrderSide.BUY,
        10,
        order_type=OrderType.LIMIT,
        limit_price=Decimal("95"),
        market_state=MarketState.PRE_OPEN,
    )
    assert status is OrderStatus.PENDING, reason


@pytest.mark.usefixtures("no_fees")
async def test_a_retried_order_does_not_buy_twice():
    """The exact failure mode of a phone on bad Wi-Fi tapping Buy again."""
    await set_market()
    await make_instrument("TESTCO", "100")
    team_id = await make_team()

    first, status, _ = await order(team_id, "TESTCO", OrderSide.BUY, 10, client_order_id="abc-123")
    assert status is OrderStatus.FILLED
    second, _, _ = await order(team_id, "TESTCO", OrderSide.BUY, 10, client_order_id="abc-123")
    assert first == second

    position = await position_of(team_id, "TESTCO")
    assert position.qty == 10, "the retry returned the original order, it did not place a new one"


@pytest.mark.usefixtures("no_fees")
async def test_prices_off_the_tick_grid_are_rejected():
    await set_market()
    await make_instrument("TESTCO", "100")
    team_id = await make_team()
    _, status, reason = await order(
        team_id, "TESTCO", OrderSide.BUY, 10, order_type=OrderType.LIMIT, limit_price=Decimal("99.99")
    )
    assert status is OrderStatus.REJECTED
    assert "multiple of" in reason


@pytest.mark.usefixtures("no_fees")
async def test_an_oversized_order_is_rejected_with_the_limit_named(default_rules):
    await set_market()
    await make_instrument("TESTCO", "100")
    team_id = await make_team(capital="1000000000")
    _, status, reason = await order(
        team_id, "TESTCO", OrderSide.BUY, default_rules.market.max_order_qty + 1
    )
    assert status is OrderStatus.REJECTED
    assert "per order" in reason


@pytest.mark.usefixtures("no_fees")
async def test_rejected_orders_are_recorded_so_the_blotter_can_explain_them():
    await set_market()
    await make_instrument("TESTCO", "100")
    team_id = await make_team(capital="100")
    await order(team_id, "TESTCO", OrderSide.BUY, 1000)

    async with session_scope() as session:
        rejected = list((await session.execute(select(Order))).scalars())
    assert len(rejected) == 1
    assert rejected[0].status is OrderStatus.REJECTED
    assert rejected[0].reason
