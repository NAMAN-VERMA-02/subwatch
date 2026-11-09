from datetime import date, datetime, timezone

from subwatch import remind
from subwatch.detect import Report, Sub
from subwatch.remind import Reminder, build, ics, notify

TODAY = date(2026, 11, 9)


def test_trial_sentence_matches_the_spec():
    s = Sub("spotify", "spotify", "Spotify", "service", "month", 11900, kind="trial")
    s.status, s.next_renewal = "trial", date(2026, 11, 10)
    [r] = build(Report(TODAY, [s]))
    assert r.text == "Spotify trial ends tomorrow: ₹119 charged, ₹1,428/year if you forget"
    assert r.link == "https://www.spotify.com/account/subscription/"


def test_unused_sentence_matches_the_spec():
    s = Sub("google-one", "google-one", "Google One", "service", "month", 13000)
    s.next_renewal, s.usage = date(2026, 11, 15), "unused"
    s.unused_since, s.wasted = date(2026, 9, 9), 26000
    [r] = build(Report(TODAY, [s]))
    assert r.text == "Google One unused 2 months: ₹260 already wasted, next ₹130 on 15 Nov"


def test_alert_yearly_and_credit_sentences():
    cancelled = Sub("chatgpt", "chatgpt", "ChatGPT", "service", "month", 199900)
    cancelled.status, cancelled.next_renewal = "cancelling", date(2026, 12, 5)
    cancelled.alert = (date(2026, 11, 1), date(2026, 11, 5), 199900)
    prime = Sub("amazon-prime", "amazon-prime", "Amazon Prime", "service", "year", 149900)
    prime.next_renewal = date(2026, 11, 12)
    report = Report(
        TODAY, [cancelled, prime], expiring=[("Claude", 491625, date(2026, 11, 14), True)]
    )
    texts = [r.text for r in build(report)]
    assert "You chose to cancel ChatGPT on 1 Nov but were charged ₹1,999 on 5 Nov" in texts
    assert "Amazon Prime renews in 3 days: ₹1,499 charged" in texts
    assert "Claude API credits expire in 5 days: up to ≈₹4,916 lost if unused" in texts


def test_no_reminder_without_a_price_or_after_keep():
    trial = Sub("notion", "notion", "Notion", "service", "month", 0, kind="trial")
    trial.status, trial.next_renewal = "trial", date(2026, 11, 20)
    kept = Sub("netflix", "netflix", "Netflix", "service", "month", 64900)
    kept.next_renewal, kept.usage, kept.unused_since, kept.kept = (
        date(2026, 12, 8),
        "unused",
        date(2026, 8, 1),
        True,
    )
    assert build(Report(TODAY, [trial, kept])) == []


def test_every_reminder_states_rupees(tmp_path):
    from subwatch.cli import demo_decisions
    from subwatch.detect import Settings, detect
    from subwatch.parse import scan_file
    from subwatch.synthetic import generate

    generate(tmp_path, TODAY, noise=0)
    events = [e for f in sorted(tmp_path.iterdir()) for e in scan_file(f)[0]]
    report = detect(events, TODAY, Settings(decisions=demo_decisions(events, TODAY)))
    reminders = build(report)
    assert {r.kind for r in reminders} >= {"trial", "unused", "alert", "credits"}
    assert all("₹" in r.text for r in reminders)


def unfold(text: str) -> list[str]:
    return text.replace("\r\n ", "").split("\r\n")


def test_ics_escapes_folds_and_parses_back():
    long = "Spotify trial ends tomorrow: ₹119 charged, ₹1,428/year if you forget; really"
    r = Reminder("trial", "spotify", date(2026, 11, 10), long, 1, "https://example.com/a,b")
    out = ics([r], now=datetime(2026, 11, 9, 5, 0, tzinfo=timezone.utc))
    assert all(len(line.encode()) <= 75 for line in out.split("\r\n"))
    lines = unfold(out)
    assert "SUMMARY:" + long.replace(",", "\\,").replace(";", "\\;") in lines
    assert "DTSTART;VALUE=DATE:20261110" in lines
    assert "DTSTAMP:20261109T050000Z" in lines
    assert "TRIGGER:-PT15H" in lines
    assert "UID:trial-spotify-20261110@subwatch" in lines
    assert out.startswith("BEGIN:VCALENDAR\r\n") and out.endswith("END:VCALENDAR\r\n")


def test_ics_newlines_cannot_inject_properties():
    r = Reminder("alert", "x", TODAY, "Evil\r\nATTENDEE:mailto:a@b.c", 0)
    assert "ATTENDEE:" not in [line.split(":")[0] + ":" for line in unfold(ics([r]))]


def test_notify_passes_text_by_environment(monkeypatch):
    seen = {}

    def fake_run(cmd, env, capture_output, timeout):
        seen["cmd"], seen["env"] = cmd, env

        class Done:
            returncode = 0

        return Done()

    monkeypatch.setattr(remind.subprocess, "run", fake_run)
    monkeypatch.setattr(remind.shutil, "which", lambda name: "/usr/bin/notify-send")
    body = "Netflix'; rm -rf / #"
    assert notify("subwatch", body)
    assert seen["env"]["SW_BODY"] == body
    if remind.sys.platform in ("win32", "darwin"):
        assert body not in " ".join(seen["cmd"])
