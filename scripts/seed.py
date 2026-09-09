"""Set up a fresh competition database.

    python -m scripts.seed --reset --demo-teams 12

Loads the basket from ``config/instruments.yaml``, the scenario scripts from
``config/scenarios/``, and creates the operator accounts. With ``--demo-teams``
it also creates practice teams so the whole thing can be driven end to end
before a single real participant has registered.

Operator passwords are printed once and are not recoverable. Write them down.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from decimal import Decimal
from pathlib import Path

import yaml
from sqlalchemy import delete, select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import CONFIG_DIR, get_rules  # noqa: E402
from app.db import create_all, dispose_engine, drop_all, session_scope  # noqa: E402
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
    Scenario,
    ScenarioStep,
    Team,
)
from app.money import D  # noqa: E402
from app.security import generate_code, generate_password, hash_password  # noqa: E402

OPERATOR_SEED = [
    ("Event Director", "director", OperatorRole.SUPER_ADMIN),
    ("Deputy Director", "deputy", OperatorRole.SUPER_ADMIN),
    ("Market Operator", "market", OperatorRole.MARKET_OPERATOR),
    ("News Desk", "news", OperatorRole.NEWS_DESK),
    ("Help Desk", "helpdesk", OperatorRole.HELP_DESK),
    ("Judges", "judge", OperatorRole.JUDGE),
    ("Projector", "projector", OperatorRole.PROJECTOR),
]


async def load_instruments(prices_csv: Path | None = None) -> int:
    """Create or update the tradable basket."""
    path = CONFIG_DIR / "instruments.yaml"
    data = yaml.safe_load(path.read_text())
    rows = data["instruments"]

    overrides: dict[str, Decimal] = {}
    if prices_csv and prices_csv.exists():
        with prices_csv.open() as handle:
            for row in csv.reader(handle):
                if len(row) >= 2 and row[0].strip().upper() != "SYMBOL":
                    overrides[row[0].strip().upper()] = D(row[1].strip())
        print(f"  price overrides loaded for {len(overrides)} symbols")

    default_band = get_rules().market.band_pct

    async with session_scope() as session:
        for order, row in enumerate(rows):
            symbol = row["symbol"].upper()
            price = overrides.get(symbol, D(row["start_price"]))
            instrument = (
                await session.execute(select(Instrument).where(Instrument.symbol == symbol))
            ).scalar_one_or_none()
            if instrument is None:
                instrument = Instrument(symbol=symbol)
                session.add(instrument)
            instrument.name = row["name"]
            instrument.sector = row["sector"]
            instrument.seed_price = price
            instrument.start_price = price
            instrument.last_price = price
            instrument.day_reference = price
            instrument.prev_close = price
            instrument.day_open = price
            instrument.day_high = price
            instrument.day_low = price
            instrument.day_volume = 0
            instrument.tick_size = D(row.get("tick_size", "0.05"))
            instrument.lot_size = int(row.get("lot_size", 1))
            instrument.spread_bps = D(row.get("spread_bps", "10"))
            instrument.daily_vol_pct = D(row.get("daily_vol_pct", "1.5"))
            instrument.liquidity_notional = D(row.get("liquidity_notional", "20000000"))
            instrument.band_pct = D(row.get("band_pct", default_band))
            instrument.index_weight = D(row.get("index_weight", "1"))
            instrument.display_order = order
            instrument.listed = True
    return len(rows)


async def load_scenarios() -> int:
    """Load every scenario script in ``config/scenarios``."""
    directory = CONFIG_DIR / "scenarios"
    if not directory.exists():
        return 0
    count = 0
    async with session_scope() as session:
        for path in sorted(directory.glob("*.yaml")):
            data = yaml.safe_load(path.read_text())
            name = data.get("name", path.stem)
            existing = (
                await session.execute(select(Scenario).where(Scenario.name == name))
            ).scalar_one_or_none()
            if existing is not None:
                await session.execute(
                    delete(ScenarioStep).where(ScenarioStep.scenario_id == existing.id)
                )
                scenario = existing
            else:
                scenario = Scenario(name=name)
                session.add(scenario)
            scenario.day_no = int(data.get("day_no", 1))
            scenario.description = data.get("description", "").strip()
            scenario.status = "IDLE"
            scenario.started_at = None
            scenario.paused_offset_seconds = 0
            await session.flush()

            for step in data.get("steps", []):
                payload = {k: v for k, v in step.items() if k not in ("at", "label")}
                session.add(
                    ScenarioStep(
                        scenario_id=scenario.id,
                        at_offset=int(step["at"]),
                        label=step.get("label", ""),
                        payload=payload,
                    )
                )
            count += 1
    return count


async def create_operators() -> list[tuple[str, str, str]]:
    created = []
    async with session_scope() as session:
        for name, login, role in OPERATOR_SEED:
            existing = (
                await session.execute(select(Operator).where(Operator.login == login))
            ).scalar_one_or_none()
            if existing is not None:
                continue
            password = generate_password(10)
            session.add(
                Operator(
                    name=name,
                    login=login,
                    password_hash=hash_password(password),
                    role=role,
                )
            )
            created.append((name, login, password))
    return created


async def create_demo_teams(count: int, shared_password: str | None = None) -> list[tuple[str, str, str, str]]:
    """Practice teams for rehearsals and load tests.

    ``shared_password`` gives every practice account the same password, which is
    what makes ``scripts/loadtest.py`` runnable without juggling a credentials
    file. Never use it for real teams.
    """
    rules = get_rules()
    created = []
    async with session_scope() as session:
        for index in range(1, count + 1):
            name = f"Practice Team {index:02d}"
            if (
                await session.execute(select(Team).where(Team.name == name))
            ).scalar_one_or_none() is not None:
                continue
            code = generate_code()
            team = Team(name=name, code=code, cash=Decimal("0"), college="Demo")
            session.add(team)
            await session.flush()
            post_ledger(
                session, team, LedgerKind.OPENING, rules.starting_capital, note="Opening balance", day_no=0
            )
            password = shared_password or generate_password(8)
            login = f"{code.lower()}-1"
            session.add(
                Member(
                    team_id=team.id,
                    name=f"Captain {index}",
                    login=login,
                    password_hash=hash_password(password),
                    role=MemberRole.CAPTAIN,
                )
            )
            created.append((name, code, login, password))
    return created


async def init_market_state() -> None:
    rules = get_rules()
    async with session_scope() as session:
        state = (
            await session.execute(select(MarketStateRow).where(MarketStateRow.id == 1))
        ).scalar_one_or_none()
        if state is None:
            state = MarketStateRow(id=1)
            session.add(state)
        state.state = MarketState.CLOSED
        state.day_no = 0
        state.total_days = rules.session.trading_days
        state.index_value = rules.market.index_base
        state.index_prev_close = rules.market.index_base
        state.session_ends_at = None
        state.banner = "The market has not opened yet."
        state.leaderboard_blackout = False


async def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the mock exchange database.")
    parser.add_argument("--reset", action="store_true", help="Drop every table first. Destroys all data.")
    parser.add_argument("--demo-teams", type=int, default=0, help="Create N practice teams.")
    parser.add_argument(
        "--demo-password",
        help="Give every practice team the same password. For rehearsals and load tests only.",
    )
    parser.add_argument("--prices", type=Path, help="CSV of symbol,price to override start prices.")
    args = parser.parse_args()

    if args.reset:
        print("Dropping all tables...")
        await drop_all()

    await create_all()
    await init_market_state()

    count = await load_instruments(args.prices)
    print(f"Loaded {count} instruments.")

    scenarios = await load_scenarios()
    print(f"Loaded {scenarios} scenario scripts.")

    operators = await create_operators()
    if operators:
        print("\nOperator accounts (these passwords are shown once):")
        print(f"  {'name':<20} {'login':<12} password")
        for name, login, password in operators:
            print(f"  {name:<20} {login:<12} {password}")
    else:
        print("Operator accounts already exist; left untouched.")

    if args.demo_teams:
        teams = await create_demo_teams(args.demo_teams, args.demo_password)
        print(f"\nPractice teams ({len(teams)}):")
        print(f"  {'team':<20} {'code':<8} {'login':<12} password")
        for name, code, login, password in teams:
            print(f"  {name:<20} {code:<8} {login:<12} {password}")

    print("\nReady. Start the server with:  python -m app.main")
    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
