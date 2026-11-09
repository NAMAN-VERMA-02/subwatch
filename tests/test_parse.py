"""Parser fixtures. Every SMS and email here is synthetic, written in the style of real
Indian bank alerts and service receipts; account numbers and refs are made up."""

from datetime import date, datetime
from email.message import EmailMessage
from email.utils import format_datetime

import pytest

from subwatch.parse import (
    IST,
    Event,
    classify_email,
    classify_sms,
    find_amounts,
    find_dates,
    known_merchant,
    normalise,
    read_imap,
    scan_file,
)

NOV9 = datetime(2026, 11, 9, 10, 30, tzinfo=IST)


def sms(body, when=NOV9, sender="VM-HDFCBK"):
    return classify_sms(sender, body, when)


# ---------------------------------------------------------------- amounts and dates


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Rs.119.00 debited", (11900, "INR")),
        ("INR 1,499 spent", (149900, "INR")),
        ("₹ 649 charged", (64900, "INR")),
        ("Rs 1,02,345.50 bal", (10234550, "INR")),
        ("USD 20.00 spent", (2000, "USD")),
        ("Amount paid $20.00", (2000, "USD")),
        ("20.00 USD charged", (2000, "USD")),
    ],
)
def test_amounts(text, expected):
    assert find_amounts(text)[0][1:] == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("on 05Nov26 transfer", date(2026, 11, 5)),
        ("ends on 12 November, 2026.", date(2026, 11, 12)),
        ("ends on Nov 12, 2026", date(2026, 11, 12)),
        ("on 08-Nov-26 on NETFLIX", date(2026, 11, 8)),
        ("at X on 2026-11-08:10:22:33.", date(2026, 11, 8)),
        ("First debit on 14-11-2026.", date(2026, 11, 14)),
        ("ends tomorrow", date(2026, 11, 10)),
        ("ends in 3 days", date(2026, 11, 12)),
        ("renews on December 2", date(2026, 12, 2)),
    ],
)
def test_dates(text, expected):
    assert find_dates(text, date(2026, 11, 9))[0][1] == expected


def test_ambiguous_numeric_date_takes_the_near_future_reading():
    # 12/11 from a US sender on 9 Nov: 11 Dec (dd/mm) is near, 12 Nov (mm/dd) nearer;
    # only one reading falls 0-60 days ahead when the other is in the past.
    assert find_dates("ends 11/15/2026", date(2026, 11, 9))[0][1] == date(2026, 11, 15)
    assert find_dates("ends 05/11/2026", date(2026, 10, 30))[0][1] == date(2026, 11, 5)


def test_merchant_aliases():
    assert known_merchant("UPI/P2M/123/netflixupi.payu@icici") == "netflix"
    assert known_merchant("Your Google Play Order: YouTube Premium") == "youtube"
    assert known_merchant("Google Play order") == "google-play"
    assert known_merchant("JIOMART order") is None
    assert known_merchant("paid to JioHotstar") == "jiohotstar"
    assert normalise("PAYU*ACME CLOUD PVT LTD") == "acme cloud"
    assert normalise("acme.payu@hdfcbank") == "acme"
    assert normalise("ACH D- BSE LIMITED") == "bse"


# ---------------------------------------------------------------- bank SMS


@pytest.mark.parametrize(
    "body, merchant, amount, currency",
    [
        (  # HDFC UPI
            "Sent Rs.119.00\nFrom HDFC Bank A/C *1234\nTo SPOTIFY INDIA\nOn 05/11/26\n"
            "Ref 631234567890\nNot You?\nCall 18002586161/SMS BLOCK UPI to 7308080808",
            "spotify",
            11900,
            "INR",
        ),
        (  # HDFC card
            "Rs.649 spent on HDFC Bank Card x1234 at NETFLIX.COM on 2026-11-08:10:22:33. "
            "Not you? To block+reissue, Call 18002586161",
            "netflix",
            64900,
            "INR",
        ),
        (  # ICICI card
            "INR 649.00 spent using ICICI Bank Card XX1234 on 08-Nov-26 on NETFLIX. "
            "Avl Limit: INR 45,000.00. If not you, call 1800 2662",
            "netflix",
            64900,
            "INR",
        ),
        (  # SBI, unknown merchant
            "Dear Customer, Rs.2,450.00 debited from A/c XX1234 on 05Nov26 transfer to "
            "ACME CLOUD PVT LTD Ref No 123456789. If not done by you, call 1800111109 -SBI",
            "acme cloud",
            245000,
            "INR",
        ),
        (  # Axis UPI P2M
            "INR 299.00 debited\nA/c no. XX1234\n05-11-26, 10:22:33\n"
            "UPI/P2M/631234567890/JioHotstar\nNot you? SMS BLOCKUPI Cust ID to 919951860002",
            "jiohotstar",
            29900,
            "INR",
        ),
        (  # Kotak UPI to a VPA
            "Sent Rs.199.00 from Kotak Bank AC X1234 to fitbox@ybl on 05-11-26.UPI Ref "
            "631234567890. Not you, https://kotak.com/KBANKT/Fraud",
            "fitbox",
            19900,
            "INR",
        ),
        (  # international card charge
            "USD 20.00 spent on HDFC Bank Card x1234 at CLAUDE.AI SUBSCRIPTION on "
            "2026-11-03:09:10:11.",
            "claude",
            2000,
            "USD",
        ),
        (  # aggregator descriptor
            "Rs.130.00 spent on SBI Card XX1234 at GOOGLE PLAY on 05/11/26.",
            "google-play",
            13000,
            "INR",
        ),
    ],
)
def test_bank_charges(body, merchant, amount, currency):
    ev = sms(body)
    assert ev and ev.kind == "charge"
    assert (ev.merchant, ev.amount, ev.currency) == (merchant, amount, currency)
    assert ev.date == date(2026, 11, 9)


def test_sms_timestamp_is_converted_to_ist():
    late = datetime(2026, 11, 8, 19, 0, tzinfo=IST).astimezone(tz=None)
    utc_evening = datetime.fromtimestamp(late.timestamp(), tz=IST)
    assert sms("Rs.119 debited to SPOTIFY", when=utc_evening).date == date(2026, 11, 8)


def test_mandate_and_predebit():
    m = sms(
        "Your UPI-Mandate is successfully created for ZEE5 for Rs 99.00. Frequency: Monthly. "
        "First debit on 14-11-2026. -HDFC Bank"
    )
    assert (m.kind, m.merchant, m.amount, m.period, m.due) == (
        "mandate",
        "zee5",
        9900,
        "month",
        date(2026, 11, 14),
    )
    p = sms(
        "Pre-debit Notification: Rs 649.00 will be debited from your card XX1234 on "
        "08-12-2026 towards NETFLIX as per e-mandate. -ICICI Bank"
    )
    assert (p.kind, p.merchant, p.amount, p.due) == (
        "predebit",
        "netflix",
        64900,
        date(2026, 12, 8),
    )


def test_commitment_hints():
    emi = sms(
        "Rs 23,500.00 debited from A/c XX1234 on 05-11-26 towards ACH D- HDFC BANK LTD-HOMELOAN "
        "EMI. Avl Bal Rs 1,02,345.00"
    )
    assert emi.kind == "charge" and emi.category == "commitment" and emi.amount == 2350000
    rent = sms("Sent Rs.15000.00 from Kotak Bank AC X1234 to rameshkumar@okhdfcbank on 01-11-26.")
    assert rent.category == "commitment"


def test_login_otp_is_usage():
    ev = sms("482910 is your OTP to login to JioHotstar. Do not share it with anyone.")
    assert (ev.kind, ev.merchant) == ("usage", "jiohotstar")


@pytest.mark.parametrize(
    "body",
    [
        "123456 is OTP for txn of INR 2,499.00 at BOOKMYSHOW on HDFC Bank card ending 1234. "
        "Do not share OTP for security reasons",
        "Refund of Rs 649.00 from NETFLIX credited to your card XX1234",
        "Rs 50,000.00 credited to A/c XX1234 by NEFT SALARY",
        "Payment of Rs 12,345.00 received towards your HDFC Bank Credit Card XX1234. Thank you",
        "Rs 12,345.00 debited from A/c XX1234 to CRED Club on 05-11-26",
        "Rs 2,000.00 debited from A/c XX1234 for UPI Lite top-up",
        "Txn of Rs 649 at NETFLIX declined due to insufficient balance",
        "Get flat Rs 200 cashback on Swiggy! Offer valid till 30 Nov",
    ],
)
def test_sms_negatives(body):
    assert sms(body) is None


# ---------------------------------------------------------------- email


def mail(subject, body, sender, when=NOV9, labels="Inbox", html=False):
    m = EmailMessage()
    m["Subject"] = subject
    m["From"] = sender
    m["Date"] = format_datetime(when)
    m["Message-ID"] = f"<{abs(hash((subject, body)))}@test>"
    m["X-Gmail-Labels"] = labels
    m.set_content(body, subtype="html" if html else "plain")
    return classify_email(bytes(m))


def test_receipt_email():
    ev = mail(
        "Your Spotify Premium receipt",
        "Thanks for your payment. Premium Individual ₹119.00 / month. Total ₹119.00",
        "Spotify <no-reply@spotify.com>",
    )
    assert (ev.kind, ev.merchant, ev.amount, ev.period) == ("charge", "spotify", 11900, "month")


def test_html_receipt_email():
    ev = mail(
        "Your Spotify Premium receipt",
        "<html><body><p>Premium Individual</p><td>Total</td><td>&#8377;119.00</td></body></html>",
        "Spotify <no-reply@spotify.com>",
        html=True,
    )
    assert (ev.kind, ev.amount) == ("charge", 11900)


def test_google_play_receipt_names_the_real_service():
    ev = mail(
        "Your Google Play Order Receipt from Nov 5, 2026",
        "Order number GPA.3312-1234. YouTube Premium (YouTube) ₹129.00/month. Total: ₹129.00",
        "Google Play <googleplay-noreply@google.com>",
    )
    assert (ev.kind, ev.merchant, ev.amount) == ("charge", "youtube", 12900)


def test_claude_receipt_in_usd_and_api_topup():
    pro = mail(
        "Your receipt from Anthropic, PBC #2045-1234",
        "Receipt. Claude Pro, monthly. Amount paid $20.00",
        "Anthropic, PBC <invoice+statements@mail.anthropic.com>",
    )
    assert (pro.kind, pro.merchant, pro.amount, pro.currency) == ("charge", "claude", 2000, "USD")
    topup = mail(
        "Your receipt from Anthropic, PBC #2045-5678",
        "API credits purchase. Amount paid $50.00. Credits expire on November 3, 2027.",
        "Anthropic, PBC <invoice+statements@mail.anthropic.com>",
    )
    assert (topup.kind, topup.amount, topup.due) == ("topup", 5000, date(2027, 11, 3))


def test_trial_email():
    ev = mail(
        "Your free trial ends soon",
        "Your free trial ends on 12 November 2026. After that, you'll be charged ₹399/month.",
        "SonyLIV <noreply@sonyliv.com>",
    )
    assert (ev.kind, ev.merchant, ev.due, ev.amount, ev.period) == (
        "trial",
        "sonyliv",
        date(2026, 11, 12),
        39900,
        "month",
    )


def test_usage_emails():
    claude = mail(
        "Secure link to log in to Claude.ai", "Click to log in", "Claude <a@anthropic.com>"
    )
    assert (claude.kind, claude.merchant) == ("usage", "claude")
    netflix = mail(
        "New sign-in to your account", "We noticed a new sign-in", "Netflix <info@netflix.com>"
    )
    assert (netflix.kind, netflix.merchant) == ("usage", "netflix")


@pytest.mark.parametrize(
    "subject, body, sender, labels",
    [
        (
            "Start your free trial of Apple TV+",
            "Try it free for 7 days, then ₹99/month. Your trial starts today.",
            "Apple <news@insideapple.apple.com>",
            "Inbox",
        ),
        (
            "Get YouTube Premium for just ₹129/month",
            "Ad-free subscription with auto renewal.",
            "YouTube <noreply@youtube.com>",
            "Inbox",
        ),
        (
            "Your Netflix receipt",
            "Total ₹649.00",
            "Netflix <info@netflix.com>",
            "Spam",
        ),
        ("Weekly digest", "Top stories this week", "News <digest@news.example>", "Inbox"),
    ],
)
def test_email_negatives(subject, body, sender, labels):
    assert mail(subject, body, sender, labels=labels) is None


# ---------------------------------------------------------------- files and IMAP


def test_sms_xml_and_csv(tmp_path):
    xml = tmp_path / "sms-20261109.xml"
    xml.write_text(
        '<?xml version="1.0" encoding="UTF-8"?><smses count="3">'
        '<sms address="VM-HDFCBK" date="1762664400000" type="1" '
        'body="Rs.119.00 debited from A/c XX1234 to SPOTIFY on 09-11-25" />'
        '<sms address="VM-HDFCBK" date="1762664400000" type="2" body="sent by me Rs 5 paid" />'
        '<sms address="AD-ZEE5IN" date="1762664400000" type="1" body="hello" /></smses>',
        encoding="utf-8",
    )
    events, read = scan_file(xml)
    assert read == 2 and [e.merchant for e in events] == ["spotify"]

    csv_file = tmp_path / "messages.csv"
    csv_file.write_text(
        "Date,Sender,Message\n09/11/2026 10:30,VM-HDFCBK,Rs.649 spent on card at NETFLIX\n",
        encoding="utf-8",
    )
    events, read = scan_file(csv_file)
    assert read == 1 and events[0].merchant == "netflix" and events[0].date == date(2026, 11, 9)


def test_csv_without_message_column_is_rejected(tmp_path):
    bad = tmp_path / "x.csv"
    bad.write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="message column"):
        scan_file(bad)


def test_mbox(tmp_path):
    import mailbox

    box = mailbox.mbox(tmp_path / "All mail.mbox")
    m = EmailMessage()
    m["Subject"], m["From"], m["Date"] = (
        "Your Spotify Premium receipt",
        "Spotify <no-reply@spotify.com>",
        format_datetime(NOV9),
    )
    m.set_content("Total ₹119.00")
    box.add(m)
    box.flush()
    box.close()
    events, read = scan_file(tmp_path / "All mail.mbox")
    assert read == 1 and events[0].kind == "charge"


class FakeImap:
    def __init__(self, messages):
        self.messages = messages
        self.calls = []

    def login(self, user, password):
        self.calls.append("login")

    def list(self):
        return "OK", [b'(\\HasNoChildren \\All) "/" "[Gmail]/All Mail"']

    def select(self, folder, readonly):
        assert readonly is True
        self.calls.append(folder)

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            return "OK", [b" ".join(str(u).encode() for u in self.messages)]
        return "OK", [(b"1 (BODY[] {n}", self.messages[int(args[0])]), b")"]

    def logout(self):
        self.calls.append("logout")


def test_imap_reads_only_new_messages():
    m = EmailMessage()
    m["Subject"], m["From"], m["Date"] = (
        "Your Spotify Premium receipt",
        "Spotify <no-reply@spotify.com>",
        format_datetime(NOV9),
    )
    m.set_content("Total ₹119.00")
    fake = FakeImap({7: bytes(m), 9: bytes(m)})
    events, read, top = read_imap("me@gmail.com", "app-pass", since_uid=7, conn=fake)
    assert (read, top, len(events)) == (1, 9, 1)
    assert fake.calls == ["login", '"[Gmail]/All Mail"', "logout"]


def test_event_json_roundtrip():
    ev = Event(date(2026, 11, 9), "sms", "trial", "zee5", 9900, due=date(2026, 11, 14))
    assert Event.from_json(ev.to_json()) == ev
