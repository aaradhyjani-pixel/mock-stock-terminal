"""The risk desk: margin warnings, forced covers, bust, and the borrow fee.

This runs on every tick. A broker's risk desk does not negotiate and neither
does this one: when equity falls below the maintenance requirement the engine
buys the shorts back at market, worst loser first, until the account is back
above the target. There is no grace period, because a grace period in a
25-minute trading day is the whole day.

Every action it takes is a real order, tagged ``MARGIN``, visible in the team's
order list and the operator's blotter. Nothing happens to a team's book that
they cannot see and explain afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_rules
from ..db import locked_team, session_scope
from ..models import (
    Instrument,
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
from ..money import ZERO, paise, pct
from ..risk import MarginState, liquidation_plan, valuate
from . import matching
from .valuations import TeamValuation


@dataclass
class RiskEvent:
    team_id: int
    kind: str  # margin_warning | margin_call | busted
    message: str
    payload: dict = field(default_factory=dict)


async def sweep(
    valuations: list[TeamValuation],
    marks: dict[str, Decimal],
    market_state: MarketState,
    day_no: int,
    *,
    warned: set[int] | None = None,
) -> list[RiskEvent]:
    """Check every team and act on the ones in trouble.

    ``valuations`` comes from the bulk pass, so the common case (nobody in
    trouble) costs no extra queries at all. Only teams that need action open a
    transaction, and each one holds its own lock through commit.
    """
    events: list[RiskEvent] = []
    warned = warned if warned is not None else set()

    for tv in valuations:
        if tv.status is not TeamStatus.ACTIVE:
            continue
        state = tv.valuation.margin_state

        if state is MarginState.OK:
            warned.discard(tv.team_id)
            continue

        if state is MarginState.WARNING:
            if tv.team_id not in warned:
                warned.add(tv.team_id)
                distance = tv.valuation.distance_to_call_pct
                events.append(
                    RiskEvent(
                        team_id=tv.team_id,
                        kind="margin_warning",
                        message=(
                            "Margin warning. Your shorts are close to a forced cover. "
                            + (
                                f"Another {distance.quantize(Decimal('0.1'))}% against you triggers it."
                                if distance is not None
                                else "Reduce your short exposure or add cash by closing longs."
                            )
                        ),
                        payload=tv.valuation.as_dict(),
                    )
                )
            continue

        # CALL or BUST: act.
        events.extend(await _liquidate_team(tv.team_id, marks, market_state, day_no))
        warned.discard(tv.team_id)

    return events


async def _liquidate_team(
    team_id: int, marks: dict[str, Decimal], market_state: MarketState, day_no: int
) -> list[RiskEvent]:
    """Cover a team's shorts until it is back above the target, or it is bust."""
    events: list[RiskEvent] = []
    rules = get_rules().margin

    async with locked_team(team_id):
        async with session_scope() as session:
            team = (await session.execute(select(Team).where(Team.id == team_id))).scalar_one_or_none()
            if team is None or team.status is not TeamStatus.ACTIVE:
                return events

            positions = await matching.load_positions(session, team_id)
            valuation = valuate(team.cash, positions, marks)
            if valuation.margin_state not in (MarginState.CALL, MarginState.BUST):
                return events

            plan = liquidation_plan(valuation, positions, marks, rules)
            covered: list[dict] = []

            for leg in plan:
                instrument = (
                    await session.execute(select(Instrument).where(Instrument.symbol == leg.symbol))
                ).scalar_one_or_none()
                if instrument is None or not instrument.status.tradable:
                    # A halted stock cannot be covered yet. The cover fires on
                    # the tick after it resumes.
                    continue

                order = Order(
                    team_id=team.id,
                    member_id=None,
                    symbol=leg.symbol,
                    side=OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    qty=leg.qty,
                    status=OrderStatus.PENDING,
                    tag=OrderTag.MARGIN,
                    day_no=day_no,
                    reason="Forced cover: margin call",
                )
                session.add(order)
                await session.flush()

                fills = await matching.try_execute(
                    session,
                    team=team,
                    order=order,
                    instrument=instrument,
                    market_state=market_state,
                    day_no=day_no,
                    enforce_slippage=False,
                    enforce_funds=False,
                )
                if fills:
                    covered.append(
                        {
                            "symbol": leg.symbol,
                            "qty": sum(f.fill.qty for f in fills),
                            "price": str(fills[-1].fill.price),
                        }
                    )

            positions = await matching.load_positions(session, team_id)
            valuation = valuate(team.cash, positions, marks)

            if covered:
                lines = ", ".join(f"{c['qty']} {c['symbol']} at {c['price']}" for c in covered)
                events.append(
                    RiskEvent(
                        team_id=team_id,
                        kind="margin_call",
                        message=f"Margin call. The exchange covered {lines} to restore your margin.",
                        payload={"covered": covered, **valuation.as_dict()},
                    )
                )

            if valuation.equity <= 0:
                await _bust_team(session, team, marks, market_state, day_no)
                events.append(
                    RiskEvent(
                        team_id=team_id,
                        kind="busted",
                        message=(
                            "Your account value reached zero. All positions are closed and trading "
                            "is over for your team. You stay on the leaderboard at zero."
                        ),
                        payload={"equity": "0"},
                    )
                )

    return events


async def _bust_team(
    session: AsyncSession, team: Team, marks: dict[str, Decimal], market_state: MarketState, day_no: int
) -> None:
    """Close everything and mark the team out."""
    positions = await matching.load_positions(session, team.id)
    for position in positions:
        if position.qty == 0:
            continue
        instrument = (
            await session.execute(select(Instrument).where(Instrument.symbol == position.symbol))
        ).scalar_one_or_none()
        if instrument is None or not instrument.status.tradable:
            continue
        side = OrderSide.SELL if position.qty > 0 else OrderSide.BUY
        order = Order(
            team_id=team.id,
            symbol=position.symbol,
            side=side,
            order_type=OrderType.MARKET,
            qty=abs(position.qty),
            status=OrderStatus.PENDING,
            tag=OrderTag.SQUARE_OFF,
            day_no=day_no,
            reason="Account value reached zero",
        )
        session.add(order)
        await session.flush()
        await matching.try_execute(
            session,
            team=team,
            order=order,
            instrument=instrument,
            market_state=market_state,
            day_no=day_no,
            enforce_slippage=False,
            enforce_funds=False,
        )

    # Cancel anything still working.
    for order in await matching.working_orders(session, team_id=team.id):
        order.status = OrderStatus.CANCELLED
        order.reason = "Team out of the competition"

    team.status = TeamStatus.BUSTED
    team.busted_at = utcnow()


async def charge_borrow_fees(session: AsyncSession, day_no: int) -> list[dict]:
    """Charge every open short a day's borrow fee. Runs at each close.

    Small enough not to matter for a trade held for ten minutes, large enough
    that carrying a big short book across every day of the competition costs
    something, which is the point.
    """
    rules = get_rules().margin
    if rules.borrow_fee_pct_per_day <= 0:
        return []

    marks = await matching.load_marks(session)
    shorts = list((await session.execute(select(Position).where(Position.qty < 0))).scalars())
    charged: list[dict] = []

    by_team: dict[int, list[Position]] = {}
    for position in shorts:
        by_team.setdefault(position.team_id, []).append(position)

    for team_id, positions in by_team.items():
        team = (await session.execute(select(Team).where(Team.id == team_id))).scalar_one_or_none()
        if team is None or team.status is not TeamStatus.ACTIVE:
            continue
        total = ZERO
        detail = []
        for position in positions:
            mark = marks.get(position.symbol)
            if mark is None:
                continue
            value = mark * abs(position.qty)
            fee = paise(pct(value, rules.borrow_fee_pct_per_day))
            if fee <= 0:
                continue
            total += fee
            detail.append({"symbol": position.symbol, "qty": abs(position.qty), "fee": str(fee)})
        if total > 0:
            matching.post_ledger(
                session,
                team,
                LedgerKind.BORROW_FEE,
                -total,
                note=f"Overnight borrow fee, day {day_no}",
                day_no=day_no,
            )
            charged.append({"team_id": team_id, "total": str(total), "detail": detail})

    return charged


def summarise(valuation_dict: dict) -> str:
    return (
        f"equity {valuation_dict['equity']}, short {valuation_dict['short_mv']}, "
        f"available {valuation_dict['available']}"
    )

