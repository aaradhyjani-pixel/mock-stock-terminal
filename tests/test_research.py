"""The participant research desk: paid, AI-drafted, and structurally blind to
the scenario scripts.

As with the news desk, the suite never calls the real Anthropic API -
``draft_research`` is monkeypatched at the module the router imports it into.
What these tests actually prove is the money and safety plumbing: it charges
the ledger, it refuses when a team cannot afford it, it never invents a real
brokerage's name, and it never receives anything from the scenario system in
the first place.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from app.ai import AiNewsUnavailable, ResearchDraft, _HOUSE_NAMES
from app.main import app
from app.models import LedgerKind
from tests.conftest import make_instrument, make_team, set_market


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def login(client: AsyncClient, login_name: str, password: str = "password123") -> str:
    response = await client.post("/api/auth/login", json={"login": login_name, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


FAKE_DRAFT = ResearchDraft(
    house_name="Meridian Street Research",
    rating="BUY",
    target_price=Decimal("1720.00"),
    headline="Initiates coverage with a BUY, target 1,720",
    body="Valuation looks reasonable against sector peers. Momentum is constructive.",
)


async def test_buying_a_report_charges_the_ledger_and_returns_it(client, monkeypatch):
    from app.routers import trading as trading_router

    monkeypatch.setattr(trading_router, "draft_research", lambda *a, **k: FAKE_DRAFT)
    await set_market()
    await make_instrument("TESTCO", "1600")
    await make_team("Alpha")
    token = await login(client, "alpha-1")

    response = await client.post("/api/research", headers=auth(token), json={"symbol": "TESTCO"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["house_name"] == "Meridian Street Research"
    assert body["rating"] == "BUY"
    assert body["cost"] == "1500.00"

    ledger = (await client.get("/api/ledger", headers=auth(token))).json()["ledger"]
    research_entries = [e for e in ledger if e["kind"] == LedgerKind.RESEARCH.value]
    assert len(research_entries) == 1
    assert research_entries[0]["amount"] == "-1500.00"

    portfolio = (await client.get("/api/portfolio", headers=auth(token))).json()
    assert portfolio["funds"]["cash"] == "998500.00"


async def test_a_team_that_cannot_afford_it_is_refused_before_any_ai_call(client, monkeypatch):
    from app.routers import trading as trading_router

    calls = []
    monkeypatch.setattr(
        trading_router, "draft_research", lambda *a, **k: calls.append(1) or FAKE_DRAFT
    )
    await set_market()
    await make_instrument("TESTCO", "1600")
    await make_team("Alpha", capital="100")
    token = await login(client, "alpha-1")

    response = await client.post("/api/research", headers=auth(token), json={"symbol": "TESTCO"})
    assert response.status_code == 400
    assert "cash" in response.json()["detail"].lower()
    assert calls == [], "must not spend an AI call on a request that cannot be paid for"


async def test_buying_the_same_symbol_twice_quickly_is_refused(client, monkeypatch):
    from app.routers import trading as trading_router

    monkeypatch.setattr(trading_router, "draft_research", lambda *a, **k: FAKE_DRAFT)
    await set_market()
    await make_instrument("TESTCO", "1600")
    await make_team("Alpha")
    token = await login(client, "alpha-1")

    first = await client.post("/api/research", headers=auth(token), json={"symbol": "TESTCO"})
    assert first.status_code == 200
    second = await client.post("/api/research", headers=auth(token), json={"symbol": "TESTCO"})
    assert second.status_code == 429
    assert "TESTCO" in second.json()["detail"]


async def test_a_missing_key_is_a_plain_400_not_a_crash(client, monkeypatch):
    from app.routers import trading as trading_router

    def unavailable(*a, **k):
        raise AiNewsUnavailable("The research desk is not set up.")

    monkeypatch.setattr(trading_router, "draft_research", unavailable)
    await set_market()
    await make_instrument("TESTCO", "1600")
    await make_team("Alpha")
    token = await login(client, "alpha-1")

    response = await client.post("/api/research", headers=auth(token), json={"symbol": "TESTCO"})
    assert response.status_code == 400
    assert "not set up" in response.json()["detail"]


async def test_a_declined_purchase_never_reaches_the_ledger(client, monkeypatch):
    """If the AI call fails after the affordability check, no cash should move."""
    from app.ai import AiNewsError
    from app.routers import trading as trading_router

    def failing(*a, **k):
        raise AiNewsError("upstream failure")

    monkeypatch.setattr(trading_router, "draft_research", failing)
    await set_market()
    await make_instrument("TESTCO", "1600")
    await make_team("Alpha")
    token = await login(client, "alpha-1")

    response = await client.post("/api/research", headers=auth(token), json={"symbol": "TESTCO"})
    assert response.status_code == 502

    portfolio = (await client.get("/api/portfolio", headers=auth(token))).json()
    assert portfolio["funds"]["cash"] == "1000000.00", "a failed draft must not charge the team"


async def test_bought_reports_can_be_reread_for_free(client, monkeypatch):
    from app.routers import trading as trading_router

    monkeypatch.setattr(trading_router, "draft_research", lambda *a, **k: FAKE_DRAFT)
    await set_market()
    await make_instrument("TESTCO", "1600")
    await make_team("Alpha")
    token = await login(client, "alpha-1")

    await client.post("/api/research", headers=auth(token), json={"symbol": "TESTCO"})
    listing = await client.get("/api/research", headers=auth(token))
    assert listing.status_code == 200
    reports = listing.json()["reports"]
    assert len(reports) == 1
    assert reports[0]["symbol"] == "TESTCO"


def test_only_invented_house_names_are_ever_used():
    """The one property that matters most here: no real brokerage's name can
    come out of this feature, because the pool it draws from never contains
    one. Names picked deliberately unlike any real Indian brokerage."""
    forbidden_fragments = (
        "zerodha", "groww", "upstox", "angel", "motilal", "icici", "hdfc securities",
        "kotak securities", "edelweiss", "sharekhan", "5paisa", "paytm money",
    )
    for house in _HOUSE_NAMES:
        lowered = house.lower()
        for fragment in forbidden_fragments:
            assert fragment not in lowered, f"{house!r} resembles a real brokerage"


async def test_research_status_reports_unavailable_with_no_key(client):
    response = await client.get("/api/research/status")
    assert response.status_code == 200
    body = response.json()
    assert "available" in body
    assert "cost" in body
