"""Placing orders and reading your own book.

The order endpoint is the only place in the API where money moves, so it is the
only place that opens its own transaction rather than using the request-scoped
session. It has to: the team lock must be held across the read, the funds check,
the write *and* the commit. A dependency-managed session commits after the
handler returns, which would let a second request slip in between the check and
the commit. One endpoint doing something slightly unusual, with a comment saying
why, is a better trade than a subtly wrong fill under load.
"""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_rules, get_settings
from ..db import get_db, locked_team, session_scope
from ..engine import pricing
from ..engine.market import get_engine
from ..engine.matching import (
    OrderRejected,
    cancel_order,
    load_marks,
    post_ledger,
    valuate_team,
)
from ..engine.matching import submit_order as engine_submit
from ..fees import compute_fees
from ..ai import AiNewsError, AiNewsUnavailable, draft_research
from ..models import (
    AuditLog,
    Fill,
    Instrument,
    LedgerKind,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    ResearchReport,
    Team,
)
from ..money import ZERO, money, paise
from ..risk import max_affordable_qty, project_available
from ..schemas import (
    OrderRequest,
    PreviewRequest,
    ResearchRequest,
    fill_row,
    ledger_row,
    order_row,
    percent,
    position_row,
    research_row,
    rupees,
)
from ..security import MemberIdentity, SlidingWindow, current_member
from ..ws import hub

router = APIRouter(prefix="/api", tags=["trading"])

_order_limiter: SlidingWindow | None = None


def order_limiter() -> SlidingWindow:
    global _order_limiter
    if _order_limiter is None:
        rules = get_rules().market
        _order_limiter = SlidingWindow(limit=rules.max_orders_per_10s, window_seconds=10.0)
    return _order_limiter


@router.post("/orders", status_code=status.HTTP_201_CREATED)
async def place_order(
    payload: OrderRequest,
    identity: MemberIdentity = Depends(current_member),
):
    """Place an order. Fills immediately if marketable, otherwise rests."""
    limiter = order_limiter()
    if not limiter.check(f"team:{identity.team_id}"):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Your team is placing orders too quickly. The limit is "
            f"{get_rules().market.max_orders_per_10s} in 10 seconds.",
        )

    engine = get_engine()
    default_tolerance = get_rules().market.default_slippage_tolerance_pct
    tolerance = (
        payload.slippage_tolerance_pct
        if payload.slippage_tolerance_pct is not None
        else default_tolerance
    )

    async with locked_team(identity.team_id):
        async with session_scope() as session:
            state = await engine.get_state(session)
            team = (
                await session.execute(select(Team).where(Team.id == identity.team_id))
            ).scalar_one()

            try:
                outcome = await engine_submit(
                    session,
                    team=team,
                    member_id=identity.member.id,
                    symbol=payload.symbol,
                    side=payload.side,
                    order_type=payload.order_type,
                    qty=payload.qty,
                    market_state=state.state,
                    day_no=state.day_no,
                    limit_price=payload.limit_price,
                    trigger_price=payload.trigger_price,
                    slippage_tolerance_pct=tolerance,
                    client_order_id=payload.client_order_id,
                )
            except OrderRejected as exc:
                raise HTTPException(exc.http_status, exc.message) from exc

            await session.flush()
            order = outcome.order
            body = order_row(order)
            fills = [fill_row(result.fill) for result in outcome.fills]
            body["fills"] = fills
            replayed = outcome.replayed
            team_id = identity.team_id

            for result in outcome.fills:
                signed = result.fill.gross * (1 if order.side is OrderSide.BUY else -1)
                engine.record_flow(order.symbol, signed)

    # A rejected order is recorded and returned rather than raised, so the
    # participant's order list explains what happened instead of the order
    # silently vanishing. The HTTP status stays 201: the request was handled.
    if not replayed:
        await hub.to_team(team_id, "order_update", body)
        if fills:
            await hub.to_ops("blotter", {"team_id": team_id, "team": identity.team.name, **body})
            await _push_portfolio(team_id)

    return body


@router.post("/orders/preview")
async def preview_order(
    payload: PreviewRequest,
    identity: MemberIdentity = Depends(current_member),
    session: AsyncSession = Depends(get_db),
):
    """What this order would cost, before confirming it.

    Shows the estimated fill price including slippage, the itemised charges, the
    margin it would lock, and what would be left available afterwards. The
    participant sees the whole cost of the trade before they commit to it.
    """
    instrument = (
        await session.execute(select(Instrument).where(Instrument.symbol == payload.symbol))
    ).scalar_one_or_none()
    if instrument is None:
        raise HTTPException(404, f"{payload.symbol} is not listed in this competition.")

    team = (await session.execute(select(Team).where(Team.id == identity.team_id))).scalar_one()
    marks = await load_marks(session)
    valuation, positions = await valuate_team(session, team, marks)
    current_qty = next((p.qty for p in positions if p.symbol == payload.symbol), 0)

    quote = pricing.quote_for(instrument)
    fq = pricing.fill_quote(instrument, payload.side, payload.qty, quote)
    price = fq.price
    if payload.order_type is OrderType.LIMIT and payload.limit_price is not None:
        price = (
            min(price, payload.limit_price)
            if payload.side is OrderSide.BUY
            else max(price, payload.limit_price)
        )

    gross = paise(price * payload.qty)
    fees = compute_fees(gross, payload.side)
    projected = project_available(
        valuation,
        current_qty=current_qty,
        side=payload.side,
        qty=payload.qty,
        fill_price=price,
        mark=instrument.last_price,
        fees=fees.total,
    )

    opens_short = current_qty - payload.qty < 0 and payload.side is OrderSide.SELL
    margin_locked = ZERO
    if opens_short:
        new_short = abs(min(0, current_qty - payload.qty))
        prior_short = abs(min(0, current_qty))
        margin_locked = money(
            instrument.last_price
            * (new_short - prior_short)
            * (Decimal("1") + get_rules().margin.initial_pct / Decimal("100"))
        )

    net = gross + fees.total if payload.side is OrderSide.BUY else gross - fees.total
    return {
        "symbol": instrument.symbol,
        "side": payload.side.value,
        "qty": payload.qty,
        "quote": {"bid": rupees(quote.bid), "ask": rupees(quote.ask), "last": rupees(quote.last)},
        "estimated_price": rupees(price),
        "slippage_pct": percent(fq.slippage_pct),
        "gross": rupees(gross),
        "charges": {
            "total": rupees(fees.total),
            "items": {k: rupees(v) for k, v in fees.items.items() if v != 0},
        },
        "net": rupees(net),
        "margin_locked": rupees(margin_locked),
        "available_before": rupees(valuation.available),
        "available_after": rupees(projected),
        "affordable": projected >= 0,
        "max_qty": max_affordable_qty(valuation, current_qty, payload.side, price),
        "position_after": current_qty + payload.side.sign * payload.qty,
    }


@router.get("/orders")
async def list_orders(
    identity: MemberIdentity = Depends(current_member),
    session: AsyncSession = Depends(get_db),
    open_only: bool = False,
    limit: int = Query(default=200, ge=1, le=500),
):
    stmt = (
        select(Order)
        .where(Order.team_id == identity.team_id)
        .order_by(Order.created_at.desc())
        .limit(limit)
    )
    if open_only:
        stmt = stmt.where(Order.status.in_([OrderStatus.PENDING, OrderStatus.TRIGGERED]))
    orders = list((await session.execute(stmt)).scalars())
    return {"orders": [order_row(o) for o in orders]}


@router.delete("/orders/{order_id}")
async def cancel(order_id: int, identity: MemberIdentity = Depends(current_member)):
    async with locked_team(identity.team_id):
        async with session_scope() as session:
            order = (
                await session.execute(
                    select(Order).where(Order.id == order_id, Order.team_id == identity.team_id)
                )
            ).scalar_one_or_none()
            if order is None:
                raise HTTPException(404, "That order does not belong to your team.")
            try:
                await cancel_order(session, order)
            except OrderRejected as exc:
                raise HTTPException(400, exc.message) from exc
            body = order_row(order)
    await hub.to_team(identity.team_id, "order_update", body)
    return body


@router.get("/fills")
async def list_fills(
    identity: MemberIdentity = Depends(current_member),
    session: AsyncSession = Depends(get_db),
    limit: int = Query(default=200, ge=1, le=500),
):
    fills = list(
        (
            await session.execute(
                select(Fill)
                .where(Fill.team_id == identity.team_id)
                .order_by(Fill.ts.desc())
                .limit(limit)
            )
        ).scalars()
    )
    return {"fills": [fill_row(f) for f in fills]}


@router.get("/portfolio")
async def portfolio(
    identity: MemberIdentity = Depends(current_member),
    session: AsyncSession = Depends(get_db),
):
    team = (await session.execute(select(Team).where(Team.id == identity.team_id))).scalar_one()
    marks = await load_marks(session)
    valuation, positions = await valuate_team(session, team, marks)
    rules = get_rules()

    return {
        "team": {"id": team.id, "name": team.name, "code": team.code, "status": team.status.value},
        "funds": valuation.as_dict(),
        "positions": [position_row(p, marks.get(p.symbol)) for p in positions if p.qty != 0],
        "closed_positions": [position_row(p, marks.get(p.symbol)) for p in positions if p.qty == 0],
        "limits": {
            "starting_capital": str(rules.starting_capital),
            "max_leverage": str(rules.margin.max_leverage),
            "initial_margin_pct": str(rules.margin.initial_pct),
            "maintenance_pct": str(rules.margin.maintenance_pct),
            "warning_pct": str(rules.margin.warning_pct),
        },
        "pnl": {
            "total": str(money(valuation.equity - rules.starting_capital)),
            "total_pct": str(
                money((valuation.equity - rules.starting_capital) / rules.starting_capital * 100).quantize(
                    Decimal("0.01")
                )
            ),
        },
    }


@router.get("/ledger")
async def ledger(
    identity: MemberIdentity = Depends(current_member),
    session: AsyncSession = Depends(get_db),
    limit: int = Query(default=200, ge=1, le=1000),
):
    from ..models import LedgerEntry

    rows = list(
        (
            await session.execute(
                select(LedgerEntry)
                .where(LedgerEntry.team_id == identity.team_id)
                .order_by(LedgerEntry.ts.desc(), LedgerEntry.id.desc())
                .limit(limit)
            )
        ).scalars()
    )
    return {"ledger": [ledger_row(r) for r in rows]}


@router.get("/activity")
async def team_activity(
    identity: MemberIdentity = Depends(current_member),
    session: AsyncSession = Depends(get_db),
    limit: int = Query(default=100, ge=1, le=300),
):
    """Who on the team did what. Settles arguments, and doubles as a log the
    team can review afterwards to see how they actually traded."""
    from ..models import Member

    members = {
        m.id: m.name
        for m in (
            await session.execute(select(Member).where(Member.team_id == identity.team_id))
        ).scalars()
    }
    orders = list(
        (
            await session.execute(
                select(Order)
                .where(Order.team_id == identity.team_id)
                .order_by(Order.created_at.desc())
                .limit(limit)
            )
        ).scalars()
    )
    logins = list(
        (
            await session.execute(
                select(AuditLog)
                .where(AuditLog.actor_type == "member", AuditLog.action == "login")
                .order_by(AuditLog.ts.desc())
                .limit(50)
            )
        ).scalars()
    )
    return {
        "orders": [
            {**order_row(o), "member": members.get(o.member_id, "System" if o.member_id is None else "Unknown")}
            for o in orders
        ],
        "logins": [
            {"member": row.actor_name, "ts": row.ts.isoformat()}
            for row in logins
            if row.actor_id in members
        ],
    }


# ------------------------------------------------------------- research desk

_research_limiter = SlidingWindow(limit=10, window_seconds=600.0)
_research_cooldowns: dict[tuple[int, str], float] = {}


@router.get("/research/status")
async def research_status():
    """So the terminal can grey the button out with a real reason."""
    rules = get_rules().research
    available = rules.enabled and get_settings().ai_news_available
    return {"available": available, "cost": rupees(rules.cost)}


@router.get("/research")
async def list_research(
    identity: MemberIdentity = Depends(current_member),
    session: AsyncSession = Depends(get_db),
    symbol: str | None = None,
):
    """Reports this team already bought. Free to reread, never re-charged."""
    stmt = select(ResearchReport).where(ResearchReport.team_id == identity.team_id)
    if symbol:
        stmt = stmt.where(ResearchReport.symbol == symbol.upper())
    stmt = stmt.order_by(ResearchReport.created_at.desc())
    reports = list((await session.execute(stmt)).scalars())
    return {"reports": [research_row(r) for r in reports]}


@router.post("/research")
async def buy_research(
    payload: ResearchRequest,
    identity: MemberIdentity = Depends(current_member),
):
    """Spend real cash on a fictional analyst's opinion of one stock.

    This is colour and a strategic choice, never an edge: the model drafting
    the report is only ever given the stock's public price and today's move,
    the same numbers already on the watchlist. There is no scenario data in
    its input for it to leak, so paying for this can make a team feel more
    informed, never actually better informed than reading the tape.
    """
    import time

    rules = get_rules().research
    if not rules.enabled:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The research desk is switched off for this event.")

    if not _research_limiter.check(f"research:{identity.team_id}"):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many research requests in a short time. Wait a few minutes.",
        )

    cooldown_key = (identity.team_id, payload.symbol)
    now = time.monotonic()
    remaining = rules.cooldown_seconds_per_symbol - (now - _research_cooldowns.get(cooldown_key, 0))
    if remaining > 0:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"A report on {payload.symbol} was bought recently. "
            f"Wait {int(remaining)}s before buying another one for the same stock.",
        )

    async with locked_team(identity.team_id):
        async with session_scope() as session:
            instrument = (
                await session.execute(select(Instrument).where(Instrument.symbol == payload.symbol))
            ).scalar_one_or_none()
            if instrument is None:
                raise HTTPException(404, f"{payload.symbol} is not listed in this competition.")

            team = (await session.execute(select(Team).where(Team.id == identity.team_id))).scalar_one()
            if team.cash < rules.cost:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"A report costs {rupees(rules.cost)}. Your team has {rupees(team.cash)} in cash.",
                )

            change_pct = (
                (instrument.last_price - instrument.prev_close) / instrument.prev_close * 100
                if instrument.prev_close > 0
                else ZERO
            )

            try:
                draft = await run_in_threadpool(
                    draft_research,
                    instrument.symbol,
                    instrument.name,
                    instrument.sector,
                    instrument.last_price,
                    money(change_pct),
                )
            except AiNewsUnavailable as exc:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
            except AiNewsError as exc:
                raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

            post_ledger(
                session,
                team,
                LedgerKind.RESEARCH,
                -rules.cost,
                note=f"Research report: {payload.symbol} from {draft.house_name}",
            )
            report = ResearchReport(
                team_id=identity.team_id,
                symbol=payload.symbol,
                house_name=draft.house_name,
                rating=draft.rating,
                target_price=draft.target_price,
                headline=draft.headline,
                body=draft.body,
                cost=rules.cost,
            )
            session.add(report)
            await session.flush()
            _research_cooldowns[cooldown_key] = now
            session.add(
                AuditLog(
                    actor_type="member",
                    actor_id=identity.member.id,
                    actor_name=identity.member.name,
                    action="research.buy",
                    target=payload.symbol,
                )
            )
            row = research_row(report)

    await _push_portfolio(identity.team_id)
    return row


async def _push_portfolio(team_id: int) -> None:
    async with session_scope() as session:
        team = (await session.execute(select(Team).where(Team.id == team_id))).scalar_one_or_none()
        if team is None:
            return
        marks = await load_marks(session)
        valuation, positions = await valuate_team(session, team, marks)
        await hub.to_team(
            team_id,
            "portfolio",
            {
                "funds": valuation.as_dict(),
                "positions": [position_row(p, marks.get(p.symbol)) for p in positions if p.qty != 0],
            },
        )

