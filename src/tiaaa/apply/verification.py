"""Read-only, narrowly scoped email verification retrieval. No inbox text reaches an LLM."""

from __future__ import annotations

import imaplib
import os
import re
import ssl
import time
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

_CODE = re.compile(
    r"(?:verification|security|one[ -]?time|confirmation|authentication|login|sign[ -]?in)"
    r"\s+(?:code|passcode)\s*(?:is\s*)?[:\-]?\s*([A-Z0-9]{4,8})\b",
    re.I,
)
_CODE_BEFORE = re.compile(
    r"\b([A-Z0-9]{4,8})\s+is your\s+(?:verification|security|one[ -]?time|confirmation)"
    r"\s+(?:code|passcode)\b",
    re.I,
)


def sender_domains(application_url: str, configured: dict[str, Any]) -> set[str]:
    """Only the portal's domain and explicit per-portal aliases can supply a code."""
    host = (urlsplit(application_url).hostname or "").casefold()
    domains = {host} if host else set()
    for domain in ("ashbyhq.com", "myworkday.com", "workday.com", "oraclecloud.com", "icims.com"):
        if host == domain or host.endswith("." + domain):
            domains.add(domain)
    aliases = configured.get("sender_domains", {}).get(host, [])
    if isinstance(aliases, list):
        domains.update(str(item).strip().casefold() for item in aliases if item)
    return domains


def extract_code(
    raw: bytes, *, recipient: str, domains: set[str], since: datetime, now: datetime | None = None
) -> str | None:
    if len(raw) > 262144:
        return None
    message = BytesParser(policy=policy.default).parsebytes(raw)
    senders = getaddresses(message.get_all("From", []))
    if len(senders) != 1:
        return None
    domain = senders[0][1].rpartition("@")[2].casefold()
    if not any(domain == allowed or domain.endswith("." + allowed) for allowed in domains):
        return None
    recipients = getaddresses(message.get_all("To", []) + message.get_all("Delivered-To", []))
    if recipient.casefold() not in {address.casefold() for _, address in recipients}:
        return None
    try:
        sent = parsedate_to_datetime(str(message.get("Date", "")))
        if sent.tzinfo is None:
            return None
        now = now or datetime.now(UTC)
        if sent < since or sent > now or (now - sent).total_seconds() > 600:
            return None
    except (ValueError, TypeError, OverflowError):
        return None
    chunks = [str(message.get("Subject", ""))]
    for part in message.walk():
        if part.get_content_disposition() == "attachment":
            continue
        if part.get_content_type() in {"text/plain", "text/html"}:
            content = part.get_content()
            if isinstance(content, str):
                if part.get_content_type() == "text/html":
                    content = BeautifulSoup(content, "html.parser").get_text(" ", strip=True)
                chunks.append(content)
    text = " ".join(chunks)
    codes = {
        match.group(1)
        for pattern in (_CODE, _CODE_BEFORE)
        for match in pattern.finditer(text)
        if (
            any(char.isdigit() for char in match.group(1))
            or (
                len(match.group(1)) == 8
                and match.group(1).isupper()
                and match.group(1) not in {"PASSWORD", "REQUIRED", "EXPIRED"}
            )
        )
    }
    return next(iter(codes)) if len(codes) == 1 else None


class EmailVerification:
    def __init__(self, *, settings: dict[str, Any], recipient: str, application_url: str) -> None:
        self.settings = settings
        self.recipient = recipient.strip().casefold()
        self.domains = sender_domains(application_url, settings)
        self.used: set[bytes] = set()

    @property
    def enabled(self) -> bool:
        return bool(
            self.settings.get("enabled")
            and os.environ.get("TIAAA_EMAIL_APP_PASSWORD")
            and self.recipient
            and self.domains
        )

    def poll(self, *, since: datetime, should_stop: Any = None) -> str | None:
        if not self.enabled:
            return None
        timeout = max(1, min(180, int(self.settings.get("timeout_seconds", 90))))
        deadline = time.monotonic() + timeout
        host = str(self.settings.get("imap_host", "imap.gmail.com"))
        username = str(self.settings.get("username") or self.recipient)
        try:
            with imaplib.IMAP4_SSL(host, 993, ssl_context=ssl.create_default_context(), timeout=10) as inbox:
                inbox.login(username, os.environ["TIAAA_EMAIL_APP_PASSWORD"])
                status, _ = inbox.select("INBOX", readonly=True)
                if status != "OK":
                    return None
                while time.monotonic() < deadline:
                    if should_stop is not None and should_stop():
                        return None
                    status, data = inbox.uid("search", None, "SINCE", since.strftime("%d-%b-%Y"))
                    if status != "OK":
                        return None
                    matches: list[tuple[bytes, str]] = []
                    for uid in reversed((data[0] or b"").split()[-30:]):
                        if uid in self.used:
                            continue
                        if time.monotonic() >= deadline or (should_stop is not None and should_stop()):
                            return None
                        status, parts = inbox.uid("fetch", uid, "(BODY.PEEK[]<0.262145>)")
                        if status != "OK":
                            continue
                        for part in parts:
                            if not isinstance(part, tuple) or not isinstance(part[1], bytes):
                                continue
                            code = extract_code(
                                part[1], recipient=self.recipient, domains=self.domains, since=since
                            )
                            if code:
                                matches.append((uid, code))
                    distinct = {code for _, code in matches}
                    if len(distinct) > 1:
                        return None
                    if len(distinct) == 1:
                        self.used.update(uid for uid, _ in matches)
                        return distinct.pop()
                    for _ in range(20):
                        if time.monotonic() >= deadline or (should_stop is not None and should_stop()):
                            return None
                        time.sleep(0.25)
        except (OSError, imaplib.IMAP4.error, ValueError):
            # Do not log errors containing credentials, email bodies, or codes.
            return None
        return None
