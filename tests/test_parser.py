from __future__ import annotations

from datetime import date

from tiaaa.config import SOURCE_DOCUMENTS
from tiaaa.discovery.parser import canonicalize_url, listing_fingerprint, parse_document


def test_markdown_table_flags_dates_and_inherited_company() -> None:
    markdown = "\n".join(
        [
            "## The list",
            "",
            "| Company | Role | Location | Apply | Added |",
            "| --- | --- | --- | --- | --- |",
            (
                "| Acme | Software Engineering Intern 🛂 | Remote | "
                "[apply](https://jobs.example.com/acme?utm_source=github) | 2026-07-16 |"
            ),
            (
                "| ↳ | Backend Intern 🇺🇸 | Seattle, WA | "
                "[apply](https://jobs.example.com/backend?ref=list) | Jul 09 |"
            ),
        ]
    )
    jobs = parse_document(markdown, SOURCE_DOCUMENTS[0], today=date(2026, 7, 18))

    assert len(jobs) == 2
    assert jobs[0].company == "Acme"
    assert jobs[0].no_sponsorship is True
    assert jobs[0].application_url == "https://jobs.example.com/acme"
    assert jobs[0].posting_date == "2026-07-16"
    assert jobs[1].company == "Acme"
    assert jobs[1].citizenship_required is True
    assert jobs[1].posting_date == "2026-07-09"


def test_html_table_prefers_direct_apply_link_over_simplify_tracking_link() -> None:
    document = """
<h2>Software Engineering Internship Roles</h2>
<table><thead><tr>
  <th>Company</th><th>Role</th><th>Location</th><th>Application</th><th>Age</th>
</tr></thead><tbody><tr>
  <td><strong><a href="https://simplify.jobs/c/Acme">Acme</a></strong></td>
  <td>Platform Engineer Intern 🎓</td><td>SF</td>
  <td><a
    href="https://jobs.ashbyhq.com/acme/abc/application?embed=true&utm_source=Simplify"
  ><img alt="Apply"></a>
      <a href="https://simplify.jobs/p/uuid?utm_source=GHList"><img alt="Simplify"></a></td>
  <td>2d</td>
</tr></tbody></table>
"""
    jobs = parse_document(document, SOURCE_DOCUMENTS[2], today=date(2026, 7, 18))

    assert len(jobs) == 1
    assert jobs[0].company == "Acme"
    assert jobs[0].role == "Platform Engineer Intern"
    assert jobs[0].category == "Software Engineering"
    assert jobs[0].application_url == "https://jobs.ashbyhq.com/acme/abc/application"
    assert jobs[0].posting_date == "2026-07-16"


def test_yearless_posting_date_cannot_sort_as_a_future_listing() -> None:
    document = """
| Company | Role | Location | Apply | Added |
| --- | --- | --- | --- | --- |
| Acme | Software Intern | Remote | [apply](https://jobs.test/1) | Aug 30 |
"""

    jobs = parse_document(document, SOURCE_DOCUMENTS[0], today=date(2026, 7, 25))

    assert jobs[0].posting_date == "2025-08-30"


def test_canonical_url_preserves_job_identifier_and_removes_campaign_parameters() -> None:
    value = (
        "HTTPS://Jobs.Example.com/open?gh_jid=123&utm_source=github"
        "&ref=Simplify&token=abc#apply"
    )
    assert canonicalize_url(value) == "https://jobs.example.com/open?gh_jid=123&token=abc"


def test_canonical_url_preserves_spa_job_route_and_rejects_non_web_links() -> None:
    assert canonicalize_url("https://careers.example.com/#/jobs/ABC-123") == (
        "https://careers.example.com/#/jobs/ABC-123"
    )
    assert canonicalize_url("mailto:recruiting@example.com") == ""


def test_canonical_url_rejects_local_network_and_credentialed_targets() -> None:
    assert canonicalize_url("http://localhost:8787/api/config") == ""
    assert canonicalize_url("http://127.0.0.1/private") == ""
    assert canonicalize_url("http://2130706433/private") == ""
    assert canonicalize_url("http://[::1]/private") == ""
    assert canonicalize_url("https://user:password@jobs.example.com/apply") == ""


def test_fingerprint_normalizes_common_title_and_location_variants() -> None:
    one = listing_fingerprint("Acme", "Software Engineering Intern", "San Francisco")
    two = listing_fingerprint("acme", "Software Engineer Intern", "SF")
    assert one == two
