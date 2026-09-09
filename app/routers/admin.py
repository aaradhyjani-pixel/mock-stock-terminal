"""The operator console API.

Two rules shape this module.

**Everything is logged.** Every write here records an audit row with who did it,
what changed, and the before and after values, written in the same transaction
as the change itself. If a result is challenged after the event, the answer is
in one table.

**Nothing reverses a trade.** Operators can undo a price action, freeze the
market and adjust a team's cash with a second operator's approval, but a fill
that happened, happened. Reversing trades after the fact turns every dispute
into a negotiation, and there is no version of that which ends well with 500
participants in a hall.
"""

from __future__ import annotations

import csv
import html
import io
from datetime import timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_rules, reload_rules
from ..db import get_db, locked_team, session_scope
from ..engine.market import get_engine
from ..engine.matching import load_marks, load_positions, post_ledger, valuate_team
from ..engine.valuations import rank, valuate_all
from ..results import compute_awards, compute_results, equity_sparkline
from ..models import (
    Adjustment,
    AuditLog,
    Candle,
    EquitySnapshot,
    Fill,
    Instrument,
    InstrumentStatus,
    LedgerEntry,
    LedgerKind,
    MarketState,
    Member,
    MemberRole,
    NewsItem,
    NewsKind,
    OperatorRole,
    Order,
    OrderStatus,
    Position,
    PriceAction,
    PriceActionKind,
    Scenario,
    ScenarioStep,
    Team,
    TeamStatus,
    Tick,
    utcnow,
)
from ..money import ZERO, fmt_inr, money, paise, round_to_tick
from ..schemas import (
    AdjustmentRequest,
    BandRequest,
    BroadcastRequest,
    DividendRequest,
    FreezeRequest,
    HaltRequest,
    InstrumentUpsert,
    JumpRequest,
    MoveRequest,
    NewsRequest,
    SplitRequest,
    TeamImportRequest,
    VolatilityRequest,
    fill_row,
    instrument_row,
    ledger_row,
    news_row,
    order_row,
    position_row,
)
from ..security import (
    OperatorIdentity,
    generate_code,
    generate_password,
    hash_password,
    require_roles,
)
from ..ws import hub

router = APIRouter(prefix="/api/admin", tags=["admin"])

MARKET_OPS = require_roles(OperatorRole.MARKET_OPERATOR)
NEWS_OPS = require_roles(OperatorRole.NEWS_DESK)
DESK_OPS = require_roles(OperatorRole.HELP_DESK, OperatorRole.MARKET_OPERATOR, OperatorRole.NEWS_DESK)
VIEW_OPS = require_roles(
    OperatorRole.HELP_DESK,
    OperatorRole.MARKET_OPERATOR,
    OperatorRole.NEWS_DESK,
    OperatorRole.JUDGE,
    OperatorRole.PROJECTOR,
)
ADMIN_ONLY = require_roles()


def audit(
    session: AsyncSession,
    identity: OperatorIdentity,
    action: str,
    target: str | None = None,
    before: dict | None = None,
    after: dict | None = None,
) -> None:
    session.add(
        AuditLog(
            actor_type="operator",
            actor_id=identity.operator.id,
            actor_name=identity.operator.name,
            action=action,
            target=target,
            before=before,
            after=after,
        )
    )


# ------------------------------------------------------------ market control


@router.post("/market/pre-open")
async def pre_open(identity: OperatorIdentity = Depends(MARKET_OPS)):
    engine = get_engine()
    async with session_scope() as session:
        state = await engine.get_state(session)
        if state.state is MarketState.FINAL:
            raise HTTPException(400, "The competition is over. Reset the event to run it again.")
        before = {"state": state.state.value, "day": state.day_no}
        await engine.start_day(session, state, state.day_no + 1)
        audit(session, identity, "market.pre_open", f"day {state.day_no}", before, {"state": "PRE_OPEN"})
        return engine.state_payload(state)


@router.post("/market/open")
async def open_market(identity: OperatorIdentity = Depends(MARKET_OPS)):
    engine = get_engine()
    async with session_scope() as session:
        state = await engine.get_state(session)
        if state.state is MarketState.FINAL:
            raise HTTPException(400, "The competition is over.")
        if state.day_no == 0:
            await engine.start_day(session, state, 1)
        before = {"state": state.state.value}
        await engine.open_trading(session, state)
        audit(session, identity, "market.open", f"day {state.day_no}", before, {"state": "OPEN"})
        return engine.state_payload(state)


@router.post("/market/close")
async def close_market(identity: OperatorIdentity = Depends(MARKET_OPS)):
    engine = get_engine()
    async with session_scope() as session:
        state = await engine.get_state(session)
        before = {"state": state.state.value}
        await engine.close_day(session, state)
        audit(session, identity, "market.close", f"day {state.day_no}", before, {"state": "CLOSED"})
        return engine.state_payload(state)


@router.post("/market/halt")
async def halt_market(
    payload: BroadcastRequest, identity: OperatorIdentity = Depends(MARKET_OPS)
):
    engine = get_engine()
    async with session_scope() as session:
        state = await engine.get_state(session)
        before = {"state": state.state.value}
        state.state = MarketState.HALTED
        state.banner = payload.message
        state.banner_severity = "warning"
        await hub.broadcast_public("market_state", engine.state_payload(state), durable=True)
        audit(session, identity, "market.halt", None, before, {"state": "HALTED"})
        return engine.state_payload(state)


@router.post("/market/freeze")
async def freeze_market(payload: FreezeRequest, identity: OperatorIdentity = Depends(MARKET_OPS)):
    """Stop everything, clock included. The first move in any incident."""
    if payload.confirm.strip().upper() != "FREEZE":
        raise HTTPException(400, "Type FREEZE to confirm. This stops trading for every team.")
    engine = get_engine()
    async with session_scope() as session:
        state = await engine.get_state(session)
        before = {"state": state.state.value}
        await engine.freeze(session, state, payload.message)
        audit(session, identity, "market.freeze", None, before, {"state": "FROZEN"})
        return engine.state_payload(state)


@router.post("/market/resume")
async def resume_market(
    to: str = Query(default="OPEN", pattern="^(OPEN|PRE_OPEN|CLOSED)$"),
    identity: OperatorIdentity = Depends(MARKET_OPS),
):
    engine = get_engine()
    async with session_scope() as session:
        state = await engine.get_state(session)
        if state.state is not MarketState.FROZEN:
            raise HTTPException(400, "The market is not frozen.")
        before = {"state": state.state.value, "remaining": state.frozen_remaining_seconds}
        await engine.resume(session, state, MarketState(to))
        audit(session, identity, "market.resume", None, before, {"state": to})
        return engine.state_payload(state)


@router.post("/market/finalise")
async def finalise_market(
    confirm: str = Query(...), identity: OperatorIdentity = Depends(ADMIN_ONLY)
):
    if confirm.strip().upper() != "FINAL":
        raise HTTPException(400, "Type FINAL to confirm. This locks the results permanently.")
    engine = get_engine()
    async with session_scope() as session:
        state = await engine.get_state(session)
        await engine.finalise(session, state)
        audit(session, identity, "market.finalise", None, None, {"state": "FINAL"})
        return engine.state_payload(state)


@router.post("/market/broadcast")
async def broadcast(payload: BroadcastRequest, identity: OperatorIdentity = Depends(DESK_OPS)):
    async with session_scope() as session:
        state = await get_engine().get_state(session)
        state.banner = payload.message
        state.banner_severity = payload.severity
        audit(session, identity, "market.broadcast", payload.severity, None, {"message": payload.message})
    await hub.broadcast_public(
        "announcement", {"message": payload.message, "severity": payload.severity}, durable=True
    )
    return {"ok": True}


@router.post("/market/blackout")
async def set_blackout(on: bool = True, identity: OperatorIdentity = Depends(MARKET_OPS)):
    engine = get_engine()
    async with session_scope() as session:
        state = await engine.get_state(session)
        before = {"blackout": state.leaderboard_blackout}
        state.leaderboard_blackout = on
        audit(session, identity, "market.blackout", None, before, {"blackout": on})
        await hub.broadcast_public("market_state", engine.state_payload(state), durable=True)
        return {"blackout": on}


# ---------------------------------------------------------------- price desk


@router.post("/prices/move")
async def price_move(payload: MoveRequest, identity: OperatorIdentity = Depends(MARKET_OPS)):
    """Walk a price to a target over a duration. The default price control."""
    if payload.pct is None and payload.target_price is None:
        raise HTTPException(400, "Give either a percentage or a target price.")
    if payload.target_price is not None and not payload.symbol:
        raise HTTPException(400, "A target price applies to one stock. Use a percentage for a sector.")

    async with session_scope() as session:
        instruments = await _resolve_scope(session, payload.symbol, payload.sector)
        if not instruments:
            raise HTTPException(404, "No listed stock matches that symbol or sector.")
        now = utcnow()
        created = []
        for instrument in instruments:
            action = PriceAction(
                symbol=instrument.symbol,
                sector=payload.sector,
                kind=PriceActionKind.MOVE if payload.over_seconds > 0 else PriceActionKind.JUMP,
                params={
                    "pct": str(payload.pct) if payload.pct is not None else None,
                    "target_price": str(payload.target_price) if payload.target_price is not None else None,
                    "anchor_price": str(instrument.last_price),
                },
                price_before=instrument.last_price,
                started_at=now,
                ends_at=now + timedelta(seconds=payload.over_seconds) if payload.over_seconds else now,
                operator_id=identity.operator.id,
                note=payload.note,
            )
            session.add(action)
            created.append(instrument.symbol)
        audit(
            session,
            identity,
            "price.move",
            payload.symbol or payload.sector or "ALL",
            None,
            {"pct": str(payload.pct), "over_seconds": payload.over_seconds, "symbols": created},
        )
    return {"ok": True, "symbols": created, "over_seconds": payload.over_seconds}


@router.post("/prices/jump")
async def price_jump(payload: JumpRequest, identity: OperatorIdentity = Depends(MARKET_OPS)):
    """Move a price instantly. Needs the symbol typed back to confirm.

    A jump creates an arbitrage window for anyone watching closely, so it is
    reserved for opening gaps and scripted shocks. The confirmation step exists
    because the cost of a mistyped jump is paid by every team at once.
    """
    if payload.confirm.strip().upper() != payload.symbol:
        raise HTTPException(400, f"Type {payload.symbol} to confirm this instant price change.")
    if payload.pct is None and payload.target_price is None:
        raise HTTPException(400, "Give either a percentage or a target price.")

    async with session_scope() as session:
        instrument = await _one(session, payload.symbol)
        before = {"last": str(instrument.last_price), "status": instrument.status.value}
        if payload.pct is not None and abs(payload.pct) > 15:
            audit(session, identity, "price.jump.large", payload.symbol, before, {"pct": str(payload.pct)})
        session.add(
            PriceAction(
                symbol=instrument.symbol,
                kind=PriceActionKind.JUMP,
                params={
                    "pct": str(payload.pct) if payload.pct is not None else None,
                    "target_price": str(payload.target_price) if payload.target_price is not None else None,
                    "anchor_price": str(instrument.last_price),
                },
                price_before=instrument.last_price,
                started_at=utcnow(),
                ends_at=utcnow(),
                operator_id=identity.operator.id,
                note=payload.note,
            )
        )
        audit(session, identity, "price.jump", payload.symbol, before, {"pct": str(payload.pct)})
    return {"ok": True, "symbol": payload.symbol}


@router.post("/prices/volatility")
async def price_volatility(
    payload: VolatilityRequest, identity: OperatorIdentity = Depends(MARKET_OPS)
):
    async with session_scope() as session:
        instruments = await _resolve_scope(session, payload.symbol, payload.sector)
        if not instruments:
            raise HTTPException(404, "No listed stock matches that symbol or sector.")
        now = utcnow()
        for instrument in instruments:
            session.add(
                PriceAction(
                    symbol=instrument.symbol,
                    sector=payload.sector,
                    kind=PriceActionKind.VOLATILITY,
                    params={"multiplier": str(payload.multiplier)},
                    started_at=now,
                    ends_at=now + timedelta(seconds=payload.over_seconds) if payload.over_seconds else None,
                    operator_id=identity.operator.id,
                )
            )
        audit(
            session,
            identity,
            "price.volatility",
            payload.symbol or payload.sector or "ALL",
            None,
            {"multiplier": str(payload.multiplier), "over_seconds": payload.over_seconds},
        )
    return {"ok": True, "symbols": [i.symbol for i in instruments]}


@router.post("/prices/halt")
async def halt_instrument(payload: HaltRequest, identity: OperatorIdentity = Depends(MARKET_OPS)):
    async with session_scope() as session:
        instrument = await _one(session, payload.symbol)
        before = {"status": instrument.status.value}
        instrument.status = InstrumentStatus.HALTED
        instrument.halt_reason = payload.reason or "pending an announcement"
        session.add(
            PriceAction(
                symbol=instrument.symbol,
                kind=PriceActionKind.HALT,
                params={"reason": instrument.halt_reason},
                price_before=instrument.last_price,
                started_at=utcnow(),
                operator_id=identity.operator.id,
            )
        )
        audit(session, identity, "price.halt", payload.symbol, before, {"status": "HALTED"})
        await hub.broadcast_public(
            "instrument_status",
            {"symbol": instrument.symbol, "status": "HALTED", "reason": instrument.halt_reason},
            durable=True,
        )
    return {"ok": True}


@router.post("/prices/resume")
async def resume_instrument(payload: HaltRequest, identity: OperatorIdentity = Depends(MARKET_OPS)):
    async with session_scope() as session:
        instrument = await _one(session, payload.symbol)
        before = {"status": instrument.status.value}
        instrument.status = InstrumentStatus.ACTIVE
        instrument.halt_reason = None
        audit(session, identity, "price.resume", payload.symbol, before, {"status": "ACTIVE"})
        await hub.broadcast_public(
            "instrument_status", {"symbol": instrument.symbol, "status": "ACTIVE"}, durable=True
        )
    return {"ok": True}


@router.post("/prices/band")
async def set_band(payload: BandRequest, identity: OperatorIdentity = Depends(MARKET_OPS)):
    engine = get_engine()
    async with session_scope() as session:
        state = await engine.get_state(session)
        if state.state is MarketState.OPEN:
            raise HTTPException(
                400,
                "Bands can only be changed while the market is not open. "
                "Halt or close first, so no order is priced against a band that moved under it.",
            )
        instrument = await _one(session, payload.symbol)
        before = {"band_pct": str(instrument.band_pct)}
        instrument.band_pct = payload.band_pct
        audit(session, identity, "price.band", payload.symbol, before, {"band_pct": str(payload.band_pct)})
    return {"ok": True}


@router.post("/prices/undo")
async def undo_price_action(
    symbol: str | None = None, identity: OperatorIdentity = Depends(MARKET_OPS)
):
    """Revert the most recent price action, optionally for one stock.

    The price goes back. Fills that happened at the wrong price stand, and the
    response lists them so an operator can decide whether a cash adjustment is
    warranted. That decision belongs to people, in a break, not to this endpoint.
    """
    async with session_scope() as session:
        stmt = (
            select(PriceAction)
            .where(PriceAction.undone_at.is_(None), PriceAction.kind.in_(
                [PriceActionKind.MOVE, PriceActionKind.JUMP, PriceActionKind.VOLATILITY]
            ))
            .order_by(PriceAction.started_at.desc())
            .limit(1)
        )
        if symbol:
            stmt = stmt.where(PriceAction.symbol == symbol.upper())
        action = (await session.execute(stmt)).scalar_one_or_none()
        if action is None:
            raise HTTPException(404, "There is no price action to undo.")

        affected_fills = []
        if action.symbol and action.price_before is not None:
            instrument = await _one(session, action.symbol)
            before = {"last": str(instrument.last_price)}
            instrument.last_price = round_to_tick(action.price_before, instrument.tick_size)
            instrument.vol_multiplier = Decimal("1")
            fills = list(
                (
                    await session.execute(
                        select(Fill).where(
                            Fill.symbol == action.symbol, Fill.ts >= action.started_at
                        )
                    )
                ).scalars()
            )
            affected_fills = [fill_row(f) for f in fills]
            audit(
                session,
                identity,
                "price.undo",
                action.symbol,
                before,
                {"last": str(instrument.last_price), "affected_fills": len(affected_fills)},
            )

        action.undone_at = utcnow()

    return {
        "ok": True,
        "symbol": action.symbol,
        "restored_price": str(action.price_before) if action.price_before else None,
        "affected_fills": affected_fills,
        "note": "Fills stand. Review these and raise a cash adjustment if the club decides one is due.",
    }


@router.get("/prices/actions")
async def list_price_actions(
    session: AsyncSession = Depends(get_db), _: OperatorIdentity = Depends(VIEW_OPS)
):
    now = utcnow()
    rows = list(
        (
            await session.execute(
                select(PriceAction).order_by(PriceAction.started_at.desc()).limit(60)
            )
        ).scalars()
    )
    out = []
    for action in rows:
        ends_at = action.ends_at
        active = (
            action.undone_at is None
            and action.cancelled_at is None
            and ends_at is not None
            and ends_at.replace(tzinfo=ends_at.tzinfo or now.tzinfo) > now
        )
        out.append(
            {
                "id": action.id,
                "symbol": action.symbol,
                "sector": action.sector,
                "kind": action.kind.value,
                "params": action.params,
                "price_before": str(action.price_before) if action.price_before else None,
                "started_at": action.started_at.isoformat(),
                "ends_at": ends_at.isoformat() if ends_at else None,
                "active": active,
                "undone": action.undone_at is not None,
                "note": action.note,
            }
        )
    return {"actions": out}


# --------------------------------------------------------------------- news


@router.post("/news")
async def publish_news(payload: NewsRequest, identity: OperatorIdentity = Depends(NEWS_OPS)):
    async with session_scope() as session:
        state = await get_engine().get_state(session)
        scheduled = payload.publish_at is not None and payload.publish_at > utcnow()
        item = NewsItem(
            headline=payload.headline,
            body=payload.body,
            kind=NewsKind(payload.kind.upper()),
            symbols=payload.symbols,
            sectors=payload.sectors,
            sentiment=payload.sentiment,
            publish_at=payload.publish_at,
            published_at=None if scheduled else utcnow(),
            author_id=identity.operator.id,
            day_no=state.day_no,
        )
        session.add(item)
        await session.flush()
        audit(session, identity, "news.publish", item.headline[:80], None, {"id": item.id})
        row = news_row(item, for_operator=True)
        publish_now = not scheduled

    if publish_now:
        await hub.broadcast_public("news", {k: v for k, v in row.items() if k != "sentiment"}, durable=True)
    return row


@router.post("/news/{news_id}/retract")
async def retract_news(news_id: int, identity: OperatorIdentity = Depends(NEWS_OPS)):
    """Mark a headline retracted. It stays visible, struck through.

    Deleting a headline that teams have already traded on would rewrite history
    they can remember. A retraction is itself information, and in a market that
    is exactly how it works.
    """
    async with session_scope() as session:
        item = (await session.execute(select(NewsItem).where(NewsItem.id == news_id))).scalar_one_or_none()
        if item is None:
            raise HTTPException(404, "No such news item.")
        item.retracted_at = utcnow()
        audit(session, identity, "news.retract", item.headline[:80], None, {"id": news_id})
        row = news_row(item, for_operator=True)
    await hub.broadcast_public("news_retracted", {"id": news_id}, durable=True)
    return row


@router.get("/news")
async def list_all_news(
    session: AsyncSession = Depends(get_db), _: OperatorIdentity = Depends(VIEW_OPS)
):
    items = list(
        (await session.execute(select(NewsItem).order_by(NewsItem.created_at.desc()).limit(200))).scalars()
    )
    return {"news": [news_row(i, for_operator=True) for i in items]}


# ---------------------------------------------------------------- scenarios


@router.get("/scenarios")
async def list_scenarios(
    session: AsyncSession = Depends(get_db), _: OperatorIdentity = Depends(VIEW_OPS)
):
    scenarios = list((await session.execute(select(Scenario).order_by(Scenario.day_no))).scalars())
    out = []
    for scenario in scenarios:
        steps = list(
            (
                await session.execute(
                    select(ScenarioStep)
                    .where(ScenarioStep.scenario_id == scenario.id)
                    .order_by(ScenarioStep.at_offset)
                )
            ).scalars()
        )
        out.append(
            {
                "id": scenario.id,
                "name": scenario.name,
                "day_no": scenario.day_no,
                "status": scenario.status,
                "description": scenario.description,
                "steps": [
                    {
                        "id": s.id,
                        "at_offset": s.at_offset,
                        "label": s.label,
                        "payload": s.payload,
                        "fired_at": s.fired_at.isoformat() if s.fired_at else None,
                        "skipped": s.skipped,
                    }
                    for s in steps
                ],
            }
        )
    return {"scenarios": out}


@router.post("/scenarios/{scenario_id}/play")
async def play_scenario(scenario_id: int, identity: OperatorIdentity = Depends(MARKET_OPS)):
    async with session_scope() as session:
        scenario = await _scenario(session, scenario_id)
        now = utcnow()
        if scenario.status == "PAUSED" and scenario.paused_at is not None:
            paused_at = scenario.paused_at
            if paused_at.tzinfo is None:
                paused_at = paused_at.replace(tzinfo=now.tzinfo)
            scenario.paused_offset_seconds += int((now - paused_at).total_seconds())
        else:
            scenario.started_at = now
            scenario.paused_offset_seconds = 0
        scenario.paused_at = None
        scenario.status = "PLAYING"
        audit(session, identity, "scenario.play", scenario.name)
        return {"ok": True, "status": scenario.status}


@router.post("/scenarios/{scenario_id}/pause")
async def pause_scenario(scenario_id: int, identity: OperatorIdentity = Depends(MARKET_OPS)):
    async with session_scope() as session:
        scenario = await _scenario(session, scenario_id)
        scenario.status = "PAUSED"
        scenario.paused_at = utcnow()
        audit(session, identity, "scenario.pause", scenario.name)
        return {"ok": True, "status": scenario.status}


@router.post("/scenarios/steps/{step_id}/fire")
async def fire_step(step_id: int, identity: OperatorIdentity = Depends(MARKET_OPS)):
    engine = get_engine()
    async with session_scope() as session:
        step = (
            await session.execute(select(ScenarioStep).where(ScenarioStep.id == step_id))
        ).scalar_one_or_none()
        if step is None:
            raise HTTPException(404, "No such step.")
        state = await engine.get_state(session)
        await engine.fire_step(session, state, step)
        audit(session, identity, "scenario.fire_step", step.label or str(step_id))
        return {"ok": True, "fired_at": step.fired_at.isoformat() if step.fired_at else None}


@router.post("/scenarios/steps/{step_id}/skip")
async def skip_step(step_id: int, identity: OperatorIdentity = Depends(MARKET_OPS)):
    async with session_scope() as session:
        step = (
            await session.execute(select(ScenarioStep).where(ScenarioStep.id == step_id))
        ).scalar_one_or_none()
        if step is None:
            raise HTTPException(404, "No such step.")
        step.skipped = True
        audit(session, identity, "scenario.skip_step", step.label or str(step_id))
        return {"ok": True}


# -------------------------------------------------------------------- teams


@router.get("/teams")
async def list_teams(
    session: AsyncSession = Depends(get_db), _: OperatorIdentity = Depends(VIEW_OPS)
):
    marks = await load_marks(session)
    valuations = await valuate_all(session, marks)
    ranked = rank(valuations)
    order_counts = dict(
        (row.team_id, row.n)
        for row in await session.execute(
            select(Order.team_id, func.count(Order.id).label("n")).group_by(Order.team_id)
        )
    )
    return {
        "teams": [
            {
                "rank": position,
                "team_id": tv.team_id,
                "name": tv.team_name,
                "code": tv.code,
                "status": tv.status.value,
                "orders": order_counts.get(tv.team_id, 0),
                **tv.valuation.as_dict(),
            }
            for position, tv in ranked
        ]
    }


@router.get("/teams/{team_id}")
async def team_detail(
    team_id: int, session: AsyncSession = Depends(get_db), _: OperatorIdentity = Depends(VIEW_OPS)
):
    """A team's book, exactly as they see it."""
    team = (await session.execute(select(Team).where(Team.id == team_id))).scalar_one_or_none()
    if team is None:
        raise HTTPException(404, "No such team.")
    marks = await load_marks(session)
    valuation, positions = await valuate_team(session, team, marks)
    orders = list(
        (
            await session.execute(
                select(Order).where(Order.team_id == team_id).order_by(Order.created_at.desc()).limit(100)
            )
        ).scalars()
    )
    ledger = list(
        (
            await session.execute(
                select(LedgerEntry)
                .where(LedgerEntry.team_id == team_id)
                .order_by(LedgerEntry.id.desc())
                .limit(100)
            )
        ).scalars()
    )
    members = list(
        (await session.execute(select(Member).where(Member.team_id == team_id))).scalars()
    )
    return {
        "team": {
            "id": team.id,
            "name": team.name,
            "code": team.code,
            "status": team.status.value,
            "college": team.college,
        },
        "members": [
            {
                "id": m.id,
                "name": m.name,
                "login": m.login,
                "role": m.role.value,
                "active": m.active,
                "last_seen_at": m.last_seen_at.isoformat() if m.last_seen_at else None,
            }
            for m in members
        ],
        "funds": valuation.as_dict(),
        "positions": [position_row(p, marks.get(p.symbol)) for p in positions if p.qty != 0],
        "orders": [order_row(o) for o in orders],
        "ledger": [ledger_row(row) for row in ledger],
    }


@router.post("/teams/import")
async def import_teams(payload: TeamImportRequest, identity: OperatorIdentity = Depends(ADMIN_ONLY)):
    """Create teams and members from a list, returning the credentials once.

    Passwords are shown exactly here and never again: they are stored only as
    scrypt hashes. Print the response, hand out the cards at check-in, and if a
    card is lost the help desk issues a new password rather than recovering the
    old one.
    """
    engine = get_engine()
    async with session_scope() as session:
        state = await engine.get_state(session)
        if state.state is MarketState.OPEN:
            raise HTTPException(400, "Import teams before the market opens.")

        if payload.reset_existing:
            await session.execute(delete(Member))
            await session.execute(delete(Team))

        rules = get_rules()
        created = []
        for row in payload.teams:
            code = generate_code()
            team = Team(
                name=row.team_name,
                code=code,
                cash=ZERO,
                college=row.college,
                contact_email=row.contact_email,
            )
            session.add(team)
            await session.flush()

            post_ledger(
                session,
                team,
                LedgerKind.OPENING,
                rules.starting_capital,
                note="Opening balance",
                day_no=0,
            )

            member_rows = []
            names = row.members or ["Captain"]
            for index, name in enumerate(names[: rules.max_members_per_team]):
                password = generate_password()
                login = f"{code.lower()}-{index + 1}"
                member = Member(
                    team_id=team.id,
                    name=name,
                    login=login,
                    password_hash=hash_password(password),
                    role=MemberRole.CAPTAIN if index == 0 else MemberRole.MEMBER,
                )
                session.add(member)
                member_rows.append({"name": name, "login": login, "password": password})

            created.append(
                {"team": row.team_name, "code": code, "members": member_rows}
            )

        audit(session, identity, "teams.import", f"{len(created)} teams", None, {"count": len(created)})
    return {"created": created}


@router.post("/teams/{team_id}/reset-password")
async def reset_password(
    team_id: int, member_id: int, identity: OperatorIdentity = Depends(DESK_OPS)
):
    async with session_scope() as session:
        member = (
            await session.execute(
                select(Member).where(Member.id == member_id, Member.team_id == team_id)
            )
        ).scalar_one_or_none()
        if member is None:
            raise HTTPException(404, "No such member on that team.")
        password = generate_password()
        member.password_hash = hash_password(password)
        member.token_version += 1  # kicks any existing session immediately
        audit(session, identity, "team.reset_password", f"member:{member_id}")
        return {"login": member.login, "password": password}


@router.post("/teams/{team_id}/status")
async def set_team_status(
    team_id: int, status_value: str = Query(alias="status"), identity: OperatorIdentity = Depends(ADMIN_ONLY)
):
    async with session_scope() as session:
        team = (await session.execute(select(Team).where(Team.id == team_id))).scalar_one_or_none()
        if team is None:
            raise HTTPException(404, "No such team.")
        before = {"status": team.status.value}
        team.status = TeamStatus(status_value.upper())
        audit(session, identity, "team.status", team.code, before, {"status": team.status.value})
        return {"team_id": team_id, "status": team.status.value}


@router.post("/adjustments")
async def request_adjustment(
    payload: AdjustmentRequest, identity: OperatorIdentity = Depends(DESK_OPS)
):
    """Request a cash correction. A second operator must approve it."""
    async with session_scope() as session:
        team = (await session.execute(select(Team).where(Team.id == payload.team_id))).scalar_one_or_none()
        if team is None:
            raise HTTPException(404, "No such team.")
        adjustment = Adjustment(
            team_id=payload.team_id,
            amount=paise(payload.amount),
            reason=payload.reason,
            requested_by=identity.operator.id,
        )
        session.add(adjustment)
        await session.flush()
        audit(
            session,
            identity,
            "adjustment.request",
            team.code,
            None,
            {"amount": str(payload.amount), "reason": payload.reason},
        )
        return {"id": adjustment.id, "status": "awaiting approval"}


@router.post("/adjustments/{adjustment_id}/approve")
async def approve_adjustment(adjustment_id: int, identity: OperatorIdentity = Depends(ADMIN_ONLY)):
    async with session_scope() as session:
        adjustment = (
            await session.execute(select(Adjustment).where(Adjustment.id == adjustment_id))
        ).scalar_one_or_none()
        if adjustment is None:
            raise HTTPException(404, "No such adjustment.")
        if adjustment.resolved_at is not None:
            raise HTTPException(400, "That adjustment has already been resolved.")
        if adjustment.requested_by == identity.operator.id:
            raise HTTPException(
                403,
                "A cash adjustment needs a second pair of eyes. Ask another operator to approve it.",
            )
        team_id = adjustment.team_id
        amount = adjustment.amount
        reason = adjustment.reason

    async with locked_team(team_id):
        async with session_scope() as session:
            adjustment = (
                await session.execute(select(Adjustment).where(Adjustment.id == adjustment_id))
            ).scalar_one()
            team = (await session.execute(select(Team).where(Team.id == team_id))).scalar_one()
            entry = post_ledger(
                session,
                team,
                LedgerKind.ADJUSTMENT,
                amount,
                note=f"Adjustment approved by {identity.operator.name}: {reason}",
            )
            await session.flush()
            adjustment.approved_by = identity.operator.id
            adjustment.resolved_at = utcnow()
            adjustment.ledger_id = entry.id
            audit(session, identity, "adjustment.approve", team.code, None, {"amount": str(amount)})

    await hub.to_team(
        team_id,
        "announcement",
        {"message": f"The organisers adjusted your cash by Rs {amount}. Reason: {reason}", "severity": "warning"},
    )
    return {"ok": True, "amount": str(amount)}


@router.get("/adjustments")
async def list_adjustments(
    session: AsyncSession = Depends(get_db), _: OperatorIdentity = Depends(VIEW_OPS)
):
    rows = list(
        (await session.execute(select(Adjustment).order_by(Adjustment.created_at.desc()).limit(100))).scalars()
    )
    return {
        "adjustments": [
            {
                "id": a.id,
                "team_id": a.team_id,
                "amount": str(a.amount),
                "reason": a.reason,
                "requested_by": a.requested_by,
                "approved_by": a.approved_by,
                "resolved": a.resolved_at is not None,
                "created_at": a.created_at.isoformat(),
            }
            for a in rows
        ]
    }


# --------------------------------------------------------- blotter and audit


@router.get("/blotter")
async def blotter(
    session: AsyncSession = Depends(get_db),
    _: OperatorIdentity = Depends(VIEW_OPS),
    limit: int = Query(default=200, ge=1, le=1000),
    symbol: str | None = None,
    team_id: int | None = None,
):
    stmt = select(Fill).order_by(Fill.ts.desc()).limit(limit)
    if symbol:
        stmt = stmt.where(Fill.symbol == symbol.upper())
    if team_id:
        stmt = stmt.where(Fill.team_id == team_id)
    fills = list((await session.execute(stmt)).scalars())
    teams = {
        t.id: t.name for t in (await session.execute(select(Team))).scalars()
    }
    return {
        "fills": [{**fill_row(f), "team": teams.get(f.team_id, "?")} for f in fills]
    }


@router.get("/audit")
async def audit_log(
    session: AsyncSession = Depends(get_db),
    _: OperatorIdentity = Depends(VIEW_OPS),
    limit: int = Query(default=200, ge=1, le=1000),
):
    rows = list(
        (await session.execute(select(AuditLog).order_by(AuditLog.ts.desc()).limit(limit))).scalars()
    )
    return {
        "entries": [
            {
                "id": r.id,
                "actor": f"{r.actor_name or r.actor_type}",
                "actor_type": r.actor_type,
                "action": r.action,
                "target": r.target,
                "before": r.before,
                "after": r.after,
                "ts": r.ts.isoformat() if r.ts else None,
            }
            for r in rows
        ]
    }


@router.get("/analytics")
async def analytics(
    session: AsyncSession = Depends(get_db), _: OperatorIdentity = Depends(VIEW_OPS)
):
    """What the field is doing. Read this before firing a scripted shock."""
    marks = await load_marks(session)
    valuations = await valuate_all(session, marks)

    turnover = dict(
        (row.symbol, row.value)
        for row in await session.execute(
            select(Fill.symbol, func.sum(Fill.gross).label("value")).group_by(Fill.symbol)
        )
    )
    positions_by_symbol: dict[str, dict] = {}
    for tv in valuations:
        for position in await load_positions(session, tv.team_id):
            if position.qty == 0:
                continue
            bucket = positions_by_symbol.setdefault(
                position.symbol, {"long_qty": 0, "short_qty": 0, "teams_long": 0, "teams_short": 0}
            )
            if position.qty > 0:
                bucket["long_qty"] += position.qty
                bucket["teams_long"] += 1
            else:
                bucket["short_qty"] += abs(position.qty)
                bucket["teams_short"] += 1

    equities = sorted(tv.valuation.equity for tv in valuations)
    median = equities[len(equities) // 2] if equities else ZERO

    return {
        "field": {
            "teams": len(valuations),
            "active": sum(1 for tv in valuations if tv.status is TeamStatus.ACTIVE),
            "busted": sum(1 for tv in valuations if tv.status is TeamStatus.BUSTED),
            "median_equity": str(median),
            "total_short_mv": str(money(sum((tv.valuation.short_mv for tv in valuations), ZERO))),
            "in_margin_trouble": sum(
                1 for tv in valuations if tv.valuation.margin_state.value in ("WARNING", "CALL")
            ),
        },
        "exposure": [
            {"symbol": symbol, **data, "turnover": str(turnover.get(symbol, ZERO))}
            for symbol, data in sorted(positions_by_symbol.items())
        ],
    }


@router.get("/export/{table}.csv")
async def export_csv(
    table: str, session: AsyncSession = Depends(get_db), _: OperatorIdentity = Depends(VIEW_OPS)
):
    """Everything, as a spreadsheet. Run this before announcing results."""
    writers = {
        "orders": (Order, ["id", "team_id", "member_id", "symbol", "side", "order_type", "qty",
                           "limit_price", "trigger_price", "status", "filled_qty", "avg_price",
                           "fees_total", "reason", "tag", "day_no", "created_at"]),
        "fills": (Fill, ["id", "order_id", "team_id", "symbol", "side", "qty", "price", "gross",
                         "fees_total", "realised_pnl", "day_no", "ts"]),
        "ledger": (LedgerEntry, ["id", "team_id", "kind", "amount", "balance_after", "note",
                                 "ref_type", "ref_id", "day_no", "ts"]),
        "snapshots": (EquitySnapshot, ["id", "team_id", "ts", "cash", "long_mv", "short_mv",
                                       "equity", "rank", "day_no", "is_close"]),
        "audit": (AuditLog, ["id", "actor_type", "actor_name", "action", "target", "ts"]),
    }
    if table not in writers:
        raise HTTPException(404, f"Nothing to export called '{table}'. Try {', '.join(writers)}.")

    model, columns = writers[table]
    rows = list((await session.execute(select(model))).scalars())
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(columns)
    for row in rows:
        writer.writerow(
            [
                getattr(getattr(row, column), "value", getattr(row, column))
                if getattr(row, column) is not None
                else ""
                for column in columns
            ]
        )
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{table}.csv"'},
    )


@router.get("/health")
async def health(_: OperatorIdentity = Depends(VIEW_OPS)):
    from ..db import healthcheck

    engine = get_engine()
    return {"database": await healthcheck(), "engine": engine.health}


@router.post("/invariants/check")
async def check_invariants(
    session: AsyncSession = Depends(get_db), _: OperatorIdentity = Depends(VIEW_OPS)
):
    """Recompute every team's cash from the ledger and compare.

    ``teams.cash`` is a cache. This proves the cache still matches the
    append-only truth. Run it in every break; it takes milliseconds and it is
    the single best evidence that the results are sound.
    """
    ledger_totals = {
        row.team_id: row.total
        for row in await session.execute(
            select(LedgerEntry.team_id, func.sum(LedgerEntry.amount).label("total")).group_by(
                LedgerEntry.team_id
            )
        )
    }
    problems = []
    teams = list((await session.execute(select(Team))).scalars())
    for team in teams:
        expected = paise(ledger_totals.get(team.id, ZERO))
        if paise(team.cash) != expected:
            problems.append(
                {
                    "team_id": team.id,
                    "team": team.name,
                    "cash": str(paise(team.cash)),
                    "ledger_total": str(expected),
                    "difference": str(paise(team.cash) - expected),
                }
            )
    return {"teams_checked": len(teams), "ok": not problems, "mismatches": problems}


@router.post("/config/reload")
async def reload_configuration(identity: OperatorIdentity = Depends(ADMIN_ONLY)):
    async with session_scope() as session:
        state = await get_engine().get_state(session)
        if state.state is MarketState.OPEN:
            raise HTTPException(
                400, "The rulebook cannot change while the market is open. Close or freeze first."
            )
        audit(session, identity, "config.reload")
    rules = reload_rules()
    return {"ok": True, "competition": rules.competition_name}


@router.post("/instruments")
async def upsert_instrument(
    payload: InstrumentUpsert, identity: OperatorIdentity = Depends(ADMIN_ONLY)
):
    async with session_scope() as session:
        state = await get_engine().get_state(session)
        if state.state is MarketState.OPEN:
            raise HTTPException(400, "Add or edit stocks before the market opens.")
        instrument = (
            await session.execute(select(Instrument).where(Instrument.symbol == payload.symbol))
        ).scalar_one_or_none()
        fresh = instrument is None
        if fresh:
            instrument = Instrument(
                symbol=payload.symbol,
                seed_price=payload.start_price,
                last_price=payload.start_price,
                day_reference=payload.start_price,
                prev_close=payload.start_price,
                day_open=payload.start_price,
                day_high=payload.start_price,
                day_low=payload.start_price,
                start_price=payload.start_price,
                name=payload.name,
                sector=payload.sector,
            )
            session.add(instrument)
        for field in (
            "name",
            "sector",
            "tick_size",
            "spread_bps",
            "daily_vol_pct",
            "liquidity_notional",
            "band_pct",
            "index_weight",
            "lot_size",
        ):
            setattr(instrument, field, getattr(payload, field))
        audit(session, identity, "instrument.upsert", payload.symbol, None, {"new": fresh})
        return instrument_row(instrument)


@router.post("/instruments/{symbol}/suspend")
async def suspend_instrument(symbol: str, identity: OperatorIdentity = Depends(ADMIN_ONLY)):
    async with session_scope() as session:
        instrument = await _one(session, symbol)
        before = {"status": instrument.status.value}
        instrument.status = InstrumentStatus.SUSPENDED
        await session.execute(
            update(Order)
            .where(Order.symbol == instrument.symbol, Order.status.in_([OrderStatus.PENDING, OrderStatus.TRIGGERED]))
            .values(status=OrderStatus.CANCELLED, reason=f"{instrument.symbol} suspended")
        )
        audit(session, identity, "instrument.suspend", symbol, before, {"status": "SUSPENDED"})
    await hub.broadcast_public(
        "instrument_status", {"symbol": symbol.upper(), "status": "SUSPENDED"}, durable=True
    )
    return {"ok": True}




# ------------------------------------------------------------ practice reset


@router.post("/reset-trading")
async def reset_trading(
    confirm: str = Query(...), identity: OperatorIdentity = Depends(ADMIN_ONLY)
):
    """Wipe every trade and start the competition over.

    This exists for the practice session the evening before, so that the
    rehearsal does not contaminate the real event. It clears trading data and
    restores every team to their opening balance, but keeps the teams, their
    members, the operators and the audit log: people should not have to be
    handed new cards because the organisers wanted a clean slate.

    Refused while the market is open, and confirmed by typing RESET.
    """
    if confirm.strip().upper() != "RESET":
        raise HTTPException(
            400,
            "Type RESET to confirm. This deletes every trade and returns all teams to their opening balance.",
        )

    engine = get_engine()
    rules = get_rules()

    async with session_scope() as session:
        state = await engine.get_state(session)
        if state.state is MarketState.OPEN:
            raise HTTPException(400, "Close or freeze the market before resetting.")

        teams = list((await session.execute(select(Team))).scalars())

        # Order matters only where foreign keys point; fills reference orders.
        for model in (Fill, Order, Position, LedgerEntry, EquitySnapshot, Tick, Candle,
                      NewsItem, PriceAction, Adjustment):
            await session.execute(delete(model))

        for team in teams:
            team.cash = ZERO
            team.status = TeamStatus.ACTIVE
            team.busted_at = None
            post_ledger(
                session, team, LedgerKind.OPENING, rules.starting_capital,
                note="Opening balance (competition reset)", day_no=0,
            )

        for instrument in (await session.execute(select(Instrument))).scalars():
            # From the seed price, not start_price: a split during the practice
            # session scaled start_price, and a reset has to undo that too.
            instrument.start_price = instrument.seed_price
            instrument.last_price = instrument.seed_price
            instrument.day_reference = instrument.seed_price
            instrument.prev_close = instrument.seed_price
            instrument.day_open = instrument.seed_price
            instrument.day_low = instrument.seed_price
            instrument.day_high = instrument.seed_price
            instrument.day_volume = 0
            instrument.status = InstrumentStatus.ACTIVE
            instrument.halt_reason = None
            instrument.vol_multiplier = Decimal("1")

        for scenario in (await session.execute(select(Scenario))).scalars():
            scenario.status = "IDLE"
            scenario.started_at = None
            scenario.paused_at = None
            scenario.paused_offset_seconds = 0
        await session.execute(
            update(ScenarioStep).values(fired_at=None, skipped=False)
        )

        state.state = MarketState.CLOSED
        state.day_no = 0
        state.total_days = rules.session.trading_days
        state.session_ends_at = None
        state.frozen_remaining_seconds = None
        state.index_value = rules.market.index_base
        state.index_prev_close = rules.market.index_base
        state.leaderboard_blackout = False
        state.breaker_until = None
        state.banner = "The competition has been reset. The market has not opened yet."
        state.banner_severity = "info"

        audit(
            session, identity, "competition.reset", f"{len(teams)} teams", None,
            {"teams_reset": len(teams), "starting_capital": str(rules.starting_capital)},
        )
        team_count = len(teams)
        payload = engine.state_payload(state)

    await hub.broadcast_public("market_state", payload, durable=True)
    await hub.broadcast_public(
        "announcement",
        {
            "message": "The competition has been reset. Every team is back to their opening balance.",
            "severity": "warning",
        },
        durable=True,
    )
    return {"ok": True, "teams_reset": team_count, "state": "CLOSED"}


# --------------------------------------------------------- corporate actions


async def _require_market_closed(session: AsyncSession, action: str) -> None:
    """Corporate actions change positions and prices at once.

    Doing that while the tape is live would let a fill land halfway through the
    adjustment, so both of these require the market to be stopped. In practice
    they happen between trading days, which is also when they happen in reality.
    """
    state = await get_engine().get_state(session)
    if state.state is MarketState.OPEN:
        raise HTTPException(
            400,
            f"Close, halt or freeze the market before {action}. "
            "It changes prices and positions together, and neither should move under a live order.",
        )


@router.post("/prices/dividend")
async def pay_dividend(payload: DividendRequest, identity: OperatorIdentity = Depends(MARKET_OPS)):
    """Pay a dividend: credit longs, debit shorts, mark the stock ex-dividend.

    A dividend leaves every team's account value exactly where it was at the
    moment it is paid. Holders gain cash and lose the same amount of share
    value; shorts pay it, because whoever lent them the stock is entitled to it.
    That is the whole lesson, and the test asserts it.
    """
    if payload.confirm.strip().upper() != payload.symbol:
        raise HTTPException(400, f"Type {payload.symbol} to confirm this dividend.")

    async with session_scope() as session:
        await _require_market_closed(session, "paying a dividend")
        instrument = await _one(session, payload.symbol)
        amount = paise(payload.amount_per_share)
        if amount >= instrument.last_price:
            raise HTTPException(
                400,
                f"A dividend of Rs {amount} is not less than the share price of "
                f"Rs {instrument.last_price}. Check the amount.",
            )

        positions = list(
            (
                await session.execute(
                    select(Position).where(Position.symbol == instrument.symbol, Position.qty != 0)
                )
            ).scalars()
        )

        paid: list[dict] = []
        for position in positions:
            team = (
                await session.execute(select(Team).where(Team.id == position.team_id))
            ).scalar_one_or_none()
            if team is None:
                continue
            cash = paise(amount * position.qty)  # signed: shorts pay
            post_ledger(
                session,
                team,
                LedgerKind.DIVIDEND,
                cash,
                note=(
                    f"Dividend of Rs {amount} per share on "
                    f"{abs(position.qty)} {instrument.symbol} "
                    f"({'held' if position.qty > 0 else 'short'})"
                ),
                ref_type="instrument",
                day_no=(await get_engine().get_state(session)).day_no,
            )
            paid.append({"team_id": team.id, "qty": position.qty, "amount": str(cash)})

        before = str(instrument.last_price)
        ex_price = round_to_tick(instrument.last_price - amount, instrument.tick_size)
        instrument.last_price = ex_price
        instrument.day_reference = ex_price
        instrument.prev_close = ex_price

        session.add(
            PriceAction(
                symbol=instrument.symbol,
                kind=PriceActionKind.DIVIDEND,
                params={"amount_per_share": str(amount)},
                price_before=Decimal(before),
                started_at=utcnow(),
                ends_at=utcnow(),
                operator_id=identity.operator.id,
                note=payload.note,
            )
        )
        audit(
            session,
            identity,
            "price.dividend",
            payload.symbol,
            {"last": before},
            {"amount_per_share": str(amount), "last": str(ex_price), "teams_paid": len(paid)},
        )

    await hub.broadcast_public(
        "announcement",
        {
            "message": (
                f"{payload.symbol} goes ex-dividend: Rs {payload.amount_per_share} per share. "
                "Holders are credited, short sellers are debited."
            ),
            "severity": "info",
        },
        durable=True,
    )
    return {"ok": True, "symbol": payload.symbol, "teams_paid": len(paid), "ex_price": str(ex_price)}


@router.post("/prices/split")
async def split_stock(payload: SplitRequest, identity: OperatorIdentity = Depends(MARKET_OPS)):
    """Split a stock: scale every position and the price by the same ratio.

    A ratio of 2 is a 2-for-1: twice the shares at half the price, so nobody is
    richer or poorer. Resting orders are cancelled rather than rescaled; a limit
    at 1,000 meant something at the old price and nothing at the new one, and
    silently moving somebody's order is worse than telling them why it went.
    """
    if payload.confirm.strip().upper() != payload.symbol:
        raise HTTPException(400, f"Type {payload.symbol} to confirm this split.")

    async with session_scope() as session:
        await _require_market_closed(session, "splitting a stock")
        instrument = await _one(session, payload.symbol)
        ratio = payload.ratio
        before = str(instrument.last_price)

        positions = list(
            (
                await session.execute(
                    select(Position).where(Position.symbol == instrument.symbol, Position.qty != 0)
                )
            ).scalars()
        )
        for position in positions:
            # Integer share counts: a split that would leave a fraction rounds
            # down, and the fractional remainder simply is not created. With
            # whole ratios, which is all anybody uses, this never bites.
            position.qty = int(position.qty * ratio)
            position.avg_cost = money(position.avg_cost / ratio)

        for price_field in ("last_price", "day_reference", "prev_close", "day_open", "day_high", "day_low", "start_price"):
            current = getattr(instrument, price_field)
            setattr(instrument, price_field, round_to_tick(current / ratio, instrument.tick_size))

        cancelled = await session.execute(
            update(Order)
            .where(
                Order.symbol == instrument.symbol,
                Order.status.in_([OrderStatus.PENDING, OrderStatus.TRIGGERED]),
            )
            .values(
                status=OrderStatus.CANCELLED,
                reason=f"Cancelled: {instrument.symbol} split {ratio}-for-1. Place a new order at the new price.",
            )
        )

        session.add(
            PriceAction(
                symbol=instrument.symbol,
                kind=PriceActionKind.SPLIT,
                params={"ratio": str(ratio)},
                price_before=Decimal(before),
                started_at=utcnow(),
                ends_at=utcnow(),
                operator_id=identity.operator.id,
                note=payload.note,
            )
        )
        audit(
            session,
            identity,
            "price.split",
            payload.symbol,
            {"last": before},
            {"ratio": str(ratio), "last": str(instrument.last_price), "positions": len(positions)},
        )
        new_price = str(instrument.last_price)
        cancelled_count = cancelled.rowcount or 0

    await hub.broadcast_public(
        "announcement",
        {
            "message": (
                f"{payload.symbol} splits {payload.ratio}-for-1. Your holding is scaled and the "
                "price adjusted, so your position is worth exactly what it was. "
                "Resting orders in it have been cancelled."
            ),
            "severity": "warning",
        },
        durable=True,
    )
    return {
        "ok": True,
        "symbol": payload.symbol,
        "new_price": new_price,
        "positions_adjusted": len(positions),
        "orders_cancelled": cancelled_count,
    }


# ------------------------------------------------------------------- results


@router.get("/results")
async def results(
    session: AsyncSession = Depends(get_db), _: OperatorIdentity = Depends(VIEW_OPS)
):
    """The final table and the awards.

    Available at any time so the numbers can be sanity-checked during a break,
    but only meaningful once the competition is FINAL.
    """
    state = await get_engine().get_state(session)
    rows = await compute_results(session)
    return {
        "final": state.state is MarketState.FINAL,
        "day_no": state.day_no,
        "results": [row.as_dict() for row in rows],
        "awards": [award.as_dict() for award in compute_awards(rows)],
    }


@router.get("/results/report-cards.html", response_class=Response)
async def report_cards(
    session: AsyncSession = Depends(get_db), _: OperatorIdentity = Depends(VIEW_OPS)
):
    """A printable card per team: where they finished and how they traded.

    Everyone gets one, not only the winners. Most teams will not make the podium
    and this is the thing they take away, so it shows their own trades rather
    than just their rank.
    """
    rules = get_rules()
    rows = await compute_results(session, with_curves=True)
    competition = html.escape(rules.competition_name)

    def card(result) -> str:
        pnl_class = "up" if result.pnl >= 0 else "down"
        return f"""
    <div class="card">
      <div class="head">
        <span class="comp">{competition}</span>
        <span class="rank">#{result.rank} of {len(rows)}</span>
      </div>
      <h2>{html.escape(result.team)}</h2>
      {f'<div class="college">{html.escape(result.college)}</div>' if result.college else ''}
      <div class="hero">
        <div>
          <div class="k">Final account value</div>
          <div class="v">{fmt_inr(result.final_equity)}</div>
        </div>
        <div>
          <div class="k">Profit and loss</div>
          <div class="v {pnl_class}">{fmt_inr(result.pnl)} ({result.pnl_pct.quantize(Decimal('0.01'))}%)</div>
        </div>
      </div>
      <div class="spark">{equity_sparkline(result.equity_curve)}</div>
      <table>
        <tr><td>Trades placed</td><td class="n">{result.trades}</td></tr>
        <tr><td>Trades closed</td><td class="n">{result.closed_trades}</td></tr>
        <tr><td>Win rate</td><td class="n">{result.win_rate.quantize(Decimal('0.1'))}%</td></tr>
        <tr><td>Best trade</td><td class="n">{fmt_inr(result.best_trade)}</td></tr>
        <tr><td>Worst trade</td><td class="n">{fmt_inr(result.worst_trade)}</td></tr>
        <tr><td>Largest fall from a peak</td><td class="n">{result.max_drawdown_pct.quantize(Decimal('0.01'))}%</td></tr>
        <tr><td>Turnover</td><td class="n">{fmt_inr(result.turnover)}</td></tr>
        <tr><td>Charges paid</td><td class="n">{fmt_inr(result.charges)}</td></tr>
      </table>
      {'<div class="busted">Out of the competition: account value reached zero.</div>' if result.status is TeamStatus.BUSTED else ''}
    </div>"""

    body = "".join(card(row) for row in rows)
    page = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Report cards</title>
<style>
  @page {{ size: A4; margin: 12mm; }}
  body {{ font: 12px -apple-system, "Segoe UI", Roboto, sans-serif; color: #111; margin: 0; }}
  .sheet {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8mm; }}
  .card {{ border: 1px solid #999; border-radius: 3mm; padding: 5mm; page-break-inside: avoid; }}
  .head {{ display: flex; justify-content: space-between; align-items: baseline;
           border-bottom: 1px solid #ddd; padding-bottom: 2mm; margin-bottom: 3mm; }}
  .comp {{ font-size: 9px; letter-spacing: .08em; text-transform: uppercase; color: #666; }}
  .rank {{ font: 700 15px ui-monospace, Menlo, monospace; }}
  h2 {{ margin: 0 0 1mm; font-size: 15px; }}
  .college {{ color: #666; font-size: 11px; }}
  .hero {{ display: grid; grid-template-columns: 1fr 1fr; gap: 4mm; margin: 3mm 0; }}
  .hero .k {{ font-size: 8px; letter-spacing: .07em; text-transform: uppercase; color: #666; }}
  .hero .v {{ font: 700 14px ui-monospace, Menlo, monospace; }}
  .up {{ color: #10864f; }} .down {{ color: #c4342f; }}
  .spark {{ margin: 2mm 0; }}
  table {{ width: 100%; border-collapse: collapse; }}
  td {{ padding: 1.2mm 0; border-bottom: 1px dotted #ddd; font-size: 11px; }}
  td.n {{ text-align: right; font-family: ui-monospace, Menlo, monospace; }}
  .busted {{ margin-top: 3mm; font-size: 10px; color: #c4342f; }}
</style></head>
<body><div class="sheet">{body}</div></body></html>
"""
    return Response(content=page, media_type="text/html")


# ------------------------------------------------------------------ helpers


async def _one(session: AsyncSession, symbol: str) -> Instrument:
    instrument = (
        await session.execute(select(Instrument).where(Instrument.symbol == symbol.upper()))
    ).scalar_one_or_none()
    if instrument is None:
        raise HTTPException(404, f"{symbol.upper()} is not listed in this competition.")
    return instrument


async def _resolve_scope(
    session: AsyncSession, symbol: str | None, sector: str | None
) -> list[Instrument]:
    stmt = select(Instrument).where(Instrument.listed.is_(True))
    if symbol:
        stmt = stmt.where(Instrument.symbol == symbol.upper())
    elif sector:
        stmt = stmt.where(Instrument.sector == sector)
    return list((await session.execute(stmt)).scalars())


async def _scenario(session: AsyncSession, scenario_id: int) -> Scenario:
    scenario = (
        await session.execute(select(Scenario).where(Scenario.id == scenario_id))
    ).scalar_one_or_none()
    if scenario is None:
        raise HTTPException(404, "No such scenario.")
    return scenario

