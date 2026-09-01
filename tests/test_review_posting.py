from __future__ import annotations

import json

import httpx
import pytest

from tiaaa.review import posting
from tiaaa.review.posting import PostingDocument, fetch_posting, html_to_text

# Captured before the autouse fixture replaces it, so the guard itself stays testable.
host_is_public = posting._host_is_public


@pytest.fixture(autouse=True)
def allow_test_hosts(monkeypatch):
    """Treat the fake hosts used below as public so the SSRF guard lets them through."""

    monkeypatch.setattr(posting, "_host_is_public", lambda hostname: bool(hostname))


def transport(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_html_to_text_keeps_list_structure() -> None:
    text = html_to_text(
        "<div><h2>Requirements</h2><ul><li>Pursuing a BS</li>"
        "<li>Graduating in 2027</li></ul><script>ignored()</script></div>"
    )

    assert "Requirements" in text
    assert "- Pursuing a BS" in text
    assert "- Graduating in 2027" in text
    assert "ignored" not in text


def test_greenhouse_posting_is_read_from_the_board_api() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "boards-api.greenhouse.io"
        assert request.url.path == "/v1/boards/acme/jobs/4210"
        return httpx.Response(
            200,
            json={
                "title": "Software Engineer Intern",
                "content": "<p>You will build services.</p><ul><li>Rust</li></ul>",
            },
        )

    with transport(handler) as client:
        document = fetch_posting(
            "https://boards.greenhouse.io/acme/jobs/4210", client=client
        )

    assert document.status == "ok"
    assert document.source == "greenhouse"
    assert document.title == "Software Engineer Intern"
    assert "You will build services." in document.text
    assert "- Rust" in document.text


def test_workday_posting_uses_the_cxs_json_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == (
            "/wday/cxs/acme/External/job/Seattle/Software-Intern_JR-9"
        )
        return httpx.Response(
            200,
            json={
                "jobPostingInfo": {
                    "title": "Software Intern",
                    "jobDescription": "<p>Must graduate by June 2027.</p>",
                    "location": "Seattle, WA",
                    "jobRequisitionId": "JR-9",
                }
            },
        )

    with transport(handler) as client:
        document = fetch_posting(
            "https://acme.wd1.myworkdayjobs.com/en-US/External/job/Seattle/Software-Intern_JR-9",
            client=client,
        )

    assert document.status == "ok"
    assert document.source == "workday"
    assert "Must graduate by June 2027." in document.text
    assert "Seattle, WA" in document.text


def test_json_ld_is_preferred_over_raw_page_text() -> None:
    payload = {
        "@type": "JobPosting",
        "title": "Data Intern",
        "description": "<p>Open to undergraduates only.</p>",
    }
    markup = (
        "<html><head><title>Careers</title>"
        f'<script type="application/ld+json">{json.dumps(payload)}</script>'
        "</head><body><nav>Menu clutter</nav></body></html>"
    )

    with transport(lambda _r: httpx.Response(200, html=markup)) as client:
        document = fetch_posting("https://careers.acme.test/jobs/7", client=client)

    assert document.status == "ok"
    assert document.source == "json-ld"
    assert document.text == "Open to undergraduates only."


def test_closed_postings_are_reported_as_closed() -> None:
    markup = "<html><body><h1>Software Intern</h1><p>This job is no longer available.</p></body></html>"

    with transport(lambda _r: httpx.Response(200, html=markup)) as client:
        document = fetch_posting("https://careers.acme.test/jobs/8", client=client)

    assert document.status == "closed"


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [(403, "blocked"), (429, "blocked"), (404, "not_found"), (500, "error")],
)
def test_http_failures_map_to_readable_statuses(status_code, expected) -> None:
    with transport(lambda _r: httpx.Response(status_code, text="nope")) as client:
        document = fetch_posting("https://careers.acme.test/jobs/9", client=client)

    assert document.status == expected
    assert not document.usable


def test_private_addresses_are_never_fetched(monkeypatch) -> None:
    monkeypatch.setattr(posting, "_host_is_public", lambda hostname: False)

    document = fetch_posting("http://127.0.0.1:8787/admin")

    assert document.status == "error"
    assert "public web address" in document.detail


def test_posting_text_is_truncated_to_the_prompt_budget() -> None:
    markup = "<html><body><p>" + ("requirement " * 8000) + "</p></body></html>"

    with transport(lambda _r: httpx.Response(200, html=markup)) as client:
        document = fetch_posting("https://careers.acme.test/jobs/10", client=client)

    assert len(document.text) <= posting.MAX_POSTING_CHARS


def test_posting_document_usable_requires_text() -> None:
    assert PostingDocument("ok", text="Real requirements").usable
    assert not PostingDocument("ok", text="   ").usable
    assert not PostingDocument("blocked", text="Real requirements").usable


def test_lever_posting_merges_its_description_and_list_sections() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.lever.co"
        assert request.url.path == "/v0/postings/acme/abc-123"
        return httpx.Response(
            200,
            json={
                "text": "Software Engineering Intern",
                "descriptionPlain": "Join the platform team.",
                "lists": [
                    {
                        "text": "Requirements",
                        "content": "<li>Enrolled undergraduate</li><li>Available Summer 2027</li>",
                    }
                ],
                "additionalPlain": "We do not sponsor visas.",
            },
        )

    with transport(handler) as client:
        document = fetch_posting(
            "https://jobs.lever.co/acme/abc-123", client=client
        )

    assert document.status == "ok"
    assert document.source == "lever"
    assert document.title == "Software Engineering Intern"
    assert "Join the platform team." in document.text
    assert "- Enrolled undergraduate" in document.text
    assert "We do not sponsor visas." in document.text


def test_smartrecruiters_posting_is_assembled_from_its_ad_sections() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/companies/AcmeCorp/postings/7788"
        return httpx.Response(
            200,
            json={
                "name": "Data Intern",
                "jobAd": {
                    "sections": {
                        "jobDescription": {
                            "title": "Job Description",
                            "text": "<p>Analyze product metrics.</p>",
                        },
                        "qualifications": {
                            "title": "Qualifications",
                            "text": "<p>Graduating in 2027.</p>",
                        },
                    }
                },
            },
        )

    with transport(handler) as client:
        document = fetch_posting(
            "https://jobs.smartrecruiters.com/AcmeCorp/7788", client=client
        )

    assert document.status == "ok"
    assert document.source == "smartrecruiters"
    assert "Analyze product metrics." in document.text
    assert "Graduating in 2027." in document.text


def test_ashby_posting_is_matched_by_id_within_the_job_board() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/posting-api/job-board/acme"
        return httpx.Response(
            200,
            json={
                "jobs": [
                    {"id": "other", "title": "Wrong role", "descriptionHtml": "<p>Nope.</p>"},
                    {
                        "id": "wanted-id",
                        "title": "Infrastructure Intern",
                        "location": "Remote",
                        "employmentType": "Intern",
                        "descriptionHtml": "<p>You will run Kubernetes.</p>",
                    },
                ]
            },
        )

    with transport(handler) as client:
        document = fetch_posting(
            "https://jobs.ashbyhq.com/acme/wanted-id", client=client
        )

    assert document.status == "ok"
    assert document.source == "ashby"
    assert document.title == "Infrastructure Intern"
    assert "You will run Kubernetes." in document.text
    assert "Nope." not in document.text


def test_a_failing_ats_endpoint_falls_back_to_the_rendered_page() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.host == "boards-api.greenhouse.io":
            return httpx.Response(500, text="upstream error")
        return httpx.Response(
            200, html="<html><body><h1>Backend Intern</h1><p>Real requirements.</p></body></html>"
        )

    with transport(handler) as client:
        document = fetch_posting(
            "https://boards.greenhouse.io/acme/jobs/4210", client=client
        )

    assert len(calls) == 2, "the API is tried first, then the page itself"
    assert document.status == "ok"
    assert document.source == "html"
    assert "Real requirements." in document.text


def test_host_resolution_rejects_private_and_loopback_targets(monkeypatch) -> None:
    import socket

    def resolve(hostname, *_args, **_kwargs):
        table = {
            "public.test": "93.184.216.34",
            "internal.test": "10.0.0.5",
            "loopback.test": "127.0.0.1",
            "mixed.test": ("93.184.216.34", "192.168.1.9"),
        }
        addresses = table[hostname]
        addresses = (addresses,) if isinstance(addresses, str) else addresses
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (item, 0)) for item in addresses]

    monkeypatch.setattr(posting.socket, "getaddrinfo", resolve)

    assert host_is_public("public.test") is True
    assert host_is_public("internal.test") is False
    assert host_is_public("loopback.test") is False
    # One private answer is enough to refuse the whole hostname.
    assert host_is_public("mixed.test") is False
    assert host_is_public("localhost") is False
    assert host_is_public("service.internal") is False
    assert host_is_public("") is False


def test_a_redirect_to_a_private_address_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(
        posting,
        "_host_is_public",
        lambda hostname: hostname == "careers.acme.test",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "careers.acme.test":
            return httpx.Response(302, headers={"Location": "http://192.168.1.9/secrets"})
        return httpx.Response(200, html="<html><body>internal</body></html>")

    with transport(handler) as client:
        document = fetch_posting("https://careers.acme.test/jobs/11", client=client)

    assert document.status == "error"
    assert "non-public" in document.detail
