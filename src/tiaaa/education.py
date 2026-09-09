"""Date-aware education facts; explicit manual standing remains available."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
from typing import Any


def education_facts(profile: dict[str, Any], *, today: date | None = None) -> dict[str, Any]:
    result = deepcopy(profile)
    education = result.get("education", {})
    today = today or date.today()
    if education.get("current_year_mode") == "manual":
        education.pop("current_year_as_of", None)
        education.pop("current_year_basis", None)
        return result
    degree = str(education.get("degree", "")).casefold().replace(".", "")
    if not ("bachelor" in degree or degree.strip() in {"bs", "ba", "bsc", "beng"}):
        return result
    graduation = str(education.get("graduation_date") or education.get("expected_graduation") or "")
    parsed = None
    for fmt in ("%B %Y", "%b %Y", "%Y-%m", "%Y-%m-%d", "%m/%Y"):
        try:
            parsed = datetime.strptime(graduation.strip(), fmt).date()
            break
        except ValueError:
            continue
    if parsed is None or (parsed.year, parsed.month) < (today.year, today.month):
        return result
    # Academic year rolls over in August. Fall graduates finish in that same academic year.
    academic_end = today.year + (today.month >= 8)
    graduating_end = parsed.year + (parsed.month >= 8)
    remaining = graduating_end - academic_end
    if 0 <= remaining <= 3:
        education["current_year"] = ("senior", "junior", "sophomore", "freshman")[remaining]
        education["current_year_as_of"] = today.isoformat()
        education["current_year_basis"] = "Graduation cohort; use manual mode for credit-based standing"
    return result
