"""Account valuation, buying power, and the margin model.

The whole margin system is one line:

    available = cash - short_mv - initial_margin x short_mv

With ``initial_margin`` at 20 per cent that is ``cash - 1.2 x short_mv``, which
is exactly the "5x leverage on shorts" rule in the brief: short exposure can
reach five times account value and no further.

Every funds decision in the system is the same question asked of that one line:
*would this trade leave available funds below zero?* Both rules fall out of it.

* Buying a stock spends cash and adds nothing to collateral, so a long position
  must be paid for in full. Cash-only longs, as specified.
* Shorting credits the sale proceeds to cash but locks those proceeds plus a
  further 20 per cent, so the net cost of opening a short is 20 per cent of its
  value.

Deriving both from one formula is what stops the two rules drifting apart, which
is the usual source of arguments in a trading game.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from .config import MarginRules, get_rules
from .models import OrderSide, Position
from .money import ZERO, money, paise


class MarginState(str, Enum):
    OK = "OK"
    WARNING = "WARNING"
    CALL = "CALL"
    BUST = "BUST"


@dataclass(frozen=True)
class Valuation:
    """A team's account at one instant, marked to the given prices."""

    cash: Decimal
    long_mv: Decimal
    short_mv: Decimal
    equity: Decimal
    margin_required: Decimal
    available: Decimal
    maintenance_required: Decimal
    warning_threshold: Decimal
    unrealised_pnl: Decimal
    realised_pnl: Decimal

    @property
    def leverage(self) -> Decimal:
        """Gross short exposure as a multiple of account value."""
        if self.equity <= 0:
            return ZERO if self.short_mv == 0 else Decimal("999")
        return money(self.short_mv / self.equity)

    @property
    def margin_state(self) -> MarginState:
        if self.equity <= 0:
            return MarginState.BUST
        if self.short_mv <= 0:
            return MarginState.OK
        if self.equity < self.maintenance_required:
            return MarginState.CALL
        if self.equity < self.warning_threshold:
            return MarginState.WARNING
        return MarginState.OK

    @property
    def distance_to_call_pct(self) -> Decimal | None:
        """How far the short book can move against the team before a call.

        Returns the adverse percentage move in short positions that would put
        equity exactly at the maintenance requirement, or ``None`` when the team
        has no shorts. This is the number the terminal shows as a gauge, because
        it is the one number that tells a participant how much room they have.
        """
        if self.short_mv <= 0:
            return None
        rules = get_rules().margin
        maint = rules.maintenance_pct / Decimal("100")
        # equity - x*short_mv = maint * short_mv * (1 + x)   ->  solve for x
        denominator = self.short_mv * (Decimal("1") + maint)
        if denominator <= 0:
            return None
        x = (self.equity - maint * self.short_mv) / denominator
        return money(x * Decimal("100"))

    def as_dict(self) -> dict:
        return {
            "cash": str(paise(self.cash)),
            "long_mv": str(paise(self.long_mv)),
            "short_mv": str(paise(self.short_mv)),
            "equity": str(paise(self.equity)),
            "margin_required": str(paise(self.margin_required)),
            "available": str(paise(self.available)),
            "maintenance_required": str(paise(self.maintenance_required)),
            "unrealised_pnl": str(paise(self.unrealised_pnl)),
            "realised_pnl": str(paise(self.realised_pnl)),
            "leverage": str(self.leverage.quantize(Decimal("0.01"))),
            "margin_state": self.margin_state.value,
            "distance_to_call_pct": (
                str(self.distance_to_call_pct.quantize(Decimal("0.01")))
                if self.distance_to_call_pct is not None
                else None
            ),
        }


def valuate(
    cash: Decimal,
    positions: list[Position],
    marks: dict[str, Decimal],
    rules: MarginRules | None = None,
) -> Valuation:
    """Mark a team's book to ``marks`` and derive every funds number from it."""
    rules = rules or get_rules().margin
    long_mv = ZERO
    short_mv = ZERO
    unrealised = ZERO
    realised = ZERO

    for position in positions:
        realised += position.realised_pnl
        if position.qty == 0:
            continue
        mark = marks.get(position.symbol)
        if mark is None:
            # An instrument with no price is marked at its average cost, which
            # makes it P&L-neutral rather than silently worth zero.
            mark = position.avg_cost
        value = mark * abs(position.qty)
        if position.qty > 0:
            long_mv += value
            unrealised += (mark - position.avg_cost) * position.qty
        else:
            short_mv += value
            unrealised += (position.avg_cost - mark) * abs(position.qty)

    initial = rules.initial_pct / Decimal("100")
    maintenance = rules.maintenance_pct / Decimal("100")
    warning = rules.warning_pct / Decimal("100")

    margin_required = short_mv * initial
    equity = cash + long_mv - short_mv
    available = cash - short_mv - margin_required

    return Valuation(
        cash=money(cash),
        long_mv=money(long_mv),
        short_mv=money(short_mv),
        equity=money(equity),
        margin_required=money(margin_required),
        available=money(available),
        maintenance_required=money(short_mv * maintenance),
        warning_threshold=money(short_mv * warning),
        unrealised_pnl=money(unrealised),
        realised_pnl=money(realised),
    )


def project_available(
    valuation: Valuation,
    current_qty: int,
    side: OrderSide,
    qty: int,
    fill_price: Decimal,
    mark: Decimal,
    fees: Decimal,
    rules: MarginRules | None = None,
) -> Decimal:
    """Available funds *after* a hypothetical fill.

    ``current_qty`` is the team's signed position in that symbol before the
    trade. The order path accepts a trade when this is at or above zero, which
    is the single funds rule for buys, sells, covers and new shorts alike.
    """
    rules = rules or get_rules().margin
    initial = rules.initial_pct / Decimal("100")

    gross = fill_price * qty
    delta_cash = (-gross if side is OrderSide.BUY else gross) - fees

    new_qty = current_qty + side.sign * qty
    short_before = max(0, -current_qty)
    short_after = max(0, -new_qty)
    delta_short_mv = mark * (short_after - short_before)

    new_cash = valuation.cash + delta_cash
    new_short_mv = valuation.short_mv + delta_short_mv
    return money(new_cash - new_short_mv - new_short_mv * initial)


def max_affordable_qty(
    valuation: Valuation,
    current_qty: int,
    side: OrderSide,
    price: Decimal,
    rules: MarginRules | None = None,
) -> int:
    """Largest quantity that would still leave available funds at or above zero.

    Fees are approximated at a flat 0.15 per cent, which is above the realistic
    schedule, so the number shown as "max" is always actually placeable. The
    terminal uses this for its quantity slider.
    """
    if price <= 0:
        return 0
    rules = rules or get_rules().margin
    initial = rules.initial_pct / Decimal("100")
    fee_rate = Decimal("0.0015")

    if side is OrderSide.BUY:
        if current_qty < 0:
            # Covering a short only releases margin, so the whole short can
            # always be bought back.
            return abs(current_qty)
        per_share = price * (Decimal("1") + fee_rate)
    else:
        if current_qty > 0:
            return current_qty
        per_share = price * (initial + fee_rate)

    if per_share <= 0:
        return 0
    return max(0, int(valuation.available / per_share))


@dataclass(frozen=True)
class LiquidationLeg:
    symbol: str
    qty: int
    unrealised_loss: Decimal


def liquidation_plan(
    valuation: Valuation,
    positions: list[Position],
    marks: dict[str, Decimal],
    rules: MarginRules | None = None,
) -> list[LiquidationLeg]:
    """Which shorts to buy back, and how many, to clear a margin call.

    Worst loser first: the position that has moved furthest against the team is
    the one costing the most margin, so closing it restores the account fastest.
    The plan stops as soon as projected equity is back above
    ``liquidate_to_pct`` of the remaining short exposure.
    """
    rules = rules or get_rules().margin
    target = rules.liquidate_to_pct / Decimal("100")

    shorts = [p for p in positions if p.qty < 0 and marks.get(p.symbol) is not None]
    if not shorts:
        return []

    def loss(p: Position) -> Decimal:
        return (marks[p.symbol] - p.avg_cost) * abs(p.qty)

    shorts.sort(key=loss, reverse=True)

    equity = valuation.equity
    short_mv = valuation.short_mv
    legs: list[LiquidationLeg] = []

    for position in shorts:
        if short_mv <= 0 or equity >= target * short_mv:
            break
        mark = marks[position.symbol]
        available_qty = abs(position.qty)
        # Covering does not change equity (cash falls by exactly the value that
        # leaves short_mv), so each share covered reduces the requirement by
        # ``target * mark``. Solve for the shares needed.
        shortfall = target * short_mv - equity
        if mark <= 0:
            continue
        needed = int((shortfall / (target * mark)).to_integral_value(rounding="ROUND_CEILING"))
        take = max(1, min(available_qty, needed))
        legs.append(LiquidationLeg(position.symbol, take, loss(position)))
        short_mv -= mark * take

    return legs


def is_bust(valuation: Valuation) -> bool:
    return valuation.equity <= 0
