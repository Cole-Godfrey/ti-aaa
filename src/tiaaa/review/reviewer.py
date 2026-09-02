"""Decide, company by company, which internships are worth an application.

One company is one Claude call. That is deliberate: the interesting judgment is
comparative — which two of Amazon's eleven open listings deserve the candidate's
two application slots — and that judgment is only possible when the model sees
the whole company at once.
"""

from __future__ import annotations

import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx

from tiaaa.config import AppPaths
from tiaaa.database import (
    company_application_history,
    get_connection,
    record_posting,
    record_review,
    record_review_error,
    reviewable_listings,
)
from tiaaa.resumes import import_legacy_resume
from tiaaa.review.client import (
    DEFAULT_MODEL,
    ReviewClient,
    ReviewUnavailable,
    get_review_client,
)
from tiaaa.review.decision import (
    SYSTEM_PROMPT,
    ApplyDecision,
    CompanyReview,
    build_review_prompt,
    decision_schema_for_resumes,
    parse_company_review,
)
from tiaaa.review.posting import fetch_posting

log = logging.getLogger(__name__)

DEFAULT_BUDGET = 2
DEFAULT_REFRESH_DAYS = 21
DEFAULT_MAX_COMPANIES = 12
DEFAULT_MAX_LISTING_AGE_DAYS = 2
_POSTING_FETCH_WORKERS = 6
_MAX_LISTINGS_PER_CALL = 14


def _company_key(company: str) -> str:
    return "".join(ch for ch in str(company or "").casefold() if ch.isalnum())


def _preserved_company_approvals(
    connection: Any, company: str, *, exclude_job_ids: set[int]
) -> list[str]:
    """Describe stored YES decisions that a pending-only retry must not reconsider."""

    rows = connection.execute(
        """
        SELECT id, company, role, location
        FROM jobs
        WHERE apply_decision = 'apply' AND applied_at IS NULL AND is_active = 1
          AND availability_status != 'closed'
          AND pipeline_status NOT IN ('applied', 'applying', 'manual_review', 'withdrawn', 'expired')
        ORDER BY reviewed_at DESC, id DESC
        """
    ).fetchall()
    company_key = _company_key(company)
    return [
        f"{row['role']} ({row['location'] or 'location not listed'}) — "
        "previously marked Apply, not yet started"
        for row in rows
        if int(row["id"]) not in exclude_job_ids
        and _company_key(str(row["company"])) == company_key
    ]


def _iso_date(value: Any) -> date | None:
    text = str(value or "")[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _age_days(listing: dict[str, Any], today: date) -> int | str:
    seen = _iso_date(listing.get("posting_date")) or _iso_date(listing.get("first_seen_at"))
    return (today - seen).days if seen else "unknown"


def _load_resumes(connection: Any, *, limit: int = 6) -> list[dict[str, Any]]:
    """Read every active resume, so the model chooses rather than a keyword score."""

    from tiaaa.database import list_resumes

    resumes: list[dict[str, Any]] = []
    for record in list_resumes(connection)[:limit]:
        try:
            text = Path(str(record["text_path"])).read_text(encoding="utf-8")
        except OSError:
            continue
        resumes.append(
            {
                "id": int(record["id"]),
                "name": str(record["name"]),
                "tags": list(record.get("tags") or []),
                "text": text,
            }
        )
    return resumes


def _profile_digest(profile: dict[str, Any]) -> dict[str, Any]:
    """Send the facts a hiring decision turns on, not the whole address book."""

    education = profile.get("education", {})
    authorization = profile.get("work_authorization", {})
    experience = profile.get("experience", {})
    preferences = profile.get("preferences", {})
    personal = profile.get("personal", {})
    return {
        "education": {
            key: education.get(key)
            for key in (
                "school",
                "degree",
                "major",
                "minor",
                "current_year",
                "graduation_date",
                "expected_graduation",
                "gpa",
            )
            if education.get(key)
        },
        "work_authorization": {
            key: authorization.get(key)
            for key in (
                "us_citizen",
                "requires_sponsorship",
                "work_authorized",
                "visa_status",
                "security_clearance",
            )
            if authorization.get(key) is not None
        },
        "experience": {
            key: experience.get(key)
            for key in ("previous_internship_companies", "years_of_experience")
            if experience.get(key)
        },
        "preferences": {
            key: preferences.get(key)
            for key in ("roles", "locations", "terms", "willing_to_relocate", "remote_only")
            if preferences.get(key) not in (None, "", [])
        },
        "location": personal.get("city") or personal.get("state") or "",
        "skills": profile.get("skills", {}),
    }


def _signature(
    *,
    profile_digest: dict[str, Any],
    resumes: list[dict[str, Any]],
    listing_ids: list[int],
    budget: int,
    model: str,
    history_used: int,
) -> str:
    """Fingerprint every input a decision depended on, so staleness is detectable."""

    payload = json.dumps(
        {
            "profile": profile_digest,
            "resumes": sorted(
                (item["id"], hashlib.sha256(item["text"].encode()).hexdigest()[:16])
                for item in resumes
            ),
            "listings": sorted(listing_ids),
            "budget": budget,
            "model": model,
            "history_used": history_used,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _needs_posting(listing: dict[str, Any], *, refresh_days: int, today: date) -> bool:
    if not (listing.get("posting_body") or "").strip():
        return True
    fetched = _iso_date(listing.get("posting_fetched_at"))
    return fetched is None or (today - fetched).days >= refresh_days


def _fetch_postings(
    connection: Any,
    listings: list[dict[str, Any]],
    *,
    refresh_days: int,
    today: date,
    timeout: float,
) -> None:
    """Read the employer pages that are missing or stale, in parallel."""

    pending = [
        listing
        for listing in listings
        if _needs_posting(listing, refresh_days=refresh_days, today=today)
    ]
    if not pending:
        return

    def read(listing: dict[str, Any]) -> tuple[dict[str, Any], Any]:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"Accept-Language": "en-US,en;q=0.9"},
        ) as client:
            return listing, fetch_posting(str(listing["application_url"]), client=client)

    workers = min(_POSTING_FETCH_WORKERS, len(pending))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for listing, document in pool.map(read, pending):
            record_posting(
                connection,
                int(listing["id"]),
                status=document.status,
                title=document.title,
                body=document.text,
                source=document.source,
                detail=document.detail,
                final_url=document.final_url,
            )
            listing["posting_status"] = document.status
            listing["posting_title"] = document.title
            listing["posting_body"] = document.text
            listing["posting_detail"] = document.detail


def _prompt_listings(listings: list[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    return [
        {
            "id": int(listing["id"]),
            "role": listing.get("role"),
            "location": listing.get("location"),
            "category": listing.get("category"),
            "application_url": listing.get("application_url"),
            "posting_date": listing.get("posting_date"),
            "first_seen_at": listing.get("first_seen_at"),
            "age_days": _age_days(listing, today),
            "no_sponsorship": bool(listing.get("no_sponsorship")),
            "citizenship_required": bool(listing.get("citizenship_required")),
            "advanced_degree_required": bool(listing.get("advanced_degree_required")),
            "posting": {
                "status": listing.get("posting_status") or "unknown",
                "text": listing.get("posting_body") or "",
                "detail": listing.get("posting_detail") or "",
            },
        }
        for listing in listings
    ]


def _chunk(listings: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [listings[index : index + size] for index in range(0, len(listings), size)]


def review_company(
    connection: Any,
    *,
    company: str,
    listings: list[dict[str, Any]],
    profile_digest: dict[str, Any],
    resumes: list[dict[str, Any]],
    client: ReviewClient,
    budget: int,
    model: str,
    today: date,
    preserved_approvals: list[str] | None = None,
) -> list[CompanyReview]:
    """Run the comparison for one company and persist every decision it returns."""

    history = company_application_history(connection, company)
    preserved_approvals = list(preserved_approvals or [])
    if preserved_approvals:
        history = {
            **history,
            "approved": len(preserved_approvals),
            "approved_entries": preserved_approvals,
        }
    resume_ids = {item["name"].casefold(): item["id"] for item in resumes}
    response_schema = decision_schema_for_resumes(
        [str(item["name"]) for item in resumes]
    )
    reviews: list[CompanyReview] = []
    spent = int(history.get("used", 0)) + len(preserved_approvals)
    chosen_this_run: list[str] = []

    for batch in _chunk(listings, _MAX_LISTINGS_PER_CALL):
        batch_history = dict(history)
        # A company with more roles than fit in one call is decided over several
        # batches. Slots taken by an earlier batch are already claimed, but they
        # are not submitted applications, so they are reported as their own thing.
        batch_history["in_progress"] = int(history.get("in_progress", 0)) + len(
            chosen_this_run
        )
        batch_history["in_progress_entries"] = [
            *(history.get("in_progress_entries") or []),
            *chosen_this_run,
        ]
        batch_history["used"] = spent
        prompt = build_review_prompt(
            company=company,
            listings=_prompt_listings(batch, today),
            profile=profile_digest,
            resumes=resumes,
            history=batch_history,
            budget=budget,
            today=today.isoformat(),
        )
        payload = client.decide(
            system=SYSTEM_PROMPT,
            prompt=prompt,
            schema=response_schema,
        )
        review = parse_company_review(
            payload,
            company=company,
            allowed_job_ids={int(item["id"]) for item in batch},
        )
        missing_resume = [
            decision.job_id
            for decision in review.decisions
            if decision.should_apply
            and (
                decision.resume_name.casefold() not in resume_ids
                or not decision.resume_reason.strip()
            )
        ]
        if missing_resume:
            raise ValueError(
                "Claude did not choose and explain an active resume for Apply job(s): "
                + ", ".join(str(job_id) for job_id in missing_resume)
            )
        by_id = {int(item["id"]): item for item in batch}
        signature_ids = [int(item["id"]) for item in listings]
        approved = 0
        for decision in review.decisions:
            listing = by_id.get(decision.job_id)
            if listing is None:
                continue
            # The model owns the ranking; this is the hard stop that keeps a
            # miscounted batch from blowing through the company budget.
            if decision.should_apply:
                if spent + approved >= budget:
                    decision.decision = "skip"
                    decision.headline = (
                        f"Your self-imposed limit of {budget} application(s) at {company} "
                        "is already allocated to closer matches"
                    )
                    decision.confidence = "high"
                else:
                    approved += 1
                    chosen_this_run.append(
                        f"{listing.get('role')} "
                        f"({listing.get('location') or 'location not listed'}) — "
                        "chosen in this review, not yet applied to"
                    )
            decision.posting_status = str(listing.get("posting_status") or "unknown")
            record_review(
                connection,
                decision.job_id,
                decision=decision.decision,
                confidence=decision.confidence,
                headline=decision.headline,
                rationale=decision.rationale_json(),
                signature=_signature(
                    profile_digest=profile_digest,
                    resumes=resumes,
                    listing_ids=signature_ids,
                    budget=budget,
                    model=model,
                    history_used=int(history.get("used", 0)) + len(preserved_approvals),
                ),
                model=model,
                resume_id=resume_ids.get(decision.resume_name.casefold()),
            )
        spent += approved
        reviews.append(review)
    return reviews


def review_jobs(
    *,
    paths: AppPaths,
    profile: dict[str, Any],
    settings: dict[str, Any],
    db_path: str | Path | None = None,
    target_job_id: int | None = None,
    first_seen_from: str | None = None,
    first_seen_before: str | None = None,
    force: bool = False,
    client: ReviewClient | None = None,
) -> dict[str, Any]:
    """Review every company whose stored decisions no longer match its inputs.

    A first-seen window is a recovery pass: it reviews only rows that still have
    no stored apply/skip decision. Existing YES decisions remain budget context,
    but they are never sent back to the model or overwritten.
    """

    scoped_retry = first_seen_from is not None or first_seen_before is not None
    if scoped_retry and (first_seen_from is None or first_seen_before is None):
        raise ValueError("Both first-seen bounds are required")
    if scoped_retry and target_job_id is not None:
        raise ValueError("A review cannot target both one job and a first-seen window")

    review_settings = settings.get("review", {}) or {}
    if not force and not review_settings.get("enabled", True):
        return {"reviewed": 0, "companies": 0, "apply": 0, "skip": 0, "errors": 0, "status": "disabled"}

    import_legacy_resume(paths=paths, db_path=db_path)
    connection = get_connection(db_path)
    resumes = _load_resumes(connection)
    if not resumes:
        return {
            "reviewed": 0,
            "companies": 0,
            "apply": 0,
            "skip": 0,
            "errors": 0,
            "status": "no_resumes",
        }

    budget = max(1, min(10, int(review_settings.get("max_applications_per_company", DEFAULT_BUDGET))))
    refresh_days = max(1, int(review_settings.get("refresh_after_days", DEFAULT_REFRESH_DAYS)))
    max_companies = max(1, int(review_settings.get("max_companies_per_cycle", DEFAULT_MAX_COMPANIES)))
    # 0 means no age limit; the default keeps a first sync from reviewing a
    # whole repository backlog that is already past applying to.
    max_age_days = max(
        0, int(review_settings.get("max_listing_age_days", DEFAULT_MAX_LISTING_AGE_DAYS))
    )
    model = str(review_settings.get("model") or DEFAULT_MODEL)
    fetch_enabled = bool(review_settings.get("fetch_postings", True))
    timeout = float(review_settings.get("posting_timeout_seconds", 25))

    # An explicit re-check is a deliberate request, so it reaches listings the
    # cheap pre-filters ruled out and listings past the age window; the reviewer
    # can then say why on the record.
    explicit = target_job_id is not None
    selected_count = 0
    if scoped_retry:
        selected = reviewable_listings(
            connection,
            max_age_days=0,
            first_seen_from=first_seen_from,
            first_seen_before=first_seen_before,
            undecided_only=True,
        )
        selected_count = len(selected)
        # Do not expand this list to already-decided peers. This action repairs
        # missing answers; it is intentionally different from Re-check listing.
        listings = selected
    else:
        listings = reviewable_listings(
            connection,
            target_job_id=target_job_id,
            include_ineligible=explicit,
            max_age_days=0 if explicit else max_age_days,
        )
    if explicit and listings:
        # Re-decide the whole company: one listing's verdict depends on its rivals.
        company = str(listings[0]["company"])
        listings = [
            row
            for row in reviewable_listings(connection, include_ineligible=True)
            if _company_key(str(row["company"])) == _company_key(company)
        ]
    if not listings:
        return {
            "reviewed": 0,
            "companies": 0,
            "apply": 0,
            "skip": 0,
            "errors": 0,
            "selected": selected_count,
            "status": "idle",
        }

    profile_digest = _profile_digest(profile)
    today = datetime.now(UTC).date()

    grouped: dict[str, list[dict[str, Any]]] = {}
    for listing in listings:
        grouped.setdefault(_company_key(str(listing["company"])), []).append(listing)

    stale: list[tuple[str, list[dict[str, Any]]]] = []
    for rows in grouped.values():
        company = str(rows[0]["company"])
        history_used = int(company_application_history(connection, company).get("used", 0))
        signature = _signature(
            profile_digest=profile_digest,
            resumes=resumes,
            listing_ids=[int(row["id"]) for row in rows],
            budget=budget,
            model=model,
            history_used=history_used,
        )
        needs_review = force or any(
            row["apply_decision"] is None
            or row["review_signature"] != signature
            or _iso_date(row["reviewed_at"]) is None
            or (today - (_iso_date(row["reviewed_at"]) or today)).days >= refresh_days
            for row in rows
        )
        if needs_review:
            stale.append((company, rows))

    if not stale:
        return {
            "reviewed": 0,
            "companies": 0,
            "apply": 0,
            "skip": 0,
            "errors": 0,
            "selected": selected_count,
            "status": "current",
        }

    # Companies whose listings are freshest first: those are the ones still worth applying to.
    stale.sort(
        key=lambda item: max(
            str(row.get("posting_date") or row.get("first_seen_at") or "") for row in item[1]
        ),
        reverse=True,
    )
    # The configured cap protects unattended cycles. A user-confirmed daily
    # retry promises to cover the whole selected day, so it is not truncated.
    if not scoped_retry:
        stale = stale[:max_companies]

    owned_client = client is None
    try:
        client = client or get_review_client(model=model, cwd=str(paths.root))
    except ReviewUnavailable as exc:
        log.warning("Apply/skip review is unavailable: %s", exc)
        return {
            "reviewed": 0,
            "companies": 0,
            "apply": 0,
            "skip": 0,
            "errors": 1,
            "selected": selected_count,
            "status": "unavailable",
            "detail": str(exc),
        }

    totals = {
        "reviewed": 0,
        "companies": 0,
        "apply": 0,
        "skip": 0,
        "errors": 0,
        "selected": selected_count,
    }
    try:
        for company, rows in stale:
            try:
                if fetch_enabled:
                    _fetch_postings(
                        connection,
                        rows,
                        refresh_days=refresh_days,
                        today=today,
                        timeout=timeout,
                    )
                else:
                    for row in rows:
                        row["posting_status"] = "skipped"
                open_rows = [
                    row for row in rows if str(row.get("posting_status") or "") != "closed"
                ]
                if not open_rows:
                    continue
                reviews = review_company(
                    connection,
                    company=company,
                    listings=open_rows,
                    profile_digest=profile_digest,
                    resumes=resumes,
                    client=client,
                    budget=budget,
                    model=model,
                    today=today,
                    preserved_approvals=(
                        _preserved_company_approvals(
                            connection,
                            company,
                            exclude_job_ids={int(row["id"]) for row in open_rows},
                        )
                        if scoped_retry
                        else None
                    ),
                )
            except Exception as exc:
                totals["errors"] += 1
                log.warning("Could not review %s: %s", company, exc)
                for row in rows:
                    record_review_error(connection, int(row["id"]), message=str(exc))
                continue
            totals["companies"] += 1
            for review in reviews:
                for decision in review.decisions:
                    totals["reviewed"] += 1
                    totals["apply" if decision.should_apply else "skip"] += 1
    finally:
        if owned_client:
            client.close()
    totals["status"] = "complete"
    return totals


__all__ = ["ApplyDecision", "CompanyReview", "review_company", "review_jobs"]
