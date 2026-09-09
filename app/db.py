"""Database engine, sessions, and the per-team lock that makes trading safe.

The system runs as a single process on purpose (see ``docs/architecture.md``).
That decision is what makes the concurrency story simple enough to prove: two
members of the same team tapping "Buy" at the same instant are serialised by an
``asyncio.Lock`` held for the whole read-check-write cycle, so the second one
sees the funds the first one spent.

On PostgreSQL we additionally take a row lock (``SELECT ... FOR UPDATE``) on the
team, so that a future second process, an ad-hoc psql session or a migration
script cannot interleave with a fill. On SQLite the row lock is a no-op and the
asyncio lock is the whole guarantee.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from .config import get_settings
from .models import Base

_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
_team_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
_engine_lock = asyncio.Lock()


def get_engine():
    global _engine, _session_factory
    if _engine is None:
        settings = get_settings()
        url = settings.database_url
        kwargs: dict = {"echo": False, "future": True}
        if url.startswith("sqlite"):
            # SQLite's own locking is coarse; one connection avoids "database is
            # locked" entirely, and a single process does not need a pool.
            kwargs["poolclass"] = NullPool
            kwargs["connect_args"] = {"timeout": 30}
        else:
            kwargs["pool_size"] = 20
            kwargs["max_overflow"] = 10
            # Managed Postgres closes idle connections; pre-ping trades a
            # round trip for never handing a dead connection to an order.
            kwargs["pool_pre_ping"] = True
            if settings.postgres_ssl_required:
                # asyncpg spells this differently from libpq, so the sslmode
                # parameter was stripped from the URL and reapplied here.
                kwargs["connect_args"] = {"ssl": "require"}
        _engine = create_async_engine(url, **kwargs)

        if url.startswith("sqlite"):

            synchronous = settings.sqlite_synchronous.upper()
            if synchronous not in {"OFF", "NORMAL", "FULL", "EXTRA"}:
                synchronous = "FULL"

            @event.listens_for(_engine.sync_engine, "connect")
            def _sqlite_pragmas(dbapi_conn, _record):
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA foreign_keys=ON")
                # FULL in production. With WAL and synchronous=NORMAL, a killed
                # container can lose the last few committed transactions, and
                # those transactions are somebody's trades.
                cur.execute(f"PRAGMA synchronous={synchronous}")
                cur.execute("PRAGMA busy_timeout=30000")
                cur.close()

        _session_factory = async_sessionmaker(
            _engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _session_factory is not None
    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """A transaction. Commits on success, rolls back on any exception."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    async with session_scope() as session:
        yield session


def team_lock(team_id: int) -> asyncio.Lock:
    """The lock that serialises everything touching one team's money."""
    return _team_locks[team_id]


@asynccontextmanager
async def locked_team(team_id: int) -> AsyncIterator[None]:
    async with team_lock(team_id):
        yield


async def lock_team_row(session: AsyncSession, team_id: int):
    """Take the database row lock for a team. No-op on SQLite."""
    from .models import Team

    settings = get_settings()
    stmt = None
    if settings.is_postgres:
        from sqlalchemy import select

        stmt = select(Team).where(Team.id == team_id).with_for_update()
    else:
        from sqlalchemy import select

        stmt = select(Team).where(Team.id == team_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def create_all() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def drop_all() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
    _team_locks.clear()


async def healthcheck() -> bool:
    try:
        async with session_scope() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
