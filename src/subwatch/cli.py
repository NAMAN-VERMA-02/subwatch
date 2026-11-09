"""Command-line entry point."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import tempfile
import webbrowser
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from . import __version__
from .detect import DEFAULT_FOREX_MARKUP, DEFAULT_USD_RATE, Settings, Sub, detect
from .money import PERIOD_WORD, inr, short_date, yearly
from .parse import Event, known_merchant, newest_backup, normalise, read_imap, scan_file
from .remind import CANCEL_LINKS, build, ics, notify, trial_text, unused_text
from .report import html, terminal

# ---------------------------------------------------------------- local state


def home() -> Path:
    return Path(os.environ.get("SUBWATCH_HOME") or Path.home() / ".subwatch")


def load_state() -> dict:
    path = home() / "state.json"
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    for key, empty in (
        ("decisions", {}),
        ("ignore", []),
        ("price", {}),
        ("sources", []),
        ("imap", {}),
    ):
        state.setdefault(key, empty)
    return state


def save_state(state: dict) -> None:
    home().mkdir(parents=True, exist_ok=True)
    (home() / "state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")


def load_events() -> list[Event]:
    path = home() / "events.jsonl"
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [Event.from_json(json.loads(line)) for line in f if line.strip()]


def add_events(new: list[Event]) -> int:
    """Append events not seen before (by message/SMS id), so rescans are incremental."""
    seen = {e.ref for e in load_events()}
    fresh = [e for e in new if e.ref not in seen]
    seen_now: set[str] = set()
    home().mkdir(parents=True, exist_ok=True)
    with open(home() / "events.jsonl", "a", encoding="utf-8") as f:
        for e in fresh:
            if e.ref in seen_now:
                continue
            seen_now.add(e.ref)
            f.write(json.dumps(e.to_json()) + "\n")
    return len(seen_now)


def settings(state: dict, args) -> Settings:
    return Settings(
        unused_days=getattr(args, "unused_days", None) or state.get("unused_days", 30),
        usd_rate=getattr(args, "usd_rate", None) or state.get("usd_rate", DEFAULT_USD_RATE),
        forex_markup=getattr(args, "forex_markup", None)
        if getattr(args, "forex_markup", None) is not None
        else state.get("forex_markup", DEFAULT_FOREX_MARKUP),
        decisions=state["decisions"],
        ignore=state["ignore"],
        price=state["price"],
    )


def today_of(args) -> date:
    return date.fromisoformat(args.today) if getattr(args, "today", None) else date.today()


def current(args):
    state = load_state()
    return detect(load_events(), today_of(args), settings(state, args)), state


def resolve(name: str) -> str:
    return known_merchant(name) or normalise(name)


# ---------------------------------------------------------------- commands


def _files(path: Path) -> list[Path]:
    if path.is_dir():
        files = sorted(path.glob("*.mbox"))
        if backup := newest_backup(path):
            files.append(backup)
        return files
    return [path]


def cmd_scan(args) -> int:
    state = load_state()
    found: list[Event] = []
    read = 0
    for raw in args.paths:
        path = Path(raw).expanduser().resolve()
        if not path.exists():
            print(f"subwatch: {raw}: no such file or folder", file=sys.stderr)
            return 2
        for f in _files(path):
            print(f"reading {f.name} ...", flush=True)
            try:
                events, n = scan_file(f)
            except (ValueError, OSError) as e:
                print(f"subwatch: {e}", file=sys.stderr)
                return 2
            found += events
            read += n
        if str(path) not in state["sources"]:
            state["sources"].append(str(path))
    if args.imap:
        password = os.environ.get("SUBWATCH_IMAP_PASSWORD") or getpass.getpass(
            f"Gmail app password for {args.imap} (not stored): "
        )
        last = state["imap"].get("last_uid", 0) if state["imap"].get("user") == args.imap else 0
        events, n, top = read_imap(args.imap, password, since_uid=last)
        found += events
        read += n
        state["imap"] = {"user": args.imap, "last_uid": top}
    added = add_events(found)
    save_state(state)
    kinds = Counter(e.kind for e in found)
    summary = ", ".join(f"{n} {k}" for k, n in kinds.most_common()) or "nothing relevant"
    print(f"read {read} messages: {summary}. {added} new events saved to {home()}")
    return 0


def cmd_report(args) -> int:
    report, _ = current(args)
    print(terminal(report))
    if args.html:
        out = Path(args.html)
        out.write_text(html(report), encoding="utf-8")
        print(f"\ndashboard written to {out.resolve()}")
        if args.open:
            webbrowser.open(out.resolve().as_uri())
    return 0


def cmd_remind(args) -> int:
    report, _ = current(args)
    reminders = build(report)
    for r in reminders:
        print(f"{r.due:%d %b}  {r.text}")
    if not reminders:
        print("no reminders")
    if args.ics:
        Path(args.ics).write_text(ics(reminders), encoding="utf-8", newline="")
        print(f"\n{len(reminders)} reminders written to {Path(args.ics).resolve()}")
    if args.notify:
        soon = [r for r in reminders if (r.due - report.today).days <= 2]
        for r in soon:
            if not notify("subwatch", f"{r.text}. Continue? Run subwatch review"):
                print("(desktop notifications aren't available here)", file=sys.stderr)
                break
    return 0


def _queue(report) -> list[tuple[Sub, str]]:
    today, out = report.today, []
    for s in report.subs:
        if s.alert:
            out.append((s, "alert"))
        elif s.status == "trial":
            out.append((s, "trial"))
        elif s.status != "active" or s.kept:
            continue
        elif s.usage == "unused":
            out.append((s, "unused"))
        elif s.usage == "unknown" and not s.decision:
            out.append((s, "unknown"))
        elif s.period == "year" and (s.next_renewal - today).days <= 30:
            out.append((s, "renewal"))
    return out


def _describe(s: Sub, why: str, today: date) -> str:
    per = f"{inr(s.amount, s.approx)}/{PERIOD_WORD[s.period]}" if s.amount else "price unknown"
    yr = f", {inr(yearly(s.amount, s.period), s.approx)}/year" if s.amount else ""
    if why == "trial":
        return trial_text(s, today) if s.amount else f"{s.name}: trial ends, {per}"
    if why == "unused":
        return unused_text(s, today)
    if why == "alert":
        on, charged, amount = s.alert
        return (
            f"{s.name}: you chose to cancel on {short_date(on, today)} but were charged "
            f"{inr(amount, s.approx)} on {short_date(charged, today)}"
        )
    if why == "renewal":
        return f"{s.name}: {per}, renews {short_date(s.next_renewal, today)}"
    return f"{s.name}: {per}{yr}. No usage signal seen, are you using it?"


def cmd_review(args) -> int:
    report, state = current(args)
    today = report.today
    queue = _queue(report)
    if not queue:
        print("Nothing to review.")
        return 0
    for s, why in queue:
        print(f"\n{_describe(s, why, today)}")
        try:
            answer = input("Continue? [k]eep / [c]ancel / [n]ot using it / [s]kip / [q]uit: ")
        except EOFError:
            break
        a = answer.strip().lower()[:1]
        if a == "q":
            break
        if a == "k":
            state["decisions"][s.merchant] = {"choice": "keep", "on": today.isoformat()}
            print("  kept: no reminders until its next charge")
        elif a == "c":
            state["decisions"][s.merchant] = {"choice": "cancel", "on": today.isoformat()}
            link = CANCEL_LINKS.get(s.merchant)
            print(f"  cancel it here: {link}" if link else "  cancel it where you subscribed")
            print("  subwatch counts it as saved once the next due date passes with no charge")
        elif a == "n":
            try:
                since = input("  Not used since? (YYYY-MM-DD, Enter = today): ").strip()
            except EOFError:
                since = ""
            on = date.fromisoformat(since) if since else today
            state["decisions"][s.merchant] = {"choice": "unused", "on": on.isoformat()}
            print("  marked unused: wasted money now counts from that date")
    save_state(state)
    return 0


def cmd_mark(args) -> int:
    state = load_state()
    key = resolve(args.merchant)
    if args.price is not None:
        state["price"][key] = round(args.price * 100)
        print(f"{key}: price set to {inr(state['price'][key])}")
    if args.action == "ignore":
        if key not in state["ignore"]:
            state["ignore"].append(key)
        print(f"{key}: ignored from now on")
    elif args.action == "clear":
        state["decisions"].pop(key, None)
        state["ignore"] = [k for k in state["ignore"] if k != key]
        print(f"{key}: decision cleared")
    elif args.action:
        on = args.on or date.today().isoformat()
        state["decisions"][key] = {"choice": args.action, "on": on}
        print(f"{key}: {args.action} on {on}")
    elif args.price is None:
        print("nothing to do: give an action or --price", file=sys.stderr)
        return 2
    save_state(state)
    return 0


def cmd_daily(args) -> int:
    """For Task Scheduler / cron: rescan SMS folders (and IMAP when its password is in the
    environment), refresh the .ics and dashboard, and toast what's due within 2 days."""
    state = load_state()
    sms = [s for s in state["sources"] if not s.lower().endswith(".mbox")]
    paths = [p for s in sms if (p := Path(s)).exists()]
    imap_user = state["imap"].get("user") if os.environ.get("SUBWATCH_IMAP_PASSWORD") else None
    if paths or imap_user:
        scan = argparse.Namespace(paths=[str(p) for p in paths], imap=imap_user)
        if cmd_scan(scan):
            return 2
    ns = argparse.Namespace(**{**vars(args), "ics": str(home() / "reminders.ics"), "notify": True})
    cmd_remind(ns)
    report, _ = current(args)
    (home() / "dashboard.html").write_text(html(report), encoding="utf-8")
    return 0


def demo_decisions(events: list[Event], today: date) -> dict:
    """What a user might have answered in `review`, so the demo shows every state."""
    chatgpt = max(e.date for e in events if e.merchant == "chatgpt" and e.kind == "charge")
    iso = lambda d: d.isoformat()  # noqa: E731
    return {
        "chatgpt": {"choice": "cancel", "on": iso(chatgpt - timedelta(days=2))},
        "cultfit": {"choice": "cancel", "on": iso(today - timedelta(days=115))},
        "canva": {"choice": "cancel", "on": iso(today - timedelta(days=50))},
        "google-one": {"choice": "unused", "on": iso(today - timedelta(days=75))},
        "spotify": {"choice": "keep", "on": iso(today)},
    }


def cmd_demo(args) -> int:
    from .synthetic import generate

    today = today_of(args)
    work = Path(tempfile.mkdtemp(prefix="subwatch-demo-"))
    generate(work / "data", today, noise=300)
    events: list[Event] = []
    for f in _files(work / "data"):
        events += scan_file(f)[0]
    # The demo user has also hidden their monthly transfer to their own account.
    s = Settings(decisions=demo_decisions(events, today), ignore=["naman v"])
    report = detect(events, today, s)
    print(terminal(report))
    print()
    for r in build(report):
        print(f"{r.due:%d %b}  {r.text}")
    out = Path(args.out) if args.out else work / "dashboard.html"
    out.write_text(html(report, demo=True), encoding="utf-8")
    print(f"\nDemo on synthetic data (no real person). Dashboard: {out.resolve()}")
    if not args.no_open:
        webbrowser.open(out.resolve().as_uri())
    return 0


# ---------------------------------------------------------------- parser


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="subwatch",
        description="Find the subscriptions and free trials draining your money. "
        "Everything runs on this machine.",
    )
    ap.add_argument("--version", action="version", version=f"subwatch {__version__}")
    sub = ap.add_subparsers(dest="cmd")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--today", metavar="YYYY-MM-DD", help="pretend it is this date")
    common.add_argument("--unused-days", type=int, metavar="N", help="unused after N days (30)")
    common.add_argument("--usd-rate", type=float, metavar="R", help=f"₹ per $ ({DEFAULT_USD_RATE})")
    common.add_argument("--forex-markup", type=float, metavar="PCT", help="card markup %% (3.5)")

    p = sub.add_parser("scan", help="read exports (mbox, SMS .xml/.csv, folders) or Gmail IMAP")
    p.add_argument("paths", nargs="*", help="Takeout .mbox, SMS backup .xml/.csv, or a folder")
    p.add_argument("--imap", metavar="YOU@gmail.com", help="also read Gmail over IMAP (read-only)")
    p.set_defaults(fn=cmd_scan)

    p = sub.add_parser("report", parents=[common], help="totals, subscriptions, trials")
    p.add_argument("--html", metavar="FILE", help="also write the dashboard")
    p.add_argument("--open", action="store_true", help="open the dashboard in a browser")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("remind", parents=[common], help="reminders: print, .ics, or toast")
    p.add_argument("--ics", metavar="FILE", help="write a calendar file to import")
    p.add_argument("--notify", action="store_true", help="desktop toast for items due in 2 days")
    p.set_defaults(fn=cmd_remind)

    p = sub.add_parser("review", parents=[common], help="decide: continue or cancel, one by one")
    p.set_defaults(fn=cmd_review)

    p = sub.add_parser("mark", help="record a decision without the interactive review")
    p.add_argument("merchant")
    p.add_argument("action", nargs="?", choices=["keep", "cancel", "unused", "ignore", "clear"])
    p.add_argument("--on", metavar="YYYY-MM-DD", help="when you decided (today)")
    p.add_argument("--price", type=float, metavar="RUPEES", help="price for a trial with none")
    p.set_defaults(fn=cmd_mark)

    p = sub.add_parser("daily", parents=[common], help="rescan, refresh .ics, toast (schedule it)")
    p.set_defaults(fn=cmd_daily)

    p = sub.add_parser("demo", help="try it on synthetic data")
    p.add_argument("--today", metavar="YYYY-MM-DD")
    p.add_argument("--out", metavar="FILE", help="where to write the dashboard")
    p.add_argument("--no-open", action="store_true")
    p.set_defaults(fn=cmd_demo)

    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        # Piped or redirected output on Windows defaults to cp1252, which has no ₹ sign.
        if not stream.isatty() and (stream.encoding or "").lower() not in ("utf-8", "utf8"):
            stream.reconfigure(encoding="utf-8")
    if not getattr(args, "fn", None):
        ap.print_help()
        return 0
    if args.cmd == "scan" and not args.paths and not args.imap:
        ap.error("scan needs a file, a folder or --imap")
    return args.fn(args)
