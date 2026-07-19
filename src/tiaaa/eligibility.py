"""Internship-specific eligibility gates and transparent heuristic scoring."""

from __future__ import annotations

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


def evaluate_listing(
    listing: InternshipListing,
    profile: dict[str, Any],
    settings: dict[str, Any],
) -> Eligibility:
    """Reject hard mismatches, then score role/location preference alignment."""

    if listing.closed:
        return Eligibility(False, "listing marked closed", 0, "Closed upstream")

    authorization = profile.get("work_authorization", {})
    if listing.citizenship_required and not bool(authorization.get("us_citizen")):
        return Eligibility(False, "requires U.S. citizenship", 0, "Citizenship requirement mismatch")
    if listing.no_sponsorship and bool(authorization.get("requires_sponsorship")):
        return Eligibility(False, "does not offer sponsorship", 0, "Sponsorship requirement mismatch")

    filters = settings.get("filters", {})
    haystack = f"{listing.company} {listing.role} {listing.location} {listing.category}".casefold()
    excluded = _strings(filters.get("exclude_keywords"))
    if match := next((keyword for keyword in excluded if keyword in haystack), None):
        return Eligibility(False, f"excluded keyword: {match}", 0, "User-defined exclusion")

    included = _strings(filters.get("include_role_keywords"))
    if included and not any(keyword in haystack for keyword in included):
        return Eligibility(False, "role is outside configured keywords", 0, "No required role keyword")

    location = listing.location.casefold()
    if filters.get("remote_only") and "remote" not in location:
        return Eligibility(False, "remote-only filter", 0, "Listing is not marked remote")
    allowed_locations = _strings(filters.get("allowed_locations"))
    if allowed_locations and not any(item in location for item in allowed_locations):
        return Eligibility(False, "location is outside configured list", 0, "Location filter mismatch")

    preferences = profile.get("preferences", {})
    preferred_roles = _strings(preferences.get("roles"))
    preferred_locations = _strings(preferences.get("locations"))
    score = 5
    reasons = ["community-curated tech internship"]

    role_text = f"{listing.role} {listing.category}".casefold()
    role_matches = [item for item in preferred_roles if item in role_text]
    if role_matches:
        score += 3
        reasons.append(f"preferred role match ({role_matches[0]})")
    elif preferred_roles:
        score -= 1
        reasons.append("outside preferred role keywords")

    location_matches = [item for item in preferred_locations if item in location]
    if location_matches:
        score += 1
        reasons.append(f"preferred location match ({location_matches[0]})")
    elif "remote" in location:
        score += 1
        reasons.append("remote option")

    education = profile.get("education", {})
    degree_text = f"{education.get('degree', '')} {education.get('current_year', '')}".casefold()
    advanced_role = any(marker in role_text for marker in ("phd", "ph.d", "master's", "masters", "mba"))
    advanced_profile = any(marker in degree_text for marker in ("phd", "ph.d", "master", "mba", "graduate"))
    if advanced_role and not advanced_profile:
        score -= 3
        reasons.append("advanced-degree marker may not match profile")

    score = max(1, min(10, score))
    return Eligibility(True, "eligible", score, "; ".join(reasons))
