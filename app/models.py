"""Database schema.

Two invariants govern this schema and the tests enforce both:

1. ``ledger`` is append-only and is the truth about cash. ``teams.cash`` is a
   cache of ``SUM(ledger.amount)`` for that team. Nothing may write to
   ``teams.cash`` without writing the matching ledger row in the same
   transaction.
2. ``positions.qty`` is signed. Positive is long, negative is short. A team is
   never simultaneously long and short the same instrument, so one row per
   (team, symbol) is enough and netting is done at fill time.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .money import Money


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    type_annotation_map = {Decimal: Money}


def _enum(py_enum, name: str):
    """Store enums as readable strings. CSV exports stay human-readable and the
    two database backends behave identically."""
    return Enum(py_enum, name=name, native_enum=False, length=32, validate_strings=True)


# High-volume tables (ticks, orders, fills, ledger) need a 64-bit key on
# PostgreSQL. SQLite only auto-increments a column declared exactly INTEGER, so
# it gets the plain type there; its rowids are 64-bit regardless.
BigIntPK = BigInteger().with_variant(Integer, "sqlite")


# ---------------------------------------------------------------- enumerations


class MarketState(str, enum.Enum):
    PRE_OPEN = "PRE_OPEN"
    OPEN = "OPEN"
    HALTED = "HALTED"  # market-wide halt; the clock keeps running
    CLOSED = "CLOSED"  # between trading days
    FROZEN = "FROZEN"  # incident stop; the clock stops too
    FINAL = "FINAL"  # competition over, rankings locked

    @property
    def accepts_orders(self) -> bool:
        return self in (MarketState.OPEN, MarketState.PRE_OPEN)

    @property
    def ticking(self) -> bool:
        return self is MarketState.OPEN


class InstrumentStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    HALTED = "HALTED"
    SUSPENDED = "SUSPENDED"
    UPPER_CIRCUIT = "UPPER_CIRCUIT"
    LOWER_CIRCUIT = "LOWER_CIRCUIT"

    @property
    def tradable(self) -> bool:
        return self in (
            InstrumentStatus.ACTIVE,
            InstrumentStatus.UPPER_CIRCUIT,
            InstrumentStatus.LOWER_CIRCUIT,
        )


class TeamStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    FROZEN = "FROZEN"  # temporarily blocked by the help desk
    BUSTED = "BUSTED"  # equity hit zero; out of the competition
    DISQUALIFIED = "DISQUALIFIED"


class MemberRole(str, enum.Enum):
    MEMBER = "MEMBER"
    CAPTAIN = "CAPTAIN"


class OperatorRole(str, enum.Enum):
    SUPER_ADMIN = "SUPER_ADMIN"
    MARKET_OPERATOR = "MARKET_OPERATOR"
    NEWS_DESK = "NEWS_DESK"
    HELP_DESK = "HELP_DESK"
    JUDGE = "JUDGE"
    PROJECTOR = "PROJECTOR"


class OrderSide(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> "OrderSide":
        return OrderSide.SELL if self is OrderSide.BUY else OrderSide.BUY

    @property
    def sign(self) -> int:
        return 1 if self is OrderSide.BUY else -1


class OrderType(str, enum.Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL_M = "SL_M"  # stop-loss market
    SL_L = "SL_L"  # stop-loss limit

    @property
    def is_stop(self) -> bool:
        return self in (OrderType.SL_M, OrderType.SL_L)


class OrderStatus(str, enum.Enum):
    PENDING = "PENDING"  # accepted, resting, not yet triggered or filled
    TRIGGERED = "TRIGGERED"  # stop crossed, now working as market or limit
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"

    @property
    def is_open(self) -> bool:
        return self in (OrderStatus.PENDING, OrderStatus.TRIGGERED)

    @property
    def is_terminal(self) -> bool:
        return not self.is_open


class OrderTag(str, enum.Enum):
    NORMAL = "NORMAL"
    MARGIN = "MARGIN"  # placed by the risk engine to cover a short
    SQUARE_OFF = "SQUARE_OFF"  # placed by an operator or at bust


class LedgerKind(str, enum.Enum):
    OPENING = "OPENING"
    TRADE = "TRADE"
    FEE = "FEE"
    BORROW_FEE = "BORROW_FEE"
    DIVIDEND = "DIVIDEND"
    ADJUSTMENT = "ADJUSTMENT"
    BONUS = "BONUS"


class NewsKind(str, enum.Enum):
    NEWS = "NEWS"
    RUMOUR = "RUMOUR"
    RESULTS = "RESULTS"
    MACRO = "MACRO"
    REGULATORY = "REGULATORY"
    ANNOUNCEMENT = "ANNOUNCEMENT"


class PriceActionKind(str, enum.Enum):
    MOVE = "MOVE"
    JUMP = "JUMP"
    VOLATILITY = "VOLATILITY"
    BAND = "BAND"
    HALT = "HALT"
    RESUME = "RESUME"
    DIVIDEND = "DIVIDEND"
    SPLIT = "SPLIT"


# -------------------------------------------------------------------- accounts


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    cash: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    status: Mapped[TeamStatus] = mapped_column(
        _enum(TeamStatus, "team_status"), default=TeamStatus.ACTIVE
    )
    busted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    college: Mapped[str | None] = mapped_column(String(120))
    contact_email: Mapped[str | None] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    members: Mapped[list["Member"]] = relationship(back_populates="team", cascade="all, delete-orphan")
    positions: Mapped[list["Position"]] = relationship(back_populates="team", cascade="all, delete-orphan")

    @property
    def can_trade(self) -> bool:
        return self.status is TeamStatus.ACTIVE


class Member(Base):
    __tablename__ = "members"
    __table_args__ = (UniqueConstraint("login", name="uq_member_login"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    login: Mapped[str] = mapped_column(String(64))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[MemberRole] = mapped_column(_enum(MemberRole, "member_role"), default=MemberRole.MEMBER)
    # Bumping this invalidates every token already issued to this member, which
    # is how the help desk kicks someone off instantly.
    token_version: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    team: Mapped[Team] = relationship(back_populates="members")


class Operator(Base):
    __tablename__ = "operators"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    login: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[OperatorRole] = mapped_column(_enum(OperatorRole, "operator_role"))
    token_version: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ----------------------------------------------------------------- instruments


class Instrument(Base):
    __tablename__ = "instruments"

    symbol: Mapped[str] = mapped_column(String(24), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    sector: Mapped[str] = mapped_column(String(60), index=True)
    isin: Mapped[str | None] = mapped_column(String(24))

    # The price this instrument was seeded at. A split scales ``start_price``
    # (so that the index, which is measured against it, does not jump when the
    # units change) but never this. A competition reset restores from here, so
    # that starting over really does mean starting over.
    seed_price: Mapped[Decimal] = mapped_column()
    start_price: Mapped[Decimal] = mapped_column()
    last_price: Mapped[Decimal] = mapped_column()
    # The band is measured from this. Set at each day's open to the previous
    # close, so a band is a per-day construct exactly as on the real exchange.
    day_reference: Mapped[Decimal] = mapped_column()
    prev_close: Mapped[Decimal] = mapped_column()
    day_open: Mapped[Decimal] = mapped_column()
    day_high: Mapped[Decimal] = mapped_column()
    day_low: Mapped[Decimal] = mapped_column()
    day_volume: Mapped[int] = mapped_column(BigInteger, default=0)

    tick_size: Mapped[Decimal] = mapped_column(default=Decimal("0.05"))
    lot_size: Mapped[int] = mapped_column(Integer, default=1)
    spread_bps: Mapped[Decimal] = mapped_column(default=Decimal("10"))
    daily_vol_pct: Mapped[Decimal] = mapped_column(default=Decimal("1.5"))
    # Notional that can be traded in one slice before slippage starts.
    liquidity_notional: Mapped[Decimal] = mapped_column(default=Decimal("20000000"))
    band_pct: Mapped[Decimal] = mapped_column(default=Decimal("20"))
    index_weight: Mapped[Decimal] = mapped_column(default=Decimal("1"))
    # Multiplier on the instrument's noise, raised by a VOLATILITY action.
    vol_multiplier: Mapped[Decimal] = mapped_column(default=Decimal("1"))

    status: Mapped[InstrumentStatus] = mapped_column(
        _enum(InstrumentStatus, "instrument_status"), default=InstrumentStatus.ACTIVE
    )
    halt_reason: Mapped[str | None] = mapped_column(String(200))
    listed: Mapped[bool] = mapped_column(Boolean, default=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0)

    @property
    def band_low(self) -> Decimal:
        return self.day_reference * (Decimal("100") - self.band_pct) / Decimal("100")

    @property
    def band_high(self) -> Decimal:
        return self.day_reference * (Decimal("100") + self.band_pct) / Decimal("100")


class MarketStateRow(Base):
    """Exactly one row, id=1. History lives in ``audit_log``."""

    __tablename__ = "market_state"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    state: Mapped[MarketState] = mapped_column(
        _enum(MarketState, "market_state"), default=MarketState.CLOSED
    )
    day_no: Mapped[int] = mapped_column(Integer, default=0)
    total_days: Mapped[int] = mapped_column(Integer, default=5)
    session_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set while FROZEN so the remaining time can be restored on resume.
    frozen_remaining_seconds: Mapped[int | None] = mapped_column(Integer)
    banner: Mapped[str | None] = mapped_column(String(300))
    banner_severity: Mapped[str] = mapped_column(String(16), default="info")
    index_value: Mapped[Decimal] = mapped_column(default=Decimal("20000"))
    index_prev_close: Mapped[Decimal] = mapped_column(default=Decimal("20000"))
    breaker_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    leaderboard_blackout: Mapped[bool] = mapped_column(Boolean, default=False)
    changed_by: Mapped[int | None] = mapped_column(ForeignKey("operators.id"))
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ----------------------------------------------------------------- price data


class Tick(Base):
    __tablename__ = "ticks"
    __table_args__ = (Index("ix_ticks_symbol_ts", "symbol", "ts"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(24), ForeignKey("instruments.symbol"))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last: Mapped[Decimal] = mapped_column()
    bid: Mapped[Decimal] = mapped_column()
    ask: Mapped[Decimal] = mapped_column()
    volume: Mapped[int] = mapped_column(BigInteger, default=0)
    day_no: Mapped[int] = mapped_column(Integer, default=0)


class Candle(Base):
    __tablename__ = "candles"
    __table_args__ = (
        UniqueConstraint("symbol", "interval", "ts", name="uq_candle"),
        Index("ix_candles_lookup", "symbol", "interval", "ts"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(24), ForeignKey("instruments.symbol"))
    interval: Mapped[str] = mapped_column(String(8))  # "1m" | "5m"
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    o: Mapped[Decimal] = mapped_column()
    h: Mapped[Decimal] = mapped_column()
    low: Mapped[Decimal] = mapped_column("l")
    c: Mapped[Decimal] = mapped_column()
    v: Mapped[int] = mapped_column(BigInteger, default=0)
    day_no: Mapped[int] = mapped_column(Integer, default=0)


class PriceAction(Base):
    """An operator's intent for a price. The engine reads active rows every tick."""

    __tablename__ = "price_actions"
    __table_args__ = (Index("ix_price_actions_active", "symbol", "ends_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str | None] = mapped_column(String(24))  # null means market-wide
    sector: Mapped[str | None] = mapped_column(String(60))
    kind: Mapped[PriceActionKind] = mapped_column(_enum(PriceActionKind, "price_action_kind"))
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    price_before: Mapped[Decimal | None] = mapped_column()
    target_price: Mapped[Decimal | None] = mapped_column()
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    operator_id: Mapped[int | None] = mapped_column(ForeignKey("operators.id"))
    note: Mapped[str | None] = mapped_column(String(300))


# --------------------------------------------------------------------- trading


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("team_id", "client_order_id", name="uq_order_idempotency"),
        Index("ix_orders_team_created", "team_id", "created_at"),
        Index("ix_orders_working", "status", "symbol"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), index=True)
    member_id: Mapped[int | None] = mapped_column(ForeignKey("members.id"))
    client_order_id: Mapped[str | None] = mapped_column(String(64))
    symbol: Mapped[str] = mapped_column(String(24), ForeignKey("instruments.symbol"))

    side: Mapped[OrderSide] = mapped_column(_enum(OrderSide, "order_side"))
    order_type: Mapped[OrderType] = mapped_column(_enum(OrderType, "order_type"))
    qty: Mapped[int] = mapped_column(Integer)
    limit_price: Mapped[Decimal | None] = mapped_column()
    trigger_price: Mapped[Decimal | None] = mapped_column()
    slippage_tolerance_pct: Mapped[Decimal | None] = mapped_column()

    status: Mapped[OrderStatus] = mapped_column(_enum(OrderStatus, "order_status"))
    filled_qty: Mapped[int] = mapped_column(Integer, default=0)
    avg_price: Mapped[Decimal | None] = mapped_column()
    fees_total: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    reason: Mapped[str | None] = mapped_column(String(200))
    tag: Mapped[OrderTag] = mapped_column(_enum(OrderTag, "order_tag"), default=OrderTag.NORMAL)

    day_no: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    fills: Mapped[list["Fill"]] = relationship(back_populates="order", cascade="all, delete-orphan")


class Fill(Base):
    __tablename__ = "fills"
    __table_args__ = (Index("ix_fills_team_ts", "team_id", "ts"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), index=True)
    symbol: Mapped[str] = mapped_column(String(24))
    side: Mapped[OrderSide] = mapped_column(_enum(OrderSide, "fill_side"))
    qty: Mapped[int] = mapped_column(Integer)
    price: Mapped[Decimal] = mapped_column()
    gross: Mapped[Decimal] = mapped_column()
    fees_total: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    fees: Mapped[dict] = mapped_column(JSON, default=dict)
    realised_pnl: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    day_no: Mapped[int] = mapped_column(Integer, default=0)

    order: Mapped[Order] = relationship(back_populates="fills")


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (UniqueConstraint("team_id", "symbol", name="uq_position"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), index=True)
    symbol: Mapped[str] = mapped_column(String(24), ForeignKey("instruments.symbol"))
    qty: Mapped[int] = mapped_column(Integer, default=0)  # signed: <0 is short
    avg_cost: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    realised_pnl: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    fees_paid: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    team: Mapped[Team] = relationship(back_populates="positions")

    @property
    def is_short(self) -> bool:
        return self.qty < 0


class LedgerEntry(Base):
    """Append-only. Never updated, never deleted."""

    __tablename__ = "ledger"
    __table_args__ = (Index("ix_ledger_team_ts", "team_id", "ts"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), index=True)
    kind: Mapped[LedgerKind] = mapped_column(_enum(LedgerKind, "ledger_kind"))
    amount: Mapped[Decimal] = mapped_column()  # signed
    balance_after: Mapped[Decimal] = mapped_column()
    ref_type: Mapped[str | None] = mapped_column(String(24))
    ref_id: Mapped[int | None] = mapped_column(BigInteger)
    note: Mapped[str | None] = mapped_column(String(300))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    day_no: Mapped[int] = mapped_column(Integer, default=0)


# ------------------------------------------------------------- news, scenarios


class NewsItem(Base):
    __tablename__ = "news"

    id: Mapped[int] = mapped_column(primary_key=True)
    headline: Mapped[str] = mapped_column(String(240))
    body: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[NewsKind] = mapped_column(_enum(NewsKind, "news_kind"), default=NewsKind.NEWS)
    symbols: Mapped[list] = mapped_column(JSON, default=list)
    sectors: Mapped[list] = mapped_column(JSON, default=list)
    # Never shown to participants. Used for the post-event analytics only.
    sentiment: Mapped[str | None] = mapped_column(String(16))
    publish_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    retracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    author_id: Mapped[int | None] = mapped_column(ForeignKey("operators.id"))
    day_no: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Scenario(Base):
    __tablename__ = "scenarios"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    day_no: Mapped[int] = mapped_column(Integer, default=1)
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="IDLE")  # IDLE|PLAYING|PAUSED|DONE
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Accumulated pause time, so a paused scenario resumes at the right offset.
    paused_offset_seconds: Mapped[int] = mapped_column(Integer, default=0)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    steps: Mapped[list["ScenarioStep"]] = relationship(
        back_populates="scenario", cascade="all, delete-orphan", order_by="ScenarioStep.at_offset"
    )


class ScenarioStep(Base):
    __tablename__ = "scenario_steps"

    id: Mapped[int] = mapped_column(primary_key=True)
    scenario_id: Mapped[int] = mapped_column(ForeignKey("scenarios.id", ondelete="CASCADE"), index=True)
    at_offset: Mapped[int] = mapped_column(Integer)  # seconds from scenario start
    label: Mapped[str] = mapped_column(String(160), default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    skipped: Mapped[bool] = mapped_column(Boolean, default=False)

    scenario: Mapped[Scenario] = relationship(back_populates="steps")


# ------------------------------------------------------- scoring and oversight


class EquitySnapshot(Base):
    __tablename__ = "equity_snapshots"
    __table_args__ = (Index("ix_snapshots_team_ts", "team_id", "ts"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    cash: Mapped[Decimal] = mapped_column()
    long_mv: Mapped[Decimal] = mapped_column()
    short_mv: Mapped[Decimal] = mapped_column()
    equity: Mapped[Decimal] = mapped_column()
    rank: Mapped[int | None] = mapped_column(Integer)
    day_no: Mapped[int] = mapped_column(Integer, default=0)
    is_close: Mapped[bool] = mapped_column(Boolean, default=False)


class Adjustment(Base):
    """A cash correction. Requires a second operator to approve before it posts."""

    __tablename__ = "adjustments"

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), index=True)
    amount: Mapped[Decimal] = mapped_column()
    reason: Mapped[str] = mapped_column(String(300))
    requested_by: Mapped[int] = mapped_column(ForeignKey("operators.id"))
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("operators.id"))
    rejected_by: Mapped[int | None] = mapped_column(ForeignKey("operators.id"))
    ledger_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_ts", "ts"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    actor_type: Mapped[str] = mapped_column(String(16))  # operator | member | system
    actor_id: Mapped[int | None] = mapped_column(Integer)
    actor_name: Mapped[str | None] = mapped_column(String(80))
    action: Mapped[str] = mapped_column(String(64), index=True)
    target: Mapped[str | None] = mapped_column(String(120))
    before: Mapped[dict | None] = mapped_column(JSON)
    after: Mapped[dict | None] = mapped_column(JSON)
    ip: Mapped[str | None] = mapped_column(String(64))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now())
