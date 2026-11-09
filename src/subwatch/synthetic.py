"""Synthetic data: ~18 months of Gmail and bank SMS for one made-up Indian user.

Everything here is invented and written in the style of real HDFC/ICICI/SBI/Axis/Kotak alerts
and service receipts. The same person wrote this generator and the detector, so scores on it
are a regression gate, not evidence of real-world accuracy.
"""

from __future__ import annotations

import csv
import mailbox
import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import quoteattr

from .money import add_months
from .parse import IST

MBOX_NAME = "All mail Including Spam and Trash.mbox"


def _r(paise: int, commas: bool = False) -> str:
    return f"{paise / 100:,.2f}" if commas else f"{paise / 100:.2f}"


SMS = {
    "hdfc_upi": "Sent Rs.{amt}\nFrom HDFC Bank A/C *{acct}\nTo {payee}\nOn {d:%d/%m/%y}\nRef {ref}\n"
    "Not You?\nCall 18002586161/SMS BLOCK UPI to 7308080808",
    "hdfc_card": "Rs.{amt} spent on HDFC Bank Card x{acct} at {payee} on {d:%Y-%m-%d}:{t}. "
    "Not you? To block+reissue, Call 18002586161/SMS BLOCK DC {acct} to 7308080808",
    "icici_card": "INR {amtc} spent using ICICI Bank Card XX{acct} on {d:%d-%b-%y} on {payee}. "
    "Avl Limit: INR {bal}. If not you, call 1800 2662/SMS BLOCK {acct} to 9215676766",
    "sbi_upi": "Dear UPI user A/C X{acct} debited by {plain} on date {d:%d%b%y} trf to {payee} "
    "Refno {ref}. If not u? call 1800111109. -SBI",
    "axis_upi": "INR {amt} debited\nA/c no. XX{acct}\n{d:%d-%m-%y}, {t}\nUPI/P2M/{ref}/{payee}\n"
    "Not you? SMS BLOCKUPI Cust ID to 919951860002\nAxis Bank",
    "kotak_upi": "Sent Rs.{amt} from Kotak Bank AC X{acct} to {payee} on {d:%d-%m-%y}.UPI Ref {ref}. "
    "Not you, https://kotak.com/KBANKT/Fraud",
    "nach": "Rs {amt} debited from A/c XX{acct} on {d:%d-%m-%y} towards ACH D- {payee}. "
    "Avl Bal Rs {bal}",
    "hdfc_usd": "USD {usd} spent on HDFC Bank Card x{acct} at {payee} on {d:%Y-%m-%d}:{t}. "
    "Not you? Call 18002586161",
}
SENDERS = {
    "hdfc": "VM-HDFCBK",
    "icici": "AD-ICICIT",
    "sbi": "JD-SBIUPI",
    "axis": "VK-AXISBK",
    "kotak": "BZ-KOTAKB",
    "nach": "VM-HDFCBK",
}


@dataclass
class Plan:
    """One recurring payment in the made-up user's life, and its ground-truth label."""

    key: str  # label key: the alias key, or a name for unknown payees
    name: str
    amount: int
    period: str
    category: str
    channel: str  # SMS template
    payee: str
    start: date
    end: date | None = None  # last charge on or before this date
    receipt: tuple[str, str, str] | None = None  # (from, subject, body) templates
    receipt_rate: float = 1.0  # share of months whose receipt email exists
    hike: tuple[date, int] | None = None  # price change from this date
    usd: int | None = None  # cents, for USD card charges
    bill_range: tuple[int, int] | None = None  # variable bills


class Gen:
    def __init__(self, folder: Path, today: date, seed: int):
        self.rng = random.Random(seed)
        self.today = today
        self.start = today - timedelta(days=540)
        self.folder = folder
        self.sms: list[tuple[str, datetime, str]] = []
        self.mails: list[EmailMessage] = []
        self.labels: list[dict] = []

    # ------------------------------------------------------------ primitives

    def at(self, d: date) -> datetime:
        return datetime.combine(d, time(self.rng.randint(7, 22), self.rng.randint(0, 59)), IST)

    def ref(self) -> str:
        return str(self.rng.randint(10**11, 10**12 - 1))

    def text(self, channel: str, d: date, paise: int, payee: str, usd: int | None = None) -> str:
        return SMS[channel].format(
            amt=_r(paise),
            amtc=_r(paise, True),
            plain=f"{paise / 100:.1f}",
            acct=self.rng.choice(["1234", "5678", "4321"]),
            payee=payee,
            d=d,
            t=f"{self.rng.randint(0, 23):02d}:{self.rng.randint(0, 59):02d}:{self.rng.randint(0, 59):02d}",
            ref=self.ref(),
            bal=_r(self.rng.randint(5_000_00, 3_00_000_00), True),
            usd=f"{(usd or 0) / 100:.2f}",
        )

    def add_sms(self, sender: str, d: date, body: str) -> None:
        self.sms.append((sender, self.at(d), body))

    def charge_sms(self, channel: str, d: date, paise: int, payee: str, usd: int | None = None):
        bank = "nach" if channel == "nach" else channel.split("_")[0]
        self.add_sms(SENDERS[bank], d, self.text(channel, d, paise, payee, usd))

    def mail(self, sender: str, subject: str, body: str, d: date, labels: str = "Inbox") -> None:
        m = EmailMessage()
        m["From"] = sender
        m["To"] = "me@example.com"
        m["Subject"] = subject
        m["Date"] = format_datetime(self.at(d))
        m["Message-ID"] = f"<{self.ref()}.{len(self.mails)}@synthetic.example>"
        m["X-Gmail-Labels"] = labels
        m.set_content(body)
        self.mails.append(m)

    def label(self, key, name, amount, period, category, active, kind="sub", note=""):
        self.labels.append(
            dict(
                key=key,
                name=name,
                amount_rupees=amount // 100,
                period=period,
                category=category,
                active="yes" if active else "no",
                kind=kind,
                note=note,
            )
        )

    # ------------------------------------------------------------ scenarios

    def dates(self, p: Plan) -> list[date]:
        out, i = [], 0
        while True:
            if p.period in ("28d", "84d"):
                d = p.start + timedelta(days=int(p.period[:-1]) * i)
            else:
                d = add_months(p.start, {"month": 1, "quarter": 3, "year": 12}[p.period] * i)
            if d > self.today or (p.end and d > p.end):
                return out
            if d >= self.start:
                out.append(d)
            i += 1

    def recurring(self, p: Plan) -> None:
        for d in self.dates(p):
            amount = p.amount
            if p.hike and d >= p.hike[0]:
                amount = p.hike[1]
            if p.bill_range:
                amount = self.rng.randint(*p.bill_range) * 100
            self.charge_sms(p.channel, d, amount, p.payee, p.usd)
            if p.receipt and self.rng.random() < p.receipt_rate:
                frm, subject, body = p.receipt
                fields = dict(
                    amt=_r(amount, True),
                    usd=f"{(p.usd or 0) / 100:.2f}",
                    d=d,
                    next=add_months(d, 1),
                    n=self.rng.randint(1000, 9999),
                )
                self.mail(frm, subject.format(**fields), body.format(**fields), d)
        last_amount = p.hike[1] if p.hike else p.amount
        active = p.end is None
        self.label(p.key, p.name, last_amount, p.period, p.category, active)

    def build(self) -> None:
        t = self.today
        S = self.start
        plans = [
            Plan(
                "spotify",
                "Spotify",
                11900,
                "month",
                "service",
                "hdfc_upi",
                "SPOTIFY INDIA",
                S + timedelta(days=18),
                receipt=(
                    "Spotify <no-reply@spotify.com>",
                    "Your Spotify Premium receipt",
                    "Thanks for your payment.\nPremium Individual\n₹{amt} / month\nTotal ₹{amt}\n"
                    "Next payment: {next:%d %B %Y}",
                ),
                receipt_rate=0.9,
            ),
            Plan(
                "netflix",
                "Netflix",
                49900,
                "month",
                "service",
                "icici_card",
                "NETFLIX.COM",
                S + timedelta(days=2),
                hike=(t - timedelta(days=200), 64900),
            ),
            Plan(
                "youtube",
                "YouTube Premium",
                12900,
                "month",
                "service",
                "hdfc_card",
                "GOOGLE PLAY",
                S + timedelta(days=46),
                receipt=(
                    "Google Play <googleplay-noreply@google.com>",
                    "Your Google Play Order Receipt from {d:%b} {d.day}, {d.year}",
                    "Thank you. You've made a purchase from Google Play.\nOrder number: GPA.{n}\n"
                    "Item: YouTube Premium (YouTube)\nAuto-renewing subscription\n"
                    "Price: ₹{amt}/month\nTotal: ₹{amt}",
                ),
                receipt_rate=0.7,
            ),
            Plan(
                "google-one",
                "Google One",
                13000,
                "month",
                "service",
                "hdfc_card",
                "GOOGLE PLAY",
                S + timedelta(days=95),
                receipt=(
                    "Google Play <googleplay-noreply@google.com>",
                    "Your Google Play Order Receipt from {d:%b} {d.day}, {d.year}",
                    "Thank you. You've made a purchase from Google Play.\nOrder number: GPA.{n}\n"
                    "Item: Google One 100 GB (Google One)\nAuto-renewing subscription\n"
                    "Price: ₹{amt}/month\nTotal: ₹{amt}",
                ),
                receipt_rate=0.6,
            ),
            Plan(
                "amazon-prime",
                "Amazon Prime",
                149900,
                "year",
                "service",
                "icici_card",
                "AMAZON PRIME",
                S + timedelta(days=38),
                receipt=(
                    "Amazon.in <no-reply@amazon.in>",
                    "Your Amazon Prime membership has been renewed",
                    "Thank you for being a Prime member. Your membership has been renewed for "
                    "₹{amt}. Your next renewal date is {d:%d %B} next year.",
                ),
            ),
            Plan(
                "jiohotstar",
                "JioHotstar",
                29900,
                "quarter",
                "service",
                "axis_upi",
                "JioHotstar",
                S + timedelta(days=23),
            ),
            Plan(
                "claude",
                "Claude Pro",
                196650,
                "month",
                "service",
                "hdfc_usd",
                "CLAUDE.AI SUBSCRIPTION",
                t - timedelta(days=279),
                usd=2000,
                receipt=(
                    "Anthropic, PBC <invoice+statements@mail.anthropic.com>",
                    "Your receipt from Anthropic, PBC #{n}-{n}",
                    "Receipt\nClaude Pro\n${usd}\nAmount paid ${usd}\nDate paid {d:%B %d, %Y}",
                ),
            ),
            Plan(
                "chatgpt",
                "ChatGPT Plus",
                199900,
                "month",
                "service",
                "hdfc_card",
                "OPENAI *CHATGPT SUBSCR",
                t - timedelta(days=120),
                receipt=(
                    "OpenAI <noreply@tm.openai.com>",
                    "Your ChatGPT Plus receipt",
                    "ChatGPT Plus Subscription\n₹{amt} per month\nAmount paid ₹{amt}",
                ),
            ),
            Plan(
                "airtel",
                "Airtel postpaid",
                49900,
                "month",
                "bill",
                "kotak_upi",
                "airtelpayments@airtel",
                S + timedelta(days=11),
                bill_range=(499, 650),
            ),
            Plan(
                "jio",
                "Jio prepaid",
                34900,
                "28d",
                "bill",
                "hdfc_upi",
                "RELIANCE JIO INFOCOMM",
                S + timedelta(days=15),
            ),
            Plan(
                "vi",
                "Vi prepaid",
                71900,
                "84d",
                "bill",
                "sbi_upi",
                "VODAFONE IDEA",
                S + timedelta(days=29),
            ),
            Plan(
                "act",
                "ACT Fibernet",
                105000,
                "month",
                "bill",
                "icici_card",
                "ACT FIBERNET",
                S + timedelta(days=8),
                bill_range=(1050, 1180),
            ),
            Plan(
                "hdfc homeloan emi",
                "Home loan EMI",
                2350000,
                "month",
                "commitment",
                "nach",
                "HDFC BANK LTD-HOMELOAN EMI",
                S + timedelta(days=20),
            ),
            Plan(
                "bse sip",
                "Mutual fund SIP",
                500000,
                "month",
                "commitment",
                "nach",
                "BSE LIMITED",
                S + timedelta(days=25),
            ),
            Plan(
                "rameshkumar",
                "Rent",
                1500000,
                "month",
                "commitment",
                "kotak_upi",
                "rameshkumar@okhdfcbank",
                S + timedelta(days=16),
            ),
            Plan(
                "lic",
                "LIC premium",
                451200,
                "quarter",
                "commitment",
                "hdfc_upi",
                "LIC OF INDIA",
                S + timedelta(days=60),
            ),
            Plan(
                "swiggy-one",
                "Swiggy One",
                29900,
                "quarter",
                "service",
                "hdfc_card",
                "SWIGGY ONE MEMBERSHIP",
                S + timedelta(days=107),
            ),
            Plan(
                "icloud",
                "iCloud+",
                7500,
                "month",
                "service",
                "icici_card",
                "APPLE MEDIA SERVICES",
                S + timedelta(days=12),
                receipt=(
                    "Apple <no_reply@email.apple.com>",
                    "Your receipt from Apple.",
                    "iCloud+ with 50 GB storage\nMonthly\n₹{amt}\nTOTAL ₹{amt}",
                ),
                receipt_rate=0.8,
            ),
            Plan(
                "cultfit",
                "cult.fit",
                149900,
                "month",
                "service",
                "hdfc_card",
                "CULTFIT",
                S + timedelta(days=25),
                end=t - timedelta(days=120),
            ),
        ]
        for p in plans:
            self.recurring(p)

        # Netflix: card e-mandate pre-debit notices, sign-ins that stopped in August.
        for d in self.dates(plans[1]):
            due = d
            self.add_sms(
                SENDERS["icici"],
                due - timedelta(days=1),
                f"Dear Customer, Rs {_r(64900 if plans[1].hike and d >= plans[1].hike[0] else 49900)} "
                f"will be debited from your ICICI Bank Credit Card XX1234 on {due:%d-%m-%Y} "
                "towards NETFLIX as per e-mandate. -ICICI Bank",
            )
        for d in self._spread(S, t - timedelta(days=85), 6):
            self.mail(
                "Netflix <info@account.netflix.com>",
                "New sign-in to your account",
                "We noticed a new sign-in to your Netflix account on a TV.",
                d,
            )
        # JioHotstar: mandate at the start, login OTPs every week or two.
        self.add_sms(
            SENDERS["axis"],
            plans[5].start,
            "Mandate created successfully for JioHotstar of Rs 299.00 on your Axis Bank A/c "
            "XX1234. Frequency: Quarterly. -Axis Bank",
        )
        d = S
        while d < t:
            d += timedelta(days=self.rng.randint(5, 14))
            self.add_sms(
                "JD-JIOHTR",
                d,
                f"{self.rng.randint(100000, 999999)} is your OTP to login to JioHotstar. "
                "Do not share it with anyone.",
            )
        # Claude: magic-link logins every few days.
        d = plans[6].start
        while d < t:
            d += timedelta(days=self.rng.randint(2, 6))
            self.mail(
                "Claude Team <no-reply@mail.anthropic.com>",
                "Secure link to log in to Claude.ai",
                "Click the link below to log in to Claude.ai. This link expires in 1 hour.",
                d,
            )
        # LinkedIn: one annual charge.
        li = t - timedelta(days=240)
        self.charge_sms("icici_card", li, 1100000, "LINKEDIN")
        self.mail(
            "LinkedIn <billing@linkedin.com>",
            "Your LinkedIn Premium receipt",
            "Premium Career annual subscription\n₹11,000.00 / year\nTotal ₹11,000.00",
            li,
        )
        self.label("linkedin", "LinkedIn Premium", 1100000, "year", "service", True)
        self.trials()
        self.negatives()

    def _spread(self, a: date, b: date, n: int) -> list[date]:
        return sorted(a + timedelta(days=self.rng.randint(0, (b - a).days)) for _ in range(n))

    def trials(self) -> None:
        t = self.today
        # Duolingo: trial started, then converted into a monthly sub.
        start = t - timedelta(days=100)
        self.mail(
            "Duolingo <no-reply@duolingo.com>",
            "Your Super Duolingo free trial has started",
            f"You won't be charged until {start + timedelta(days=14):%B %d, %Y}. After your "
            "trial, you'll be charged ₹499.00/month.",
            start,
        )
        for i in range(4):
            d = add_months(start + timedelta(days=14), i)
            if d > t:
                break
            self.charge_sms("hdfc_card", d, 49900, "DUOLINGO")
            self.mail(
                "Duolingo <no-reply@duolingo.com>",
                "Your Super Duolingo receipt",
                "Super Duolingo monthly\nTotal ₹499.00 / month",
                d,
            )
        self.label("duolingo", "Super Duolingo", 49900, "month", "service", True)
        # SonyLIV: trial ending in 3 days.
        self.mail(
            "SonyLIV <noreply@sonyliv.com>",
            "Your free trial ends soon",
            f"Your free trial ends on {t + timedelta(days=3):%d %B %Y}. After that, you'll be "
            "charged ₹399/month.",
            t - timedelta(days=4),
        )
        self.label("sonyliv", "SonyLIV", 39900, "month", "service", True, "trial")
        # ZEE5: AutoPay mandate, first debit in 5 days.
        self.add_sms(
            SENDERS["hdfc"],
            t - timedelta(days=1),
            "Your UPI-Mandate is successfully created for ZEE5 for Rs 99.00. Frequency: Monthly. "
            f"First debit on {t + timedelta(days=5):%d-%m-%Y}. -HDFC Bank",
        )
        self.label("zee5", "ZEE5", 9900, "month", "service", True, "trial")
        # Canva: trial ended 5 weeks ago without a charge.
        c = t - timedelta(days=70)
        self.mail(
            "Canva <no-reply@canva.com>",
            "Your Canva Pro free trial has started",
            f"Your trial ends on {c + timedelta(days=30):%d %B %Y}. You'll be charged ₹499/month "
            "after your trial.",
            c,
        )
        self.label("canva", "Canva Pro", 49900, "month", "service", False, "trial")

    def negatives(self) -> None:
        t, S, rng = self.today, self.start, self.rng
        neg = lambda key, note: self.label(key, key, 0, "", "", False, "negative", note)  # noqa: E731
        d = S
        while d < t:  # food orders: frequent, random amounts, SMS + email receipt
            d += timedelta(days=rng.randint(1, 5))
            amt = rng.randint(180, 650) * 100
            self.charge_sms("hdfc_upi", d, amt, "Swiggy")
            if rng.random() < 0.7:
                self.mail(
                    "Swiggy <noreply@swiggy.in>",
                    f"Your Swiggy order summary for Order No. {self.ref()}",
                    f"Thank you for your order! Order Total ₹{_r(amt, True)}",
                    d,
                )
        neg("swiggy", "food orders")
        for d in self._spread(S, t, 70):
            self.charge_sms("kotak_upi", d, rng.randint(150, 900) * 100, "zomato@hdfcbank")
        neg("zomato", "food orders")
        for d in self._spread(S, t, 160):
            self.charge_sms("icici_card", d, rng.randint(90, 450) * 100, "UBER INDIA")
        neg("uber", "rides")
        for d in self._spread(S, t, 50):
            self.charge_sms("icici_card", d, rng.randint(299, 4999) * 100, "AMAZON")
        neg("amazon", "shopping next to Amazon Prime")
        for d in self._spread(S, t, 12):
            self.charge_sms("hdfc_card", d, rng.randint(400, 900) * 100, "BOOKMYSHOW")
        neg("bookmyshow", "movie tickets")
        m = S + timedelta(days=3)
        while m < t:  # monthly payment OTPs for the same amount: never a charge
            self.add_sms(
                SENDERS["hdfc"],
                m,
                f"{rng.randint(100000, 999999)} is OTP for txn of INR 1,999.00 at MYNTRA on HDFC "
                "Bank card ending 1234. Valid till 10:22. Do not share OTP for security reasons",
            )
            self.add_sms(
                SENDERS["hdfc"],
                m + timedelta(days=1),
                "Rs.12000.00 debited from A/c **1234 towards ICICI Bank Credit Card bill payment",
            )
            self.add_sms(
                SENDERS["hdfc"],
                m + timedelta(days=2),
                "Rs 85,000.00 credited to A/c XX1234 by NEFT-ACME LABS SALARY. Avl Bal Rs 1,20,000",
            )
            self.charge_sms("kotak_upi", m + timedelta(days=3), 1000000, "naman.v@oksbi")
            self.add_sms(
                SENDERS["hdfc"],
                m + timedelta(days=9),
                "Rs 2,000.00 debited from A/c XX1234 for UPI Lite top-up. -HDFC Bank",
            )
            self.mail(
                "HDFC Bank Credit Cards <emailstatements.cards@hdfcbank.net>",
                "Your HDFC Bank Credit Card statement",
                f"Statement for your credit card. Total Amount Due ₹{_r(rng.randint(4000, 20000) * 100, True)}"
                ". Please pay your bill by the due date.",
                m + timedelta(days=5),
            )
            m = add_months(m, 1)
        neg("myntra", "monthly payment OTPs")
        neg("icici card bill", "credit card bill payments")
        neg("salary", "salary credits")
        neg("naman v", "monthly self-transfer to own account")
        neg("upi lite", "UPI Lite top-ups")
        neg("hdfc bank", "credit card statement emails")
        # One-off API credits: top-ups, not subscriptions.
        a = t - timedelta(days=20)
        self.charge_sms("hdfc_usd", a, 0, "ANTHROPIC", usd=5000)
        self.mail(
            "Anthropic, PBC <invoice+statements@mail.anthropic.com>",
            "Your receipt from Anthropic, PBC #7731-0042",
            f"API credits purchase\nAmount paid $50.00\nCredits expire on "
            f"{a + timedelta(days=365):%B %d, %Y}.",
            a,
        )
        neg("anthropic api credits", "one-off credit top-up")
        for d in self._spread(S, t, 25):  # refunds
            self.add_sms(
                SENDERS["icici"], d, "Refund of Rs 649.00 from NETFLIX credited to your card XX1234"
            )
        # Marketing that names prices and trials.
        for d in self._spread(S, t, 10):
            self.mail(
                "Apple <news@insideapple.apple.com>",
                "Start your free trial of Apple TV+",
                "Try it free for 7 days, then ₹99/month.",
                d,
            )
            self.mail(
                "YouTube <noreply@youtube.com>",
                "Get YouTube Premium for just ₹129/month",
                "Ad-free videos. Subscription with auto renewal. Cancel anytime.",
                d,
            )
        neg("apple tv", "trial marketing")

    def noise(self, n: int) -> None:
        senders = [
            ("Medium Daily Digest <noreply@medium.com>", "Stories for you"),
            ("LinkedIn <notifications@linkedin.com>", "You appeared in 9 searches this week"),
            ("GitHub <noreply@github.com>", "[repo] New issue opened"),
            ("Zomato <noreply@zomato.com>", "Hungry? 50% off on your next order"),
            ("Google <no-reply@accounts.google.com>", "Security alert"),
            ("College Office <office@example.edu>", "Timetable for next week"),
        ]
        for i in range(n):
            frm, subject = senders[i % len(senders)]
            d = self.start + timedelta(days=self.rng.randint(0, 539))
            self.mail(frm, subject, f"Lorem ipsum update number {i}. Read more on the site.", d)

    def write(self) -> list[dict]:
        self.folder.mkdir(parents=True, exist_ok=True)
        mbox_path = self.folder / MBOX_NAME
        mbox_path.unlink(missing_ok=True)
        box = mailbox.mbox(mbox_path)
        box.lock()
        try:
            for m in sorted(self.mails, key=lambda m: m["Date"].datetime):
                box.add(m)
        finally:
            box.flush()
            box.unlock()
            box.close()
        rows = sorted(self.sms, key=lambda r: r[1])
        lines = [f'<?xml version="1.0" encoding="UTF-8"?>\n<smses count="{len(rows)}">']
        for sender, when, body in rows:
            ms = int(when.timestamp() * 1000)
            lines.append(
                f'  <sms protocol="0" address={quoteattr(sender)} date="{ms}" type="1" '
                f'body={quoteattr(body)} read="1" />'
            )
        lines.append("</smses>\n")
        (self.folder / f"sms-{self.today:%Y%m%d}.xml").write_text(
            "\n".join(lines), encoding="utf-8"
        )
        return self.labels


def generate(folder: Path, today: date, seed: int = 7, noise: int = 1500) -> list[dict]:
    """Write a synthetic mbox and SMS backup into folder; return the ground-truth labels."""
    g = Gen(Path(folder), today, seed)
    g.build()
    g.noise(noise)
    return g.write()


def write_labels(labels: list[dict], path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(labels[0]))
        w.writeheader()
        w.writerows(labels)
