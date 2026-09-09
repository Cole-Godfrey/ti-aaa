from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime

import pytest

from tiaaa.apply.verification import EmailVerification, extract_code, sender_domains

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


def mail(
    *,
    sender="noreply@ashbyhq.com",
    recipient="candidate@example.com",
    sent=NOW,
    body="Your verification code is: A1B2C3D4",
    html=False,
):
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Date"] = format_datetime(sent)
    msg["Subject"] = "Verify your application"
    msg.set_content(body, subtype="html" if html else "plain")
    return msg.as_bytes()


def extract(raw):
    return extract_code(
        raw,
        recipient="candidate@example.com",
        domains={"ashbyhq.com"},
        since=NOW - timedelta(seconds=30),
        now=NOW + timedelta(seconds=1),
    )


def test_plain_and_html_codes_preserve_case():
    assert extract(mail()) == "A1B2C3D4"
    assert extract(mail(body="<p>Your verification code is: <b>a1b2c3d4</b></p>", html=True)) == "a1b2c3d4"
    assert extract(mail(body="123456 is your security code")) == "123456"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"sender": "attacker@notashbyhq.com"},
        {"sender": "attacker@ashbyhq.com.evil.test"},
        {"recipient": "someone-else@example.com"},
        {"sent": NOW - timedelta(minutes=2)},
        {"sent": NOW + timedelta(minutes=2)},
        {"body": "Verification code is 123456. Security code is 999999."},
        {"body": "Visit https://example.com/123456 to verify"},
        {"body": "Your verification code is invalid"},
    ],
)
def test_unrelated_stale_ambiguous_or_non_code_messages_are_rejected(kwargs):
    assert extract(mail(**kwargs)) is None


def test_portal_sender_scope_is_not_a_global_allowlist():
    assert sender_domains("https://jobs.ashbyhq.com/company", {}) == {"jobs.ashbyhq.com", "ashbyhq.com"}
    assert "ashbyhq.com" not in sender_domains("https://evil.test", {})
    assert sender_domains(
        "https://careers.example.com", {"sender_domains": {"careers.example.com": ["mailer.example.org"]}}
    ) == {"careers.example.com", "mailer.example.org"}


def test_poll_is_readonly_and_never_reuses_a_message(monkeypatch):
    from tiaaa.apply import verification

    calls = []

    class Inbox:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def login(self, *args):
            pass

        def select(self, folder, readonly):
            assert readonly is True
            return "OK", []

        def uid(self, command, *args):
            calls.append((command, args))
            return (
                ("OK", [b"7"]) if command == "search" else ("OK", [(b"header", mail(sent=datetime.now(UTC)))])
            )

    monkeypatch.setenv("TIAAA_EMAIL_APP_PASSWORD", "private")
    monkeypatch.setattr(verification.imaplib, "IMAP4_SSL", Inbox)
    reader = EmailVerification(
        settings={"enabled": True},
        recipient="candidate@example.com",
        application_url="https://jobs.ashbyhq.com/company",
    )
    assert reader.poll(since=datetime.now(UTC) - timedelta(seconds=2)) == "A1B2C3D4"
    assert reader.used == {b"7"}
    assert calls[1] == ("fetch", (b"7", "(BODY.PEEK[]<0.262145>)"))
    assert reader.poll(since=NOW, should_stop=lambda: True) is None


def test_unconfigured_mailbox_does_not_connect(monkeypatch):
    monkeypatch.delenv("TIAAA_EMAIL_APP_PASSWORD", raising=False)
    reader = EmailVerification(
        settings={"enabled": True},
        recipient="candidate@example.com",
        application_url="https://jobs.ashbyhq.com/company",
    )
    assert reader.poll(since=NOW) is None


def test_eight_character_alphabetic_codes():
    assert extract(mail(body="Verification code: ABCDEFGH")) == "ABCDEFGH"


def test_two_fresh_codes_from_same_portal_require_manual_input(monkeypatch):
    from tiaaa.apply import verification

    class Inbox:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def login(self, *args):
            pass

        def select(self, *args, **kwargs):
            return "OK", []

        def uid(self, command, *args):
            if command == "search":
                return "OK", [b"7 8"]
            code = "123456" if args[0] == b"7" else "999999"
            return "OK", [(b"header", mail(sent=datetime.now(UTC), body=f"Verification code: {code}"))]

    monkeypatch.setenv("TIAAA_EMAIL_APP_PASSWORD", "private")
    monkeypatch.setattr(verification.imaplib, "IMAP4_SSL", Inbox)
    reader = EmailVerification(
        settings={"enabled": True},
        recipient="candidate@example.com",
        application_url="https://jobs.ashbyhq.com/company",
    )
    assert reader.poll(since=datetime.now(UTC) - timedelta(seconds=2)) is None
    assert not reader.used
