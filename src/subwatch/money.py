"""Money math in integer paise, Indian digit grouping, and the sentences that state the loss."""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

# period: (num, den) so that yearly cost = amount * num // den
PER_YEAR = {"28d": (365, 28), "month": (12, 1), "84d": (365, 84), "quarter": (4, 1), "year": (1, 1)}
PERIOD_WORD = {
    "28d": "28 days",
    "month": "month",
    "84d": "84 days",
    "quarter": "quarter",
    "year": "year",
}
GRACE_DAYS = {"28d": 5, "month": 7, "84d": 10, "quarter": 15, "year": 30}


def add_months(d: date, n: int) -> date:
    """Calendar month add; 31 Jan + 1 month is 28/29 Feb."""
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def step(d: date, period: str, k: int = 1) -> date:
    if period == "28d":
        return d + timedelta(days=28 * k)
    if period == "84d":
        return d + timedelta(days=84 * k)
    return add_months(d, {"month": 1, "quarter": 3, "year": 12}[period] * k)


def yearly(amount: int, period: str) -> int:
    """Cost over a year, to the nearest rupee: it is an estimate, so paise would be noise."""
    num, den = PER_YEAR[period]
    return (amount * num // den + 50) // 100 * 100


def monthly(amount: int, period: str) -> int:
    return (yearly(amount, period) + 6) // 12


def usd_to_paise(cents: int, rate: float, markup_pct: float) -> int:
    """Card charges in USD carry the bank's forex markup on top of the exchange rate."""
    v = Decimal(cents) * Decimal(str(rate)) * (1 + Decimal(str(markup_pct)) / 100)
    return int(v.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def inr(paise: int, approx: bool = False) -> str:
    """₹1,428 / ₹1,00,000 / ₹129.50; approximate (converted) amounts round to the rupee."""
    sign = "-" if paise < 0 else ""
    rupees, p = divmod(abs(paise), 100)
    if approx:
        rupees, p = (abs(paise) + 50) // 100, 0
    s = str(rupees)
    if len(s) > 3:
        head, groups = s[:-3], [s[-3:]]
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        s = ",".join([head, *groups])
    return f"{sign}{'≈' if approx else ''}₹{s}" + (f".{p:02d}" if p else "")


def short_date(d: date, today: date) -> str:
    return f"{d.day} {d:%b}" + (f" {d.year}" if d.year != today.year else "")


def when(d: date, today: date) -> str:
    n = (d - today).days
    if n == 0:
        return "today"
    if n == 1:
        return "tomorrow"
    if 1 < n < 7:
        return f"in {n} days"
    return f"on {short_date(d, today)}"


def duration(days: int) -> str:
    if days < 1:
        return "since today"
    return f"{days // 30} months" if days >= 60 else f"{days} day{'s' if days > 1 else ''}"


def totals(subs, credits_30: int = 0, credits_365: int = 0) -> dict[str, int]:
    """Dashboard totals. Burn counts active services and bills; commitments (EMI, SIP, rent,
    insurance) get their own line so they don't drown the subscription number."""
    live = [s for s in subs if s.status in ("active", "cancelling")]
    burn_year = sum(yearly(s.amount, s.period) for s in live if s.category != "commitment")
    commit_year = sum(yearly(s.amount, s.period) for s in live if s.category == "commitment")
    return {
        "monthly_burn": (burn_year + 6) // 12,
        "yearly_burn": burn_year,
        "commitments_monthly": (commit_year + 6) // 12,
        "wasted": sum(s.wasted for s in subs),
        "saved": sum(s.saved for s in subs),
        "credits_30": credits_30,
        "credits_365": credits_365,
    }
