"""Reminders that state the money at stake: .ics calendar events and desktop toasts."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from .detect import Report, Sub
from .money import duration, inr, short_date, when, yearly

# Manage/cancel pages, checked 2026-11-09. Services without a checked link get none.
CANCEL_LINKS = {
    "spotify": "https://www.spotify.com/account/subscription/",
    "netflix": "https://www.netflix.com/cancelplan",
    "youtube": "https://www.youtube.com/paid_memberships",
    "google-one": "https://play.google.com/store/account/subscriptions",
    "google-play": "https://play.google.com/store/account/subscriptions",
    "apple": "https://apps.apple.com/account/subscriptions",
    "icloud": "https://apps.apple.com/account/subscriptions",
    "claude": "https://claude.ai/settings/billing",
}
ASK = "Continue? Run `subwatch review`"


@dataclass
class Reminder:
    kind: str  # trial | unused | renewal | alert | credits
    key: str
    due: date
    text: str
    alarm_days: int  # alarm this many days before `due`
    link: str | None = None


def _money(sub: Sub, paise: int) -> str:
    return inr(paise, sub.approx)


def trial_text(sub: Sub, today: date) -> str:
    what = "trial ends" if sub.kind == "trial" else "AutoPay starts"
    return (
        f"{sub.name} {what} {when(sub.next_renewal, today)}: {_money(sub, sub.amount)} charged, "
        f"{_money(sub, yearly(sub.amount, sub.period))}/year if you forget"
    )


def unused_text(sub: Sub, today: date) -> str:
    gap = duration((today - sub.unused_since).days)
    nxt = f"next {_money(sub, sub.amount)} on {short_date(max(sub.next_renewal, today), today)}"
    if sub.wasted:
        return f"{sub.name} unused {gap}: {_money(sub, sub.wasted)} already wasted, {nxt}"
    return f"{sub.name} unused {gap}: {nxt}, {_money(sub, yearly(sub.amount, sub.period))}/year"


def alert_text(sub: Sub, today: date) -> str:
    on, charged, amount = sub.alert
    return (
        f"You chose to cancel {sub.name} on {short_date(on, today)} but were charged "
        f"{_money(sub, amount)} on {short_date(charged, today)}"
    )


def build(report: Report) -> list[Reminder]:
    """Every reminder states rupees; items without a known price are left to the dashboard."""
    today = report.today
    out: list[Reminder] = []
    for s in report.subs:
        link = CANCEL_LINKS.get(s.merchant)
        if s.alert:
            out.append(Reminder("alert", s.key, today, alert_text(s, today), 0, link))
        if s.kept or not s.amount:
            continue
        if s.status == "trial" and s.next_renewal >= today:
            out.append(Reminder("trial", s.key, s.next_renewal, trial_text(s, today), 1, link))
        elif s.status == "active" and s.usage == "unused":
            due = max(s.next_renewal, today)
            out.append(Reminder("unused", s.key, due, unused_text(s, today), 1, link))
        elif s.status == "active" and s.period == "year" and s.next_renewal >= today:
            text = f"{s.name} renews {when(s.next_renewal, today)}: {_money(s, s.amount)} charged"
            out.append(Reminder("renewal", s.key, s.next_renewal, text, 3, link))
    for name, paise, due, approx in report.expiring:
        lost = f"up to {inr(paise, approx)} lost if unused"
        text = f"{name} API credits expire {when(due, today)}: {lost}"
        out.append(Reminder("credits", name.lower(), due, text, 7))
    return sorted(out, key=lambda r: r.due)


# ---------------------------------------------------------------- .ics


def _escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r", "")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> str:
    """RFC 5545: lines over 75 octets continue on the next line after a space."""
    parts, cur, limit = [], b"", 75
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > limit:
            parts.append(cur.decode("utf-8"))
            cur, limit = b"", 74  # the leading space counts
        cur += b
    parts.append(cur.decode("utf-8"))
    return "\r\n ".join(parts)


def ics(reminders: list[Reminder], now: datetime | None = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//subwatch//subwatch//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:subwatch",
    ]
    for r in reminders:
        detail = f"{r.text}\n{ASK}" + (f"\nCancel: {r.link}" if r.link else "")
        trigger = f"-P{r.alarm_days - 1}DT15H" if r.alarm_days > 1 else "-PT15H"  # 9 am
        if r.alarm_days == 0:
            trigger = "PT9H"
        lines += [
            "BEGIN:VEVENT",
            f"UID:{r.kind}-{r.key.replace(' ', '-')}-{r.due:%Y%m%d}@subwatch",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{r.due:%Y%m%d}",
            f"DTEND;VALUE=DATE:{r.due + timedelta(days=1):%Y%m%d}",
            f"SUMMARY:{_escape(r.text)}",
            f"DESCRIPTION:{_escape(detail)}",
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{_escape(r.text)}",
            f"TRIGGER:{trigger}",
            "END:VALARM",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "".join(_fold(line) + "\r\n" for line in lines)


# ---------------------------------------------------------------- desktop toast

# Text reaches the OS through environment variables, never through the script source, so a
# merchant name from an email can't inject commands.
_PS_TOAST = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$n = $t.GetElementsByTagName('text')
$n.Item(0).AppendChild($t.CreateTextNode($env:SW_TITLE)) | Out-Null
$n.Item(1).AppendChild($t.CreateTextNode($env:SW_BODY)) | Out-Null
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($t))
"""  # noqa: E501
_OSA = 'display notification (system attribute "SW_BODY") with title (system attribute "SW_TITLE")'


def notify(title: str, body: str) -> bool:
    """Show a desktop notification. Returns False when the platform has no way to."""
    env = {**os.environ, "SW_TITLE": title, "SW_BODY": body}
    if sys.platform == "win32":
        cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_TOAST]
    elif sys.platform == "darwin":
        cmd = ["osascript", "-e", _OSA]
    elif shutil.which("notify-send"):
        cmd = ["notify-send", title, body]
    else:
        return False
    try:
        return subprocess.run(cmd, env=env, capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
