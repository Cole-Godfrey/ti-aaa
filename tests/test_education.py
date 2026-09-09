from datetime import date

from tiaaa.education import education_facts


def test_stale_sophomore_becomes_junior_without_mutating_saved_profile(profile):
    profile["education"].update(graduation_date="May 2028", current_year="sophomore")
    facts = education_facts(profile, today=date(2026, 9, 9))
    assert facts["education"]["current_year"] == "junior"
    assert profile["education"]["current_year"] == "sophomore"


def test_rollover_and_manual_standing(profile):
    profile["education"]["graduation_date"] = "2028-05"
    assert education_facts(profile, today=date(2026, 7, 31))["education"]["current_year"] == "sophomore"
    assert education_facts(profile, today=date(2026, 8, 1))["education"]["current_year"] == "junior"
    profile["education"]["current_year_mode"] = "manual"
    assert education_facts(profile, today=date(2026, 9, 9))["education"]["current_year"] == "sophomore"


def test_unknown_and_graduate_degrees_are_not_guessed(profile):
    for degree, graduation in [("Master of Science", "May 2028"), ("Bachelor of Science", "unknown")]:
        profile["education"].update(degree=degree, graduation_date=graduation)
        assert education_facts(profile, today=date(2026, 9, 9)) == profile


def test_review_signature_does_not_expire_every_day(profile):
    from tiaaa.review.reviewer import _profile_digest, _signature

    profile["education"]["graduation_date"] = "May 2028"
    digest = _profile_digest(profile)
    options = dict(resumes=[], listing_ids=[1], budget=2, model="codex", history_used=0)
    before = _signature(profile_digest=digest, **options)
    digest["education"]["current_year_as_of"] = "2026-09-10"
    assert _signature(profile_digest=digest, **options) == before
    digest["education"]["current_year"] = "senior"
    assert _signature(profile_digest=digest, **options) != before
