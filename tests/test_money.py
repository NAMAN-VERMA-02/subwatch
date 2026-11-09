from datetime import date
from types import SimpleNamespace

import pytest

from subwatch.money import (
    add_months,
    duration,
    inr,
    monthly,
    step,
    totals,
    usd_to_paise,
    when,
    yearly,
)


@pytest.mark.parametrize(
    "paise, text",
    [
        (0, "₹0"),
        (11900, "₹119"),
        (142800, "₹1,428"),
        (12950, "₹129.50"),
        (10000000, "₹1,00,000"),
        (12345678, "₹1,23,456.78"),
        (-26000, "-₹260"),
    ],
)
def test_inr_uses_indian_grouping(paise, text):
    assert inr(paise) == text


def test_converted_amounts_round_to_the_rupee():
    assert inr(196650, approx=True) == "≈₹1,967"


def test_add_months_clamps_the_day():
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2028, 1, 31), 1) == date(2028, 2, 29)
    assert add_months(date(2026, 12, 15), 1) == date(2027, 1, 15)
    assert add_months(date(2026, 11, 30), 3) == date(2027, 2, 28)


def test_step():
    assert step(date(2026, 11, 5), "28d") == date(2026, 12, 3)
    assert step(date(2026, 11, 5), "84d") == date(2027, 1, 28)
    assert step(date(2026, 11, 5), "year") == date(2027, 11, 5)
    assert step(date(2026, 11, 5), "month", 2) == date(2027, 1, 5)


def test_yearly_and_monthly_cost():
    assert yearly(11900, "month") == 142800  # Spotify ₹119 -> ₹1,428/year
    assert yearly(149900, "year") == 149900
    assert yearly(29900, "quarter") == 119600
    assert yearly(34900, "28d") == 454900  # 13.04 recharges = ₹4,549.46, to the nearest rupee
    assert yearly(71900, "84d") == 312400
    assert monthly(149900, "year") == 12492
    assert monthly(13000, "month") == 13000


def test_usd_includes_forex_markup():
    # $20 at ₹95 plus a 3.5% card markup
    assert usd_to_paise(2000, 95.0, 3.5) == 196650
    assert usd_to_paise(2000, 95.0, 0) == 190000


def test_when_and_duration():
    today = date(2026, 11, 9)
    assert when(today, today) == "today"
    assert when(date(2026, 11, 10), today) == "tomorrow"
    assert when(date(2026, 11, 12), today) == "in 3 days"
    assert when(date(2026, 11, 30), today) == "on 30 Nov"
    assert when(date(2027, 1, 3), today) == "on 3 Jan 2027"
    assert duration(45) == "45 days"
    assert duration(61) == "2 months"


def sub(amount, period, category="service", status="active", wasted=0, saved=0):
    return SimpleNamespace(
        amount=amount, period=period, category=category, status=status, wasted=wasted, saved=saved
    )


def test_totals():
    subs = [
        sub(11900, "month", wasted=23800),  # Spotify
        sub(149900, "year"),  # Prime
        sub(49900, "month", category="bill"),  # Airtel
        sub(2350000, "month", category="commitment"),  # EMI: separate line
        sub(64900, "month", status="ended", saved=129800),  # cancelled Netflix: not in burn
        sub(19900, "month", status="stopped"),  # stopped by itself: neither burn nor saved
    ]
    t = totals(subs, credits_30=500000)
    assert t["yearly_burn"] == 142800 + 149900 + 598800
    assert t["monthly_burn"] == (142800 + 149900 + 598800 + 6) // 12
    assert t["commitments_monthly"] == 2350000
    assert t["wasted"] == 23800
    assert t["saved"] == 129800
    assert t["credits_30"] == 500000
