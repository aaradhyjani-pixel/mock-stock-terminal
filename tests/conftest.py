"""Test fixtures.

Tests run against a real database (SQLite, exact via the ``Money`` column type)
with the engine's tick loop switched off, so every tick is driven explicitly and
nothing is timing-dependent. A test that passes here would pass identically on
PostgreSQL; that is the whole reason for the exact-decimal storage type.
"""

from __future__ import annotations

import os
import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

TMP_DB = Path(tempfile.gettempdir()) / "exchange_test.db"
os.environ["EXCHANGE_DATABASE_URL"] = f"sqlite+aiosqlite:///{TMP_DB}"
os.environ["EXCHANGE_RUN_ENGINE"] = "false"
os.environ["EXCHANGE_SECRET_KEY"] = "test-secret"
# Durability does not matter for a database that is dropped between tests.
os.environ["EXCHANGE_SQLITE_SYNCHRONOUS"] = "NORMAL"

from app.config import get_rules  # noqa: E402
from app.db import create_all, dispose_engine, drop_all, session_scope  # noqa: E402
from app.engine.market import MarketEngine  # noqa: E402
from app.engine.matching import post_ledger  # noqa: E402
from app.models import (  # noqa: E402
    Instrument,
    LedgerKind,
    MarketState,
    MarketStateRow,
    Member,
    MemberRole,
    Operator,
    OperatorRole,
    Team,
)
from app.security import hash_password  # noqa: E402


@pytest.fixture(autouse=True)
async def fresh_database():
    await drop_all()
    await create_all()
    yield
    await dispose_engine()


@pytest.fixture(autouse=True)
def default_rules():
    """Reset the rulebook between tests.

    ``get_rules`` is cached, so tests that tune a rule are mutating a shared
    object. Restoring the defaults afterwards keeps them independent.
    """
    rules = get_rules()
    snapshot = rules.model_dump()
    yield rules
    for key, value in type(rules).model_validate(snapshot).__dict__.items():
        setattr(rules, key, value)


@pytest.fixture(autouse=True)
def reset_rate_limiters():
    """Rate limiters are process-global by design. Clear them between tests."""
    from app.routers import trading
    from app.security import login_ip_limiter, login_limiter

    for limiter in (login_limiter, login_ip_limiter):
        limiter._hits.clear()
    # Rebuilt on next use, so a test that changes the rule gets the new limit.
    trading._order_limiter = None
    yield


@pytest.fixture
def no_fees(default_rules):
    """Turn charges off, for tests that assert on exact cash arithmetic."""
    default_rules.fees.enabled = False
    return default_rules


@pytest.fixture
def no_slippage(default_rules):
    default_rules.market.slippage_enabled = False
    default_rules.market.impact_enabled = False
    return default_rules


async def make_instrument(
    symbol: str = "TESTCO",
    price: str = "2500",
    *,
    spread_bps: str = "0",
    liquidity: str = "100000000000",
    band_pct: str = "20",
    daily_vol_pct: str = "0",
    sector: str = "Test",
) -> Instrument:
    """A frictionless instrument by default, so arithmetic assertions are exact."""
    async with session_scope() as session:
        instrument = Instrument(
            symbol=symbol,
            name=f"{symbol} Ltd",
            sector=sector,
            seed_price=Decimal(price),
            start_price=Decimal(price),
            last_price=Decimal(price),
            day_reference=Decimal(price),
            prev_close=Decimal(price),
            day_open=Decimal(price),
            day_high=Decimal(price),
            day_low=Decimal(price),
            spread_bps=Decimal(spread_bps),
            liquidity_notional=Decimal(liquidity),
            band_pct=Decimal(band_pct),
            daily_vol_pct=Decimal(daily_vol_pct),
            tick_size=Decimal("0.05"),
        )
        session.add(instrument)
        await session.flush()
        session.expunge(instrument)
        return instrument


async def make_team(name: str = "Test Team", capital: str | None = None) -> int:
    rules = get_rules()
    amount = Decimal(capital) if capital is not None else rules.starting_capital
    async with session_scope() as session:
        team = Team(name=name, code=name.upper().replace(" ", "")[:12], cash=Decimal("0"))
        session.add(team)
        await session.flush()
        post_ledger(session, team, LedgerKind.OPENING, amount, note="Opening balance")
        session.add(
            Member(
                team_id=team.id,
                name=f"{name} captain",
                login=f"{team.code.lower()}-1",
                password_hash=hash_password("password123"),
                role=MemberRole.CAPTAIN,
            )
        )
        await session.flush()
        return team.id


async def make_operator(login: str = "director", role: OperatorRole = OperatorRole.SUPER_ADMIN) -> int:
    async with session_scope() as session:
        operator = Operator(
            name=login.title(),
            login=login,
            password_hash=hash_password("password123"),
            role=role,
        )
        session.add(operator)
        await session.flush()
        return operator.id


async def set_market(state: MarketState = MarketState.OPEN, day_no: int = 1) -> None:
    async with session_scope() as session:
        row = (await session.execute(__import__("sqlalchemy").select(MarketStateRow))).scalar_one_or_none()
        if row is None:
            row = MarketStateRow(id=1)
            session.add(row)
        row.state = state
        row.day_no = day_no
        row.total_days = 5


@pytest.fixture
def deterministic_engine() -> MarketEngine:
    """An engine with a fixed seed, so price paths repeat exactly."""
    return MarketEngine(seed=20260909)


@pytest.fixture
async def instrument():
    return await make_instrument()


@pytest.fixture
async def team_id():
    return await make_team()
