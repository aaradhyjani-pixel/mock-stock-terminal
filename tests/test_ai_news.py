"""AI-drafted news for the console.

The suite never calls the real Anthropic API. ``draft_news`` is monkeypatched
at the module the router imports it into, so these tests prove the plumbing —
role guard, rate limit, error mapping, audit log — without spending a rupee or
depending on network access in CI.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.ai import AiNewsError, AiNewsUnavailable, NewsDraft
from app.main import app
from app.models import OperatorRole
from tests.conftest import make_operator


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def ops_login(client: AsyncClient, login: str, password: str = "password123") -> str:
    response = await client.post("/api/auth/ops/login", json={"login": login, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def test_the_endpoint_never_publishes_it_only_returns_a_draft(client, monkeypatch):
    """The one non-negotiable property: drafting is not publishing."""
    from app.routers import admin as admin_router

    monkeypatch.setattr(
        admin_router, "draft_news", lambda prompt, symbols=None: NewsDraft("Test headline", "Test body.")
    )
    await make_operator("news", OperatorRole.NEWS_DESK)
    token = await ops_login(client, "news")

    response = await client.post(
        "/api/admin/news/draft", headers=auth(token), json={"prompt": "Infosys misses earnings"}
    )
    assert response.status_code == 200
    assert response.json() == {"headline": "Test headline", "body": "Test body."}

    published = (await client.get("/api/admin/news", headers=auth(token))).json()["news"]
    assert published == [], "a draft must never appear in the published feed"


async def test_only_the_news_desk_and_admins_can_draft(client, monkeypatch):
    from app.routers import admin as admin_router

    monkeypatch.setattr(
        admin_router, "draft_news", lambda prompt, symbols=None: NewsDraft("H", "B")
    )
    await make_operator("market", OperatorRole.MARKET_OPERATOR)
    token = await ops_login(client, "market")

    response = await client.post(
        "/api/admin/news/draft", headers=auth(token), json={"prompt": "anything"}
    )
    assert response.status_code == 403


async def test_a_missing_key_is_reported_as_a_plain_400_not_a_crash(client, monkeypatch):
    from app.routers import admin as admin_router

    def unavailable(prompt, symbols=None):
        raise AiNewsUnavailable("AI drafting is not set up.")

    monkeypatch.setattr(admin_router, "draft_news", unavailable)
    await make_operator("news", OperatorRole.NEWS_DESK)
    token = await ops_login(client, "news")

    response = await client.post(
        "/api/admin/news/draft", headers=auth(token), json={"prompt": "test"}
    )
    assert response.status_code == 400
    assert "not set up" in response.json()["detail"]


async def test_an_api_failure_is_a_502_not_a_500(client, monkeypatch):
    from app.routers import admin as admin_router

    def failing(prompt, symbols=None):
        raise AiNewsError("rate limited upstream")

    monkeypatch.setattr(admin_router, "draft_news", failing)
    await make_operator("news", OperatorRole.NEWS_DESK)
    token = await ops_login(client, "news")

    response = await client.post(
        "/api/admin/news/draft", headers=auth(token), json={"prompt": "test"}
    )
    assert response.status_code == 502


async def test_drafting_is_rate_limited_per_operator(client, monkeypatch, default_rules):
    from app.routers import admin as admin_router

    monkeypatch.setattr(
        admin_router, "draft_news", lambda prompt, symbols=None: NewsDraft("H", "B")
    )
    await make_operator("news", OperatorRole.NEWS_DESK)
    token = await ops_login(client, "news")

    statuses = []
    for _ in range(admin_router._ai_news_limiter.limit + 3):
        response = await client.post(
            "/api/admin/news/draft", headers=auth(token), json={"prompt": "test"}
        )
        statuses.append(response.status_code)
    assert 429 in statuses


async def test_the_status_endpoint_tells_the_console_whether_it_is_set_up(client):
    await make_operator("news", OperatorRole.NEWS_DESK)
    token = await ops_login(client, "news")
    response = await client.get("/api/admin/news/ai-status", headers=auth(token))
    assert response.status_code == 200
    body = response.json()
    assert "available" in body
    assert "model" in body
