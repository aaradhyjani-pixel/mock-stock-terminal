"""Final results, tie-breaks and awards.

The participant rulebook promises specific tie-break rules and five named
awards. These tests are what keep that promise honest: if the ranking here ever
disagrees with the rulebook, one of the two is wrong and somebody will find out
in front of 500 people.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db import session_scope
from app.models import EquitySnapshot, MarketState, OrderSide, Team, TeamStatus, utcnow
from app.results import (
    compute_awards,
    compute_results,
    equity_sparkline,
    max_drawdown,
    risk_adjusted_score,
    team_report,
)
from tests.conftest import make_instrument, make_team, set_market

L = Decimal("100000")


async def add_snapshots(team_id: int, equities: list[str], *, closes: set[int] | None = None) -> None:
    """Write an equity series for a team, one snapshot a minute."""
    closes = closes or set()
    base = utcnow() - timedelta(minutes=len(equities))
    async with session_scope() as session:
        for index, value in enumerate(equities):
            session.add(
                EquitySnapshot(
                    team_id=team_id,
                    ts=base + timedelta(minutes=index),
                    cash=Decimal(value),
                    long_mv=Decimal("0"),
                    short_mv=Decimal("0"),
                    equity=Decimal(value),
                    day_no=index + 1,
                    is_close=index in closes,
                )
            )


# --------------------------------------------------------------- drawdown


def test_drawdown_measures_the_largest_fall_from_a_peak():
    fall, pct = max_drawdown([Decimal(v) for v in ("100", "120", "90", "110", "80")])
    # The peak was 120 and the trough after it was 80.
    assert fall == Decimal("40")
    assert pct.quantize(Decimal("0.01")) == Decimal("33.33")


def test_a_curve_that_only_rises_has_no_drawdown():
    fall, pct = max_drawdown([Decimal(v) for v in ("100", "110", "125", "180")])
    assert fall == 0
    assert pct == 0


def test_drawdown_of_an_empty_or_single_point_curve_is_zero():
    assert max_drawdown([]) == (Decimal("0"), Decimal("0"))
    assert max_drawdown([Decimal("100")]) == (Decimal("0"), Decimal("0"))


def test_risk_adjusted_score_needs_variation_to_mean_anything():
    opening = Decimal("1000")

    # Two different daily returns: there is a spread, so there is a score.
    assert risk_adjusted_score([Decimal("1100"), Decimal("1300")], opening) is not None

    # Exactly 10% on both days. No standard deviation at all, so there is no
    # score rather than a division by zero: a flat line is not skill.
    assert risk_adjusted_score([Decimal("1100"), Decimal("1210")], opening) is None

    # Fewer than two days gives nothing to compare.
    assert risk_adjusted_score([Decimal("1100")], opening) is None
    assert risk_adjusted_score([], opening) is None


def test_steady_gains_score_higher_than_erratic_ones():
    opening = Decimal("1000")
    steady = risk_adjusted_score(
        [Decimal("1050"), Decimal("1102"), Decimal("1157"), Decimal("1215")], opening
    )
    erratic = risk_adjusted_score(
        [Decimal("1300"), Decimal("900"), Decimal("1400"), Decimal("1215")], opening
    )
    assert steady is not None and erratic is not None
    assert steady > erratic, "the same journey with less drama is the better score"


# ------------------------------------------------------------- tie-breaks


async def test_ranking_is_by_account_value():
    await set_market(MarketState.FINAL)
    first = await make_team("Alpha")
    second = await make_team("Bravo")
    await add_snapshots(first, ["1000000", "1200000"])
    await add_snapshots(second, ["1000000", "1500000"])

    async with session_scope() as session:
        results = await compute_results(session)

    assert [r.team for r in results] == ["Bravo", "Alpha"]
    assert results[0].rank == 1
    assert results[0].pnl == 5 * L


async def test_a_tie_is_broken_by_lower_drawdown():
    """Two teams finish level. The one who got there calmly wins."""
    await set_market(MarketState.FINAL)
    calm = await make_team("Calm")
    wild = await make_team("Wild")

    await add_snapshots(calm, ["1000000", "1100000", "1200000"])
    await add_snapshots(wild, ["1000000", "600000", "1200000"])

    async with session_scope() as session:
        results = await compute_results(session)

    assert [r.team for r in results] == ["Calm", "Wild"]
    assert results[0].max_drawdown == 0
    assert results[1].max_drawdown == 4 * L


@pytest.mark.usefixtures("no_fees", "no_slippage")
async def test_a_tie_on_drawdown_is_broken_by_fewer_trades():
    """Level on value and on drawdown: the one who churned less wins."""
    from app.engine import matching
    from app.models import OrderType

    await set_market(MarketState.OPEN)
    await make_instrument("TESTCO", "100")
    patient = await make_team("Patient")
    busy = await make_team("Busy")

    async def trade(team_id: int, times: int) -> None:
        for _ in range(times):
            async with session_scope() as session:
                team = (await session.execute(select(Team).where(Team.id == team_id))).scalar_one()
                await matching.submit_order(
                    session, team=team, member_id=None, symbol="TESTCO",
                    side=OrderSide.BUY, order_type=OrderType.MARKET, qty=1,
                    market_state=MarketState.OPEN, day_no=1,
                )

    await trade(patient, 1)
    await trade(busy, 6)

    # Identical, monotonic equity curves: no drawdown for either.
    await add_snapshots(patient, ["1000000", "1100000"])
    await add_snapshots(busy, ["1000000", "1100000"])

    async with session_scope() as session:
        results = await compute_results(session)

    assert [r.team for r in results] == ["Patient", "Busy"]
    assert results[0].trades == 1
    assert results[1].trades == 6


async def test_busted_teams_rank_last_at_zero_longest_survivor_first():
    await set_market(MarketState.FINAL)
    survivor = await make_team("Survivor")
    early = await make_team("EarlyOut")
    late = await make_team("LateOut")

    await add_snapshots(survivor, ["1000000", "400000"])
    await add_snapshots(early, ["1000000", "0"])
    await add_snapshots(late, ["1000000", "0"])

    now = utcnow()
    async with session_scope() as session:
        for team_id, when in ((early, now - timedelta(minutes=30)), (late, now)):
            team = (await session.execute(select(Team).where(Team.id == team_id))).scalar_one()
            team.status = TeamStatus.BUSTED
            team.busted_at = when

    async with session_scope() as session:
        results = await compute_results(session)

    assert [r.team for r in results] == ["Survivor", "LateOut", "EarlyOut"]
    assert results[1].final_equity == 0, "a busted team shows zero, whatever the ledger says"
    assert results[2].final_equity == 0


# ----------------------------------------------------------------- awards


@pytest.mark.usefixtures("no_fees", "no_slippage")
async def test_awards_pick_the_right_team_from_a_constructed_field():
    from app.engine import matching
    from app.models import OrderType

    await set_market(MarketState.OPEN)
    await make_instrument("TESTCO", "100")

    steady = await make_team("Steady")
    swinger = await make_team("Swinger")
    shorter = await make_team("Shorter")

    # Steady climbs evenly; Swinger gets to a similar place via a big dip.
    await add_snapshots(steady, ["1050000", "1102000", "1157000", "1215000"], closes={0, 1, 2, 3})
    await add_snapshots(swinger, ["1300000", "700000", "1400000", "1210000"], closes={0, 1, 2, 3})
    await add_snapshots(shorter, ["1000000", "1010000", "1020000", "1030000"], closes={0, 1, 2, 3})

    # Shorter makes a large profit covering a short: sell high, buy back low.
    async def trade(team_id, side, qty, price):
        async with session_scope() as session:
            from app.models import Instrument

            instrument = (
                await session.execute(select(Instrument).where(Instrument.symbol == "TESTCO"))
            ).scalar_one()
            instrument.last_price = Decimal(price)
            team = (await session.execute(select(Team).where(Team.id == team_id))).scalar_one()
            await matching.submit_order(
                session, team=team, member_id=None, symbol="TESTCO", side=side,
                order_type=OrderType.MARKET, qty=qty, market_state=MarketState.OPEN, day_no=1,
            )

    await trade(shorter, OrderSide.SELL, 1000, "500")
    await trade(shorter, OrderSide.BUY, 1000, "300")  # covered 200 lower: +2,00,000

    async with session_scope() as session:
        results = await compute_results(session)
        awards = {a.key: a for a in compute_awards(results)}

    assert awards["risk_adjusted"].team == "Steady", "even gains beat a rollercoaster"
    assert awards["best_short"].team == "Shorter"
    assert awards["best_trade"].team == "Shorter"
    assert Decimal(next(r for r in results if r.team == "Shorter").best_short) == Decimal("200000")


async def test_awards_are_empty_rather_than_wrong_when_nobody_qualifies():
    """No trades at all means no award, not an arbitrary winner."""
    await set_market(MarketState.FINAL)
    await make_team("Idle")
    async with session_scope() as session:
        results = await compute_results(session)
        awards = {a.key: a for a in compute_awards(results)}

    assert awards["best_trade"].team is None
    assert awards["best_short"].team is None
    assert awards["most_disciplined"].team is None


# ------------------------------------------------------------ report card


@pytest.mark.usefixtures("no_fees", "no_slippage")
async def test_a_team_report_card_has_their_numbers_and_the_podium():
    await set_market(MarketState.FINAL)
    mine = await make_team("Mine")
    await make_team("Other")
    await add_snapshots(mine, ["1000000", "1300000"], closes={1})

    async with session_scope() as session:
        report = await team_report(session, mine)

    assert report is not None
    assert report["field_size"] == 2
    assert report["result"]["team"] == "Mine"
    assert report["result"]["final_equity"] == "1300000.00"
    assert len(report["result"]["equity_curve"]) == 2
    assert len(report["podium"]) == 2


def test_the_sparkline_survives_a_curve_with_no_points():
    assert "svg" in equity_sparkline([])
    assert "polyline" not in equity_sparkline([])


def test_the_sparkline_draws_a_rising_curve_green():
    now = utcnow()
    curve = [(now, Decimal("100")), (now + timedelta(minutes=1), Decimal("200"))]
    svg = equity_sparkline(curve)
    assert "polyline" in svg
    assert "#10864f" in svg, "rising curves are green"

    falling = [(now, Decimal("200")), (now + timedelta(minutes=1), Decimal("100"))]
    assert "#c4342f" in equity_sparkline(falling)


async def test_the_risk_adjusted_award_is_not_given_for_losing_steadily():
    """A negative score is not a performance worth a prize."""
    await set_market(MarketState.FINAL)
    sinker = await make_team("Sinker")
    # Down every day, and evenly, which would otherwise score well on a ratio
    # that only measures consistency.
    await add_snapshots(sinker, ["950000", "890000", "840000", "780000"], closes={0, 1, 2, 3})

    async with session_scope() as session:
        results = await compute_results(session)
        awards = {a.key: a for a in compute_awards(results)}

    assert results[0].risk_adjusted is not None
    assert results[0].risk_adjusted < 0
    assert awards["risk_adjusted"].team is None


@pytest.mark.usefixtures("no_fees", "no_slippage")
async def test_a_team_with_no_snapshot_is_marked_live_not_at_its_opening_balance():
    """Finalising snapshots everybody, so this is the abnormal path. Reporting
    a team's opening balance as their final result would be quietly wrong in
    exactly the way nobody checks."""
    from app.engine import matching
    from app.models import Instrument, OrderType

    await set_market(MarketState.OPEN)
    await make_instrument("TESTCO", "100")
    team_id = await make_team("Trader")

    async with session_scope() as session:
        team = (await session.execute(select(Team).where(Team.id == team_id))).scalar_one()
        await matching.submit_order(
            session, team=team, member_id=None, symbol="TESTCO", side=OrderSide.BUY,
            order_type=OrderType.MARKET, qty=1000, market_state=MarketState.OPEN, day_no=1,
        )

    # The position doubles in value, and no snapshot is ever taken.
    async with session_scope() as session:
        instrument = (
            await session.execute(select(Instrument).where(Instrument.symbol == "TESTCO"))
        ).scalar_one()
        instrument.last_price = Decimal("200")

    async with session_scope() as session:
        results = await compute_results(session)

    assert results[0].final_equity == Decimal("1100000"), "marked live, not left at the opening balance"
