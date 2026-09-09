"""Final results, tie-breaks and awards.

The participant rulebook promises that ties are broken by maximum drawdown then
trade count, and that five side awards are given out. This module is where those
promises are kept. Every number here is derived from two tables the engine has
been writing all along - ``equity_snapshots`` (every 30 seconds and at each
close) and ``fills`` - so the results cannot disagree with the live leaderboard
or with a team's own order history.

One definition worth stating, because it decides several numbers below: a fill
realises P&L only when it reduces or flips a position. So a fill with non-zero
``realised_pnl`` is a *closed trade*, a BUY that closed one is a covered short,
and a SELL that closed one is a sold long. Win rate is computed over closed
trades only, because an opening trade has not won or lost anything yet.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_rules
from .engine.matching import load_marks
from .engine.valuations import valuate_all
from .models import EquitySnapshot, Fill, LedgerEntry, LedgerKind, OrderSide, Team, TeamStatus
from .money import ZERO, money, paise


@dataclass
class TeamResult:
    team_id: int
    team: str
    code: str
    college: str | None
    status: TeamStatus
    busted_at: datetime | None

    final_equity: Decimal
    pnl: Decimal
    pnl_pct: Decimal

    max_drawdown: Decimal
    max_drawdown_pct: Decimal

    trades: int
    closed_trades: int
    wins: int
    win_rate: Decimal
    best_trade: Decimal
    worst_trade: Decimal
    best_short: Decimal

    turnover: Decimal
    charges: Decimal
    risk_adjusted: Decimal | None

    equity_curve: list[tuple[datetime, Decimal]] = field(default_factory=list)
    rank: int = 0

    def as_dict(self, *, curve: bool = False) -> dict:
        row = {
            "rank": self.rank,
            "team_id": self.team_id,
            "team": self.team,
            "code": self.code,
            "college": self.college,
            "status": self.status.value,
            "final_equity": str(paise(self.final_equity)),
            "pnl": str(paise(self.pnl)),
            "pnl_pct": str(self.pnl_pct.quantize(Decimal("0.01"))),
            "max_drawdown": str(paise(self.max_drawdown)),
            "max_drawdown_pct": str(self.max_drawdown_pct.quantize(Decimal("0.01"))),
            "trades": self.trades,
            "closed_trades": self.closed_trades,
            "wins": self.wins,
            "win_rate": str(self.win_rate.quantize(Decimal("0.1"))),
            "best_trade": str(paise(self.best_trade)),
            "worst_trade": str(paise(self.worst_trade)),
            "best_short": str(paise(self.best_short)),
            "turnover": str(paise(self.turnover)),
            "charges": str(paise(self.charges)),
            "risk_adjusted": (
                str(self.risk_adjusted.quantize(Decimal("0.001")))
                if self.risk_adjusted is not None
                else None
            ),
        }
        if curve:
            row["equity_curve"] = [
                {"ts": ts.isoformat(), "equity": str(paise(value))} for ts, value in self.equity_curve
            ]
        return row


@dataclass
class Award:
    key: str
    title: str
    description: str
    team: str | None
    code: str | None
    value: str | None

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "description": self.description,
            "team": self.team,
            "code": self.code,
            "value": self.value,
        }


def max_drawdown(curve: list[Decimal]) -> tuple[Decimal, Decimal]:
    """Largest peak-to-trough fall in an equity series.

    Returns the fall in rupees and as a percentage of the peak it fell from.
    A team that only ever went up has a drawdown of zero, which is what makes
    this a sensible tie-break: of two teams who finished level, the one who got
    there without a scare did the better job.
    """
    if not curve:
        return ZERO, ZERO
    peak = curve[0]
    worst = ZERO
    worst_pct = ZERO
    for value in curve:
        if value > peak:
            peak = value
        fall = peak - value
        if fall > worst:
            worst = fall
            worst_pct = (fall / peak * Decimal("100")) if peak > 0 else ZERO
    return money(worst), money(worst_pct)


def risk_adjusted_score(day_closes: list[Decimal], opening: Decimal) -> Decimal | None:
    """Mean daily return over its standard deviation.

    A Sharpe ratio without a risk-free rate, which is the right simplification
    for a competition lasting an afternoon. Needs at least two days to have a
    standard deviation at all, and returns ``None`` rather than a made-up number
    when a team has fewer.
    """
    if len(day_closes) < 2:
        return None
    returns: list[Decimal] = []
    previous = opening
    for close in day_closes:
        if previous > 0:
            returns.append((close - previous) / previous)
        previous = close
    if len(returns) < 2:
        return None
    mean = statistics.fmean(float(r) for r in returns)
    stdev = statistics.pstdev(float(r) for r in returns)
    if stdev == 0:
        # No variation at all. A flat line is not skill, and dividing by zero is
        # not a score, so this team simply does not place in this award.
        return None
    return Decimal(repr(mean / stdev))


async def compute_results(session: AsyncSession, *, with_curves: bool = False) -> list[TeamResult]:
    """Final table for every team, ranked with the documented tie-breaks."""
    rules = get_rules()
    opening = rules.starting_capital

    teams = list((await session.execute(select(Team).order_by(Team.id))).scalars())
    if not teams:
        return []

    snapshots_by_team: dict[int, list[EquitySnapshot]] = {t.id: [] for t in teams}
    for snapshot in (
        await session.execute(select(EquitySnapshot).order_by(EquitySnapshot.ts))
    ).scalars():
        if snapshot.team_id in snapshots_by_team:
            snapshots_by_team[snapshot.team_id].append(snapshot)

    fills_by_team: dict[int, list[Fill]] = {t.id: [] for t in teams}
    for fill in (await session.execute(select(Fill).order_by(Fill.ts))).scalars():
        if fill.team_id in fills_by_team:
            fills_by_team[fill.team_id].append(fill)

    # A team's final equity comes from its last snapshot. Finalising the
    # competition writes one for everybody, so in the normal flow there is
    # always a snapshot to read. This is the fallback for the abnormal flow:
    # marking the book live is right, and silently reporting the opening
    # balance as somebody's final result is not.
    marks = await load_marks(session)
    live = {tv.team_id: tv.valuation.equity for tv in await valuate_all(session, marks)}

    charges_by_team: dict[int, Decimal] = {}
    for entry in (
        await session.execute(
            select(LedgerEntry).where(
                LedgerEntry.kind.in_([LedgerKind.FEE, LedgerKind.BORROW_FEE])
            )
        )
    ).scalars():
        charges_by_team[entry.team_id] = charges_by_team.get(entry.team_id, ZERO) - entry.amount

    results: list[TeamResult] = []
    for team in teams:
        snapshots = snapshots_by_team.get(team.id, [])
        fills = fills_by_team.get(team.id, [])

        curve_values = [s.equity for s in snapshots] or [live.get(team.id, opening)]
        final_equity = ZERO if team.status is TeamStatus.BUSTED else curve_values[-1]
        drawdown, drawdown_pct = max_drawdown([opening, *curve_values])

        day_closes = [s.equity for s in snapshots if s.is_close]
        closed = [f for f in fills if f.realised_pnl != 0]
        wins = [f for f in closed if f.realised_pnl > 0]
        short_closes = [f for f in closed if f.side is OrderSide.BUY]

        results.append(
            TeamResult(
                team_id=team.id,
                team=team.name,
                code=team.code,
                college=team.college,
                status=team.status,
                busted_at=team.busted_at,
                final_equity=final_equity,
                pnl=money(final_equity - opening),
                pnl_pct=money((final_equity - opening) / opening * Decimal("100")) if opening else ZERO,
                max_drawdown=drawdown,
                max_drawdown_pct=drawdown_pct,
                trades=len(fills),
                closed_trades=len(closed),
                wins=len(wins),
                win_rate=(
                    money(Decimal(len(wins)) / Decimal(len(closed)) * Decimal("100"))
                    if closed
                    else ZERO
                ),
                best_trade=max((f.realised_pnl for f in closed), default=ZERO),
                worst_trade=min((f.realised_pnl for f in closed), default=ZERO),
                best_short=max((f.realised_pnl for f in short_closes), default=ZERO),
                turnover=sum((f.gross for f in fills), ZERO),
                charges=charges_by_team.get(team.id, ZERO),
                risk_adjusted=risk_adjusted_score(day_closes, opening),
                equity_curve=[(s.ts, s.equity) for s in snapshots] if with_curves else [],
            )
        )

    _apply_ranking(results)
    return results


def _apply_ranking(results: list[TeamResult]) -> None:
    """Sort by the documented tie-breaks and stamp the rank.

    Account value, then lower maximum drawdown, then fewer trades, then earlier
    registration. Busted teams sort below everyone at zero, most recent bust
    first, so that surviving longer counts for something.
    """

    def key(result: TeamResult):
        busted = result.status is TeamStatus.BUSTED
        return (
            1 if busted else 0,
            -result.final_equity,
            # Among busted teams, the one who lasted longest ranks higher.
            -(result.busted_at.timestamp() if busted and result.busted_at else 0),
            result.max_drawdown,
            result.trades,
            result.team_id,
        )

    results.sort(key=key)
    for position, result in enumerate(results, start=1):
        result.rank = position


def compute_awards(results: list[TeamResult]) -> list[Award]:
    """The five side awards, from the same data as the main table."""
    from .money import fmt_inr

    active = [r for r in results if r.status is not TeamStatus.DISQUALIFIED]

    def best(candidates, key, title, description, award_key, formatter):
        pool = [c for c in candidates if key(c) is not None]
        if not pool:
            return Award(award_key, title, description, None, None, None)
        winner = max(pool, key=key)
        return Award(award_key, title, description, winner.team, winner.code, formatter(winner))

    awards = [
        best(
            # A negative score means they lost money steadily, which is not a
            # thing to hand out a prize for. If nobody managed a positive
            # risk-adjusted return, the award goes unclaimed.
            [r for r in active if r.risk_adjusted is not None and r.risk_adjusted > 0],
            lambda r: r.risk_adjusted,
            "Best risk-adjusted return",
            "Highest average daily return relative to how much it swung about.",
            "risk_adjusted",
            lambda r: f"{r.risk_adjusted.quantize(Decimal('0.001'))}",
        ),
        best(
            [r for r in active[:20] if r.trades > 0],
            lambda r: -r.max_drawdown_pct,
            "Steadiest hand",
            "Smallest peak-to-trough fall, among the top twenty by account value.",
            "lowest_drawdown",
            lambda r: f"{r.max_drawdown_pct.quantize(Decimal('0.01'))}% drawdown",
        ),
        best(
            [r for r in active if r.best_trade > 0],
            lambda r: r.best_trade,
            "Best single trade",
            "Largest profit realised on one closing trade.",
            "best_trade",
            lambda r: f"Rs {fmt_inr(r.best_trade)}",
        ),
        best(
            [r for r in active if r.best_short > 0],
            lambda r: r.best_short,
            "Best short",
            "Largest profit realised covering a short position.",
            "best_short",
            lambda r: f"Rs {fmt_inr(r.best_short)}",
        ),
        best(
            [r for r in active if r.closed_trades >= 10],
            lambda r: r.win_rate,
            "Most disciplined",
            "Highest proportion of profitable trades, over at least ten closed trades.",
            "most_disciplined",
            lambda r: f"{r.win_rate.quantize(Decimal('0.1'))}% of {r.closed_trades} trades",
        ),
    ]
    return awards


async def team_report(session: AsyncSession, team_id: int) -> dict | None:
    """One team's report card: their result, their curve, and their best trades."""
    results = await compute_results(session, with_curves=True)
    mine = next((r for r in results if r.team_id == team_id), None)
    if mine is None:
        return None
    return {
        "result": mine.as_dict(curve=True),
        "field_size": len(results),
        "podium": [r.as_dict() for r in results[:3]],
    }


def equity_sparkline(curve: list[tuple[datetime, Decimal]], width: int = 300, height: int = 60) -> str:
    """An inline SVG of an equity curve, for the printed report cards.

    Inline rather than a chart library, for the same reason the terminal's chart
    is hand-drawn: this page has to print correctly on a laptop in a hall with
    no internet.
    """
    values = [float(v) for _, v in curve]
    if len(values) < 2:
        return f'<svg width="{width}" height="{height}" role="img" aria-label="No data"></svg>'

    low, high = min(values), max(values)
    span = (high - low) or 1.0
    step = width / (len(values) - 1)
    points = " ".join(
        f"{i * step:.1f},{height - ((v - low) / span) * (height - 4) - 2:.1f}"
        for i, v in enumerate(values)
    )
    rising = values[-1] >= values[0]
    colour = "#10864f" if rising else "#c4342f"
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="Equity curve">'
        f'<polyline fill="none" stroke="{colour}" stroke-width="1.5" '
        f'stroke-linejoin="round" points="{points}"/></svg>'
    )
