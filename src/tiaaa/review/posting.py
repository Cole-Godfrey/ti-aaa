"""Read one employer job posting so the reviewer decides on real requirements.

Discovery still never enumerates an employer's job catalog. This module opens
exactly the direct application link a configured GitHub list already published,
follows its redirects, and returns the description text.
"""

from __future__ import annotations

import html
import ipaddress
import json
import logging
import re
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

MAX_POSTING_CHARS = 24000
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_LOCAL_HOST_SUFFIXES = (".internal", ".lan", ".local", ".localhost")
_CLOSED_MARKERS = (
    "no longer accepting applications",
    "this job is no longer available",
    "this position has been filled",
    "this posting has closed",
    "this posting is closed",
    "job posting has expired",
    "no longer accepting new applications",
    "position is no longer open",
    "requisition is closed",
)
_BLOCKED_MARKERS = (
    "access denied",
    "attention required",
    "checking your browser",
    "enable javascript and cookies to continue",
    "just a moment",
    "please verify you are a human",
    "request unsuccessful",
    "you have been blocked",
)
_STRIPPED_TAGS = ("script", "style", "noscript", "svg", "template", "iframe")


@dataclass(frozen=True, slots=True)
class PostingDocument:
    """One employer posting, or the reason it could not be read."""

    status: str
    text: str = ""
    title: str = ""
    detail: str = ""
    source: str = "unknown"
    final_url: str = ""

    @property
    def usable(self) -> bool:
        return self.status == "ok" and bool(self.text.strip())


def _host_is_public(hostname: str) -> bool:
    """Reject local, private, and unroutable targets before any request."""

    hostname = (hostname or "").casefold().rstrip(".")
    if not hostname or hostname == "localhost" or hostname.endswith(_LOCAL_HOST_SUFFIXES):
        return False
    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    resolved = {info[4][0] for info in infos}
    if not resolved:
        return False
    for item in resolved:
        try:
            address = ipaddress.ip_address(item)
        except ValueError:
            return False
        if not address.is_global:
            return False
    return True


def _url_is_fetchable(url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme.casefold() not in {"http", "https"}:
        return False
    try:
        hostname = parts.hostname
    except ValueError:
        return False
    return _host_is_public(hostname or "")


def _collapse(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"[ \t ]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def html_to_text(markup: str) -> str:
    """Flatten posting markup into readable text without losing list structure."""

    soup = BeautifulSoup(markup or "", "html.parser")
    for tag in soup(list(_STRIPPED_TAGS)):
        tag.decompose()
    for tag in soup.find_all("li"):
        tag.insert_before("\n- ")
    for name in ("br", "p", "div", "tr", "h1", "h2", "h3", "h4", "h5", "h6"):
        for tag in soup.find_all(name):
            tag.insert_after("\n")
    return _collapse(soup.get_text(" "))


def _json_ld_posting(markup: str) -> tuple[str, str]:
    """Return (title, description) from a schema.org JobPosting block if present."""

    soup = BeautifulSoup(markup or "", "html.parser")
    for block in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = block.string or block.get_text() or ""
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = parsed if isinstance(parsed, list) else [parsed]
        for item in candidates:
            if not isinstance(item, dict):
                continue
            graph = item.get("@graph")
            if isinstance(graph, list):
                candidates.extend(entry for entry in graph if isinstance(entry, dict))
            types = item.get("@type")
            types = types if isinstance(types, list) else [types]
            if not any(str(value).casefold() == "jobposting" for value in types):
                continue
            description = item.get("description")
            if isinstance(description, str) and description.strip():
                return str(item.get("title") or ""), html_to_text(description)
    return "", ""


def _page_title(markup: str) -> str:
    soup = BeautifulSoup(markup or "", "html.parser")
    if soup.title and soup.title.string:
        return _collapse(str(soup.title.string))[:200]
    heading = soup.find(["h1", "h2"])
    return _collapse(heading.get_text(" "))[:200] if heading else ""


def _greenhouse_api(parts: Any) -> str | None:
    if not parts.hostname or "greenhouse.io" not in parts.hostname:
        return None
    segments = [item for item in parts.path.split("/") if item]
    if len(segments) >= 3 and segments[-2] == "jobs":
        board, job_id = segments[0], segments[-1]
        return f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}"
    return None


def _lever_api(parts: Any) -> str | None:
    if not parts.hostname or not parts.hostname.endswith("lever.co"):
        return None
    segments = [item for item in parts.path.split("/") if item]
    if len(segments) >= 2:
        return f"https://api.lever.co/v0/postings/{segments[0]}/{segments[1]}"
    return None


def _smartrecruiters_api(parts: Any) -> str | None:
    if not parts.hostname or "smartrecruiters.com" not in parts.hostname:
        return None
    segments = [item for item in parts.path.split("/") if item]
    if len(segments) >= 2:
        return f"https://api.smartrecruiters.com/v1/companies/{segments[0]}/postings/{segments[1]}"
    return None


def _workday_api(parts: Any) -> str | None:
    """Build the Workday CXS JSON endpoint that backs the rendered posting page."""

    hostname = parts.hostname or ""
    if "myworkdayjobs.com" not in hostname and "myworkdaysite.com" not in hostname:
        return None
    tenant = hostname.split(".")[0]
    segments = [item for item in parts.path.split("/") if item]
    if "job" not in segments:
        return None
    job_index = segments.index("job")
    before = segments[:job_index]
    if not before:
        return None
    # The path is [locale?, site, "job", ...]; the locale segment is optional.
    site = before[-1]
    tail = "/".join(segments[job_index:])
    return f"{parts.scheme}://{hostname}/wday/cxs/{tenant}/{site}/{tail}"


def _ashby_api(parts: Any) -> tuple[str, str] | None:
    if not parts.hostname or "ashbyhq.com" not in parts.hostname:
        return None
    segments = [item for item in parts.path.split("/") if item]
    if len(segments) >= 2:
        return (
            f"https://api.ashbyhq.com/posting-api/job-board/{segments[0]}?includeCompensation=true",
            segments[1],
        )
    return None


def _text_from_api_payload(payload: Any, source: str, posting_id: str = "") -> tuple[str, str]:
    """Pull (title, description) out of a known applicant-tracking JSON shape."""

    if not isinstance(payload, dict):
        return "", ""
    if source == "greenhouse":
        return str(payload.get("title") or ""), html_to_text(str(payload.get("content") or ""))
    if source == "lever":
        sections = [str(payload.get("descriptionPlain") or payload.get("description") or "")]
        for item in payload.get("lists") or []:
            if isinstance(item, dict):
                sections.append(f"{item.get('text', '')}\n{item.get('content', '')}")
        sections.append(str(payload.get("additionalPlain") or ""))
        return str(payload.get("text") or ""), html_to_text("\n\n".join(sections))
    if source == "smartrecruiters":
        blocks: list[str] = []
        advert = payload.get("jobAd")
        sections_map = advert.get("sections") if isinstance(advert, dict) else None
        if isinstance(sections_map, dict):
            for block in sections_map.values():
                if isinstance(block, dict):
                    blocks.append(f"{block.get('title', '')}\n{block.get('text', '')}")
        return str(payload.get("name") or ""), html_to_text("\n\n".join(blocks))
    if source == "workday":
        info = payload.get("jobPostingInfo")
        if isinstance(info, dict):
            extras = " ".join(
                str(info.get(key) or "")
                for key in ("location", "startDate", "postedOn", "timeType", "jobRequisitionId")
            )
            body = html_to_text(str(info.get("jobDescription") or ""))
            return str(info.get("title") or ""), _collapse(f"{extras}\n\n{body}")
        return "", ""
    if source == "ashby":
        for item in payload.get("jobs") or []:
            if not isinstance(item, dict):
                continue
            identifiers = {str(item.get("id") or ""), str(item.get("jobId") or "")}
            url = str(item.get("jobUrl") or "")
            if posting_id and posting_id not in identifiers and posting_id not in url:
                continue
            description = str(item.get("descriptionHtml") or item.get("descriptionPlain") or "")
            extras = " ".join(
                str(item.get(key) or "")
                for key in ("location", "employmentType", "department", "team")
            )
            return str(item.get("title") or ""), _collapse(f"{extras}\n\n{html_to_text(description)}")
    return "", ""


def _classify(text: str, title: str) -> str:
    haystack = f"{title}\n{text}".casefold()
    if any(marker in haystack for marker in _CLOSED_MARKERS):
        return "closed"
    if len(text.strip()) < 200 and any(marker in haystack for marker in _BLOCKED_MARKERS):
        return "blocked"
    return "ok"


def _get(client: httpx.Client, url: str, *, accept: str) -> httpx.Response | None:
    if not _url_is_fetchable(url):
        return None
    response = client.get(url, headers={"Accept": accept})
    if not _url_is_fetchable(str(response.url)):
        return None
    return response


def fetch_posting(
    url: str,
    *,
    client: httpx.Client | None = None,
    timeout: float = 25.0,
) -> PostingDocument:
    """Read one employer posting, preferring the site's own structured endpoint."""

    if not _url_is_fetchable(url):
        return PostingDocument("error", detail="Application link is not a public web address")

    owned = client is None
    client = client or httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    try:
        parts = urlsplit(url)
        api_attempts: list[tuple[str, str, str]] = []
        for source, builder in (
            ("greenhouse", _greenhouse_api),
            ("lever", _lever_api),
            ("smartrecruiters", _smartrecruiters_api),
            ("workday", _workday_api),
        ):
            if api_url := builder(parts):
                api_attempts.append((source, api_url, ""))
        if ashby := _ashby_api(parts):
            api_attempts.append(("ashby", ashby[0], ashby[1]))

        for source, api_url, posting_id in api_attempts:
            try:
                response = _get(client, api_url, accept="application/json")
                if response is None or response.status_code >= 400:
                    continue
                title, text = _text_from_api_payload(
                    response.json(), source, posting_id=posting_id
                )
            except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
                log.debug("Structured posting read failed for %s: %s", api_url, exc)
                continue
            if text.strip():
                return PostingDocument(
                    _classify(text, title),
                    text=text[:MAX_POSTING_CHARS],
                    title=title[:200],
                    source=source,
                    final_url=str(response.url),
                )

        try:
            response = _get(client, url, accept="text/html,application/xhtml+xml")
        except httpx.HTTPError as exc:
            return PostingDocument("error", detail=f"Could not open the posting: {exc}"[:300])
        if response is None:
            return PostingDocument("error", detail="Posting redirected to a non-public address")
        if response.status_code in {401, 403, 429}:
            return PostingDocument(
                "blocked",
                detail=f"Employer returned HTTP {response.status_code}",
                final_url=str(response.url),
            )
        if response.status_code == 404 or response.status_code == 410:
            return PostingDocument(
                "not_found",
                detail=f"Employer returned HTTP {response.status_code}",
                final_url=str(response.url),
            )
        if response.status_code >= 400:
            return PostingDocument(
                "error",
                detail=f"Employer returned HTTP {response.status_code}",
                final_url=str(response.url),
            )
        if len(response.content) > _MAX_RESPONSE_BYTES:
            return PostingDocument("error", detail="Posting page is unexpectedly large")

        markup = response.text
        title, text = _json_ld_posting(markup)
        source = "json-ld"
        if not text.strip():
            text = html_to_text(markup)
            source = "html"
        title = title or _page_title(markup)
        if not text.strip():
            return PostingDocument(
                "blocked",
                detail="Posting page returned no readable description",
                source=source,
                final_url=str(response.url),
            )
        return PostingDocument(
            _classify(text, title),
            text=text[:MAX_POSTING_CHARS],
            title=title[:200],
            source=source,
            final_url=str(response.url),
        )
    finally:
        if owned:
            client.close()
