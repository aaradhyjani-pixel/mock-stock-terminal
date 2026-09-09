"""Charges.

The rule that matters here is that the itemised breakdown a participant sees
always adds up to the number taken out of their cash. Each component is rounded
to the paisa once, and the total is the sum of those rounded components.
"""

from __future__ import annotations

from decimal import Decimal

from app.config import FeeRules
from app.fees import compute_fees
from app.models import OrderSide
from app.money import ZERO, fmt_inr, paise, round_to_tick

REALISTIC = FeeRules()


def test_itemised_charges_sum_to_the_total():
    for value in ("1000", "12345.67", "999999.99", "10000000"):
        for side in (OrderSide.BUY, OrderSide.SELL):
            breakdown = compute_fees(Decimal(value), side, REALISTIC)
            assert sum(breakdown.items.values(), ZERO) == breakdown.total
            for amount in breakdown.items.values():
                assert amount == paise(amount), "every line is already at paisa precision"


def test_stamp_duty_is_charged_on_the_buy_side_only():
    buy = compute_fees(Decimal("100000"), OrderSide.BUY, REALISTIC)
    sell = compute_fees(Decimal("100000"), OrderSide.SELL, REALISTIC)
    assert buy.items["stamp_duty"] == Decimal("15.00")
    assert sell.items["stamp_duty"] == ZERO
    assert buy.total > sell.total


def test_brokerage_is_capped():
    """Twenty rupees or 0.03 per cent, whichever is lower."""
    small = compute_fees(Decimal("10000"), OrderSide.BUY, REALISTIC)
    assert small.items["brokerage"] == Decimal("3.00")  # 0.03% of 10,000

    large = compute_fees(Decimal("10000000"), OrderSide.BUY, REALISTIC)
    assert large.items["brokerage"] == Decimal("20.00")  # the cap


def test_gst_applies_to_brokerage_and_exchange_charge_only():
    breakdown = compute_fees(Decimal("100000"), OrderSide.BUY, REALISTIC)
    expected = paise((breakdown.items["brokerage"] + breakdown.items["exchange"]) * Decimal("0.18"))
    assert breakdown.items["gst"] == expected


def test_a_round_trip_on_one_lakh_costs_about_a_quarter_percent():
    """A sanity check on the schedule as a whole.

    If this number drifts far from reality the competition stops teaching what
    trading actually costs, which is half the point of charging fees at all.
    """
    buy = compute_fees(Decimal("100000"), OrderSide.BUY, REALISTIC)
    sell = compute_fees(Decimal("100000"), OrderSide.SELL, REALISTIC)
    round_trip = buy.total + sell.total
    assert Decimal("250") < round_trip < Decimal("300")


def test_flat_mode_charges_one_line():
    flat = FeeRules(mode="flat", flat_pct=Decimal("0.10"))
    breakdown = compute_fees(Decimal("100000"), OrderSide.BUY, flat)
    assert breakdown.total == Decimal("100.00")
    assert list(breakdown.items) == ["charges"]


def test_fees_can_be_switched_off_entirely():
    off = FeeRules(enabled=False)
    assert compute_fees(Decimal("100000"), OrderSide.BUY, off).total == ZERO


def test_zero_and_negative_values_are_free():
    assert compute_fees(ZERO, OrderSide.BUY, REALISTIC).total == ZERO
    assert compute_fees(Decimal("-5"), OrderSide.BUY, REALISTIC).total == ZERO


# ------------------------------------------------------------- money helpers


def test_prices_snap_to_the_tick_grid():
    assert round_to_tick("100.03", "0.05") == Decimal("100.050000")
    assert round_to_tick("100.02", "0.05") == Decimal("100.000000")
    assert round_to_tick("2512.37", "0.05") == Decimal("2512.350000")


def test_indian_number_formatting():
    assert fmt_inr("1000000") == "10,00,000.00"
    assert fmt_inr("100") == "100.00"
    assert fmt_inr("12345678.5") == "1,23,45,678.50"
    assert fmt_inr("-250000") == "-2,50,000.00"
    assert fmt_inr("1000000", decimals=0) == "10,00,000"
