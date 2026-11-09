from datetime import date, timedelta

from subwatch.detect import Settings, cadence, detect
from subwatch.parse import Event

TODAY = date(2026, 11, 9)


def d(s):
    return date.fromisoformat(s)


def ev(day, kind, merchant, amount=None, source="sms", **kw):
    return Event(d(day), source, kind, merchant, amount, **kw)


def monthly(merchant, amount, first, n, source="sms", **kw):
    from subwatch.money import add_months

    start = d(first)
    return [
        ev(add_months(start, i).isoformat(), "charge", merchant, amount, source, **kw)
        for i in range(n)
    ]


def only(report, merchant):
    found = [s for s in report.subs if s.merchant == merchant]
    assert len(found) == 1, [(s.key, s.status) for s in report.subs]
    return found[0]


def test_cadence():
    assert cadence([d("2026-08-05"), d("2026-09-05"), d("2026-10-05")]) == "month"
    assert cadence([d("2026-08-05"), d("2026-10-05"), d("2026-11-05")]) == "month"  # one missed
    days = [d("2026-08-01") + timedelta(days=28 * i) for i in range(4)]
    assert cadence(days) == "28d"
    days = [d("2026-01-01") + timedelta(days=84 * i) for i in range(3)]
    assert cadence(days) == "84d"
    assert cadence([d("2025-06-20"), d("2026-06-20")]) == "year"
    assert cadence([d("2026-08-01"), d("2026-08-04"), d("2026-08-30")]) is None


def test_monthly_sub_deduped_across_sms_and_email():
    events = monthly("spotify", 11900, "2026-08-05", 4) + monthly(
        "spotify", 11900, "2026-08-05", 4, source="email"
    )
    s = only(detect(events, TODAY), "spotify")
    assert (s.period, s.amount, len(s.charges)) == ("month", 11900, 4)
    assert s.next_renewal == d("2026-12-05")
    assert (s.status, s.usage) == ("active", "unknown")


def test_two_concurrent_google_play_subs_stay_separate():
    events = monthly("google-play", 12900, "2026-06-03", 6) + monthly(
        "google-play", 13000, "2026-06-20", 6
    )
    subs = [s for s in detect(events, TODAY).subs if s.merchant == "google-play"]
    assert sorted(s.amount for s in subs) == [12900, 13000]


def test_aggregator_charges_resolve_to_the_named_service():
    sms = monthly("google-play", 12900, "2026-05-03", 6)
    emails = monthly("youtube", 12900, "2026-05-03", 3, source="email")  # receipts for 3 months
    report = detect(sms + emails, TODAY)
    s = only(report, "youtube")
    assert len(s.charges) == 6
    assert not [x for x in report.subs if x.merchant == "google-play"]


def test_price_hike_is_one_subscription():
    events = monthly("netflix", 49900, "2026-03-08", 5) + monthly("netflix", 64900, "2026-08-08", 4)
    s = only(detect(events, TODAY), "netflix")
    assert (s.amount, len(s.charges)) == (64900, 9)


def test_variable_bill():
    amounts = [49900, 56000, 62000, 51200, 50900]
    events = [
        ev(f"2026-{m:02d}-12", "charge", "airtel", a)
        for m, a in zip(range(6, 11), amounts, strict=True)
    ]
    s = only(detect(events, TODAY), "airtel")
    assert (s.category, s.amount, s.usage) == ("bill", 50900, None)


def test_food_orders_are_not_subscriptions():
    events = []
    day = d("2026-05-01")
    for i in range(60):
        day += timedelta(days=(i * 7) % 5 + 1)
        events.append(ev(day.isoformat(), "charge", "swiggy", 18000 + (i * 3700) % 41000))
    assert detect(events, TODAY).subs == []


def test_recharge_periods():
    jio = [
        ev((d("2026-06-01") + timedelta(days=28 * i)).isoformat(), "charge", "jio", 34900)
        for i in range(6)
    ]
    vi = [
        ev((d("2026-01-10") + timedelta(days=84 * i)).isoformat(), "charge", "vi", 71900)
        for i in range(4)
    ]
    report = detect(jio + vi, TODAY)
    assert only(report, "jio").period == "28d"
    assert only(report, "vi").period == "84d"


def test_yearly_and_single_stated_charge():
    prime = [
        ev("2025-06-20", "charge", "amazon-prime", 149900),
        ev("2026-06-20", "charge", "amazon-prime", 149900),
    ]
    linkedin = [
        ev("2026-03-01", "charge", "linkedin", 1100000, "email", period="year", signal=True)
    ]
    one_off = [ev("2026-10-01", "charge", "decathlon", 249900)]
    report = detect(prime + linkedin + one_off, TODAY)
    assert only(report, "amazon-prime").period == "year"
    assert only(report, "linkedin").next_renewal == d("2027-03-01")
    assert not [s for s in report.subs if s.merchant == "decathlon"]


def test_usd_charge_prefers_the_banks_rupee_amount():
    usd = monthly("claude", 2000, "2026-08-03", 4, source="email", currency="USD")
    inr = monthly("claude", 189200, "2026-08-03", 4)  # bank SMS in rupees
    s = only(detect(usd + inr, TODAY), "claude")
    assert (s.amount, s.approx, len(s.charges)) == (189200, False, 4)
    s = only(detect(usd, TODAY), "claude")
    assert (s.amount, s.approx) == (196650, True)  # $20 x 95 x 1.035


def test_usage_levels_and_wasted():
    charges = monthly("netflix", 64900, "2026-06-08", 6)
    used = detect(charges + [ev("2026-11-01", "usage", "netflix", source="email")], TODAY)
    assert only(used, "netflix").usage == "used"
    stale = detect(charges + [ev("2026-08-20", "usage", "netflix", source="email")], TODAY)
    s = only(stale, "netflix")
    assert (s.usage, s.unused_since) == ("unused", d("2026-08-20"))
    assert s.wasted == 3 * 64900  # 8 Sep, 8 Oct, 8 Nov
    assert only(detect(charges, TODAY), "netflix").usage == "unknown"


def test_bills_and_commitments_are_never_flagged_unused():
    emi = monthly("ach d hdfc", 2350000, "2026-06-05", 6, category="commitment")
    s = only(detect(emi, TODAY), "ach d hdfc")
    assert (s.category, s.usage, s.wasted) == ("commitment", None, 0)


def decided(choice, on):
    return Settings(decisions={"spotify": {"choice": choice, "on": on}})


def test_cancel_then_charged_raises_alert():
    events = monthly("spotify", 11900, "2026-07-05", 5)  # last charge 5 Nov
    s = only(detect(events, TODAY, decided("cancel", "2026-11-01")), "spotify")
    assert s.alert == (d("2026-11-01"), d("2026-11-05"), 11900)
    assert s.status == "cancelling" and s.saved == 0


def test_cancel_counts_as_saved_only_after_the_due_date_passes_uncharged():
    events = monthly("spotify", 11900, "2026-06-05", 3)  # last charge 5 Aug
    s = only(detect(events, TODAY, decided("cancel", "2026-08-10")), "spotify")
    assert s.status == "ended"
    assert s.saved == 3 * 11900  # 5 Sep, 5 Oct, 5 Nov
    assert s.alert is None


def test_stopped_without_a_decision_is_not_saved():
    events = monthly("spotify", 11900, "2026-06-05", 3)
    s = only(detect(events, TODAY), "spotify")
    assert (s.status, s.saved) == ("stopped", 0)


def test_keep_silences_until_next_charge():
    events = monthly("spotify", 11900, "2026-07-05", 5)
    assert only(detect(events, TODAY, decided("keep", "2026-11-06")), "spotify").kept
    assert not only(detect(events, TODAY, decided("keep", "2026-10-06")), "spotify").kept


def test_not_using_it_counts_wasted_from_that_date():
    events = monthly("spotify", 11900, "2026-07-05", 5)
    s = only(detect(events, TODAY, decided("unused", "2026-09-01")), "spotify")
    assert (s.usage, s.wasted) == ("unused", 3 * 11900)


def test_trial_and_conversion():
    trial = [
        ev("2026-11-05", "trial", "sonyliv", 39900, "email", due=d("2026-11-12"), period="month")
    ]
    t = only(detect(trial, TODAY), "sonyliv")
    assert (t.kind, t.status, t.amount, t.next_renewal) == (
        "trial",
        "trial",
        39900,
        d("2026-11-12"),
    )
    converted = trial + [
        ev("2026-11-12", "charge", "sonyliv", 39900, "email", period="month", signal=True)
    ]
    s = only(detect(converted, d("2026-11-15")), "sonyliv")
    assert (s.kind, s.status) == ("sub", "active")


def test_cancelled_trial_saves_from_its_end_date():
    trial = [
        ev("2026-09-01", "trial", "canva", 49900, "email", due=d("2026-10-01"), period="month")
    ]
    s = detect(
        trial, TODAY, Settings(decisions={"canva": {"choice": "cancel", "on": "2026-09-20"}})
    )
    t = only(s, "canva")
    assert (t.status, t.saved) == ("ended", 2 * 49900)  # 1 Oct, 1 Nov


def test_trial_price_falls_back_to_user_price():
    trial = [ev("2026-11-05", "trial", "notion", None, "email", due=d("2026-11-20"))]
    assert only(detect(trial, TODAY), "notion").amount == 0
    s = Settings(price={"notion": 80000})
    assert only(detect(trial, TODAY, s), "notion").amount == 80000


def test_new_mandate_is_an_upcoming_autopay():
    m = [ev("2026-11-08", "mandate", "zee5", 9900, due=d("2026-11-14"), period="month")]
    s = only(detect(m, TODAY), "zee5")
    assert (s.kind, s.status, s.amount) == ("autopay", "trial", 9900)


def test_predebit_sets_next_renewal():
    events = monthly("netflix", 64900, "2026-07-08", 4) + [
        ev("2026-11-07", "predebit", "netflix", 64900, due=d("2026-11-09"))
    ]
    events[-2].date = d("2026-10-08")
    s = only(detect(events, TODAY), "netflix")
    assert s.next_renewal == d("2026-11-09")


def test_topups_are_credits_not_subscriptions():
    events = [
        ev("2026-11-01", "topup", "claude", 5000, "email", currency="USD", due=d("2027-11-01")),
        ev("2026-11-01", "charge", "claude", 5000, currency="USD"),  # the card SMS of it
        ev("2026-03-01", "topup", "chatgpt", 200000, "email"),
    ]
    r = detect(events, TODAY)
    assert r.subs == []
    assert r.credits_30 == 491625  # $50 x 95 x 1.035
    assert r.credits_365 == 491625 + 200000
    assert r.expiring == [("Claude", 491625, d("2027-11-01"), True)]


def test_ignore_list():
    events = monthly("spotify", 11900, "2026-07-05", 5)
    assert detect(events, TODAY, Settings(ignore=["spotify"])).subs == []


def test_equal_fares_among_many_rides_are_chance():
    rides = [
        ev(
            (d("2025-06-01") + timedelta(days=3 * i)).isoformat(),
            "charge",
            "uber",
            9000 + 731 * i % 30000,
        )
        for i in range(150)
    ]
    fluke = [ev(day, "charge", "uber", 34700) for day in ("2025-07-11", "2025-10-01", "2026-01-01")]
    assert detect(rides + fluke, TODAY).subs == []


def test_synthetic_generator_labels_every_case(tmp_path):
    from subwatch.synthetic import MBOX_NAME, generate

    labels = generate(tmp_path, TODAY, noise=10)
    kinds = {r["kind"] for r in labels}
    assert kinds == {"sub", "trial", "negative"}
    assert (tmp_path / MBOX_NAME).exists() and list(tmp_path.glob("sms-*.xml"))
