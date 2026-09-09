"""Request and response shapes.

Requests are validated here so that no endpoint body has to defend itself.
Responses are built by the small serialisers at the bottom, which are the single
place where a Decimal becomes a string: money is sent to the browser as a string
so that JavaScript's floating-point number type never gets a chance to round
somebody's cash balance.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

from .models import (
    Fill,
    Instrument,
    LedgerEntry,
    NewsItem,
    Order,
    OrderSide,
    OrderType,
    Position,
)
from .money import ZERO, D, paise


# ------------------------------------------------------------------ requests


class LoginRequest(BaseModel):
    login: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)


class OrderRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=24)
    side: OrderSide
    order_type: OrderType = OrderType.MARKET
    qty: int = Field(gt=0, le=10_000_000)
    limit_price: Decimal | None = Field(default=None, gt=0)
    trigger_price: Decimal | None = Field(default=None, gt=0)
    slippage_tolerance_pct: Decimal | None = Field(default=None, ge=0, le=100)
    client_order_id: str | None = Field(default=None, max_length=64)

    @field_validator("symbol")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()


class PreviewRequest(BaseModel):
    symbol: str
    side: OrderSide
    qty: int = Field(gt=0)
    order_type: OrderType = OrderType.MARKET
    limit_price: Decimal | None = Field(default=None, gt=0)

    @field_validator("symbol")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()


class PasswordChange(BaseModel):
    member_id: int
    new_password: str = Field(min_length=6, max_length=64)


# --------------------------------------------------------- operator requests


class MoveRequest(BaseModel):
    symbol: str | None = None
    sector: str | None = None
    pct: Decimal | None = None
    target_price: Decimal | None = Field(default=None, gt=0)
    over_seconds: int = Field(default=120, ge=0, le=3600)
    note: str | None = Field(default=None, max_length=300)

    @field_validator("symbol")
    @classmethod
    def _upper(cls, v: str | None) -> str | None:
        return v.strip().upper() if v else v


class JumpRequest(BaseModel):
    symbol: str
    pct: Decimal | None = None
    target_price: Decimal | None = Field(default=None, gt=0)
    confirm: str = Field(description="Type the symbol to confirm a jump.")
    note: str | None = Field(default=None, max_length=300)

    @field_validator("symbol")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()


class VolatilityRequest(BaseModel):
    symbol: str | None = None
    sector: str | None = None
    multiplier: Decimal = Field(ge=0, le=20)
    over_seconds: int = Field(default=300, ge=0, le=7200)


class HaltRequest(BaseModel):
    symbol: str
    reason: str | None = Field(default=None, max_length=200)


class DividendRequest(BaseModel):
    symbol: str
    amount_per_share: Decimal = Field(gt=0)
    confirm: str = Field(description="Type the symbol to confirm.")
    note: str | None = Field(default=None, max_length=300)

    @field_validator("symbol")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()


class SplitRequest(BaseModel):
    symbol: str
    # 2 means a 2-for-1 split: twice the shares at half the price. Fractions are
    # allowed, so 0.5 is a reverse split.
    ratio: Decimal = Field(gt=0, le=100)
    confirm: str = Field(description="Type the symbol to confirm.")
    note: str | None = Field(default=None, max_length=300)

    @field_validator("symbol")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()


class BandRequest(BaseModel):
    symbol: str
    band_pct: Decimal = Field(gt=0, le=100)


class NewsRequest(BaseModel):
    headline: str = Field(min_length=3, max_length=240)
    body: str = Field(default="", max_length=4000)
    kind: str = "NEWS"
    symbols: list[str] = Field(default_factory=list)
    sectors: list[str] = Field(default_factory=list)
    sentiment: str | None = None
    publish_at: datetime | None = None

    @field_validator("symbols")
    @classmethod
    def _upper_all(cls, v: list[str]) -> list[str]:
        return [s.strip().upper() for s in v if s.strip()]


class BroadcastRequest(BaseModel):
    message: str = Field(min_length=1, max_length=300)
    severity: str = "info"


class FreezeRequest(BaseModel):
    message: str = Field(default="Market frozen by the organisers. Please hold.", max_length=300)
    confirm: str = Field(description="Type FREEZE to confirm.")


class AdjustmentRequest(BaseModel):
    team_id: int
    amount: Decimal
    reason: str = Field(min_length=5, max_length=300)


class TeamImportRow(BaseModel):
    team_name: str = Field(min_length=1, max_length=80)
    college: str | None = None
    contact_email: str | None = None
    members: list[str] = Field(default_factory=list)


class TeamImportRequest(BaseModel):
    teams: list[TeamImportRow]
    reset_existing: bool = False


class InstrumentUpsert(BaseModel):
    symbol: str
    name: str
    sector: str
    start_price: Decimal = Field(gt=0)
    tick_size: Decimal = Decimal("0.05")
    spread_bps: Decimal = Decimal("10")
    daily_vol_pct: Decimal = Decimal("1.5")
    liquidity_notional: Decimal = Decimal("20000000")
    band_pct: Decimal = Decimal("20")
    index_weight: Decimal = Decimal("1")
    lot_size: int = 1

    @field_validator("symbol")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()


# --------------------------------------------------------------- serialisers

TWO_DP = Decimal("0.01")


def rupees(value: Decimal | None) -> str | None:
    """Every money value the browser sees, as a two-decimal string.

    Two decimals always, so a column of numbers lines up and "990000.00" never
    appears next to "-10000" for the same kind of quantity. A string, so
    JavaScript's binary floating point never touches a rupee amount.
    """
    if value is None:
        return None
    return str(paise(value))


def percent(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return str(D(value).quantize(TWO_DP))


def instrument_row(instrument: Instrument) -> dict:
    change = instrument.last_price - instrument.prev_close
    change_pct = change / instrument.prev_close * 100 if instrument.prev_close > 0 else ZERO
    half = instrument.spread_bps / Decimal("20000")
    return {
        "symbol": instrument.symbol,
        "name": instrument.name,
        "sector": instrument.sector,
        "last": rupees(instrument.last_price),
        "bid": rupees(instrument.last_price * (1 - half)),
        "ask": rupees(instrument.last_price * (1 + half)),
        "prev_close": rupees(instrument.prev_close),
        "change": rupees(change),
        "change_pct": percent(change_pct),
        "open": rupees(instrument.day_open),
        "high": rupees(instrument.day_high),
        "low": rupees(instrument.day_low),
        "volume": instrument.day_volume,
        "status": instrument.status.value,
        "tick_size": str(instrument.tick_size.normalize()),
        "lot_size": instrument.lot_size,
        "band_low": rupees(instrument.band_low),
        "band_high": rupees(instrument.band_high),
        "halt_reason": instrument.halt_reason,
    }


def order_row(order: Order) -> dict:
    return {
        "id": order.id,
        "symbol": order.symbol,
        "side": order.side.value,
        "type": order.order_type.value,
        "qty": order.qty,
        "filled_qty": order.filled_qty,
        "remaining_qty": max(0, order.qty - order.filled_qty),
        "limit_price": rupees(order.limit_price),
        "trigger_price": rupees(order.trigger_price),
        "status": order.status.value,
        "avg_price": rupees(order.avg_price),
        "fees_total": rupees(order.fees_total),
        "reason": order.reason,
        "tag": order.tag.value,
        "member_id": order.member_id,
        "day_no": order.day_no,
        "created_at": order.created_at.isoformat() if order.created_at else None,
    }


def fill_row(fill: Fill) -> dict:
    return {
        "id": fill.id,
        "order_id": fill.order_id,
        "symbol": fill.symbol,
        "side": fill.side.value,
        "qty": fill.qty,
        "price": rupees(fill.price),
        "gross": rupees(fill.gross),
        "fees_total": rupees(fill.fees_total),
        "fees": fill.fees,
        "realised_pnl": rupees(fill.realised_pnl),
        "ts": fill.ts.isoformat() if fill.ts else None,
    }


def position_row(position: Position, mark: Decimal | None) -> dict:
    mark = mark if mark is not None else position.avg_cost
    qty = position.qty
    if qty > 0:
        unrealised = (mark - position.avg_cost) * qty
    elif qty < 0:
        unrealised = (position.avg_cost - mark) * abs(qty)
    else:
        unrealised = ZERO
    invested = position.avg_cost * abs(qty)
    return {
        "symbol": position.symbol,
        "qty": qty,
        "side": "LONG" if qty > 0 else ("SHORT" if qty < 0 else "FLAT"),
        "avg_cost": rupees(position.avg_cost),
        "last": rupees(mark),
        "value": rupees(mark * abs(qty)),
        "invested": rupees(invested),
        "unrealised_pnl": rupees(unrealised),
        "unrealised_pct": percent(unrealised / invested * 100) if invested > 0 else "0.00",
        "realised_pnl": rupees(position.realised_pnl),
        "fees_paid": rupees(position.fees_paid),
    }


def ledger_row(entry: LedgerEntry) -> dict:
    return {
        "id": entry.id,
        "kind": entry.kind.value,
        "amount": rupees(entry.amount),
        "balance_after": rupees(entry.balance_after),
        "note": entry.note,
        "day_no": entry.day_no,
        "ts": entry.ts.isoformat() if entry.ts else None,
    }


def news_row(item: NewsItem, *, for_operator: bool = False) -> dict:
    row = {
        "id": item.id,
        "headline": item.headline,
        "body": item.body,
        "kind": item.kind.value if hasattr(item.kind, "value") else item.kind,
        "symbols": item.symbols or [],
        "sectors": item.sectors or [],
        "day_no": item.day_no,
        "published_at": item.published_at.isoformat() if item.published_at else None,
        "retracted": item.retracted_at is not None,
    }
    if for_operator:
        # Sentiment is an operator's own note about what a headline is meant to
        # do. Participants must never see it; working that out is the game.
        row["sentiment"] = item.sentiment
        row["publish_at"] = item.publish_at.isoformat() if item.publish_at else None
    return row
