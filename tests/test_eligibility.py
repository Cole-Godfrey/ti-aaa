from __future__ import annotations

from tiaaa.config import SOURCE_DOCUMENTS
from tiaaa.eligibility import evaluate_listing, matches_preferences
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


def test_qualification_score_does_not_use_role_or_location_preferences(
    profile, settings
) -> None:
    result = evaluate_listing(listing(), profile, settings)
    profile["preferences"] = {
        "roles": ["hardware"],
        "locations": ["tokyo"],
        "terms": ["winter 2030"],
    }
    changed_preferences = evaluate_listing(listing(), profile, settings)

    assert result.eligible
    assert result.score == 8
    assert changed_preferences.score == result.score
    assert "relevant field of study" in result.score_reasoning
    assert "preferred" not in result.score_reasoning


def test_preferences_are_an_optional_application_gate(profile) -> None:
    assert matches_preferences(listing(), profile)[0] is True

    profile["preferences"]["roles"] = ["hardware"]
    matches, reason = matches_preferences(listing(), profile)

    assert matches is False
    assert reason == "Outside preferred role"


def test_open_ended_preferences_do_not_restrict_auto_apply(profile) -> None:
    profile["preferences"] = {
        "roles": ["Anything as long as I'm qualified."],
        "locations": ["Seattle", "Also open to really any location."],
        "terms": ["Any"],
    }

    assert matches_preferences(listing(location="Austin, TX"), profile)[0] is True


def test_explicit_filters_are_enforced(profile, settings) -> None:
    qualification_score = evaluate_listing(listing(), profile, settings).score
    settings["filters"]["remote_only"] = True
    result = evaluate_listing(listing(location="Austin, TX"), profile, settings)
    assert not result.eligible
    assert result.reason == "remote-only filter"
    assert result.score == qualification_score


def test_any_keyword_means_no_strict_filter(profile, settings) -> None:
    settings["filters"]["include_role_keywords"] = ["Any"]
    settings["filters"]["allowed_locations"] = ["Anywhere"]

    assert evaluate_listing(listing(location="Austin, TX"), profile, settings).eligible
