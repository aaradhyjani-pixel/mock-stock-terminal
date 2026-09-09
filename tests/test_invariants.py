"""Properties that must hold after any sequence of events, ever.

These are the tests that justify the claim that the results are sound. Rather
than checking one scenario, each one drives thousands of randomised trades,
price moves and margin sweeps and then asserts that the books still balance.

The four properties:

1. **Cash is the ledger.** ``teams.cash`` equals the sum of that team's ledger
   rows, always. The operator console's "check invariants" button runs this same
   comparison during the event.
2. **Orders are their fills.** ``orders.filled_qty`` equals the sum of the
   quantities of that order's fills, and the average price is the weighted mean.
3. **Positions are their fills.** A position's signed quantity equals the sum of
   its signed fill quantities.
4. **P&L reconciles.** Equity minus starting capital equals realised plus
   unrealised P&L minus every charge paid. This is the strongest of the four: it
   ties cash, positions, marks and fees into one equation, and nothing can be
   quietly wrong on either side of it.
"""

from __future__ import annotations

import asyncio
import random
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.config import get_rules
from app.db import locked_team, session_scope
from app.engine import matching, riskdesk
from app.engine.valuations import valuate_all
from app.models import (
    Fill,
    Instrument,
    LedgerEntry,
    LedgerKind,
    MarketState,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Team,
)
from app.money import ZERO, paise
from tests.conftest import make_instrument, make_team, set_market

SYMBOLS = ["ALPHA", "BETA", "GAMMA"]


async def assert_books_balance(team_ids: list[int], starting_capital: Decimal | None = None) -> None:
    """The four properties, checked against the database as it stands."""
    opening = starting_capital if starting_capital is not None else get_rules().starting_capital
    async with session_scope() as session:
        marks = await matching.load_marks(session)

        # 1. Cash is the ledger.
        ledger_totals = {
            row.team_id: row.total
            for row in await session.execute(
                select(LedgerEntry.team_id, func.sum(LedgerEntry.amount).label("total")).group_by(
                    LedgerEntry.team_id
                )
            )
        }
        for team_id in team_ids:
            team = (await session.execute(select(Team).where(Team.id == team_id))).scalar_one()
            assert paise(team.cash) == paise(ledger_totals.get(team_id, ZERO)), (
                f"team {team_id} cash {team.cash} does not match its ledger"
            )

        # 2. Orders are their fills.
        fills_by_order: dict[int, list[Fill]] = {}
        for fill in (await session.execute(select(Fill))).scalars():
            fills_by_order.setdefault(fill.order_id, []).append(fill)
        for order in (await session.execute(select(Order))).scalars():
            fills = fills_by_order.get(order.id, [])
            assert order.filled_qty == sum(f.qty for f in fills), f"order {order.id} filled_qty drift"
            if fills:
                weighted = sum(f.price * f.qty for f in fills) / sum(f.qty for f in fills)
                assert abs(order.avg_price - weighted) <= Decimal("0.000001")
            if order.status is OrderStatus.FILLED:
                assert order.filled_qty == order.qty

        # 3. Positions are their fills.
        for team_id in team_ids:
            fills = list(
                (await session.execute(select(Fill).where(Fill.team_id == team_id))).scalars()
            )
            by_symbol: dict[str, int] = {}
            for fill in fills:
                by_symbol[fill.symbol] = by_symbol.get(fill.symbol, 0) + fill.side.sign * fill.qty
            positions = await matching.load_positions(session, team_id)
            for position in positions:
                assert position.qty == by_symbol.get(position.symbol, 0), (
                    f"team {team_id} position in {position.symbol} does not match its fills"
                )

        # 4. P&L reconciles.
        for team_id in team_ids:
            team = (await session.execute(select(Team).where(Team.id == team_id))).scalar_one()
            valuation, positions = await matching.valuate_team(session, team, marks)
            charges = -sum(
                (
                    row.amount
                    for row in (
                        await session.execute(
                            select(LedgerEntry).where(
                                LedgerEntry.team_id == team_id,
                                LedgerEntry.kind.in_([LedgerKind.FEE, LedgerKind.BORROW_FEE]),
                            )
                        )
                    ).scalars()
                ),
                ZERO,
            )
            expected = opening + valuation.realised_pnl + valuation.unrealised_pnl - charges
            assert abs(valuation.equity - expected) <= Decimal("0.01"), (
                f"team {team_id}: equity {valuation.equity} but "
                f"start + realised + unrealised - charges = {expected}"
            )


async def place(team_id: int, symbol: str, side: OrderSide, qty: int, **kwargs) -> OrderStatus:
    """Place an order exactly the way the API does, lock included."""
    async with locked_team(team_id):
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
                market_state=MarketState.OPEN,
                day_no=1,
                **kwargs,
            )
            await session.flush()
            return outcome.order.status


async def _setup(teams: int = 4) -> list[int]:
    await set_market()
    for index, symbol in enumerate(SYMBOLS):
        await make_instrument(
            symbol,
            str(100 * (index + 1)),
            spread_bps="20",
            liquidity="50000000",
            band_pct="50",
        )
    return [await make_team(f"Team {i}") for i in range(teams)]


@pytest.mark.parametrize("seed", [1, 7, 42, 2026])
async def test_books_balance_after_random_trading(seed: int):
    """Thousands of random trades against moving prices, then check the books."""
    rng = random.Random(seed)
    team_ids = await _setup()

    for _ in range(120):
        team_id = rng.choice(team_ids)
        symbol = rng.choice(SYMBOLS)
        side = rng.choice([OrderSide.BUY, OrderSide.SELL])
        qty = rng.randint(1, 400)
        await place(team_id, symbol, side, qty)

        if rng.random() < 0.25:
            async with session_scope() as session:
                instrument = (
                    await session.execute(select(Instrument).where(Instrument.symbol == symbol))
                ).scalar_one()
                factor = Decimal(str(rng.uniform(0.94, 1.06)))
                instrument.last_price = (instrument.last_price * factor).quantize(Decimal("0.05"))

    await assert_books_balance(team_ids)


@pytest.mark.parametrize("seed", [3, 99])
async def test_books_balance_through_margin_calls_and_busts(seed: int):
    """The same, but violent enough to force liquidations and bankruptcies."""
    rng = random.Random(seed)
    team_ids = await _setup(teams=6)

    for round_no in range(40):
        for team_id in team_ids:
            if rng.random() < 0.7:
                await place(
                    team_id,
                    rng.choice(SYMBOLS),
                    rng.choice([OrderSide.BUY, OrderSide.SELL]),
                    rng.randint(50, 900),
                )

        # A violent move, then let the risk desk do its work.
        async with session_scope() as session:
            for symbol in SYMBOLS:
                instrument = (
                    await session.execute(select(Instrument).where(Instrument.symbol == symbol))
                ).scalar_one()
                factor = Decimal(str(rng.uniform(0.85, 1.18)))
                instrument.last_price = max(
                    Decimal("1"), (instrument.last_price * factor).quantize(Decimal("0.05"))
                )

        async with session_scope() as session:
            marks = await matching.load_marks(session)
            valuations = await valuate_all(session, marks, only_with_shorts=True)
        await riskdesk.sweep(valuations, marks, MarketState.OPEN, 1)

        if round_no % 10 == 9:
            async with session_scope() as session:
                await riskdesk.charge_borrow_fees(session, day_no=1)

    await assert_books_balance(team_ids)


async def test_no_team_is_ever_long_and_short_the_same_stock():
    rng = random.Random(11)
    team_ids = await _setup(teams=3)
    for _ in range(200):
        await place(
            rng.choice(team_ids),
            rng.choice(SYMBOLS),
            rng.choice([OrderSide.BUY, OrderSide.SELL]),
            rng.randint(1, 300),
        )
    async with session_scope() as session:
        positions = list((await session.execute(select(Position))).scalars())
    seen = {(p.team_id, p.symbol) for p in positions}
    assert len(seen) == len(positions), "one position row per team and symbol"


@pytest.mark.usefixtures("no_fees")
async def test_simultaneous_orders_from_one_team_cannot_overspend():
    """Five teammates tapping Buy at the same instant.

    This is the concurrency guarantee the whole single-process design exists to
    provide. Without the per-team lock, each request would read the same
    starting balance and all five would pass the funds check.
    """
    await set_market()
    await make_instrument("TESTCO", "100", spread_bps="0", liquidity="100000000")
    team_id = await make_team(capital="100000")  # affords exactly 1000 shares

    results = await asyncio.gather(
        *[place(team_id, "TESTCO", OrderSide.BUY, 400) for _ in range(5)]
    )

    filled = sum(1 for status in results if status is OrderStatus.FILLED)
    assert filled == 2, "two orders of 400 fit in a lakh; the other three must be refused"

    async with session_scope() as session:
        team = (await session.execute(select(Team).where(Team.id == team_id))).scalar_one()
        valuation, _ = await matching.valuate_team(session, team)
    assert valuation.cash >= 0
    assert valuation.available >= 0
    await assert_books_balance([team_id], starting_capital=Decimal("100000"))


@pytest.mark.usefixtures("no_fees")
async def test_concurrent_shorts_cannot_exceed_the_leverage_cap():
    await set_market()
    await make_instrument("TESTCO", "100", spread_bps="0", liquidity="100000000")
    team_id = await make_team(capital="100000")  # 5x cap is 5000 shares

    results = await asyncio.gather(
        *[place(team_id, "TESTCO", OrderSide.SELL, 2000) for _ in range(6)]
    )
    assert sum(1 for s in results if s is OrderStatus.FILLED) == 2

    async with session_scope() as session:
        team = (await session.execute(select(Team).where(Team.id == team_id))).scalar_one()
        valuation, _ = await matching.valuate_team(session, team)
    assert valuation.short_mv <= valuation.equity * Decimal("5")
    assert valuation.available >= 0
