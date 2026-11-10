"""Terminal summary and a single-file HTML dashboard."""

from __future__ import annotations

from datetime import date
from html import escape

from .detect import Report, Sub
from .money import PERIOD_WORD, duration, inr, monthly, short_date, totals, when, yearly
from .remind import alert_text, build


def whole(paise: int) -> str:
    """Totals read better in whole rupees."""
    return inr((paise + 50) // 100 * 100)


def usage_text(s: Sub, today: date) -> str:
    if s.usage == "used":
        return f"used {short_date(s.last_used, today)}"
    if s.usage == "unused":
        return f"unused {duration((today - s.unused_since).days)}"
    if s.usage == "unknown":
        return "no usage signal: review"
    return "-"


def decision_text(s: Sub, today: date) -> str:
    d = s.decision
    if not d:
        return "not decided" if s.category == "service" else "-"
    word = {"keep": "keep", "cancel": "cancelling", "unused": "not using"}[d["choice"]]
    return f"{word} since {short_date(d['on'], today)}"


def sections(report: Report) -> dict[str, list[Sub]]:
    by_status = {
        "Trials and upcoming AutoPay": [s for s in report.subs if s.status == "trial"],
        "Active subscriptions": [
            s
            for s in report.subs
            if s.kind == "sub"
            and s.status in ("active", "cancelling")
            and s.category != "commitment"
        ],
        "Commitments (EMI, SIP, rent, insurance)": [
            s
            for s in report.subs
            if s.status in ("active", "cancelling") and s.category == "commitment"
        ],
        "Ended or stopped": [s for s in report.subs if s.status in ("ended", "stopped")],
    }
    for subs in by_status.values():
        subs.sort(key=lambda s: -yearly(s.amount, s.period))
    return by_status


def _next(s: Sub, today: date) -> str:
    if s.status == "trial":
        if s.next_renewal < today:  # still in grace: the first charge may yet arrive
            day = short_date(s.next_renewal, today)
            return f"ended {day}" if s.kind == "trial" else f"was due {day}"
        return ("ends " if s.kind == "trial" else "first debit ") + when(s.next_renewal, today)
    if s.status in ("ended", "stopped"):
        if s.status == "ended":
            return "ended"
        if not s.charges:  # a trial that lapsed without charging
            return f"lapsed {short_date(s.next_renewal, today)}"
        return f"last {short_date(s.charges[-1][0], today)}"
    return short_date(s.next_renewal, today)


def terminal(report: Report) -> str:
    t = totals(report.subs, report.credits_30, report.credits_365)
    today = report.today
    out = [
        f"subwatch, as of {today.day} {today:%b %Y}",
        f"  Monthly burn {whole(t['monthly_burn'])}   Yearly burn {whole(t['yearly_burn'])}   "
        f"Wasted on unused {whole(t['wasted'])}   Saved by cancelling {whole(t['saved'])}",
        f"  Commitments {whole(t['commitments_monthly'])}/month   "
        f"AI credits {whole(t['credits_30'])} last 30 days, "
        f"{whole(t['credits_365'])} last 12 months",
    ]
    for s in report.subs:
        if s.alert:
            out.append(f"\n  ! {alert_text(s, today)}")
    for title, subs in sections(report).items():
        if not subs:
            continue
        out.append(f"\n  {title.upper()}")
        for s in subs:
            price = inr(s.amount, s.approx) if s.amount else "needs a price"
            out.append(
                f"  {s.name[:24]:<24} {price:>12} / {PERIOD_WORD[s.period]:<8} "
                f"{_next(s, today):<22} {usage_text(s, today):<24} {decision_text(s, today)}"
            )
    if report.expiring:
        out.append("\n  AI CREDITS EXPIRING")
        for name, paise, due, approx in report.expiring:
            out.append(f"  {name:<24} {inr(paise, approx):>12}   expire {when(due, today)}")
    return "\n".join(out)


CSS = """
:root{--bg:#f6f5f1;--panel:#fff;--ink:#1d1d1b;--muted:#6b6a64;--line:#e4e2db;--loss:#b4261a;
--gain:#1d7a46;--warn:#9a5b00;--chip:#efede6}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--panel:#1d1d1b;--ink:#f0eee8;--muted:#a3a198;
--line:#33322f;--loss:#ff7a6b;--gain:#5cc98a;--warn:#f0b24a;--chip:#2a2926}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:1080px;margin:0 auto;padding:28px 16px 48px}
header{display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:8px}
h1{font-size:22px;margin:0}
h1 span{color:var(--muted);font-weight:400;font-size:15px;margin-left:8px}
.demo{background:var(--chip);border-radius:6px;padding:6px 10px;font-size:13px;color:var(--muted)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin:20px 0}
.tile{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.tile b{display:block;font-size:26px;font-variant-numeric:tabular-nums;margin-top:2px}
.tile small{color:var(--muted)}.loss b{color:var(--loss)}.gain b{color:var(--gain)}
.sub{color:var(--muted);font-size:13px;margin:-4px 0 18px}
.alert{border-left:4px solid var(--loss);background:var(--panel);padding:10px 14px;margin:8px 0;
border-radius:6px}.remind{border-left-color:var(--warn)}
section{background:var(--panel);border:1px solid var(--line);border-radius:10px;margin:16px 0;
overflow-x:auto}h2{font-size:15px;margin:0;padding:12px 16px;border-bottom:1px solid var(--line)}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:9px 16px;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--muted);font-weight:500;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
tr:last-child td{border-bottom:0}td.num{text-align:right;font-variant-numeric:tabular-nums}
.chip{background:var(--chip);border-radius:99px;padding:2px 8px;font-size:12px}
.chip.bad{color:var(--loss)}.chip.ok{color:var(--gain)}.chip.ask{color:var(--warn)}
footer{color:var(--muted);font-size:12px;margin-top:24px}
"""


def _chip(text: str) -> str:
    cls = "bad" if text.startswith("unused") else "ok" if text.startswith("used") else ""
    cls = "ask" if "review" in text else cls
    return f'<span class="chip {cls}">{escape(text)}</span>'


def html(report: Report, demo: bool = False) -> str:
    t = totals(report.subs, report.credits_30, report.credits_365)
    today = report.today
    tiles = [
        ("Monthly burn", whole(t["monthly_burn"]), "active subscriptions and bills", ""),
        ("Yearly burn", whole(t["yearly_burn"]), "if nothing changes", ""),
        ("Wasted on unused", whole(t["wasted"]), "charges after you stopped using it", "loss"),
        ("Saved by cancelling", whole(t["saved"]), "charges avoided after you cancelled", "gain"),
    ]
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>subwatch</title><style>{CSS}</style></head><body><main>",
        f"<header><h1>subwatch<span>as of {today.day} {today:%b %Y}</span></h1>",
        "<span class='demo'>Demo: synthetic data, no real person</span>" if demo else "",
        "</header><div class='tiles'>",
    ]
    for label, value, hint, cls in tiles:
        parts.append(
            f"<div class='tile {cls}'><small>{label}</small><b>{value}</b>"
            f"<small>{hint}</small></div>"
        )
    parts.append(
        f"</div><p class='sub'>Commitments (EMI, SIP, rent, insurance) "
        f"{whole(t['commitments_monthly'])}/month, kept apart from burn. AI credits "
        f"{whole(t['credits_30'])} in the last 30 days, {whole(t['credits_365'])} in 12 months.</p>"
    )
    for r in build(report):
        cls = "alert" if r.kind == "alert" else "alert remind"
        parts.append(f"<div class='{cls}'>{escape(r.text)}</div>")
    for title, subs in sections(report).items():
        if not subs:
            continue
        parts.append(f"<section><h2>{escape(title)}</h2><table><tr><th>Name</th>")
        parts.append(
            "<th class='num'>Amount</th><th>Every</th><th class='num'>Per year</th><th>Next</th>"
            "<th>Usage</th><th>Continue?</th></tr>"
        )
        for s in subs:
            amount = inr(s.amount, s.approx) if s.amount else "needs a price"
            per_year = inr(yearly(s.amount, s.period), s.approx) if s.amount else "-"
            parts.append(
                f"<tr><td>{escape(s.name)}</td><td class='num'>{amount}</td>"
                f"<td>{PERIOD_WORD[s.period]}</td><td class='num'>{per_year}</td>"
                f"<td>{escape(_next(s, today))}</td><td>{_chip(usage_text(s, today))}</td>"
                f"<td>{escape(decision_text(s, today))}</td></tr>"
            )
        parts.append("</table></section>")
    if report.expiring:
        parts.append("<section><h2>AI credits expiring</h2><table>")
        for name, paise, due, approx in report.expiring:
            parts.append(
                f"<tr><td>{escape(name)}</td><td class='num'>{inr(paise, approx)}</td>"
                f"<td>expire {when(due, today)}</td></tr>"
            )
        parts.append("</table></section>")
    monthly_note = sum(monthly(s.amount, s.period) for s in report.subs if s.status == "trial")
    parts.append(
        f"<footer>Trials and upcoming AutoPay add {inr(monthly_note)}/month if you let them run. "
        "Built locally by subwatch; nothing left this machine. Decide with "
        "<code>subwatch review</code>.</footer></main></body></html>"
    )
    return "".join(parts)
