"""Market data, news and the leaderboard.

Everything here is readable by any signed-in participant. The only thing this
module hides is what the rules say to hide: an operator's sentiment note on a
headline, and the leaderboard during the closing blackout.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_rules
from ..db import get_db
from ..engine import pricing
from ..engine.market import get_engine
from ..engine.matching import load_marks
from ..engine.valuations import rank, valuate_all
from ..models import Candle, Instrument, MarketState, NewsItem
from ..results import team_report
from ..schemas import instrument_row, news_row
from ..security import MemberIdentity, current_member

router = APIRouter(prefix="/api", tags=["market"])


@router.get("/market")
async def market_state(session: AsyncSession = Depends(get_db)):
    """The clock, the banner and the index. Readable without signing in, so the
    login screen can show a countdown instead of leaving people guessing."""
    engine = get_engine()
    state = await engine.get_state(session)
    payload = engine.state_payload(state)
    payload["rules"] = {
        "competition": get_rules().competition_name,
        "starting_capital": str(get_rules().starting_capital),
        "max_leverage": str(get_rules().margin.max_leverage),
        "maintenance_pct": str(get_rules().margin.maintenance_pct),
    }
    return payload


@router.get("/instruments")
async def list_instruments(session: AsyncSession = Depends(get_db)):
    instruments = list(
        (
            await session.execute(
                select(Instrument)
                .where(Instrument.listed.is_(True))
                .order_by(Instrument.display_order, Instrument.symbol)
            )
        ).scalars()
    )
    return {"instruments": [instrument_row(i) for i in instruments]}


@router.get("/instruments/{symbol}")
async def instrument_detail(symbol: str, session: AsyncSession = Depends(get_db)):
    instrument = (
        await session.execute(select(Instrument).where(Instrument.symbol == symbol.upper()))
    ).scalar_one_or_none()
    if instrument is None:
        raise HTTPException(404, f"{symbol.upper()} is not listed in this competition.")
    row = instrument_row(instrument)
    row["depth"] = pricing.synth_depth(instrument)
    return row


@router.get("/instruments/{symbol}/candles")
async def candles(
    symbol: str,
    interval: str = Query(default="1m", pattern="^(1m|5m)$"),
    limit: int = Query(default=240, ge=1, le=1000),
    session: AsyncSession = Depends(get_db),
):
    rows = list(
        (
            await session.execute(
                select(Candle)
                .where(Candle.symbol == symbol.upper(), Candle.interval == interval)
                .order_by(Candle.ts.desc())
                .limit(limit)
            )
        ).scalars()
    )
    rows.reverse()
    return {
        "symbol": symbol.upper(),
        "interval": interval,
        "candles": [
            {
                "ts": c.ts.isoformat(),
                "o": str(c.o),
                "h": str(c.h),
                "l": str(c.low),
                "c": str(c.c),
                "v": c.v,
            }
            for c in rows
        ],
    }


@router.get("/news")
async def news_feed(
    symbol: str | None = None,
    kind: str | None = None,
    limit: int = Query(default=60, ge=1, le=200),
    session: AsyncSession = Depends(get_db),
    _: MemberIdentity = Depends(current_member),
):
    stmt = (
        select(NewsItem)
        .where(NewsItem.published_at.is_not(None))
        .order_by(NewsItem.published_at.desc())
        .limit(limit)
    )
    if kind:
        stmt = stmt.where(NewsItem.kind == kind.upper())
    items = list((await session.execute(stmt)).scalars())
    if symbol:
        wanted = symbol.upper()
        items = [i for i in items if wanted in (i.symbols or [])]
    return {"news": [news_row(i) for i in items]}


@router.get("/leaderboard")
async def leaderboard(
    identity: MemberIdentity = Depends(current_member),
    session: AsyncSession = Depends(get_db),
):
    """Public top N, plus the requesting team's own rank whatever it is.

    A team outside the top ten still needs to know where it stands, so its own
    row is always attached. During the closing blackout the public list is
    empty but a team can still see itself, which keeps the suspense about
    everyone else rather than about your own account.
    """
    engine = get_engine()
    state = await engine.get_state(session)
    marks = await load_marks(session)
    ranked = rank(await valuate_all(session, marks))

    rules = get_rules().session
    rows = [
        {
            "rank": position,
            "team": tv.team_name,
            "code": tv.code,
            "equity": str(tv.rankable_equity),
            "status": tv.status.value,
            "is_you": tv.team_id == identity.team_id,
        }
        for position, tv in ranked
    ]

    mine = next((r for r in rows if r["is_you"]), None)
    public = [] if state.leaderboard_blackout else rows[: rules.public_leaderboard_size]

    return {
        "blackout": state.leaderboard_blackout,
        "rows": public,
        "you": mine,
        "field_size": len(rows),
    }


@router.get("/leaderboard/history")
async def my_equity_curve(
    identity: MemberIdentity = Depends(current_member),
    session: AsyncSession = Depends(get_db),
    hours: int = Query(default=12, ge=1, le=72),
):
    from ..models import EquitySnapshot

    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = list(
        (
            await session.execute(
                select(EquitySnapshot)
                .where(EquitySnapshot.team_id == identity.team_id, EquitySnapshot.ts >= since)
                .order_by(EquitySnapshot.ts)
            )
        ).scalars()
    )
    return {
        "points": [
            {"ts": r.ts.isoformat(), "equity": str(r.equity), "rank": r.rank, "day_no": r.day_no}
            for r in rows
        ]
    }


@router.get("/results")
async def my_results(
    identity: MemberIdentity = Depends(current_member),
    session: AsyncSession = Depends(get_db),
):
    """Your team's report card, once the competition has ended.

    Held back until FINAL because a drawdown figure mid-competition is a
    scoreboard nobody agreed to play on, and because the numbers are not
    settled until the last close.
    """
    state = await get_engine().get_state(session)
    if state.state is not MarketState.FINAL:
        raise HTTPException(
            409,
            "Results are published when the competition ends. Your live position is on the board.",
        )
    report = await team_report(session, identity.team_id)
    if report is None:
        raise HTTPException(404, "No result for your team.")
    return report


@router.get("/sectors")
async def sectors(session: AsyncSession = Depends(get_db)):
    rows = (await session.execute(select(Instrument.sector).distinct())).scalars()
    return {"sectors": sorted({s for s in rows if s})}

