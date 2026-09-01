"""Cheap hard-requirement gates that decide what is worth a full posting review.

This module answers one question: is the candidate categorically ineligible on
facts already visible in the listing metadata? It deliberately does not rate how
good a match a role is — that judgment needs the employer's real posting and
lives in `tiaaa.review`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tiaaa.models import InternshipListing


@dataclass(frozen=True, slots=True)
class Eligibility:
    eligible: bool
    reason: str


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip().casefold() for item in value if str(item).strip()]


def _is_unrestricted(value: str) -> bool:
    normalized = " ".join(value.casefold().replace(".", " ").split())
    return normalized in {
        "all",
        "any",
        "anything",
        "anywhere",
        "no preference",
        "none",
    } or any(
        phrase in normalized
        for phrase in (
            "any location",
            "any role",
            "any term",
            "anything as long as",
            "open to any",
        )
    )


def _restrictions(value: Any) -> list[str]:
    values = _strings(value)
    return [] if any(_is_unrestricted(item) for item in values) else values


def _degree_markers(value: str) -> tuple[bool, bool, bool]:
    normalized = value.casefold().replace(".", "").replace("'", "").replace("’", "")
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    doctoral = bool(
        re.search(r"\b(?:phd|dphil|doctoral|doctorate|post ?doctoral)\b", normalized)
    )
    masters = bool(
        re.search(
            r"\b(?:masters?|ms|msc|meng|mba|mfe|"
            r"grad(?:uate)? students?|graduate degree|advanced degree)\b",
            normalized,
        )
    )
    bachelors = bool(
        re.search(
            r"\b(?:bachelors?|bs|bsc|ba|beng|undergrad|undergraduate)\b", normalized
        )
    )
    return doctoral, masters, bachelors


def _degree_level(value: str) -> int:
    doctoral, masters, bachelors = _degree_markers(value)
    return 3 if doctoral else 2 if masters else 1 if bachelors else 0


def _required_advanced_degree(value: str) -> int:
    doctoral, masters, bachelors = _degree_markers(value)
    if bachelors:
        return 0
    if doctoral and masters:
        return 2
    if doctoral:
        return 3
    return 2 if masters else 0


def _requires_previous_company_intern(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", value.casefold())
    return bool(
        re.search(
            r"\b(?:former|previous|prior|past|returning)\s+"
            r"(?:[a-z0-9]+\s+){0,3}interns?\b",
            normalized,
        )
        or re.search(r"\binterns?\s+only\b", normalized)
    )


def _has_previous_internship_at_company(
    profile: dict[str, Any], company: str
) -> bool:
    experience = profile.get("experience", {})
    if not isinstance(experience, dict):
        return False
    company_key = re.sub(r"[^a-z0-9]+", "", company.casefold())
    for item in _strings(experience.get("previous_internship_companies")):
        item_key = re.sub(r"[^a-z0-9]+", "", item)
        if min(len(company_key), len(item_key)) >= 4 and (
            company_key in item_key or item_key in company_key
        ):
            return True
    return False


def matches_preferences(
    listing: InternshipListing | Mapping[str, Any],
    profile: dict[str, Any],
) -> tuple[bool, str]:
    """Check optional auto-apply preferences without changing qualification fit."""

    preferences = profile.get("preferences", {})
    if not isinstance(preferences, dict):
        return True, "No application preferences configured"
    preferred_roles = _restrictions(preferences.get("roles"))
    preferred_locations = _restrictions(preferences.get("locations"))
    preferred_terms = _restrictions(preferences.get("terms"))

    def field(name: str) -> str:
        if isinstance(listing, Mapping):
            return str(listing.get(name) or "")
        return str(getattr(listing, name, "") or "")

    role_text = f"{field('role')} {field('category')}".casefold()
    location_text = field("location").casefold()
    term_text = (
        f"{field('role')} {field('posting_date')} {field('source_labels')}"
    ).casefold()
    mismatches: list[str] = []
    if preferred_roles and not any(item in role_text for item in preferred_roles):
        mismatches.append("role")
    if preferred_locations and not any(
        item in location_text for item in preferred_locations
    ):
        mismatches.append("location")
    if preferred_terms and not any(item in term_text for item in preferred_terms):
        mismatches.append("term")
    if mismatches:
        return False, f"Outside preferred {', '.join(mismatches)}"
    return True, "Matches enabled application preferences"


def evaluate_listing(
    listing: InternshipListing,
    profile: dict[str, Any],
    settings: dict[str, Any],
) -> Eligibility:
    """Apply the hard gates that make a full posting review pointless."""

    role_text = f"{listing.role} {listing.category}".casefold()
    education = profile.get("education", {})
    degree_text = (
        f"{education.get('degree', '')} {education.get('major', '')} "
        f"{education.get('current_year', '')}"
    ).casefold()

    title_degree_level = _required_advanced_degree(role_text)
    # The source lists carry a curated "advanced degree required (Master's, PhD,
    # MBA)" flag for roles whose requirement never reaches the job title. An
    # explicit bachelor's mention in the title still wins: it is more specific.
    flagged_degree_level = (
        2
        if listing.advanced_degree_required and not _degree_markers(role_text)[2]
        else 0
    )
    required_degree_level = max(title_degree_level, flagged_degree_level)
    degree_mismatch = bool(
        required_degree_level and _degree_level(degree_text) < required_degree_level
    )
    previous_intern_gap = _requires_previous_company_intern(
        role_text
    ) and not _has_previous_internship_at_company(profile, listing.company)
    authorization = profile.get("work_authorization", {})
    citizenship_gap = bool(
        listing.citizenship_required and not bool(authorization.get("us_citizen"))
    )
    sponsorship_gap = bool(
        listing.no_sponsorship and bool(authorization.get("requires_sponsorship"))
    )

    if listing.closed:
        return Eligibility(False, "listing marked closed")
    if degree_mismatch:
        if not title_degree_level:
            return Eligibility(
                False,
                "source list marks this role advanced-degree only "
                "(master's, PhD, or MBA)",
            )
        required = "doctoral" if required_degree_level == 3 else "master's or doctoral"
        return Eligibility(
            False, f"requires a {required} degree not present in the profile"
        )
    if previous_intern_gap:
        return Eligibility(
            False, "restricted to previous or returning interns at this company"
        )
    if citizenship_gap:
        return Eligibility(False, "requires U.S. citizenship")
    if sponsorship_gap:
        return Eligibility(False, "does not offer sponsorship")

    filters = settings.get("filters", {})
    haystack = (
        f"{listing.company} {listing.role} {listing.location} {listing.category}"
    ).casefold()
    excluded = _strings(filters.get("exclude_keywords"))
    if match := next((keyword for keyword in excluded if keyword in haystack), None):
        return Eligibility(False, f"excluded keyword: {match}")

    included = _restrictions(filters.get("include_role_keywords"))
    if included and not any(keyword in haystack for keyword in included):
        return Eligibility(False, "role is outside configured keywords")

    location = listing.location.casefold()
    if filters.get("remote_only") and "remote" not in location:
        return Eligibility(False, "remote-only filter")
    allowed_locations = _restrictions(filters.get("allowed_locations"))
    if allowed_locations and not any(item in location for item in allowed_locations):
        return Eligibility(False, "location is outside configured list")

    return Eligibility(True, "eligible")
