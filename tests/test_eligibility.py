from __future__ import annotations

import pytest

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
    sponsorship = evaluate_listing(listing(no_sponsorship=True), profile, settings)
    assert not sponsorship.eligible
    assert sponsorship.score <= 2

    profile["work_authorization"]["requires_sponsorship"] = False
    profile["work_authorization"]["us_citizen"] = False
    citizenship = evaluate_listing(listing(citizenship_required=True), profile, settings)
    assert not citizenship.eligible
    assert citizenship.score <= 2


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


@pytest.mark.parametrize(
    ("role", "reason"),
    [
        (
            "Machine Learning Intern (Master's/PhD)",
            "requires a master's or doctoral degree not present in the profile",
        ),
        (
            "Research Intern (PhD)",
            "requires a doctoral degree not present in the profile",
        ),
    ],
)
def test_advanced_degree_roles_are_hard_qualification_gates(
    profile, settings, role, reason
) -> None:
    result = evaluate_listing(listing(role=role), profile, settings)

    assert result.eligible is False
    assert result.reason == reason


def test_degree_alternatives_and_matching_advanced_degrees_remain_qualified(
    profile, settings
) -> None:
    assert evaluate_listing(
        listing(role="Software Intern (BS/MS)"), profile, settings
    ).eligible

    profile["education"]["degree"] = "Master of Science"
    assert evaluate_listing(
        listing(role="Machine Learning Intern (MS/PhD)"), profile, settings
    ).eligible


def test_source_advanced_degree_flag_is_a_hard_gate_without_a_title_hint(
    profile, settings
) -> None:
    flagged = listing(role="Research Scientist Intern", advanced_degree_required=True)

    result = evaluate_listing(flagged, profile, settings)

    assert result.eligible is False
    assert result.reason == (
        "source list marks this role advanced-degree only (master's, PhD, or MBA)"
    )
    assert result.score <= 2
    assert "advanced-degree only" in result.score_reasoning


def test_advanced_degree_flag_clears_for_matching_and_bachelor_friendly_roles(
    profile, settings
) -> None:
    profile["education"]["degree"] = "Master of Science"
    assert evaluate_listing(
        listing(role="Research Scientist Intern", advanced_degree_required=True),
        profile,
        settings,
    ).eligible

    profile["education"]["degree"] = "Bachelor of Science"
    # An explicit bachelor's option in the title is more specific than the
    # repository-wide flag, so the listing stays qualified.
    assert evaluate_listing(
        listing(role="Software Intern (BS/MS)", advanced_degree_required=True),
        profile,
        settings,
    ).eligible


@pytest.mark.parametrize(
    "role",
    [
        "Research Intern (Graduate Students)",
        "Product Management Intern - MBA",
        "Postdoctoral Research Intern",
        "Quantitative Intern - Advanced Degree",
    ],
)
def test_graduate_only_titles_are_detected_beyond_the_phd_keyword(
    profile, settings, role
) -> None:
    assert evaluate_listing(listing(role=role), profile, settings).eligible is False


@pytest.mark.parametrize(
    "role",
    ["New Grad Software Engineer Intern", "Graduate Program Intern", "Data Science Intern"],
)
def test_new_grad_and_general_titles_stay_qualified(profile, settings, role) -> None:
    assert evaluate_listing(listing(role=role), profile, settings).eligible is True


def test_hard_qualification_gates_pin_the_fit_score_low(profile, settings) -> None:
    qualified = evaluate_listing(listing(), profile, settings)
    blocked = evaluate_listing(listing(role="Research Intern (PhD)"), profile, settings)

    assert qualified.score >= 7
    assert blocked.score <= 2


def test_previous_company_intern_roles_require_recorded_experience(
    profile, settings
) -> None:
    restricted = listing(role="Software Intern — Previous Interns Only")

    result = evaluate_listing(restricted, profile, settings)
    assert result.eligible is False
    assert result.reason == "restricted to previous or returning interns at this company"

    profile["experience"] = {"previous_internship_companies": ["Acme Corporation"]}
    assert evaluate_listing(restricted, profile, settings).eligible


def test_preferred_previous_internship_experience_is_not_a_hard_gate(
    profile, settings
) -> None:
    result = evaluate_listing(
        listing(role="Software Intern — Previous internship experience preferred"),
        profile,
        settings,
    )

    assert result.eligible
