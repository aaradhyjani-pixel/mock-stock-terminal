"""End-to-end tests through the HTTP API.

These cover the paths a participant's phone and an operator's laptop actually
take, and in particular the authorisation boundaries: a participant token must
never reach an operator route, and no team may read another team's book.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db import session_scope
from app.main import app
from app.models import MarketState, MarketStateRow, OperatorRole, Team
from tests.conftest import make_instrument, make_operator, make_team, set_market


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def login(client: AsyncClient, login_name: str, password: str = "password123") -> str:
    response = await client.post("/api/auth/login", json={"login": login_name, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


async def ops_login(client: AsyncClient, login_name: str, password: str = "password123") -> str:
    response = await client.post(
        "/api/auth/ops/login", json={"login": login_name, "password": password}
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ----------------------------------------------------------------------- auth


async def test_login_and_whoami(client: AsyncClient):
    team_id = await make_team("Alpha")
    token = await login(client, "alpha-1")

    response = await client.get("/api/auth/me", headers=auth(token))
    assert response.status_code == 200
    body = response.json()
    assert body["team"]["id"] == team_id
    assert body["member"]["role"] == "CAPTAIN"


async def test_a_wrong_password_says_what_to_do(client: AsyncClient):
    await make_team("Alpha")
    response = await client.post(
        "/api/auth/login", json={"login": "alpha-1", "password": "wrong"}
    )
    assert response.status_code == 401
    assert "card handed to your team" in response.json()["detail"]


async def test_an_unauthenticated_request_is_refused(client: AsyncClient):
    assert (await client.get("/api/portfolio")).status_code == 401


async def test_a_participant_token_cannot_reach_the_operator_console(client: AsyncClient):
    await make_team("Alpha")
    token = await login(client, "alpha-1")
    response = await client.get("/api/admin/teams", headers=auth(token))
    assert response.status_code == 403


async def test_an_operator_token_cannot_place_trades(client: AsyncClient):
    await make_operator("director")
    await make_instrument("TESTCO", "100")
    token = await ops_login(client, "director")
    response = await client.post(
        "/api/orders",
        headers=auth(token),
        json={"symbol": "TESTCO", "side": "BUY", "qty": 1},
    )
    assert response.status_code == 403


async def test_a_role_without_permission_cannot_move_prices(client: AsyncClient):
    await make_operator("helpdesk", OperatorRole.HELP_DESK)
    await make_instrument("TESTCO", "100")
    token = await ops_login(client, "helpdesk")
    response = await client.post(
        "/api/admin/prices/move",
        headers=auth(token),
        json={"symbol": "TESTCO", "pct": "5", "over_seconds": 60},
    )
    assert response.status_code == 403
    detail = response.json()["detail"]
    # The message has to name both the role they have and the one they need,
    # or an operator mid-event just sees a button that does not work.
    assert "help desk" in detail
    assert "market operator" in detail


async def test_revoking_a_session_takes_effect_immediately(client: AsyncClient):
    from app.models import Member

    team_id = await make_team("Alpha")
    token = await login(client, "alpha-1")
    assert (await client.get("/api/auth/me", headers=auth(token))).status_code == 200

    async with session_scope() as session:
        member = (
            await session.execute(select(Member).where(Member.team_id == team_id))
        ).scalar_one()
        member.token_version += 1

    assert (await client.get("/api/auth/me", headers=auth(token))).status_code == 401


# -------------------------------------------------------------------- trading


@pytest.mark.usefixtures("no_fees", "no_slippage")
async def test_a_full_trading_round_trip(client: AsyncClient):
    await set_market(MarketState.OPEN)
    await make_instrument("TESTCO", "100")
    await make_team("Alpha")
    token = await login(client, "alpha-1")

    preview = await client.post(
        "/api/orders/preview",
        headers=auth(token),
        json={"symbol": "TESTCO", "side": "BUY", "qty": 100},
    )
    assert preview.status_code == 200
    assert preview.json()["affordable"] is True
    assert preview.json()["estimated_price"] == "100.00"

    placed = await client.post(
        "/api/orders",
        headers=auth(token),
        json={"symbol": "TESTCO", "side": "BUY", "qty": 100},
    )
    assert placed.status_code == 201, placed.text
    assert placed.json()["status"] == "FILLED"

    portfolio = (await client.get("/api/portfolio", headers=auth(token))).json()
    assert portfolio["positions"][0]["symbol"] == "TESTCO"
    assert portfolio["positions"][0]["qty"] == 100
    assert portfolio["funds"]["cash"] == "990000.00"

    ledger = (await client.get("/api/ledger", headers=auth(token))).json()["ledger"]
    assert ledger[0]["kind"] == "TRADE"
    assert ledger[0]["amount"] == "-10000.00"

    sold = await client.post(
        "/api/orders",
        headers=auth(token),
        json={"symbol": "TESTCO", "side": "SELL", "qty": 100},
    )
    assert sold.json()["status"] == "FILLED"
    portfolio = (await client.get("/api/portfolio", headers=auth(token))).json()
    assert portfolio["positions"] == []
    assert portfolio["funds"]["cash"] == "1000000.00"


@pytest.mark.usefixtures("no_fees")
async def test_a_rejected_order_explains_itself_and_still_appears(client: AsyncClient):
    await set_market(MarketState.OPEN)
    await make_instrument("TESTCO", "100")
    await make_team("Alpha")
    token = await login(client, "alpha-1")

    placed = await client.post(
        "/api/orders",
        headers=auth(token),
        json={"symbol": "TESTCO", "side": "BUY", "qty": 100000},
    )
    body = placed.json()
    assert body["status"] == "REJECTED"
    assert "per order" in body["reason"]

    orders = (await client.get("/api/orders", headers=auth(token))).json()["orders"]
    assert orders[0]["status"] == "REJECTED"


async def test_orders_are_refused_when_the_market_is_closed(client: AsyncClient):
    await set_market(MarketState.CLOSED)
    await make_instrument("TESTCO", "100")
    await make_team("Alpha")
    token = await login(client, "alpha-1")
    body = (
        await client.post(
            "/api/orders", headers=auth(token), json={"symbol": "TESTCO", "side": "BUY", "qty": 1}
        )
    ).json()
    assert body["status"] == "REJECTED"
    assert "closed" in body["reason"].lower()


@pytest.mark.usefixtures("no_fees")
async def test_a_team_cannot_cancel_another_teams_order(client: AsyncClient):
    await set_market(MarketState.OPEN)
    await make_instrument("TESTCO", "100")
    await make_team("Alpha")
    await make_team("Bravo")

    alpha = await login(client, "alpha-1")
    bravo = await login(client, "bravo-1")

    placed = await client.post(
        "/api/orders",
        headers=auth(alpha),
        json={"symbol": "TESTCO", "side": "BUY", "qty": 10, "order_type": "LIMIT", "limit_price": "50"},
    )
    order_id = placed.json()["id"]

    stolen = await client.delete(f"/api/orders/{order_id}", headers=auth(bravo))
    assert stolen.status_code == 404

    own = await client.delete(f"/api/orders/{order_id}", headers=auth(alpha))
    assert own.status_code == 200
    assert own.json()["status"] == "CANCELLED"


@pytest.mark.usefixtures("no_fees")
async def test_the_rate_limiter_stops_order_spam(client: AsyncClient, default_rules):
    await set_market(MarketState.OPEN)
    await make_instrument("TESTCO", "100")
    await make_team("Alpha")
    token = await login(client, "alpha-1")

    statuses = []
    for _ in range(default_rules.market.max_orders_per_10s + 4):
        response = await client.post(
            "/api/orders", headers=auth(token), json={"symbol": "TESTCO", "side": "BUY", "qty": 1}
        )
        statuses.append(response.status_code)
    assert 429 in statuses


# ---------------------------------------------------------- market and board


async def test_the_market_endpoint_works_without_signing_in(client: AsyncClient):
    await set_market(MarketState.PRE_OPEN, day_no=2)
    body = (await client.get("/api/market")).json()
    assert body["state"] == "PRE_OPEN"
    assert body["day_no"] == 2
    assert body["rules"]["max_leverage"] == "5"


async def test_the_leaderboard_always_shows_you_your_own_rank(client: AsyncClient):
    await set_market(MarketState.OPEN)
    for name in ("Alpha", "Bravo", "Charlie"):
        await make_team(name)
    token = await login(client, "charlie-1")

    body = (await client.get("/api/leaderboard", headers=auth(token))).json()
    assert body["field_size"] == 3
    assert body["you"]["is_you"] is True

    # During a blackout the public list empties but your own row survives.
    async with session_scope() as session:
        state = (await session.execute(select(MarketStateRow))).scalar_one()
        state.leaderboard_blackout = True

    body = (await client.get("/api/leaderboard", headers=auth(token))).json()
    assert body["blackout"] is True
    assert body["rows"] == []
    assert body["you"] is not None


async def test_news_hides_the_operators_sentiment_note(client: AsyncClient):
    await make_operator("news", OperatorRole.NEWS_DESK)
    await make_team("Alpha")
    ops = await ops_login(client, "news")
    member = await login(client, "alpha-1")

    published = await client.post(
        "/api/admin/news",
        headers=auth(ops),
        json={"headline": "Something happened", "body": "Details.", "sentiment": "very_negative"},
    )
    assert published.status_code == 200
    assert published.json()["sentiment"] == "very_negative"

    feed = (await client.get("/api/news", headers=auth(member))).json()["news"]
    assert feed[0]["headline"] == "Something happened"
    assert "sentiment" not in feed[0]


# ------------------------------------------------------------------ operator


async def test_an_operator_can_run_the_market(client: AsyncClient):
    await make_operator("market", OperatorRole.MARKET_OPERATOR)
    await make_instrument("TESTCO", "100")
    token = await ops_login(client, "market")

    opened = await client.post("/api/market/open", headers=auth(token))
    assert opened.status_code == 404, "market control lives under /api/admin"

    opened = await client.post("/api/admin/market/open", headers=auth(token))
    assert opened.status_code == 200
    assert opened.json()["state"] == "OPEN"

    frozen = await client.post(
        "/api/admin/market/freeze", headers=auth(token), json={"confirm": "FREEZE", "message": "Hold."}
    )
    assert frozen.json()["state"] == "FROZEN"

    resumed = await client.post("/api/admin/market/resume?to=OPEN", headers=auth(token))
    assert resumed.json()["state"] == "OPEN"


async def test_a_jump_needs_the_symbol_typed_back(client: AsyncClient):
    await make_operator("market", OperatorRole.MARKET_OPERATOR)
    await make_instrument("TESTCO", "100")
    token = await ops_login(client, "market")

    wrong = await client.post(
        "/api/admin/prices/jump",
        headers=auth(token),
        json={"symbol": "TESTCO", "pct": "-20", "confirm": "yes"},
    )
    assert wrong.status_code == 400
    assert "Type TESTCO" in wrong.json()["detail"]

    right = await client.post(
        "/api/admin/prices/jump",
        headers=auth(token),
        json={"symbol": "TESTCO", "pct": "-20", "confirm": "TESTCO"},
    )
    assert right.status_code == 200


async def test_a_cash_adjustment_needs_two_operators(client: AsyncClient):
    await make_operator("director", OperatorRole.SUPER_ADMIN)
    await make_operator("deputy", OperatorRole.SUPER_ADMIN)
    team_id = await make_team("Alpha")

    director = await ops_login(client, "director")
    requested = await client.post(
        "/api/admin/adjustments",
        headers=auth(director),
        json={"team_id": team_id, "amount": "5000", "reason": "Platform fault during day 2"},
    )
    assert requested.status_code == 200
    adjustment_id = requested.json()["id"]

    self_approve = await client.post(
        f"/api/admin/adjustments/{adjustment_id}/approve", headers=auth(director)
    )
    assert self_approve.status_code == 403
    assert "second pair of eyes" in self_approve.json()["detail"]

    deputy = await ops_login(client, "deputy")
    approved = await client.post(
        f"/api/admin/adjustments/{adjustment_id}/approve", headers=auth(deputy)
    )
    assert approved.status_code == 200

    async with session_scope() as session:
        team = (await session.execute(select(Team).where(Team.id == team_id))).scalar_one()
    assert team.cash == Decimal("1005000")


@pytest.mark.usefixtures("no_fees")
async def test_the_invariant_check_passes_after_real_trading(client: AsyncClient):
    await set_market(MarketState.OPEN)
    await make_instrument("TESTCO", "100")
    await make_operator("judge", OperatorRole.JUDGE)
    await make_team("Alpha")

    member = await login(client, "alpha-1")
    for side in ("BUY", "SELL", "SELL", "BUY"):
        await client.post(
            "/api/orders", headers=auth(member), json={"symbol": "TESTCO", "side": side, "qty": 25}
        )

    judge = await ops_login(client, "judge")
    result = (await client.post("/api/admin/invariants/check", headers=auth(judge))).json()
    assert result["ok"] is True
    assert result["mismatches"] == []


async def test_exports_are_available_to_judges(client: AsyncClient):
    await make_operator("judge", OperatorRole.JUDGE)
    token = await ops_login(client, "judge")
    response = await client.get("/api/admin/export/ledger.csv", headers=auth(token))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "team_id" in response.text


async def test_the_health_endpoint_reports_the_engine(client: AsyncClient):
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["database"] is True
    assert "engine" in response.json()
