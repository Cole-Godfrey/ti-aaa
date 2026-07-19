"""Parsers for the Markdown and HTML tables used by internship repositories."""

from __future__ import annotations

import hashlib
import html
import re
from datetime import date, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from tiaaa.models import InternshipListing, SourceDocument

_FLAGS = ("🔒", "🛂", "🇺🇸", "🔥", "🎓")
_IGNORED_URL_HOSTS = {"i.imgur.com", "imgur.com"}
_TRACKING_KEYS = {
    "embed",
    "iis",
    "iisn",
    "jr_id",
    "lever-source",
    "ref",
    "referrer",
    "source",
    "src",
    "trackingid",
}


def clean_text(value: str) -> str:
    """Turn a table cell containing Markdown/HTML into compact plain text."""

    value = html.unescape(value or "")
    value = re.sub(r"<br\s*/?>", " / ", value, flags=re.IGNORECASE)
    value = re.sub(r"</?(?:details|summary)[^>]*>", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"!\[[^]]*]\([^)]*\)", "", value)
    value = re.sub(r"\[([^]]+)]\([^)]*\)", r"\1", value)
    value = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    value = value.replace("**", "").replace("__", "")
    return re.sub(r"\s+", " ", value).strip(" |")


def _without_flags(value: str) -> str:
    for flag in _FLAGS:
        value = value.replace(flag, "")
    return re.sub(r"\s+", " ", value).strip()


def _header_key(value: str) -> str:
    value = clean_text(value).casefold()
    value = re.sub(r"[^a-z0-9]+", "_", value).strip("_")
    aliases = {
        "application_link": "application",
        "application": "application",
        "apply": "application",
        "link": "application",
        "date_posted": "date",
        "added": "date",
        "age": "date",
        "position": "role",
        "title": "role",
    }
    return aliases.get(value, value)


def _split_markdown_row(line: str) -> list[str]:
    body = line.strip().strip("|")
    return [part.replace(r"\|", "|").strip() for part in re.split(r"(?<!\\)\|", body)]


def _is_separator_row(line: str) -> bool:
    cells = _split_markdown_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in cells)


def canonicalize_url(value: str) -> str:
    """Remove known tracking parameters while preserving job identifiers."""

    value = html.unescape(value or "").strip().strip("<>")
    if not value:
        return ""
    parts = urlsplit(value)
    if parts.scheme.casefold() not in {"http", "https"} or not parts.netloc:
        return ""

    query: list[tuple[str, str]] = []
    for key, item in parse_qsl(parts.query, keep_blank_values=False):
        lower = key.casefold()
        if lower.startswith("utm_") or lower in _TRACKING_KEYS:
            continue
        query.append((key, item))
    query.sort(key=lambda pair: (pair[0].casefold(), pair[1]))
    path = re.sub(r"/{2,}", "/", parts.path)
    if path != "/":
        path = path.rstrip("/")
    fragment = parts.fragment
    if fragment.casefold().strip("/") in {"apply", "description", "job-description", "top"}:
        fragment = ""
    return urlunsplit(
        (parts.scheme.casefold(), parts.netloc.casefold(), path, urlencode(query), fragment)
    )


def listing_fingerprint(company: str, role: str, location: str) -> str:
    """Stable fallback identity when two repositories use different direct URLs."""

    def normalize(value: str) -> str:
        value = _without_flags(clean_text(value)).casefold()
        value = value.replace("software engineering", "software engineer")
        value = value.replace("new york city", "new york")
        value = value.replace("san francisco", "sf")
        return re.sub(r"[^a-z0-9]+", " ", value).strip()

    identity = "\x1f".join((normalize(company), normalize(role), normalize(location)))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _extract_links(cell: str | Tag) -> list[tuple[str, int]]:
    raw = str(cell)
    soup = BeautifulSoup(raw, "html.parser")
    links: list[tuple[str, int]] = []
    for anchor in soup.find_all("a", href=True):
        href = html.unescape(anchor["href"]).strip()
        alt = " ".join(image.get("alt", "") for image in anchor.find_all("img"))
        label = f"{anchor.get_text(' ', strip=True)} {alt}".casefold()
        priority = 0 if "apply" in label else 10
        if "simplify" in label:
            priority += 20
        links.append((href, priority))

    for match in re.finditer(r"\[[^]]*]\((https?://[^)\s]+)\)", raw):
        links.append((html.unescape(match.group(1)), 5))
    for match in re.finditer(r"(?<![\"'=])(https?://[^\s<>|)]+)", raw):
        links.append((html.unescape(match.group(1)), 15))
    return links


def _pick_application_url(cell: str | Tag) -> str:
    candidates: list[tuple[int, int, str]] = []
    for index, (raw_url, priority) in enumerate(_extract_links(cell)):
        canonical = canonicalize_url(raw_url)
        if not canonical:
            continue
        host = urlsplit(canonical).netloc.removeprefix("www.")
        if host in _IGNORED_URL_HOSTS:
            continue
        if host == "simplify.jobs" and urlsplit(canonical).path.startswith("/p/"):
            priority += 30
        if host == "github.com":
            priority += 40
        candidates.append((priority, index, canonical))
    return min(candidates)[2] if candidates else ""


def _parse_posting_date(raw: str, today: date) -> str | None:
    value = _without_flags(clean_text(raw)).strip("- ")
    if not value:
        return None
    if match := re.fullmatch(r"(\d+)d", value.casefold()):
        return (today - timedelta(days=int(match.group(1)))).isoformat()
    if match := re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", value):
        try:
            return date.fromisoformat(match.group(0)).isoformat()
        except ValueError:
            return None
    for pattern in ("%b %d", "%B %d"):
        try:
            import datetime as _datetime

            parsed = _datetime.datetime.strptime(value, pattern)
            candidate = date(today.year, parsed.month, parsed.day)
            if candidate > today + timedelta(days=45):
                candidate = candidate.replace(year=today.year - 1)
            return candidate.isoformat()
        except ValueError:
            continue
    return None


def _category_name(value: str) -> str:
    value = _without_flags(clean_text(value))
    value = re.sub(r"\b(?:internship|internships|roles|the list)\b", "", value, flags=re.I)
    value = re.sub(r"\s+", " ", value).strip(" -#")
    return value or "Tech"


def _make_listing(
    cells: dict[str, str | Tag],
    source: SourceDocument,
    category: str,
    inherited_company: str,
    today: date,
) -> tuple[InternshipListing | None, str]:
    company_raw = str(cells.get("company", ""))
    role_raw = str(cells.get("role", ""))
    location_raw = str(cells.get("location", ""))
    application_raw = cells.get("application", "")
    date_raw = str(cells.get("date", ""))
    row_raw = " ".join((company_raw, role_raw, location_raw, str(application_raw), date_raw))

    company = _without_flags(clean_text(company_raw))
    if company in {"", "↳"}:
        company = inherited_company
    next_inherited = company or inherited_company
    role = _without_flags(clean_text(role_raw))
    location = _without_flags(clean_text(location_raw))
    application_url = _pick_application_url(application_raw)
    if not company or not role or not application_url:
        return None, next_inherited

    listing = InternshipListing(
        company=company,
        role=role,
        location=location,
        application_url=application_url,
        source_key=source.key,
        source_label=source.label,
        source_repo_url=source.repo_url,
        source_path=source.path,
        category=_category_name(category),
        posting_date=_parse_posting_date(date_raw, today),
        raw_date=_without_flags(clean_text(date_raw)),
        closed="🔒" in row_raw or "application closed" in row_raw.casefold(),
        no_sponsorship="🛂" in row_raw,
        citizenship_required="🇺🇸" in row_raw,
        metadata={"season": source.season},
    )
    return listing, next_inherited


def _table_is_internship(headers: list[str]) -> bool:
    keys = set(headers)
    return {"company", "role", "location", "application"}.issubset(keys)


def _parse_html_tables(text: str, source: SourceDocument, today: date) -> list[InternshipListing]:
    soup = BeautifulSoup(text, "html.parser")
    listings: list[InternshipListing] = []
    for table in soup.find_all("table"):
        header_nodes = table.find_all("th")
        headers = [_header_key(node.get_text(" ", strip=True)) for node in header_nodes]
        if not _table_is_internship(headers):
            continue
        heading = table.find_previous(["h1", "h2", "h3", "h4"])
        category = heading.get_text(" ", strip=True) if heading else "Tech"
        inherited_company = ""
        for row in table.find_all("tr"):
            values = row.find_all("td", recursive=False)
            if len(values) < len(headers):
                continue
            cells = {key: value for key, value in zip(headers, values, strict=False)}
            listing, inherited_company = _make_listing(
                cells, source, category, inherited_company, today
            )
            if listing:
                listings.append(listing)
    return listings


def _parse_markdown_tables(text: str, source: SourceDocument, today: date) -> list[InternshipListing]:
    lines = text.splitlines()
    listings: list[InternshipListing] = []
    heading = "Tech"
    index = 0
    while index < len(lines):
        line = lines[index]
        if match := re.match(r"^#{1,4}\s+(.+)$", line.strip()):
            heading = match.group(1)
        if (
            line.lstrip().startswith("|")
            and index + 1 < len(lines)
            and _is_separator_row(lines[index + 1])
        ):
            headers = [_header_key(cell) for cell in _split_markdown_row(line)]
            index += 2
            if not _table_is_internship(headers):
                while index < len(lines) and lines[index].lstrip().startswith("|"):
                    index += 1
                continue
            inherited_company = ""
            while index < len(lines) and lines[index].lstrip().startswith("|"):
                values = _split_markdown_row(lines[index])
                cells = {key: value for key, value in zip(headers, values, strict=False)}
                listing, inherited_company = _make_listing(
                    cells, source, heading, inherited_company, today
                )
                if listing:
                    listings.append(listing)
                index += 1
            continue
        index += 1
    return listings


def parse_document(
    text: str,
    source: SourceDocument,
    *,
    today: date | None = None,
) -> list[InternshipListing]:
    """Parse every active internship table in one repository document."""

    today = today or date.today()
    parsed = _parse_html_tables(text, source, today) + _parse_markdown_tables(text, source, today)
    unique: dict[str, InternshipListing] = {}
    for listing in parsed:
        key = canonicalize_url(listing.application_url)
        if key and key not in unique:
            unique[key] = listing
    return list(unique.values())
