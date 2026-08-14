"""Internship-specific eligibility gates and transparent heuristic scoring."""

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
    score: int
    score_reasoning: str


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


def _profile_skill_terms(profile: dict[str, Any]) -> set[str]:
    skills = profile.get("skills", {})
    if not isinstance(skills, dict):
        return set()
    return {
        token
        for values in skills.values()
        if isinstance(values, list)
        for item in values
        for token in str(item).strip().casefold().replace("/", " ").split()
        if token
    }


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
    """Score qualifications independently, then apply hard eligibility rules."""

    role_text = f"{listing.role} {listing.category}".casefold()
    education = profile.get("education", {})
    degree_text = (
        f"{education.get('degree', '')} {education.get('major', '')} "
        f"{education.get('current_year', '')}"
    ).casefold()
    skills = _profile_skill_terms(profile)
    score = 4
    reasons = ["community-curated tech internship"]

    relevant_study = any(
        marker in degree_text
        for marker in (
            "computer",
            "software",
            "data",
            "information",
            "electrical",
            "mathematics",
            "statistics",
            "engineering",
        )
    )
    if relevant_study:
        score += 2
        reasons.append("relevant field of study")
    elif degree_text.strip():
        reasons.append("field of study is not clearly technical")

    if skills:
        score += 1
        reasons.append("documented technical skills")

    role_skill_groups = (
        (
            ("machine learning", "artificial intelligence", " ai ", "data science"),
            {"python", "r", "sql", "pytorch", "tensorflow", "pandas", "numpy"},
            "machine-learning/data skills",
        ),
        (
            ("frontend", "front end", "web", "ui engineer"),
            {"javascript", "typescript", "react", "html", "css", "vue", "angular"},
            "frontend skills",
        ),
        (
            ("embedded", "firmware", "hardware", "fpga", "silicon"),
            {"c", "c++", "cpp", "rust", "verilog", "vhdl", "embedded"},
            "embedded/hardware skills",
        ),
        (
            ("security", "cyber", "privacy"),
            {"security", "cybersecurity", "network", "linux", "cryptography"},
            "security skills",
        ),
        (
            ("quant", "trading", "research"),
            {"python", "c++", "cpp", "r", "matlab", "statistics", "math"},
            "quantitative skills",
        ),
    )
    specialized = False
    for markers, expected, label in role_skill_groups:
        if any(marker in role_text for marker in markers):
            specialized = True
            if skills & expected:
                score += 2
                reasons.append(label)
            else:
                score -= 1
                reasons.append(f"no documented {label}")
            break
    if (
        not specialized
        and skills
        and any(
            marker in role_text
            for marker in ("software", "developer", "backend", "full stack", "platform")
        )
    ):
        score += 1
        reasons.append("programming background for a general software role")

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
    profile_degree_level = _degree_level(degree_text)
    degree_mismatch = bool(
        required_degree_level and profile_degree_level < required_degree_level
    )
    previous_intern_required = _requires_previous_company_intern(role_text)
    previous_intern_match = _has_previous_internship_at_company(
        profile, listing.company
    )
    previous_intern_gap = previous_intern_required and not previous_intern_match
    authorization = profile.get("work_authorization", {})
    citizenship_gap = bool(
        listing.citizenship_required and not bool(authorization.get("us_citizen"))
    )
    sponsorship_gap = bool(
        listing.no_sponsorship and bool(authorization.get("requires_sponsorship"))
    )

    if degree_mismatch:
        reasons.append(
            "advanced-degree requirement does not match the profile"
            if title_degree_level
            else "source list marks the role advanced-degree only"
        )
    if previous_intern_required:
        reasons.append(
            "role is restricted to previous company interns"
            if previous_intern_gap
            else "previous internship at this company is recorded"
        )
    if citizenship_gap:
        reasons.append("role requires U.S. citizenship")
    if sponsorship_gap:
        reasons.append("employer does not sponsor the required work authorization")

    score = max(1, min(10, score))
    if degree_mismatch or previous_intern_gap or citizenship_gap or sponsorship_gap:
        # A hard qualification gate is not a partial deduction. The candidate
        # cannot hold the role at all, so the fit column must read that way
        # instead of showing a mid-range score next to "Not qualified".
        score = min(score, 2)
    score_reasoning = "; ".join(reasons)

    if listing.closed:
        return Eligibility(False, "listing marked closed", score, score_reasoning)
    if degree_mismatch:
        if not title_degree_level:
            return Eligibility(
                False,
                "source list marks this role advanced-degree only "
                "(master's, PhD, or MBA)",
                score,
                score_reasoning,
            )
        required = "doctoral" if required_degree_level == 3 else "master's or doctoral"
        return Eligibility(
            False,
            f"requires a {required} degree not present in the profile",
            score,
            score_reasoning,
        )
    if previous_intern_gap:
        return Eligibility(
            False,
            "restricted to previous or returning interns at this company",
            score,
            score_reasoning,
        )
    if citizenship_gap:
        return Eligibility(False, "requires U.S. citizenship", score, score_reasoning)
    if sponsorship_gap:
        return Eligibility(False, "does not offer sponsorship", score, score_reasoning)

    filters = settings.get("filters", {})
    haystack = f"{listing.company} {listing.role} {listing.location} {listing.category}".casefold()
    excluded = _strings(filters.get("exclude_keywords"))
    if match := next((keyword for keyword in excluded if keyword in haystack), None):
        return Eligibility(False, f"excluded keyword: {match}", score, score_reasoning)

    included = _restrictions(filters.get("include_role_keywords"))
    if included and not any(keyword in haystack for keyword in included):
        return Eligibility(False, "role is outside configured keywords", score, score_reasoning)

    location = listing.location.casefold()
    if filters.get("remote_only") and "remote" not in location:
        return Eligibility(False, "remote-only filter", score, score_reasoning)
    allowed_locations = _restrictions(filters.get("allowed_locations"))
    if allowed_locations and not any(item in location for item in allowed_locations):
        return Eligibility(False, "location is outside configured list", score, score_reasoning)

    return Eligibility(True, "eligible", score, score_reasoning)
