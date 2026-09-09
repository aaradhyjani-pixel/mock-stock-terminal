"""Valuing every team at once.

The margin scan runs every second, the snapshot job every thirty, and the
leaderboard every ten. Doing those one team at a time would be a hundred round
trips a second for no reason. This module values the whole field in two
queries, and the per-team transaction is then only opened for the teams that
actually need one.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Position, Team, TeamStatus
from ..money import ZERO, money
from ..risk import MarginState, Valuation, valuate


@dataclass
class TeamValuation:
    team_id: int
    team_name: str
    code: str
    status: TeamStatus
    valuation: Valuation

    @property
    def rankable_equity(self) -> Decimal:
        """Equity for ranking purposes.

        A busted team ranks at zero. Its ledger keeps the true (possibly
        negative) number, because falsifying the ledger to make a leaderboard
        tidy is how audit trails stop being trustworthy, but the board shows the
        floor.
        """
        if self.status is TeamStatus.BUSTED:
            return ZERO
        return self.valuation.equity


async def valuate_all(
    session: AsyncSession,
    marks: dict[str, Decimal],
    *,
    only_with_shorts: bool = False,
    include_inactive: bool = True,
) -> list[TeamValuation]:
    """Mark every team's book to ``marks``."""
    team_stmt = select(Team)
    if not include_inactive:
        team_stmt = team_stmt.where(Team.status == TeamStatus.ACTIVE)
    teams = list((await session.execute(team_stmt)).scalars())
    if not teams:
        return []

    positions_by_team: dict[int, list[Position]] = {t.id: [] for t in teams}
    for position in (await session.execute(select(Position))).scalars():
        if position.team_id in positions_by_team:
            positions_by_team[position.team_id].append(position)

    out: list[TeamValuation] = []
    for team in teams:
        positions = positions_by_team.get(team.id, [])
        if only_with_shorts and not any(p.qty < 0 for p in positions):
            continue
        out.append(
            TeamValuation(
                team_id=team.id,
                team_name=team.name,
                code=team.code,
                status=team.status,
                valuation=valuate(team.cash, positions, marks),
            )
        )
    return out


def rank(valuations: list[TeamValuation]) -> list[tuple[int, TeamValuation]]:
    """Sort by equity, highest first, and attach 1-based ranks.

    Ties share nothing here; the documented tie-breaks (lower drawdown, then
    fewer trades) are applied when final results are computed, where the extra
    queries are affordable.
    """
    ordered = sorted(valuations, key=lambda tv: tv.rankable_equity, reverse=True)
    return [(i + 1, tv) for i, tv in enumerate(ordered)]


def in_trouble(valuations: list[TeamValuation]) -> list[TeamValuation]:
    """Teams at or past a margin warning, worst first."""
    flagged = [
        tv
        for tv in valuations
        if tv.status is TeamStatus.ACTIVE
        and tv.valuation.margin_state in (MarginState.WARNING, MarginState.CALL, MarginState.BUST)
    ]
    return sorted(flagged, key=lambda tv: money(tv.valuation.equity - tv.valuation.maintenance_required))
