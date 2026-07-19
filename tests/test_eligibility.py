from __future__ import annotations

from tiaaa.config import SOURCE_DOCUMENTS
from tiaaa.eligibility import evaluate_listing
from tiaaa.models import InternshipListing


def listing(**overrides) -> InternshipListing:
    source = SOURCE_DOCUMENTS[0]
    values = {
        "company": "Acme",
        "role": "Software Engineer Intern",
        "location": "Remote",
        "application_url": "https://jobs.example.com/1",
        "source_key": source.key,
        "source_label": source.label,
        "source_repo_url": source.repo_url,
        "source_path": source.path,
    }
    values.update(overrides)
    return InternshipListing(**values)


def test_sponsorship_and_citizenship_are_hard_gates(profile, settings) -> None:
    profile["work_authorization"]["requires_sponsorship"] = True
    assert not evaluate_listing(listing(no_sponsorship=True), profile, settings).eligible

    profile["work_authorization"]["requires_sponsorship"] = False
    profile["work_authorization"]["us_citizen"] = False
    assert not evaluate_listing(listing(citizenship_required=True), profile, settings).eligible


def test_role_and_remote_preferences_produce_explainable_score(profile, settings) -> None:
    result = evaluate_listing(listing(), profile, settings)
    assert result.eligible
    assert result.score == 9
    assert "preferred role match" in result.score_reasoning
    assert "preferred location match (remote)" in result.score_reasoning


def test_explicit_filters_are_enforced(profile, settings) -> None:
    settings["filters"]["remote_only"] = True
    result = evaluate_listing(listing(location="Austin, TX"), profile, settings)
    assert not result.eligible
    assert result.reason == "remote-only filter"
