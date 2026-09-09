"""Dividends, splits, and the practice-mode reset.

The property that matters for both corporate actions is that they are
value-neutral at the moment they happen. A dividend moves value from the share
price into cash; a split changes the units and nothing else. If either one
quietly creates or destroys money, the leaderboard becomes a lie and the ledger
invariant catches it later with no explanation of where it went.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.db import session_scope
from app.engine import matching
from app.main import app
from app.models import (
    Instrument,
    LedgerEntry,
    LedgerKind,
    MarketState,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    OperatorRole,
    Position,
    Team,
)

from tests.conftest import make_instrument, make_operator, make_team, set_market


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def ops_token(client: AsyncClient, login: str = "market") -> dict:
    response = await client.post(
        "/api/auth/ops/login", json={"login": login, "password": "password123"}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def trade(team_id: int, side: OrderSide, qty: int) -> None:
    async with session_scope() as session:
        team = (await session.execute(select(Team).where(Team.id == team_id))).scalar_one()
        await matching.submit_order(
            session, team=team, member_id=None, symbol="TESTCO", side=side,
            order_type=OrderType.MARKET, qty=qty, market_state=MarketState.OPEN, day_no=1,
        )


async def valuation_of(team_id: int):
    async with session_scope() as session:
        team = (await session.execute(select(Team).where(Team.id == team_id))).scalar_one()
        return await matching.valuate_team(session, team)


# ---------------------------------------------------------------- dividends


@pytest.mark.usefixtures("no_fees", "no_slippage")
async def test_a_dividend_leaves_account_value_exactly_unchanged(client: AsyncClient):
    """The whole lesson of a dividend, asserted as an equality."""
    await set_market(MarketState.OPEN)
    await make_instrument("TESTCO", "100")
    await make_operator("market", OperatorRole.MARKET_OPERATOR)
    holder = await make_team("Holder")

    await trade(holder, OrderSide.BUY, 1000)
    before, _ = await valuation_of(holder)

    await set_market(MarketState.CLOSED)
    headers = await ops_token(client)
    response = await client.post(
        "/api/admin/prices/dividend",
        headers=headers,
        json={"symbol": "TESTCO", "amount_per_share": "5", "confirm": "TESTCO"},
    )
    assert response.status_code == 200, response.text

    after, _ = await valuation_of(holder)
    assert after.equity == before.equity, "a dividend moves value, it does not create it"
    assert after.cash == before.cash + Decimal("5000"), "1000 shares at Rs 5"
    assert after.long_mv == before.long_mv - Decimal("5000"), "the price went ex-dividend"


@pytest.mark.usefixtures("no_fees", "no_slippage")
async def test_a_short_seller_pays_the_dividend(client: AsyncClient):
    """Whoever lent them the stock is entitled to it, so the short pays."""
    await set_market(MarketState.OPEN)
    await make_instrument("TESTCO", "100")
    await make_operator("market", OperatorRole.MARKET_OPERATOR)
    shorter = await make_team("Shorter")

    await trade(shorter, OrderSide.SELL, 500)
    before, _ = await valuation_of(shorter)

    await set_market(MarketState.CLOSED)
    headers = await ops_token(client)
    await client.post(
        "/api/admin/prices/dividend",
        headers=headers,
        json={"symbol": "TESTCO", "amount_per_share": "5", "confirm": "TESTCO"},
    )

    after, _ = await valuation_of(shorter)
    assert after.cash == before.cash - Decimal("2500"), "500 shares short at Rs 5, debited"
    assert after.equity == before.equity, "still value-neutral for the short"

    async with session_scope() as session:
        entry = (
            await session.execute(
                select(LedgerEntry).where(LedgerEntry.kind == LedgerKind.DIVIDEND)
            )
        ).scalar_one()
    assert entry.amount == Decimal("-2500")
    assert "short" in entry.note


async def test_a_dividend_needs_the_symbol_typed_and_a_stopped_market(client: AsyncClient):
    await set_market(MarketState.OPEN)
    await make_instrument("TESTCO", "100")
    await make_operator("market", OperatorRole.MARKET_OPERATOR)
    headers = await ops_token(client)

    live = await client.post(
        "/api/admin/prices/dividend",
        headers=headers,
        json={"symbol": "TESTCO", "amount_per_share": "5", "confirm": "TESTCO"},
    )
    assert live.status_code == 400
    assert "before paying a dividend" in live.json()["detail"]

    await set_market(MarketState.CLOSED)
    unconfirmed = await client.post(
        "/api/admin/prices/dividend",
        headers=headers,
        json={"symbol": "TESTCO", "amount_per_share": "5", "confirm": "yes"},
    )
    assert unconfirmed.status_code == 400
    assert "Type TESTCO" in unconfirmed.json()["detail"]


async def test_a_dividend_larger_than_the_share_price_is_refused(client: AsyncClient):
    await set_market(MarketState.CLOSED)
    await make_instrument("TESTCO", "100")
    await make_operator("market", OperatorRole.MARKET_OPERATOR)
    headers = await ops_token(client)

    response = await client.post(
        "/api/admin/prices/dividend",
        headers=headers,
        json={"symbol": "TESTCO", "amount_per_share": "150", "confirm": "TESTCO"},
    )
    assert response.status_code == 400
    assert "Check the amount" in response.json()["detail"]


# ------------------------------------------------------------------- splits


@pytest.mark.usefixtures("no_fees", "no_slippage")
async def test_a_split_leaves_position_value_and_equity_unchanged(client: AsyncClient):
    await set_market(MarketState.OPEN)
    await make_instrument("TESTCO", "100")
    await make_operator("market", OperatorRole.MARKET_OPERATOR)
    holder = await make_team("Holder")

    await trade(holder, OrderSide.BUY, 500)
    before, _ = await valuation_of(holder)

    await set_market(MarketState.CLOSED)
    headers = await ops_token(client)
    response = await client.post(
        "/api/admin/prices/split",
        headers=headers,
        json={"symbol": "TESTCO", "ratio": "2", "confirm": "TESTCO"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["new_price"] == "50.000000"

    after, positions = await valuation_of(holder)
    position = next(p for p in positions if p.symbol == "TESTCO")

    assert position.qty == 1000, "twice the shares"
    assert position.avg_cost == Decimal("50"), "at half the average cost"
    assert after.long_mv == before.long_mv, "worth exactly what it was worth"
    assert after.equity == before.equity


@pytest.mark.usefixtures("no_fees", "no_slippage")
async def test_a_split_cancels_resting_orders_and_says_why(client: AsyncClient):
    """A limit at the old price means nothing at the new one."""
    await set_market(MarketState.OPEN)
    await make_instrument("TESTCO", "100")
    await make_operator("market", OperatorRole.MARKET_OPERATOR)
    team_id = await make_team("Holder")

    async with session_scope() as session:
        team = (await session.execute(select(Team).where(Team.id == team_id))).scalar_one()
        await matching.submit_order(
            session, team=team, member_id=None, symbol="TESTCO", side=OrderSide.BUY,
            order_type=OrderType.LIMIT, qty=10, limit_price=Decimal("80"),
            market_state=MarketState.OPEN, day_no=1,
        )

    await set_market(MarketState.CLOSED)
    headers = await ops_token(client)
    response = await client.post(
        "/api/admin/prices/split",
        headers=headers,
        json={"symbol": "TESTCO", "ratio": "2", "confirm": "TESTCO"},
    )
    assert response.json()["orders_cancelled"] == 1

    async with session_scope() as session:
        order = (await session.execute(select(Order))).scalars().first()
    assert order.status is OrderStatus.CANCELLED
    assert "split" in order.reason


@pytest.mark.usefixtures("no_fees", "no_slippage")
async def test_a_reverse_split_works_the_same_way(client: AsyncClient):
    await set_market(MarketState.OPEN)
    await make_instrument("TESTCO", "100")
    await make_operator("market", OperatorRole.MARKET_OPERATOR)
    holder = await make_team("Holder")
    await trade(holder, OrderSide.BUY, 500)
    before, _ = await valuation_of(holder)

    await set_market(MarketState.CLOSED)
    headers = await ops_token(client)
    await client.post(
        "/api/admin/prices/split",
        headers=headers,
        json={"symbol": "TESTCO", "ratio": "0.5", "confirm": "TESTCO"},
    )

    after, positions = await valuation_of(holder)
    position = next(p for p in positions if p.symbol == "TESTCO")
    assert position.qty == 250, "half the shares"
    assert position.avg_cost == Decimal("200"), "at twice the cost"
    assert after.equity == before.equity


# ------------------------------------------------------------------- reset


@pytest.mark.usefixtures("no_fees", "no_slippage")
async def test_the_reset_returns_every_team_to_their_opening_balance(client: AsyncClient):
    await set_market(MarketState.OPEN)
    await make_instrument("TESTCO", "100")
    await make_operator("director", OperatorRole.SUPER_ADMIN)
    first = await make_team("Alpha")
    second = await make_team("Bravo")

    await trade(first, OrderSide.BUY, 200)
    await trade(second, OrderSide.SELL, 300)

    async with session_scope() as session:
        instrument = (
            await session.execute(select(Instrument).where(Instrument.symbol == "TESTCO"))
        ).scalar_one()
        instrument.last_price = Decimal("140")

    await set_market(MarketState.CLOSED)
    headers = await ops_token(client, "director")
    response = await client.post("/api/admin/reset-trading?confirm=RESET", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["teams_reset"] == 2

    from app.config import get_rules

    opening = get_rules().starting_capital
    for team_id in (first, second):
        valuation, positions = await valuation_of(team_id)
        assert valuation.cash == opening
        assert valuation.equity == opening
        assert all(p.qty == 0 for p in positions)

    async with session_scope() as session:
        assert (await session.execute(select(func.count(Order.id)))).scalar_one() == 0
        assert (await session.execute(select(func.count(Position.id)))).scalar_one() == 0
        instrument = (
            await session.execute(select(Instrument).where(Instrument.symbol == "TESTCO"))
        ).scalar_one()
        assert instrument.last_price == Decimal("100"), "prices back to their start"
        assert instrument.day_volume == 0


@pytest.mark.usefixtures("no_fees", "no_slippage")
async def test_the_books_still_reconcile_after_a_reset(client: AsyncClient):
    """The invariant check is the whole point: a reset must not leave a gap."""
    await set_market(MarketState.OPEN)
    await make_instrument("TESTCO", "100")
    await make_operator("director", OperatorRole.SUPER_ADMIN)
    team_id = await make_team("Alpha")
    await trade(team_id, OrderSide.BUY, 100)

    await set_market(MarketState.CLOSED)
    headers = await ops_token(client, "director")
    await client.post("/api/admin/reset-trading?confirm=RESET", headers=headers)

    result = (await client.post("/api/admin/invariants/check", headers=headers)).json()
    assert result["ok"] is True
    assert result["mismatches"] == []

    async with session_scope() as session:
        entries = list((await session.execute(select(LedgerEntry))).scalars())
    assert len(entries) == 1, "exactly one opening row per team, nothing left over"
    assert entries[0].kind is LedgerKind.OPENING


async def test_the_reset_keeps_teams_members_and_the_audit_log(client: AsyncClient):
    """Nobody should need a new card because the organisers wanted a clean slate."""
    from app.models import AuditLog, Member

    await set_market(MarketState.CLOSED)
    await make_operator("director", OperatorRole.SUPER_ADMIN)
    await make_team("Alpha")

    headers = await ops_token(client, "director")
    await client.post("/api/admin/reset-trading?confirm=RESET", headers=headers)

    async with session_scope() as session:
        assert (await session.execute(select(func.count(Team.id)))).scalar_one() == 1
        assert (await session.execute(select(func.count(Member.id)))).scalar_one() == 1
        actions = [
            row.action
            for row in (await session.execute(select(AuditLog))).scalars()
        ]
    assert "competition.reset" in actions


async def test_the_reset_refuses_without_the_word_and_while_open(client: AsyncClient):
    await set_market(MarketState.OPEN)
    await make_operator("director", OperatorRole.SUPER_ADMIN)
    headers = await ops_token(client, "director")

    unconfirmed = await client.post("/api/admin/reset-trading?confirm=yes", headers=headers)
    assert unconfirmed.status_code == 400
    assert "Type RESET" in unconfirmed.json()["detail"]

    live = await client.post("/api/admin/reset-trading?confirm=RESET", headers=headers)
    assert live.status_code == 400
    assert "Close or freeze" in live.json()["detail"]


async def test_only_a_super_admin_can_reset(client: AsyncClient):
    await set_market(MarketState.CLOSED)
    await make_operator("market", OperatorRole.MARKET_OPERATOR)
    headers = await ops_token(client, "market")
    response = await client.post("/api/admin/reset-trading?confirm=RESET", headers=headers)
    assert response.status_code == 403




@pytest.mark.usefixtures("no_fees", "no_slippage")
async def test_a_reset_undoes_a_split_back_to_the_seeded_price(client: AsyncClient):
    """A split scales start_price so the index does not jump. The reset has to
    look past that to the price the stock was actually seeded at, or starting
    over leaves every split stock permanently at the wrong price."""
    await set_market(MarketState.CLOSED)
    await make_instrument("TESTCO", "100")
    await make_operator("director", OperatorRole.SUPER_ADMIN)
    await make_operator("market", OperatorRole.MARKET_OPERATOR)

    market_headers = await ops_token(client, "market")
    await client.post(
        "/api/admin/prices/split",
        headers=market_headers,
        json={"symbol": "TESTCO", "ratio": "2", "confirm": "TESTCO"},
    )
    async with session_scope() as session:
        instrument = (
            await session.execute(select(Instrument).where(Instrument.symbol == "TESTCO"))
        ).scalar_one()
        assert instrument.last_price == Decimal("50")
        assert instrument.start_price == Decimal("50"), "scaled, so the index does not move"
        assert instrument.seed_price == Decimal("100"), "but the seed price is untouched"

    director_headers = await ops_token(client, "director")
    await client.post("/api/admin/reset-trading?confirm=RESET", headers=director_headers)

    async with session_scope() as session:
        instrument = (
            await session.execute(select(Instrument).where(Instrument.symbol == "TESTCO"))
        ).scalar_one()
    assert instrument.last_price == Decimal("100"), "back to where it was seeded"
    assert instrument.start_price == Decimal("100")
