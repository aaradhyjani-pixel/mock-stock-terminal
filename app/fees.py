"""Trade charges.

Modelled on an Indian discount broker's equity schedule so that participants
learn what over-trading actually costs. Every component is rounded to the paisa
individually and the total is the sum of the rounded components, which means the
itemised breakdown a participant sees always adds up to the number deducted from
their cash. Nothing is ever rounded twice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .config import FeeRules, get_rules
from .models import OrderSide
from .money import ZERO, paise, pct


@dataclass(frozen=True)
class FeeBreakdown:
    total: Decimal = ZERO
    items: dict[str, Decimal] = field(default_factory=dict)

    def as_json(self) -> dict[str, str]:
        """JSON-safe form for the ``fills.fees`` column."""
        return {k: str(v) for k, v in self.items.items() if v != 0}

    def __add__(self, other: "FeeBreakdown") -> "FeeBreakdown":
        merged = dict(self.items)
        for key, value in other.items.items():
            merged[key] = merged.get(key, ZERO) + value
        return FeeBreakdown(total=self.total + other.total, items=merged)


NO_FEES = FeeBreakdown()


def compute_fees(
    value: Decimal,
    side: OrderSide,
    rules: FeeRules | None = None,
) -> FeeBreakdown:
    """Charges on a single fill of ``value`` rupees.

    ``value`` is price times quantity, always positive.
    """
    rules = rules or get_rules().fees
    if not rules.enabled or value <= 0:
        return NO_FEES

    if rules.mode == "flat":
        total = paise(pct(value, rules.flat_pct))
        return FeeBreakdown(total=total, items={"charges": total})

    is_buy = side is OrderSide.BUY

    brokerage = paise(min(rules.brokerage_cap, pct(value, rules.brokerage_pct)))
    exchange = paise(pct(value, rules.exchange_pct))
    sebi = paise(pct(value, rules.sebi_pct))
    stt = paise(pct(value, rules.stt_pct)) if (rules.stt_on_buy or not is_buy) else ZERO
    stamp = paise(pct(value, rules.stamp_duty_pct)) if is_buy else ZERO
    gst = paise(pct(brokerage + exchange, rules.gst_pct))

    items = {
        "brokerage": brokerage,
        "stt": stt,
        "exchange": exchange,
        "sebi": sebi,
        "stamp_duty": stamp,
        "gst": gst,
    }
    total = sum(items.values(), ZERO)
    return FeeBreakdown(total=total, items=items)


def estimate_fees(value: Decimal, side: OrderSide) -> Decimal:
    """Just the total. Used by the order preview and the funds check."""
    return compute_fees(value, side).total
