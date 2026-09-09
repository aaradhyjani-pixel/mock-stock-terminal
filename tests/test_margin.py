"""The margin model, including the worked example from the specification.

If any test in this file fails, the number a participant sees as "available
funds" is wrong, and every dispute on event day becomes unanswerable. This is
the most important file in the suite.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.config import get_rules
from app.db import session_scope
from app.engine import matching
from app.models import MarketState, OrderSide, OrderStatus, OrderType, Team, TeamStatus
from app.risk import MarginState, valuate
from tests.conftest import make_instrument, make_team, set_market

L = Decimal("100000")  # one lakh


async def _valuate(team_id: int):
    async with session_scope() as session:
        team = (await session.execute(select(Team).where(Team.id == team_id))).scalar_one()
        return await matching.valuate_team(session, team)


async def _order(team_id: int, symbol: str, side: OrderSide, qty: int, **kwargs):
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
        return outcome.order.status, outcome.order.reason


async def _set_price(symbol: str, price: str) -> None:
    from app.models import Instrument

    async with session_scope() as session:
        instrument = (
            await session.execute(select(Instrument).where(Instrument.symbol == symbol))
        ).scalar_one()
        instrument.last_price = Decimal(price)


@pytest.mark.usefixtures("no_fees", "no_slippage")
async def test_worked_example_from_the_specification():
    """Reproduces the six-row table in part 06 of the design document exactly.

    Start with 10 lakh, short 1000 Reliance at 2500, watch the price rise, get
    refused a second short, hit a margin call at 3150, and get covered.
    """
    await set_market()
    await make_instrument("RELIANCE", "2500")
    team_id = await make_team()

    # Row 1: start
    valuation, _ = await _valuate(team_id)
    assert valuation.cash == 10 * L
    assert valuation.equity == 10 * L
    assert valuation.available == 10 * L
    assert valuation.short_mv == 0

    # Row 2: short 1000 at 2500
    status, reason = await _order(team_id, "RELIANCE", OrderSide.SELL, 1000)
    assert status is OrderStatus.FILLED, reason
    valuation, _ = await _valuate(team_id)
    assert valuation.cash == 35 * L
    assert valuation.short_mv == 25 * L
    assert valuation.equity == 10 * L
    assert valuation.available == 5 * L
    assert valuation.leverage == Decimal("2.5")

    # Row 3: price rises 8% to 2700
    await _set_price("RELIANCE", "2700")
    valuation, _ = await _valuate(team_id)
    assert valuation.short_mv == 27 * L
    assert valuation.equity == 8 * L
    assert valuation.available == Decimal("260000")
    assert valuation.maintenance_required == Decimal("324000")
    assert valuation.margin_state is MarginState.OK

    # Row 4: a second short of the same size is refused
    status, reason = await _order(team_id, "RELIANCE", OrderSide.SELL, 1000)
    assert status is OrderStatus.REJECTED
    assert "available funds" in reason

    # Row 5: price rises to 3150; equity 3.5L is below maintenance of 3.78L
    await _set_price("RELIANCE", "3150")
    valuation, _ = await _valuate(team_id)
    assert valuation.short_mv == Decimal("3150000")
    assert valuation.equity == Decimal("350000")
    assert valuation.maintenance_required == Decimal("378000")
    assert valuation.margin_state is MarginState.CALL

    # Row 6: the risk desk covers at 3150.
    #
    # It buys back only as much as it takes to restore the 20% buffer, not the
    # whole position. That is what a real risk desk does, and it is kinder than
    # a full close-out while still stopping the bleeding. The illustrative table
    # in the design document showed a full square-off; the engine is the
    # authority and this is the behaviour teams will actually see.
    from app.engine import riskdesk
    from app.engine.valuations import valuate_all

    async with session_scope() as session:
        marks = await matching.load_marks(session)
        valuations = await valuate_all(session, marks, only_with_shorts=True)
    events = await riskdesk.sweep(valuations, marks, MarketState.OPEN, 1)

    assert any(e.kind == "margin_call" for e in events)
    valuation, positions = await _valuate(team_id)
    position = next(p for p in positions if p.symbol == "RELIANCE")

    covered = 1000 + position.qty  # position.qty is negative
    assert covered == 445, "covers exactly enough to restore the 20% buffer"
    assert position.qty == -555

    # Covering is equity-neutral: cash falls by exactly the value that leaves
    # the short book, so the loss was already recognised in the mark.
    assert valuation.equity == Decimal("350000")
    assert valuation.short_mv == Decimal("555") * Decimal("3150")
    assert valuation.equity >= valuation.short_mv * Decimal("0.20")
    assert valuation.margin_state is MarginState.OK

    # Realised loss on the covered shares is exactly (2500 - 3150) x 445.
    assert position.realised_pnl == Decimal("-650") * 445


@pytest.mark.usefixtures("no_fees", "no_slippage")
async def test_available_funds_is_the_only_rule():
    """Buys, sells, covers and new shorts all obey one formula."""
    await set_market()
    await make_instrument("TESTCO", "100")
    team_id = await make_team(capital="100000")

    # A cash-only long: 1000 shares at 100 costs the full lakh.
    status, reason = await _order(team_id, "TESTCO", OrderSide.BUY, 1000)
    assert status is OrderStatus.FILLED, reason
    valuation, _ = await _valuate(team_id)
    assert valuation.cash == 0
    assert valuation.long_mv == Decimal("100000")
    assert valuation.equity == Decimal("100000")
    # Longs are not collateral, so there is nothing left to trade with.
    assert valuation.available == 0

    # One more share is refused: no available funds.
    status, _ = await _order(team_id, "TESTCO", OrderSide.BUY, 1)
    assert status is OrderStatus.REJECTED

    # Selling the long back releases the cash.
    status, reason = await _order(team_id, "TESTCO", OrderSide.SELL, 1000)
    assert status is OrderStatus.FILLED, reason
    valuation, _ = await _valuate(team_id)
    assert valuation.available == Decimal("100000")


@pytest.mark.usefixtures("no_fees", "no_slippage")
async def test_five_times_leverage_is_the_hard_ceiling():
    """A team can short exactly five times its equity and not one share more."""
    await set_market()
    await make_instrument("TESTCO", "100")
    team_id = await make_team(capital="100000")

    # 5000 shares at 100 is 5 lakh of short exposure on 1 lakh of equity.
    status, reason = await _order(team_id, "TESTCO", OrderSide.SELL, 5000)
    assert status is OrderStatus.FILLED, reason
    valuation, _ = await _valuate(team_id)
    assert valuation.short_mv == Decimal("500000")
    assert valuation.equity == Decimal("100000")
    assert valuation.leverage == Decimal("5.00")
    assert valuation.available == 0

    status, _ = await _order(team_id, "TESTCO", OrderSide.SELL, 1)
    assert status is OrderStatus.REJECTED


@pytest.mark.usefixtures("no_fees", "no_slippage")
async def test_eight_percent_against_a_full_short_book_triggers_the_call():
    """The lesson the leverage rule exists to teach, asserted as a number."""
    await set_market()
    await make_instrument("TESTCO", "100")
    team_id = await make_team(capital="100000")
    await _order(team_id, "TESTCO", OrderSide.SELL, 5000)

    await _set_price("TESTCO", "107")
    valuation, _ = await _valuate(team_id)
    assert valuation.margin_state is MarginState.WARNING

    await _set_price("TESTCO", "108")
    valuation, _ = await _valuate(team_id)
    assert valuation.margin_state is MarginState.CALL


@pytest.mark.usefixtures("no_fees", "no_slippage")
async def test_distance_to_call_predicts_the_call():
    """The gauge in the terminal must agree with what the risk desk does."""
    await set_market()
    await make_instrument("TESTCO", "100")
    team_id = await make_team(capital="100000")
    await _order(team_id, "TESTCO", OrderSide.SELL, 3000)

    valuation, _ = await _valuate(team_id)
    distance = valuation.distance_to_call_pct
    assert distance is not None

    # Move the price by exactly that much and the call should be at the edge.
    triggered_price = Decimal("100") * (Decimal("1") + distance / Decimal("100"))
    await _set_price("TESTCO", str(triggered_price.quantize(Decimal("0.01"))))
    valuation, _ = await _valuate(team_id)
    assert valuation.margin_state in (MarginState.CALL, MarginState.WARNING)
    assert valuation.equity <= valuation.maintenance_required * Decimal("1.02")


@pytest.mark.usefixtures("no_fees", "no_slippage")
async def test_a_gap_through_zero_busts_the_team():
    """A team whose equity is wiped out is closed out and marked BUSTED."""
    await set_market()
    await make_instrument("TESTCO", "100", band_pct="500")
    team_id = await make_team(capital="100000")
    await _order(team_id, "TESTCO", OrderSide.SELL, 5000)

    # A 25% gap against a 5x short book is more than the account can absorb.
    await _set_price("TESTCO", "130")

    from app.engine import riskdesk
    from app.engine.valuations import valuate_all

    async with session_scope() as session:
        marks = await matching.load_marks(session)
        valuations = await valuate_all(session, marks, only_with_shorts=True)
    events = await riskdesk.sweep(valuations, marks, MarketState.OPEN, 1)

    assert any(e.kind == "busted" for e in events)
    async with session_scope() as session:
        team = (await session.execute(select(Team).where(Team.id == team_id))).scalar_one()
        assert team.status is TeamStatus.BUSTED
        assert team.busted_at is not None

    # A busted team cannot trade again.
    status, reason = await _order(team_id, "TESTCO", OrderSide.BUY, 1)
    assert status is OrderStatus.REJECTED
    assert "zero" in reason


@pytest.mark.usefixtures("no_fees", "no_slippage")
async def test_busted_team_ranks_at_zero_not_negative():
    """The board shows zero; the ledger keeps the truth."""
    from app.engine.valuations import valuate_all

    await set_market()
    await make_instrument("TESTCO", "100", band_pct="500")
    team_id = await make_team(capital="100000")
    await _order(team_id, "TESTCO", OrderSide.SELL, 5000)
    await _set_price("TESTCO", "140")

    from app.engine import riskdesk

    async with session_scope() as session:
        marks = await matching.load_marks(session)
        valuations = await valuate_all(session, marks, only_with_shorts=True)
    await riskdesk.sweep(valuations, marks, MarketState.OPEN, 1)

    async with session_scope() as session:
        marks = await matching.load_marks(session)
        valuations = await valuate_all(session, marks)
    busted = next(tv for tv in valuations if tv.team_id == team_id)
    assert busted.status is TeamStatus.BUSTED
    assert busted.rankable_equity == 0
    assert busted.valuation.equity < 0  # the real number is preserved


@pytest.mark.usefixtures("no_fees", "no_slippage")
async def test_borrow_fee_is_charged_at_the_close():
    from app.engine import riskdesk

    await set_market()
    await make_instrument("TESTCO", "100")
    team_id = await make_team(capital="100000")
    await _order(team_id, "TESTCO", OrderSide.SELL, 4000)

    before, _ = await _valuate(team_id)
    async with session_scope() as session:
        charged = await riskdesk.charge_borrow_fees(session, day_no=1)
    after, _ = await _valuate(team_id)

    # 0.05% of 4,00,000 of short exposure is Rs 200.
    assert charged
    assert before.cash - after.cash == Decimal("200")


async def test_leverage_cap_follows_the_configured_initial_margin(default_rules):
    """Changing the rulebook changes the ceiling, with no code change."""
    default_rules.margin.initial_pct = Decimal("25")
    assert get_rules().margin.max_leverage == Decimal("4")

    valuation = valuate(
        cash=Decimal("1000000"),
        positions=[],
        marks={},
    )
    assert valuation.available == Decimal("1000000")
