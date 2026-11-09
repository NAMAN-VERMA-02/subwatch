"""From events to subscriptions: dedupe, amount series, cadence, trials, usage and decisions."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from itertools import takewhile

from .money import GRACE_DAYS, step, usd_to_paise
from .parse import AGGREGATORS, MERCHANTS, Event, display_name

# Approximate USD/INR level in late 2026 (the rupee closed at 95.46 on 25 Aug 2026). Rates move:
# pass --usd-rate for today's value.
DEFAULT_USD_RATE = 95.0
DEFAULT_FOREX_MARKUP = 3.5  # % most Indian cards add on foreign-currency charges

# (period family, min gap, max gap) in days
_FAMILIES = [("month", 25, 35), ("quarter", 80, 100), ("year", 350, 380)]


@dataclass
class Settings:
    unused_days: int = 30
    usd_rate: float = DEFAULT_USD_RATE
    forex_markup: float = DEFAULT_FOREX_MARKUP
    decisions: dict = field(default_factory=dict)  # merchant -> {"choice": .., "on": iso date}
    ignore: list = field(default_factory=list)
    price: dict = field(default_factory=dict)  # merchant -> paise, for trials with no price


@dataclass
class Charge:
    date: date
    amount: int  # paise
    merchant: str
    source: str
    approx: bool = False
    signal: bool = False
    period: str | None = None
    category: str | None = None


@dataclass
class Sub:
    key: str
    merchant: str
    name: str
    category: str  # service | bill | commitment
    period: str
    amount: int  # paise; the latest charge, or the trial's price (0 if unknown)
    approx: bool = False
    kind: str = "sub"  # sub | trial | autopay (a mandate or pre-debit with no charge yet)
    charges: list[tuple[date, int]] = field(default_factory=list)
    next_renewal: date | None = None
    status: str = "active"  # trial | active | cancelling | ended | stopped
    usage: str | None = None  # used | unused | unknown; services only
    last_used: date | None = None
    unused_since: date | None = None
    decision: dict | None = None
    kept: bool = False  # "keep" chosen during the current billing cycle
    wasted: int = 0
    saved: int = 0
    alert: tuple[date, date, int] | None = None  # (cancelled on, charged on, amount)


@dataclass
class Report:
    today: date
    subs: list[Sub]
    credits_30: int = 0
    credits_365: int = 0
    expiring: list[tuple[str, int, date, bool]] = field(default_factory=list)


def _paise(e: Event, s: Settings) -> tuple[int, bool] | None:
    if e.amount is None:
        return None
    if e.currency == "USD":
        return usd_to_paise(e.amount, s.usd_rate, s.forex_markup), True
    return e.amount, False


def _close(a: Charge, b: Charge) -> bool:
    # ₹1; or 12% when one side is our USD conversion (the rate you set may be stale)
    tolerance = max(100, a.amount * 12 // 100) if (a.approx or b.approx) else 100
    return abs(a.amount - b.amount) <= tolerance


def _dedupe(charges: list[Charge], topups: list[Charge]) -> list[Charge]:
    """One payment often arrives three times: bank SMS, UPI-app SMS and an email receipt.
    Aggregators (Google Play, Apple) match any merchant at the same amount."""
    kept: list[Charge] = []
    resolved: dict[tuple[str, int], str] = {}  # (aggregator, amount) -> real service
    for c in sorted(charges, key=lambda c: c.date):
        if any(
            t.merchant == c.merchant and abs((t.date - c.date).days) <= 2 and _close(t, c)
            for t in topups
        ):
            continue  # the card SMS of a credit top-up, already counted as a top-up
        recent = takewhile(lambda k, c=c: (c.date - k.date).days <= 2, reversed(kept))
        k = next(
            (
                k
                for k in recent
                if (k.merchant == c.merchant or AGGREGATORS & {k.merchant, c.merchant})
                and _close(k, c)
                and (c.date == k.date or c.source != k.source)
            ),
            None,
        )
        if k is None:
            kept.append(c)
            continue
        if k.merchant in AGGREGATORS and c.merchant not in AGGREGATORS:
            resolved[(k.merchant, k.amount)] = c.merchant
            k.merchant = c.merchant
        elif c.merchant in AGGREGATORS and k.merchant not in AGGREGATORS:
            resolved[(c.merchant, c.amount)] = k.merchant
        if k.approx and not c.approx:  # the bank's rupee amount beats our conversion
            k.amount, k.approx = c.amount, False
        k.signal |= c.signal
        k.period = k.period or c.period
        k.category = k.category or c.category
    for k in kept:  # months where only the "GOOGLE PLAY" SMS arrived
        if k.merchant in AGGREGATORS and (k.merchant, k.amount) in resolved:
            k.merchant = resolved[(k.merchant, k.amount)]
    return kept


def cadence(dates: list[date]) -> str | None:
    """Period from the gaps between charges; one missed month (a 2x gap) is tolerated."""
    gaps = [(b - a).days for a, b in zip(dates, dates[1:], strict=False)]
    for family, lo, hi in _FAMILIES:
        single = [g for g in gaps if lo <= g <= hi]
        if single and all(lo <= g <= hi or 2 * lo <= g <= 2 * hi for g in gaps):
            mean = sum(single) / len(single)
            if family == "month":
                return "28d" if mean < 29 else "month"
            if family == "quarter":
                return "84d" if mean < 87 else "quarter"
            return "year"
    return None


def _series(charges: list[Charge], category: str) -> list[list[Charge]]:
    """Split one merchant's charges into exact-amount series. Series that overlap in time are
    separate subscriptions (two Google Play subs); an established series handing over to a
    new amount at the usual cadence is a price change. Bills vary, so they stay whole."""
    if category == "bill":
        return [charges]
    by_amount: dict[int, list[Charge]] = defaultdict(list)
    for c in charges:
        by_amount[c.amount].append(c)
    merged: list[list[Charge]] = []
    for s in sorted(by_amount.values(), key=lambda s: s[0].date):
        for m in merged:
            if (
                len(m) >= 2
                and s[0].date > m[-1].date
                and cadence([c.date for c in m]) is not None
                and cadence([c.date for c in m] + [s[0].date]) is not None
                and 0.65 <= s[0].amount / m[-1].amount <= 1.5
            ):
                m.extend(s)
                break
        else:
            merged.append(list(s))
    return merged


def _category(merchant: str, events: list[Event]) -> str:
    if merchant in MERCHANTS:
        return MERCHANTS[merchant][1]
    hints = [e.category for e in events if e.category]
    return max(set(hints), key=hints.count) if hints else "service"


def _decision(s: Settings, merchant: str) -> dict | None:
    d = s.decisions.get(merchant)
    return {"choice": d["choice"], "on": date.fromisoformat(d["on"])} if d else None


def _due_dates_passed(first_due: date, period: str, today: date) -> int:
    n = 0
    while step(first_due, period, n) <= today and n < 1000:
        n += 1
    return n


def _close_out(sub: Sub, due: date, today: date, dec: dict | None) -> None:
    """Status once the next due date is known. Only an explicit cancel counts as saved."""
    past_grace = (today - due).days > GRACE_DAYS[sub.period]  # bank posting can lag a few days
    if dec and dec["choice"] == "cancel":
        if past_grace:
            sub.status = "ended"
            sub.saved = sub.amount * _due_dates_passed(due, sub.period, today)
        else:
            sub.status = "cancelling"
    elif past_grace:
        sub.status = "stopped"  # stopped on its own: maybe moved to another card, so not "saved"
    else:
        sub.status = "active" if sub.kind == "sub" else "trial"


def detect(events: list[Event], today: date, s: Settings | None = None) -> Report:
    s = s or Settings()
    evs = [e for e in events if e.merchant not in s.ignore and e.date <= today]
    by_m: dict[str, list[Event]] = defaultdict(list)
    for e in evs:
        by_m[e.merchant].append(e)

    def charge(e: Event) -> Charge | None:
        conv = _paise(e, s)
        if not conv:
            return None
        return Charge(
            e.date, conv[0], e.merchant, e.source, conv[1], e.signal, e.period, e.category
        )

    topups = [c for e in evs if e.kind == "topup" and (c := charge(e))]
    charges = _dedupe([c for e in evs if e.kind == "charge" and (c := charge(e))], topups)
    charges_by_m: dict[str, list[Charge]] = defaultdict(list)
    for c in charges:
        charges_by_m[c.merchant].append(c)

    subs: list[Sub] = []
    for merchant, cs in charges_by_m.items():
        mevs = by_m.get(merchant, [])
        category = _category(merchant, mevs)
        mandates = [e for e in mevs if e.kind in ("mandate", "predebit")]
        known = merchant in MERCHANTS and merchant not in AGGREGATORS
        found = []
        for series in _series(cs, category):
            dates = [c.date for c in series]
            period = cadence(dates)
            stated = next((c.period for c in series if c.period), None)
            signal = known or bool(mandates) or any(c.signal for c in series)
            n = len(series)
            if n >= 3 and period:
                pass
            elif n == 2 and period and (signal or category == "bill"):
                pass
            elif n == 1 and signal and (stated or any(m.period for m in mandates)):
                period = stated or next(m.period for m in mandates if m.period)
            else:
                continue
            found.append((series, period))
        for series, period in found:
            last = series[-1]
            name = display_name(merchant)
            key = merchant
            if len(found) > 1:
                key = f"{merchant}@{last.amount // 100}"
                name = f"{name} ({last.amount // 100})"
            sub = Sub(
                key=key,
                merchant=merchant,
                name=name,
                category=category,
                period=period,
                amount=last.amount,
                approx=last.approx,
                charges=[(c.date, c.amount) for c in series],
            )
            due = step(last.date, period)
            pre = [
                m
                for m in mandates
                if m.kind == "predebit" and m.due and m.date >= last.date and m.due > last.date
            ]
            if pre and len(found) == 1:
                due = max(pre, key=lambda m: m.date).due
            sub.next_renewal = due
            dec = _decision(s, merchant)
            sub.decision = dec
            if dec and dec["choice"] == "cancel":
                after = [c for c in series if c.date > dec["on"]]
                if after:
                    sub.alert = (dec["on"], after[-1].date, after[-1].amount)
            _close_out(sub, due, today, dec)
            if dec and dec["choice"] == "keep" and dec["on"] >= last.date:
                sub.kept = True
            if category == "service" and sub.status in ("active", "cancelling"):
                _usage(sub, mevs, dec, today, s)
            subs.append(sub)

    subs += _trials(evs, by_m, charges_by_m, subs, today, s)
    report = Report(today, subs)
    for t in topups:
        if (today - t.date).days < 30:
            report.credits_30 += t.amount
        if (today - t.date).days < 365:
            report.credits_365 += t.amount
    for e in evs:
        if e.kind == "topup" and e.due and e.due >= today and (conv := _paise(e, s)):
            report.expiring.append((display_name(e.merchant), conv[0], e.due, conv[1]))
    return report


def _usage(sub: Sub, mevs: list[Event], dec: dict | None, today: date, s: Settings) -> None:
    uses = sorted(e.date for e in mevs if e.kind == "usage")
    sub.last_used = uses[-1] if uses else None
    if dec and dec["choice"] == "unused" and not (sub.last_used and sub.last_used > dec["on"]):
        sub.usage, sub.unused_since = "unused", dec["on"]
    elif sub.last_used and (today - sub.last_used).days <= s.unused_days:
        sub.usage = "used"
    elif sub.last_used:
        sub.usage, sub.unused_since = "unused", sub.last_used
    else:
        sub.usage = "unknown"  # never seen a usage signal: ask, don't guess
    if sub.usage == "unused":
        sub.wasted = sum(a for d, a in sub.charges if sub.unused_since < d <= today)


def _trials(evs, by_m, charges_by_m, subs, today, s) -> list[Sub]:
    """Trials, and AutoPay mandates or pre-debits whose first charge hasn't happened yet."""
    live = {x.merchant for x in subs if x.status in ("active", "cancelling")}
    out = []
    for merchant, mevs in by_m.items():
        starts = [e for e in mevs if e.kind in ("trial", "mandate", "predebit")]
        if not starts or merchant in live:
            continue
        latest = max(starts, key=lambda e: e.date)
        if any(c.date >= latest.date for c in charges_by_m.get(merchant, [])):
            continue  # it converted into a paid subscription
        due = max((e.due for e in starts if e.due), default=None)
        if due is None:
            continue
        priced = [e for e in sorted(starts, key=lambda e: e.date) if e.amount and e.amount > 200]
        price, approx = 0, False
        if priced and (conv := _paise(priced[-1], s)):
            price, approx = conv
        elif charges_by_m.get(merchant):
            price = charges_by_m[merchant][-1].amount
        elif merchant in s.price:
            price = int(s.price[merchant])
        kind = "trial" if any(e.kind == "trial" for e in starts) else "autopay"
        sub = Sub(
            key=merchant,
            merchant=merchant,
            name=display_name(merchant),
            category=_category(merchant, mevs),
            period=next((e.period for e in reversed(starts) if e.period), "month"),
            amount=price,
            approx=approx,
            kind=kind,
            next_renewal=due,
            decision=_decision(s, merchant),
        )
        _close_out(sub, due, today, sub.decision)
        if sub.status == "stopped" and (today - due).days > 90:
            continue  # an old trial that lapsed long ago is noise
        out.append(sub)
    return out
