"""Exact money and price arithmetic.

Every monetary quantity in this system is a ``decimal.Decimal``. Floats never
touch cash, prices, fees or P&L. The only floats in the codebase live in the
price-noise generator, and their output is quantised to the tick grid before it
becomes a price.

Storage: the :class:`Money` column type keeps six decimal places. On PostgreSQL
that is a native ``NUMERIC(24, 6)``. On SQLite (used for local development and
the test suite) ``NUMERIC`` would round-trip through a C double, so we store a
scaled integer instead and convert back exactly. The two backends therefore
agree bit for bit, which is what lets the test suite prove anything about the
production system.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, getcontext
from typing import Any

from sqlalchemy import BigInteger, Numeric
from sqlalchemy.types import TypeDecorator

# 28 significant digits is the default; our largest realistic value is a team's
# turnover over the event (~1e9) with six decimal places, so this is ample.
getcontext().prec = 34

MONEY_SCALE = Decimal("0.000001")  # 6 dp, the storage grid
PAISA = Decimal("0.01")  # 2 dp, the settlement grid for cash and fees
ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")

_SCALE_FACTOR = 10**6


def D(value: Any) -> Decimal:
    """Coerce to Decimal without ever going through float.

    Accepts int, str, Decimal, or float. Floats are converted via ``repr`` so
    that ``D(0.1)`` is ``Decimal('0.1')`` and not the full binary expansion.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(value)


def money(value: Any) -> Decimal:
    """Quantise to the storage grid (6 dp)."""
    return D(value).quantize(MONEY_SCALE, rounding=ROUND_HALF_UP)


def paise(value: Any) -> Decimal:
    """Quantise to the settlement grid (2 dp), rounding half up.

    Used for cash movements, fees and anything a participant sees as a rupee
    amount. Fills are rounded here individually; totals are sums of rounded
    fills, so a statement always adds up.
    """
    return D(value).quantize(PAISA, rounding=ROUND_HALF_UP)


def round_to_tick(price: Any, tick: Any) -> Decimal:
    """Snap a price to the instrument's tick grid.

    NSE quotes most equities on a 5-paisa grid. A price that is not on the grid
    is not a real price, so the engine snaps every price it produces and the
    API rejects every limit price that is not already snapped.
    """
    tick_d = D(tick)
    if tick_d <= 0:
        raise ValueError("tick size must be positive")
    steps = (D(price) / tick_d).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return money(steps * tick_d)


def floor_to_tick(price: Any, tick: Any) -> Decimal:
    tick_d = D(tick)
    steps = (D(price) / tick_d).quantize(Decimal("1"), rounding=ROUND_DOWN)
    return money(steps * tick_d)


def pct(value: Any, percent: Any) -> Decimal:
    """``percent`` per cent of ``value``. ``pct(200, 5)`` is ``10``."""
    return D(value) * D(percent) / HUNDRED


def bps(value: Any, basis_points: Any) -> Decimal:
    """``basis_points`` hundredths of a per cent of ``value``."""
    return D(value) * D(basis_points) / Decimal("10000")


def fmt_inr(value: Any, decimals: int = 2) -> str:
    """Format for display in the Indian numbering system: 12,34,567.89."""
    d = D(value).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    negative = d < 0
    digits = f"{abs(d):.{decimals}f}"
    whole, _, frac = digits.partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    out = whole + (f".{frac}" if decimals else "")
    return f"-{out}" if negative else out


class Money(TypeDecorator):
    """A ``Decimal`` column that is exact on both PostgreSQL and SQLite."""

    impl = Numeric(24, 6)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "sqlite":
            return dialect.type_descriptor(BigInteger())
        return dialect.type_descriptor(Numeric(24, 6))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        quantised = money(value)
        if dialect.name == "sqlite":
            return int(quantised.scaleb(6))
        return quantised

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if dialect.name == "sqlite":
            return Decimal(int(value)) / _SCALE_FACTOR
        return Decimal(value)
