"""Turn Gmail messages (Takeout mbox or IMAP) and Indian bank SMS backups into Events.

Only extracted fields leave this module: kind, merchant, amount and dates. Message
bodies are never stored.
"""

from __future__ import annotations

import csv
import hashlib
import html
import imaplib
import mailbox
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email import policy
from email.parser import BytesHeaderParser, BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))  # India has no DST; avoids needing tzdata on Windows


@dataclass
class Event:
    date: date
    source: str  # email | sms
    kind: str  # charge | trial | mandate | predebit | usage | topup
    merchant: str
    amount: int | None = None  # minor units: paise for INR, cents for USD
    currency: str = "INR"
    period: str | None = None  # 28d | month | 84d | quarter | year, when the text states it
    due: date | None = None  # trial end, first/next debit, credit expiry
    category: str | None = None  # bill | commitment, when the text hints it
    signal: bool = False  # subscription wording seen: autopay, mandate, renewal, membership...
    ref: str = ""  # Message-ID or SMS hash, for incremental scans

    def to_json(self) -> dict:
        d = asdict(self)
        d["date"] = self.date.isoformat()
        d["due"] = self.due.isoformat() if self.due else None
        return d

    @classmethod
    def from_json(cls, d: dict) -> Event:
        d = dict(d)
        d["date"] = date.fromisoformat(d["date"])
        d["due"] = date.fromisoformat(d["due"]) if d.get("due") else None
        return cls(**d)


# key: (display name, category, patterns). Patterns are lower-case; longest match wins.
MERCHANTS: dict[str, tuple[str, str, list[str]]] = {
    "spotify": ("Spotify", "service", ["spotify"]),
    "netflix": ("Netflix", "service", ["netflix"]),
    "youtube": ("YouTube Premium", "service", ["youtube premium", "youtubepremium", "youtube"]),
    "google-one": ("Google One", "service", ["google one", "googleone", "google storage"]),
    "google-play": ("Google Play", "service", ["google play", "googleplay", "google*play"]),
    "apple": ("Apple", "service", ["apple.com/bill", "apple services", "apple media", "itunes"]),
    "icloud": ("iCloud+", "service", ["icloud"]),
    "amazon-prime": (
        "Amazon Prime",
        "service",
        ["amazon prime", "prime video", "primevideo", "prime membership", "amzn prime"],
    ),
    "kindle-unlimited": ("Kindle Unlimited", "service", ["kindle unlimited"]),
    "audible": ("Audible", "service", ["audible"]),
    "jiohotstar": ("JioHotstar", "service", ["jiohotstar", "hotstar", "disney+ hotstar"]),
    "sonyliv": ("SonyLIV", "service", ["sonyliv", "sony liv"]),
    "zee5": ("ZEE5", "service", ["zee5"]),
    "jiosaavn": ("JioSaavn", "service", ["jiosaavn", "saavn"]),
    "claude": ("Claude", "service", ["claude.ai", "claude pro", "claude max", "anthropic"]),
    "chatgpt": ("ChatGPT", "service", ["chatgpt", "openai"]),
    "github": ("GitHub", "service", ["github"]),
    "linkedin": ("LinkedIn Premium", "service", ["linkedin"]),
    "adobe": ("Adobe", "service", ["adobe"]),
    "microsoft-365": ("Microsoft 365", "service", ["microsoft 365", "office 365", "microsoft*"]),
    "canva": ("Canva", "service", ["canva"]),
    "notion": ("Notion", "service", ["notion"]),
    "duolingo": ("Duolingo", "service", ["duolingo"]),
    "swiggy-one": ("Swiggy One", "service", ["swiggy one"]),
    "zomato-gold": ("Zomato Gold", "service", ["zomato gold"]),
    "times-prime": ("Times Prime", "service", ["times prime"]),
    "cultfit": ("cult.fit", "service", ["cult.fit", "cultfit", "curefit"]),
    "airtel": ("Airtel", "bill", ["airtel"]),
    "jio": ("Jio", "bill", ["jio prepaid", "jio recharge", "reliance jio", "jiofiber", "jio"]),
    "vi": ("Vi", "bill", ["vodafone idea", "vodafone", "vi prepaid", "vi postpaid"]),
    "act": ("ACT Fibernet", "bill", ["act fibernet", "actcorp"]),
    "bsnl": ("BSNL", "bill", ["bsnl"]),
    "tata-play": ("Tata Play", "bill", ["tata play", "tataplay"]),
    "lic": ("LIC", "commitment", ["lic of india", "lic premium", "lic"]),
}
AGGREGATORS = {"google-play", "apple"}  # billing fronts for many services


def _alias_regex(p: str) -> re.Pattern:
    # Start boundary always; end boundary only for short names, so "netflixupi@icici"
    # still matches netflix but "jiomart" does not match jio.
    right = r"(?![a-z0-9])" if len(p) <= 4 else ""
    return re.compile(r"(?<![a-z0-9])" + re.escape(p) + right)


_ALIASES = sorted(
    ((p, k) for k, (_, _, pats) in MERCHANTS.items() for p in pats), key=lambda x: -len(x[0])
)
_ALIAS_RES = [(_alias_regex(p), k) for p, k in _ALIASES]


def known_merchant(text: str, aggregators: bool = True) -> str | None:
    """Return the alias-table key named in text, preferring a specific service over a
    billing front such as Google Play."""
    t = text.lower()
    agg = None
    for rx, key in _ALIAS_RES:
        if rx.search(t):
            if key not in AGGREGATORS:
                return key
            agg = agg or key
    return agg if aggregators else None


def display_name(key: str) -> str:
    return MERCHANTS[key][0] if key in MERCHANTS else key.title()


_AGG_PREFIX = re.compile(
    r"^(?:payu|pyu|raz|rzp|razorpay|bd|billdesk|cca|ccavenue|cashfree|cf|pg|ppsl)\s*[*/\-.]+\s*"
)
_NOISE = re.compile(
    r"\b(?:pvt|private|ltd|limited|llp|inc|india|technologies|technology|services|solutions|"
    r"payments?|online|digital|www|com|in|co|upi|vpa|ref|no|payu|razorpay|rzp|billdesk|"
    r"cashfree|ccavenue|thru|through)\b|[0-9]{3,}"
)


def normalise(raw: str) -> str:
    """Stable key for a merchant the alias table doesn't know."""
    s = _AGG_PREFIX.sub("", raw.strip().lower())
    s = re.sub(r"^n?ach\s*d?\s*-\s*", "", s)  # NACH debit prefix: "ACH D- HDFC BANK LTD"
    s = s.split("@")[0]  # UPI VPA: keep the handle
    s = re.sub(r"[._\-*/#&+]+", " ", s)
    s = _NOISE.sub(" ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return " ".join(s.split()[:3])


# ---------------------------------------------------------------- amounts, dates, wording

_INR = r"(?:₹|\brs\.?|\binr)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)"
_USD = (
    r"(?:\busd|\bus\$|\$)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)|([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*usd\b"
)
_AMOUNT = re.compile(f"(?P<inr>{_INR})|(?P<usd>{_USD})", re.I)


def _minor(num: str) -> int | None:
    try:
        return int(Decimal(num.replace(",", "")) * 100)
    except InvalidOperation:
        return None


# SBI's UPI alerts carry no currency marker: "A/C X1234 debited by 719.0 on date 05Nov26".
_BARE_INR = re.compile(
    r"\b(?:debited|credited) by\s+([0-9][0-9,]*(?:\.[0-9]{1,2})?)\b(?!\s*usd)", re.I
)


def find_amounts(text: str) -> list[tuple[int, int, str]]:
    """(position, minor units, currency) for every amount in text."""
    out = []
    for m in _AMOUNT.finditer(text):
        num = m.group(2) if m.group("inr") else (m.group(4) or m.group(5))
        value = _minor(num)
        if value is not None:
            out.append((m.start(), value, "INR" if m.group("inr") else "USD"))
    for m in _BARE_INR.finditer(text):
        if (value := _minor(m.group(1))) is not None:
            out.append((m.start(1), value, "INR"))
    return sorted(out)


_PREFER = re.compile(
    r"(?:total|amount paid|amount charged|you paid|charged|price|amount)\W{0,25}$", re.I
)


def pick_amount(text: str, email: bool) -> tuple[int, str] | None:
    """SMS: the first amount (balances come later). Email: the amount after
    'total'/'amount paid'/'charged', else the first non-zero one."""
    found = [(p, v, c) for p, v, c in find_amounts(text) if v > 0]
    if not found:
        return None
    if email:
        for p, v, c in found:
            if _PREFER.search(text[max(0, p - 45) : p]):
                return v, c
    return found[0][1], found[0][2]


_MON = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
_MONTHS = {m: i for i, m in enumerate(_MON.split("|"), 1)}
_D_DMY = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?[\s\-/,]*({_MON})[a-z]*\.?[\s\-/,]*(\d{{4}}|\d{{2}}(?!\d))?",
    re.I,
)
_D_MDY = re.compile(
    rf"\b({_MON})[a-z]*\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s*(\d{{4}}))?", re.I
)
_D_NUM = re.compile(r"(?<![\d/.\-])(\d{1,4})[/\-.](\d{1,2})[/\-.](\d{2,4})(?![\d/])")
_D_REL = re.compile(r"\b(today|tomorrow|in (\d{1,2}) days?)\b", re.I)


def _safe(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _year(y: str | None, mon: int, day: int, ref: date) -> date | None:
    if y:
        return _safe(int(y) + (2000 if len(y) == 2 else 0), mon, day)
    # No year: take the one that lands closest to the message date.
    options = [d for yy in (ref.year - 1, ref.year, ref.year + 1) if (d := _safe(yy, mon, day))]
    return min(options, key=lambda d: abs((d - ref).days)) if options else None


def find_dates(text: str, ref: date) -> list[tuple[int, date]]:
    """Every date in text with its position. Month names win over numbers; an ambiguous
    numeric date (both parts <= 12) takes the reading 0-60 days after ref, else dd/mm."""
    out: list[tuple[int, date]] = []
    taken: list[range] = []
    for m in _D_DMY.finditer(text):
        if d := _year(m.group(3), _MONTHS[m.group(2).lower()[:3]], int(m.group(1)), ref):
            out.append((m.start(), d))
            taken.append(range(m.start(), m.end()))
    for m in _D_MDY.finditer(text):
        if any(m.start() in r for r in taken):
            continue
        if d := _year(m.group(3), _MONTHS[m.group(1).lower()[:3]], int(m.group(2)), ref):
            out.append((m.start(), d))
    for m in _D_NUM.finditer(text):
        a, b, c = m.groups()
        if len(a) == 4:
            d = _safe(int(a), int(b), int(c))
        else:
            y = int(c) + (2000 if len(c) == 2 else 0)
            dmy, mdy = _safe(y, int(b), int(a)), _safe(y, int(a), int(b))
            d = dmy or mdy
            if dmy and mdy and dmy != mdy:
                soon = [x for x in (dmy, mdy) if 0 <= (x - ref).days <= 60]
                d = soon[0] if len(soon) == 1 else dmy
        if d:
            out.append((m.start(), d))
    for m in _D_REL.finditer(text):
        word = m.group(1).lower()
        n = 0 if word == "today" else 1 if word == "tomorrow" else int(m.group(2))
        out.append((m.start(), ref + timedelta(days=n)))
    return sorted(out, key=lambda x: x[0])


_DUE_KW = re.compile(
    r"\b(?:ends?|ending|expires?|expiring|expiry|charged|debited|deducted|renews?|renewal|due|"
    r"until|till|starting|first debit|executed|scheduled|valid)\b",
    re.I,
)


def due_date(text: str, ref: date) -> date | None:
    """The date that follows 'ends'/'debited on'/'expires'... else the first date after ref."""
    dates = find_dates(text, ref)
    for kw in _DUE_KW.finditer(text):
        for pos, d in dates:
            if kw.end() <= pos <= kw.end() + 45:
                return d
    later = [d for _, d in dates if d > ref]
    return later[0] if later else None


def period_of(text: str) -> str | None:
    t = text.lower()
    if re.search(r"\b28 ?days?\b", t):
        return "28d"
    if re.search(r"\b84 ?days?\b", t):
        return "84d"
    if re.search(
        r"\b(?:annual|annually|yearly|per year|a year|12 months|365 days)\b|/\s*(?:year|yr)\b", t
    ):
        return "year"
    if re.search(r"\b(?:quarterly|per quarter|3 months|90 days)\b", t):
        return "quarter"
    if re.search(
        r"\b(?:monthly|per month|a month|every month|30 days|1 month)\b|/\s*(?:month|mo)\b", t
    ):
        return "month"
    return None


_COMMIT = re.compile(
    r"\bemi\b|\bloan\b|\bnach\b|\bach d\b|\bsip\b|mutual fund|\bbse\b|\bcams\b|kfin|insurance|"
    r"\bpolicy\b|premium (?:due|paid|payment)|\brent\b",
    re.I,
)
_BILL = re.compile(r"recharge|postpaid|prepaid|broadband|fibre|fiber|\bdth\b|\bbill\b", re.I)
# shortcut: a UPI payee on a consumer handle is usually a person (rent, tuition, help), so
# it is hinted as a commitment. Merchant VPAs sometimes use these handles too; the alias
# table wins for known services.
_PERSONAL_VPA = re.compile(r"@(?:ok(?:hdfcbank|icici|sbi|axis)|ybl|ibl|axl|apl|yapl)\b", re.I)
_SIGNAL = re.compile(
    r"subscri|renew|membership|autopay|auto-pay|auto pay|mandate|recurring|"
    r"standing instruction|\bplan\b|premium|e-?nach",
    re.I,
)


def category_hint(text: str) -> str | None:
    if _COMMIT.search(text) or _PERSONAL_VPA.search(text):
        return "commitment"
    if _BILL.search(text):
        return "bill"
    return None


def _ref(*parts: str) -> str:
    return hashlib.sha1("\x1f".join(parts).encode("utf-8", "replace")).hexdigest()[:16]


# ---------------------------------------------------------------- SMS

_SMS_SKIP = re.compile(
    r"credited|\breceived\b|refund|revers|cashback|declined|failed|unsuccessful|insufficient|"
    r"\boffer|congrat|pre-?approved|\bwin\b|apply now|bill (?:is )?generated|"
    r"min(?:imum)? (?:amount )?due|total (?:amount )?due|card bill|towards your (?:\w+ )?card|"
    r"\bcred\b|upi lite|wallet|add(?:ed)? money|load money|\bself\b|own account",
    re.I,
)
_OTP = re.compile(r"\botp\b|one.?time.?pass|verification code|do not share|never share", re.I)
_LOGIN = re.compile(r"log ?in|sign ?in|signin|verify your (?:mobile|number|phone)|to access", re.I)
_PAYCTX = re.compile(r"\btxn\b|transaction|purchase|payment|e-?com|\bcard\b|a/c|\bpay\b", re.I)
_MANDATE = re.compile(
    r"mandate|autopay|auto-pay|auto pay|standing instruction|\bsi\b|e-?nach|recurring", re.I
)
_CREATED = re.compile(r"creat|register|set ?up|successful|activated|approved", re.I)
_PREDEBIT = re.compile(
    r"pre-?debit|will be (?:debited|deducted|charged|executed|processed)|is scheduled|"
    r"due (?:on|for)|upcoming",
    re.I,
)
_DEBIT = re.compile(
    r"debited|\bspent\b|\bpaid\b|\bsent\b|\btxn\b|transaction of|purchase|deducted|withdrawn|"
    r"charged|\bdebit\b",
    re.I,
)
_NOT_MERCHANT = re.compile(
    r"^(?:your|the|a/?c|ac|account|card|hdfc|icici|sbi|axis|kotak|bank|upi|self|mobile|dial|"
    r"call|sms|xx|vpa|not you)\b",
    re.I,
)
_STOP = r"(?=\s+(?:on|via|ref|upi|avl|avbl|txn|using|dt|for|from|is|has)\b|[,;(]|\.(?:\s|$)|\s*$)"
_SMS_MERCHANT = [
    re.compile(r"\bupi/(?:p2[am]/)?\d*/?(?P<m>[^/\s][^/]{1,40}?)(?=/|\s|$)", re.I),
    re.compile(rf"\b(?:at|towards|info[:\-]?)\s+(?P<m>[A-Za-z0-9][^\n]{{1,40}}?){_STOP}", re.I),
    re.compile(rf"\bto\s+(?:vpa\s+)?(?P<m>[A-Za-z0-9][^\n]{{1,40}}?){_STOP}", re.I),
    re.compile(r"\bon\s+(?P<m>[A-Za-z][^\n]{1,40}?)(?=[,;]|\.\s|\.$|\s+avl|\s*$)", re.I),
]


def sms_merchant(text: str) -> str | None:
    if key := known_merchant(text):
        return key
    for rx in _SMS_MERCHANT:
        for m in rx.finditer(text):
            raw = m.group("m").strip()
            if _NOT_MERCHANT.search(raw):
                continue
            if name := normalise(raw):
                # UPI AutoPay "to Google" is Play billing for some app (YouTube, Claude...)
                return "google-play" if name == "google" else name
    return None


def classify_sms(sender: str, body: str, when: datetime) -> Event | None:
    text = " ".join(body.split())
    day = when.astimezone(IST).date()
    ref = _ref(sender, when.isoformat(), body)
    amount = pick_amount(text, email=False)
    if _OTP.search(text):
        # Login OTPs from a service are evidence you use it; payment OTPs are not charges.
        if _LOGIN.search(text) and not (amount and _PAYCTX.search(text)):
            if key := known_merchant(text):
                return Event(day, "sms", "usage", key, ref=ref)
        return None
    if _SMS_SKIP.search(text):
        return None
    merchant = sms_merchant(text)
    if not merchant:
        return None
    common = dict(
        merchant=merchant,
        amount=amount[0] if amount else None,
        currency=amount[1] if amount else "INR",
        period=period_of(text),
        category=category_hint(text),
        signal=bool(_SIGNAL.search(text)),
        ref=ref,
    )
    if _MANDATE.search(text) and _CREATED.search(text) and not _PREDEBIT.search(text):
        return Event(day, "sms", "mandate", due=due_date(text, day), **common)
    if _PREDEBIT.search(text) and amount:
        return Event(day, "sms", "predebit", due=due_date(text, day), **common)
    if _DEBIT.search(text) and amount:
        return Event(day, "sms", "charge", **common)
    return None


def _when(value: str) -> datetime | None:
    value = (value or "").strip()
    if value.isdigit():
        n = int(value)
        return datetime.fromtimestamp(n / 1000 if n > 10**11 else n, IST)
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        dt = None
        for fmt in (
            "%d/%m/%Y %H:%M:%S",
            "%d/%m/%Y %H:%M",
            "%d-%m-%Y %H:%M:%S",
            "%d-%m-%Y %H:%M",
            "%d/%m/%Y",
            "%d-%m-%Y",
            "%b %d, %Y %I:%M:%S %p",
            "%d %b %Y %H:%M",
        ):
            try:
                dt = datetime.strptime(value, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=IST)


def read_sms_xml(path: Path) -> Iterator[tuple[str, datetime | None, str]]:
    """SMS Backup & Restore format: <sms address=.. date=epoch-ms type=1 body=.. />."""
    for _, el in ET.iterparse(path):
        if el.tag == "sms":
            a = el.attrib
            if a.get("type", "1") == "1":  # received only
                yield a.get("address", ""), _when(a.get("date", "")), a.get("body", "")
            el.clear()


_CSV_BODY = ("body", "message", "text", "content", "sms", "message body")
_CSV_WHEN = ("date", "time", "timestamp", "datetime", "date sent", "received", "sent")
_CSV_WHO = ("address", "sender", "from", "number", "phone", "contact")


def read_sms_csv(path: Path) -> Iterator[tuple[str, datetime | None, str]]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = csv.DictReader(f)
        cols = {c.strip().lower(): c for c in rows.fieldnames or []}
        body = next((cols[c] for c in _CSV_BODY if c in cols), None)
        when = next((cols[c] for c in _CSV_WHEN if c in cols), None)
        who = next((cols[c] for c in _CSV_WHO if c in cols), None)
        if not body or not when:
            raise ValueError(
                f"{path.name}: need a message column and a date column, found {list(cols)}"
            )
        for row in rows:
            yield row.get(who, "") if who else "", _when(row.get(when, "")), row.get(body) or ""


# ---------------------------------------------------------------- email

_E_USAGE = re.compile(
    r"new sign-?in|signed in|sign-?in (?:attempt|link|code|alert)|new (?:login|device)|"
    r"log ?in (?:link|code)|login link|magic link|secure link to (?:log|sign)|verification code|"
    r"security code|confirm (?:it'?s|its) you|password (?:reset|changed)|"
    r"verify your (?:email|login)",
    re.I,
)
_E_TRIAL = re.compile(
    r"your (?:free )?trial|trial (?:ends|ending|will end|expires|is ending|period ends|has started|"
    r"started)|(?:will|you'll) be charged (?:on|starting|from)|won'?t be charged until|"
    r"after your (?:free )?trial|when your trial ends",
    re.I,
)
_E_TRIAL_PROMO = re.compile(
    r"start (?:your|a) (?:free )?trial|try (?:it )?(?:for )?free|claim your|sign up (?:now|today)",
    re.I,
)
_E_CONVERTED = re.compile(
    r"trial (?:has )?ended|been charged|we charged|payment (?:was )?successful", re.I
)
_E_RECEIPT = re.compile(
    r"receipt|invoice|payment (?:received|successful|confirm|of)|you paid|amount paid|"
    r"has been charged|we charged|successfully (?:renewed|charged|processed)|renewed|renewal|"
    r"membership|subscription (?:confirm|renew|payment|activated)|order (?:confirm|receipt)|"
    r"thank(?:s| you) for your (?:payment|purchase|order|subscription)|billing",
    re.I,
)
_E_PROMO = re.compile(
    r"% off|\bsale\b|\boffer\b|discount|upgrade now|limited time|\bdeal\b|save \d|special price|"
    r"exclusive|\bget\b.*\bfor just\b",
    re.I,
)
_E_TOPUP = re.compile(r"credit|prepaid|auto-?reload|top.?up|\bapi\b", re.I)
_E_PLAN = re.compile(
    r"\b(?:pro|max|plus|team|premium) plan\b|claude (?:pro|max)|chatgpt (?:plus|pro)|subscription",
    re.I,
)
_E_CANDIDATE = re.compile(
    r"receipt|invoice|payment|paid|order|trial|subscri|renew|membership|charged|billing|\bbill\b|"
    r"plan|premium|credit|sign|log ?in|verif|code|security|device|password|mandate|autopay",
    re.I,
)
_TAGS = re.compile(r"<(script|style)\b.*?</\1>|<[^>]+>", re.I | re.S)


def _html_text(s: str) -> str:
    s = re.sub(r"<br\s*/?>|</(?:p|div|tr|li|h\d)>", "\n", s, flags=re.I)
    return html.unescape(_TAGS.sub(" ", s))


def _body(msg) -> str:
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
        if part is None:
            return ""
        text = part.get_content()
    except (LookupError, ValueError, AttributeError):  # unknown charset or broken MIME
        return ""
    if part.get_content_type() == "text/html":
        text = _html_text(text)
    return " ".join(text.split())


def classify_email(raw: bytes) -> Event | None:
    """One Gmail message (raw RFC 822 bytes) to an Event, or None if irrelevant."""
    try:
        head = BytesHeaderParser(policy=policy.default).parsebytes(raw)
        labels = str(head.get("X-Gmail-Labels", ""))
        subject = str(head.get("Subject", ""))
        name, addr = parseaddr(str(head.get("From", "")))
        sent = parsedate_to_datetime(str(head.get("Date", "")))
    except (TypeError, ValueError, IndexError):
        return None
    if re.search(r"\b(?:spam|trash)\b", labels, re.I) or sent is None:
        return None
    domain = addr.rpartition("@")[2].lower()
    sender = f"{name} {domain}"
    if not (_E_CANDIDATE.search(subject) or known_merchant(f"{subject} {sender}")):
        return None  # header-only fast path: most mail never gets its body decoded
    try:
        msg = BytesParser(policy=policy.default).parsebytes(raw)
    except (TypeError, ValueError):
        return None
    body = _body(msg)[:4000]
    day = (sent if sent.tzinfo else sent.replace(tzinfo=IST)).astimezone(IST).date()
    ref = str(head.get("Message-ID", "")).strip() or _ref(subject, sender, str(sent))
    headline = f"{subject} {sender}"
    site = domain.split(".")[-2] if domain.count(".") else domain  # billing@acme.io -> acme
    merchant = (
        known_merchant(headline, aggregators=False)
        or known_merchant(body[:2000], aggregators=False)
        or known_merchant(headline)
        or normalise(name or site)
    )
    if not merchant:
        return None
    text = f"{subject}. {body}"
    amount = pick_amount(text, email=True)
    common = dict(
        merchant=merchant,
        amount=amount[0] if amount else None,
        currency=amount[1] if amount else "INR",
        period=period_of(text),
        category=category_hint(text),
        signal=bool(_SIGNAL.search(text)),
        ref=ref,
    )
    if (
        _E_TRIAL.search(text)
        and not _E_TRIAL_PROMO.search(text)
        and not (amount and _E_CONVERTED.search(text))
    ):
        return Event(day, "email", "trial", due=due_date(text, day), **common)
    if (
        amount
        and merchant in ("claude", "chatgpt")
        and _E_TOPUP.search(text)
        and not _E_PLAN.search(text)
    ):
        expiry = (
            due_date(text, day) if re.search(r"expir|valid (?:until|till)", text, re.I) else None
        )
        return Event(day, "email", "topup", **{**common, "due": expiry})
    if amount and (
        _E_RECEIPT.search(subject) or (_E_RECEIPT.search(body) and not _E_PROMO.search(subject))
    ):
        return Event(day, "email", "charge", **common)
    if _E_USAGE.search(subject):
        return Event(day, "email", "usage", merchant, ref=ref)
    return None


def read_mbox(path: Path) -> Iterator[bytes]:
    box = mailbox.mbox(path, create=False)
    try:
        for key in box.iterkeys():
            yield box.get_bytes(key)
    finally:
        box.close()


def _all_mail_folder(conn) -> str:
    """Gmail localises '[Gmail]/All Mail'; find it by its \\All flag."""
    _, folders = conn.list()
    for line in folders or []:
        s = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
        if "\\All" in s:
            return s.rsplit(' "/" ', 1)[-1].strip()
    return '"[Gmail]/All Mail"'


def read_imap(
    user: str, password: str, since_uid: int = 0, since: date | None = None, conn=None
) -> tuple[list[Event], int, int]:
    """Read-only Gmail IMAP scan of messages newer than since_uid.
    Returns (events, messages read, highest UID seen)."""
    conn = conn or imaplib.IMAP4_SSL("imap.gmail.com")
    conn.login(user, password)
    try:
        conn.select(_all_mail_folder(conn), readonly=True)
        after = since or (datetime.now(IST).date() - timedelta(days=400))
        words = "receipt OR invoice OR payment OR trial OR subscription OR renewal OR membership"
        words += " OR login OR signin OR verification OR credits OR charged"
        query = f'"after:{after:%Y/%m/%d} ({words})"'
        _, data = conn.uid("SEARCH", None, "UID", f"{since_uid + 1}:*", "X-GM-RAW", query)
        uids = [int(u) for u in (data[0] or b"").split() if int(u) > since_uid]
        events, top = [], since_uid
        for uid in uids:
            _, parts = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")  # PEEK: never marks read
            raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
            if raw and (ev := classify_email(raw)):
                events.append(ev)
            top = max(top, uid)
        return events, len(uids), top
    finally:
        conn.logout()


# ---------------------------------------------------------------- files


def newest_backup(folder: Path) -> Path | None:
    files = [p for p in folder.iterdir() if p.suffix.lower() in (".xml", ".csv")]
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def scan_file(path: Path) -> tuple[list[Event], int]:
    """Events from one export file, and how many messages it held."""
    suffix = path.suffix.lower()
    events: list[Event] = []
    read = 0
    if suffix == ".mbox":
        for raw in read_mbox(path):
            read += 1
            if ev := classify_email(raw):
                events.append(ev)
        return events, read
    if suffix == ".xml":
        rows = read_sms_xml(path)
    elif suffix == ".csv":
        rows = read_sms_csv(path)
    else:
        raise ValueError(f"{path.name}: expected a .mbox, .xml or .csv export")
    for sender, when, body in rows:
        read += 1
        if when and (ev := classify_sms(sender, body, when)):
            events.append(ev)
    return events, read
