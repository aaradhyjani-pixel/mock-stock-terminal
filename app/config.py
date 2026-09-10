"""Infrastructure settings and competition rules.

Two different kinds of configuration live here and they are deliberately kept
apart:

* :class:`Settings` is infrastructure. Database URL, secret key, port. It comes
  from the environment and changes between laptop, staging and the event VPS.
* :class:`Rules` is the rulebook. Starting capital, leverage, fee rates, session
  lengths. It comes from ``config/rules.yaml``, it is what the participant
  rulebook documents, and it must not change once the market has opened.

Keeping the rulebook in one validated file means the number in the terminal, the
number in the tests and the number handed to participants on paper all come from
the same place.
"""

from __future__ import annotations

import functools
import os
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"

# Anything running with this key can have its session tokens forged by anyone
# who has read the source. The app warns loudly at startup if it is still set.
DEFAULT_SECRET_KEY = "dev-only-secret-change-me-before-the-event"


class Settings(BaseSettings):
    """Infrastructure configuration, from the environment."""

    model_config = SettingsConfigDict(env_prefix="EXCHANGE_", env_file=".env", extra="ignore")

    database_url: str = f"sqlite+aiosqlite:///{BASE_DIR / 'data' / 'exchange.db'}"
    secret_key: str = DEFAULT_SECRET_KEY

    # How hard SQLite fsyncs before reporting a write committed. FULL survives
    # the container being killed mid-write, which is the failure mode that
    # matters when the thing being written is a team's cash balance. NORMAL is
    # faster and is what the test suite uses.
    sqlite_synchronous: str = "FULL"
    rules_file: Path = CONFIG_DIR / "rules.yaml"
    scenario_dir: Path = CONFIG_DIR / "scenarios"

    host: str = "0.0.0.0"
    port: int = 8000
    # Session lifetimes. Access tokens are short; the refresh cookie carries the
    # session across a phone locking itself for the length of a trading day.
    access_token_minutes: int = 30
    refresh_token_hours: int = 14

    # Set false on the event VPS only if it is served over plain HTTP, which it
    # should not be. Controls the Secure flag on the refresh cookie.
    secure_cookies: bool = False
    # Origins allowed to call this API with credentials. Set this when the front
    # end is served from somewhere else, for example a Vercel deployment:
    #   EXCHANGE_CORS_ORIGINS='["https://terminal.vercel.app"]'
    cors_origins: list[str] = Field(default_factory=list)

    # Turning this off stops the tick loop from starting, which is what the test
    # suite wants: tests drive the engine one tick at a time, deterministically.
    run_engine: bool = True
    log_level: str = "INFO"

    # Set by _adopt_platform_database when the URL asked for TLS.
    _ssl_required: bool = False

    @model_validator(mode="after")
    def _adopt_platform_database(self) -> "Settings":
        """Use the hosting platform's database when one is provided.

        Replit, Heroku and most managed hosts hand you a PostgreSQL connection
        string in ``DATABASE_URL``. If the operator has not named a database
        explicitly, use that one: managed Postgres is durable in a way that a
        file on a container's disk is not, and a redeploy must not be able to
        take a team's cash balance with it.

        An explicit ``EXCHANGE_DATABASE_URL`` always wins, so nothing here can
        surprise someone who has already made the decision.
        """
        if os.environ.get("EXCHANGE_DATABASE_URL"):
            return self
        platform_url = os.environ.get("DATABASE_URL")
        if platform_url and platform_url.startswith("postgres"):
            self.database_url = normalise_postgres_url(platform_url)
        return self

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgres")

    @property
    def postgres_ssl_required(self) -> bool:
        """Managed Postgres is almost always behind TLS.

        ``sslmode`` is libpq's spelling and asyncpg rejects it, so the flag is
        stripped from the URL and turned into a connect argument instead.
        """
        return self._ssl_required

    @property
    def using_default_secret(self) -> bool:
        return self.secret_key == DEFAULT_SECRET_KEY

    @property
    def split_frontend(self) -> bool:
        """True when the pages are served from a different origin than the API."""
        return bool(self.cors_origins)

    @property
    def cookie_samesite(self) -> str:
        """A cookie only travels cross-origin with SameSite=None, and browsers
        only accept SameSite=None over HTTPS. Same-origin keeps strict, which
        is the safer setting and costs nothing there."""
        return "none" if self.split_frontend else "strict"


class FeeRules(BaseModel):
    """Charges, modelled on an Indian discount broker's equity schedule.

    Every rate is a percentage of trade value unless named otherwise. Set
    ``enabled`` to false for a no-friction competition, or use the ``flat``
    preset to charge a single percentage per side.
    """

    enabled: bool = True
    mode: str = "realistic"  # "realistic" | "flat"

    flat_pct: Decimal = Decimal("0.10")

    brokerage_pct: Decimal = Decimal("0.03")
    brokerage_cap: Decimal = Decimal("20")
    stt_pct: Decimal = Decimal("0.10")
    stt_on_buy: bool = True
    exchange_pct: Decimal = Decimal("0.003")
    sebi_pct: Decimal = Decimal("0.0001")
    stamp_duty_pct: Decimal = Decimal("0.015")  # buy side only
    gst_pct: Decimal = Decimal("18")  # on brokerage + exchange charge

    @field_validator("mode")
    @classmethod
    def _known_mode(cls, v: str) -> str:
        if v not in {"realistic", "flat"}:
            raise ValueError("fee mode must be 'realistic' or 'flat'")
        return v


class MarginRules(BaseModel):
    """Short-selling margin. See ``docs/margin.md`` for the derivation.

    ``initial_pct`` of 20 is the 5x leverage cap in the brief: a team may hold
    short exposure of at most five times its account value.
    """

    initial_pct: Decimal = Decimal("20")
    maintenance_pct: Decimal = Decimal("12")
    warning_pct: Decimal = Decimal("15")
    # Where forced covering stops: the engine buys back until equity is at least
    # this percentage of the remaining short exposure.
    liquidate_to_pct: Decimal = Decimal("20")
    borrow_fee_pct_per_day: Decimal = Decimal("0.05")
    allow_long_leverage: bool = False

    @property
    def max_leverage(self) -> Decimal:
        return Decimal("100") / self.initial_pct


class SessionRules(BaseModel):
    trading_days: int = 5
    pre_open_seconds: int = 120
    open_seconds: int = 1500  # 25 minutes
    break_seconds: int = 300
    # The engine advances the day automatically when the timer runs out. Turn
    # this off to have the operator drive every transition by hand.
    auto_advance: bool = True
    tick_seconds: Decimal = Decimal("1")
    snapshot_seconds: int = 30
    leaderboard_seconds: int = 10
    public_leaderboard_size: int = 10
    blackout_last_seconds: int = 600


class MarketRules(BaseModel):
    band_pct: Decimal = Decimal("20")
    index_name: str = "CLUB 50"
    index_base: Decimal = Decimal("20000")
    market_breaker_pct: Decimal = Decimal("10")
    market_breaker_halt_seconds: int = 180
    # Order-flow impact: net traded notional in the last few seconds nudges the
    # price. Set the coefficient to 0 for a purely operator-driven tape.
    impact_enabled: bool = True
    impact_coefficient: Decimal = Decimal("0.15")
    impact_window_seconds: int = 5
    slippage_enabled: bool = True
    max_order_qty: int = 10_000
    max_orders_per_10s: int = 10
    default_slippage_tolerance_pct: Decimal = Decimal("1.0")


class Rules(BaseModel):
    """The whole rulebook, as loaded from ``config/rules.yaml``."""

    competition_name: str = "Mock Market Championship"
    starting_capital: Decimal = Decimal("1000000")
    max_members_per_team: int = 5
    fees: FeeRules = Field(default_factory=FeeRules)
    margin: MarginRules = Field(default_factory=MarginRules)
    session: SessionRules = Field(default_factory=SessionRules)
    market: MarketRules = Field(default_factory=MarketRules)

    @classmethod
    def load(cls, path: Path | None = None) -> "Rules":
        path = path or get_settings().rules_file
        if not path.exists():
            return cls()
        raw = yaml.safe_load(path.read_text()) or {}
        return cls.model_validate(raw)


def normalise_postgres_url(url: str) -> str:
    """Turn a libpq connection string into one SQLAlchemy's asyncpg driver takes.

    Two differences matter. The scheme has to name the driver, and ``sslmode``
    is a libpq parameter that asyncpg does not understand; it is dropped here
    and reapplied as a connect argument in ``db.py``.
    """
    parts = urlsplit(url)
    scheme = "postgresql+asyncpg"
    query = [(k, v) for k, v in parse_qsl(parts.query) if k not in {"sslmode", "channel_binding"}]
    return urlunsplit((scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def platform_wants_ssl() -> bool:
    url = os.environ.get("DATABASE_URL", "")
    return "sslmode=" in url and "sslmode=disable" not in url


@functools.lru_cache
def get_settings() -> Settings:
    settings = Settings()
    object.__setattr__(settings, "_ssl_required", platform_wants_ssl())
    return settings


@functools.lru_cache
def get_rules() -> Rules:
    return Rules.load()


def reload_rules() -> Rules:
    """Drop the cached rulebook. Used by tests and by the operator console's
    ``reload config`` action, which is only permitted while the market is not
    open."""
    get_rules.cache_clear()
    return get_rules()


def reset_caches() -> None:
    """Drop both caches. Tests call this after changing the environment."""
    get_settings.cache_clear()
    get_rules.cache_clear()
