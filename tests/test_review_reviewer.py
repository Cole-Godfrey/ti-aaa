from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from tiaaa.config import SOURCE_DOCUMENTS, AppPaths
from tiaaa.database import add_resume_record, get_connection, get_job, ingest_listings, init_db
from tiaaa.models import InternshipListing
from tiaaa.review import reviewer as reviewer_module
from tiaaa.review.decision import (
    DECISION_SCHEMA,
    build_review_prompt,
    parse_company_review,
)
from tiaaa.review.posting import PostingDocument
from tiaaa.review.reviewer import review_jobs


class RecordingClient:
    """Return canned decisions and remember every prompt it was given."""

    name = "recording"

    def __init__(self, responses: list[dict]) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []
        self.schemas: list[dict] = []
        self.closed = False

    def decide(self, *, system: str, prompt: str, schema: dict) -> dict:
        assert schema is not DECISION_SCHEMA
        assert schema["type"] == DECISION_SCHEMA["type"]
        assert "application strategist" in system
        self.prompts.append(prompt)
        self.schemas.append(schema)
        return self.responses.pop(0)

    def close(self) -> None:
        self.closed = True


def make_paths(tmp_path) -> AppPaths:
    paths = AppPaths(tmp_path)
    paths.resumes.mkdir(parents=True, exist_ok=True)
    paths.packets.mkdir(parents=True, exist_ok=True)
    paths.resume_text.write_text(
        "Avery Student. Built a Python service and a React dashboard.", encoding="utf-8"
    )
    paths.resume_pdf.write_bytes(b"%PDF-1.4")
    return paths


def seed(paths: AppPaths, profile: dict, settings: dict, listings: list[tuple[str, str, str]]):
    connection = init_db(paths.database)
    source = SOURCE_DOCUMENTS[0]
    ingest_listings(
        connection,
        source,
        [
            InternshipListing(
                company=company,
                role=role,
                location="Remote",
                application_url=url,
                source_key=source.key,
                source_label=source.label,
                source_repo_url=source.repo_url,
                source_path=source.path,
            )
            for company, role, url in listings
        ],
        profile=profile,
        settings=settings,
        include_existing=True,
    )
    return connection


@pytest.fixture(autouse=True)
def offline_postings(monkeypatch):
    """Every test supplies posting text explicitly; nothing reaches the network."""

    monkeypatch.setattr(
        reviewer_module,
        "fetch_posting",
        lambda url, client=None: PostingDocument(
            "ok",
            text=f"Posting body for {url}. Open to undergraduates graduating in 2027.",
            title="Internship",
            source="test",
        ),
    )


def decision(job_id: int, verdict: str, **overrides) -> dict:
    payload = {
        "job_id": job_id,
        "decision": verdict,
        "confidence": "high",
        "headline": f"Decision for {job_id}",
        "resume_name": "Default resume",
        "resume_reason": "Closest project overlap",
        "factors": [
            {"label": "Qualifications", "verdict": "positive", "detail": "Matches the posting"}
        ],
        "blockers": [],
    }
    payload.update(overrides)
    return payload


def test_review_stores_a_decision_its_reasoning_and_the_chosen_resume(
    tmp_path, profile, settings
) -> None:
    paths = make_paths(tmp_path)
    seed(paths, profile, settings, [("Acme", "Backend Intern", "https://jobs.test/acme-1")])
    client = RecordingClient(
        [
            {
                "company_summary": "One clear match at Acme.",
                "decisions": [decision(1, "apply")],
            }
        ]
    )

    result = review_jobs(
        paths=paths,
        profile=profile,
        settings=settings,
        db_path=paths.database,
        client=client,
    )

    assert result["apply"] == 1 and result["skip"] == 0 and result["errors"] == 0
    job = get_job(get_connection(paths.database), 1)
    assert job["apply_decision"] == "apply"
    assert job["apply_confidence"] == "high"
    assert job["apply_headline"] == "Decision for 1"
    assert job["posting_status"] == "ok"
    assert job["apply_resume_name"] == "Default resume"
    assert client.schemas[0]["properties"]["decisions"]["items"]["properties"][
        "resume_name"
    ]["enum"] == ["", "Default resume"]
    rationale = json.loads(job["apply_rationale"])
    assert rationale["factors"][0]["label"] == "Qualifications"
    assert rationale["company_summary"] == "One clear match at Acme."
    # The client is caller-owned here, so the reviewer must not close it.
    assert client.closed is False


def test_review_with_multiple_resumes_requires_and_records_an_exact_choice(
    tmp_path, profile, settings
) -> None:
    paths = make_paths(tmp_path)
    connection = seed(
        paths, profile, settings, [("Acme", "Backend Intern", "https://jobs.test/acme")]
    )
    general_text = paths.resumes / "general.txt"
    general_pdf = paths.resumes / "general.pdf"
    general_text.write_text("General software projects and coursework.", encoding="utf-8")
    general_pdf.write_bytes(b"%PDF-1.4")
    add_resume_record(
        connection,
        name="General resume",
        original_filename="general.pdf",
        pdf_path=str(general_pdf),
        text_path=str(general_text),
        tags=["general"],
    )
    backend_text = paths.resumes / "backend.txt"
    backend_pdf = paths.resumes / "backend.pdf"
    backend_text.write_text("Python APIs, SQL, and distributed services.", encoding="utf-8")
    backend_pdf.write_bytes(b"%PDF-1.4")
    add_resume_record(
        connection,
        name="Backend resume",
        original_filename="backend.pdf",
        pdf_path=str(backend_pdf),
        text_path=str(backend_text),
        tags=["backend"],
    )
    client = RecordingClient(
        [
            {
                "company_summary": "The backend resume is the stronger match.",
                "decisions": [
                    decision(
                        1,
                        "apply",
                        resume_name="Backend resume",
                        resume_reason="It demonstrates the required API and SQL work.",
                    )
                ],
            }
        ]
    )

    result = review_jobs(
        paths=paths,
        profile=profile,
        settings=settings,
        db_path=paths.database,
        client=client,
    )

    assert result["apply"] == 1 and result["errors"] == 0
    job = get_job(connection, 1)
    assert job["apply_resume_name"] == "Backend resume"
    assert "--- RESUME: Backend resume" in client.prompts[0]
    allowed = client.schemas[0]["properties"]["decisions"]["items"]["properties"][
        "resume_name"
    ]["enum"]
    assert set(allowed) == {"", "General resume", "Backend resume"}


def test_apply_review_without_an_active_resume_choice_is_not_saved(
    tmp_path, profile, settings
) -> None:
    paths = make_paths(tmp_path)
    connection = seed(
        paths, profile, settings, [("Acme", "Backend Intern", "https://jobs.test/acme")]
    )
    client = RecordingClient(
        [
            {
                "company_summary": "Missing the required resume choice.",
                "decisions": [decision(1, "apply", resume_name="", resume_reason="")],
            }
        ]
    )

    result = review_jobs(
        paths=paths,
        profile=profile,
        settings=settings,
        db_path=paths.database,
        client=client,
    )

    assert result["reviewed"] == 0 and result["errors"] == 1
    job = get_job(connection, 1)
    assert job["apply_decision"] is None
    assert "did not choose and explain an active resume" in job["review_error"]


def test_one_call_covers_a_whole_company_and_states_the_remaining_budget(
    tmp_path, profile, settings
) -> None:
    paths = make_paths(tmp_path)
    seed(
        paths,
        profile,
        settings,
        [
            ("Acme", "Backend Intern", "https://jobs.test/acme-1"),
            ("Acme", "ML Intern", "https://jobs.test/acme-2"),
            ("Acme", "IT Intern", "https://jobs.test/acme-3"),
        ],
    )
    client = RecordingClient(
        [
            {
                "company_summary": "Spent both slots on the engineering roles.",
                "decisions": [
                    decision(1, "apply"),
                    decision(2, "apply"),
                    decision(3, "skip"),
                ],
            }
        ]
    )

    result = review_jobs(
        paths=paths,
        profile=profile,
        settings=settings,
        db_path=paths.database,
        client=client,
    )

    assert len(client.prompts) == 1, "all of one company's roles belong in one comparison"
    prompt = client.prompts[0]
    assert "limits themselves to **2** application(s) per company" in prompt
    assert "Counting the 0 submitted and the 0 in progress, **2** slot(s) are\nfree." in prompt
    assert prompt.count("### LISTING job_id=") == 3
    assert result["companies"] == 1
    assert (result["apply"], result["skip"]) == (2, 1)


def test_the_company_budget_is_enforced_even_when_the_model_overspends(
    tmp_path, profile, settings
) -> None:
    paths = make_paths(tmp_path)
    seed(
        paths,
        profile,
        settings,
        [
            ("Acme", "Backend Intern", "https://jobs.test/acme-1"),
            ("Acme", "ML Intern", "https://jobs.test/acme-2"),
            ("Acme", "IT Intern", "https://jobs.test/acme-3"),
        ],
    )
    client = RecordingClient(
        [
            {
                "company_summary": "Everything looks good.",
                "decisions": [
                    decision(1, "apply"),
                    decision(2, "apply"),
                    decision(3, "apply"),
                ],
            }
        ]
    )

    result = review_jobs(
        paths=paths,
        profile=profile,
        settings=settings,
        db_path=paths.database,
        client=client,
    )

    assert (result["apply"], result["skip"]) == (2, 1)
    connection = get_connection(paths.database)
    overflow = get_job(connection, 3)
    assert overflow["apply_decision"] == "skip"
    assert "limit of 2 application(s)" in overflow["apply_headline"]


def test_applications_already_sent_shrink_the_remaining_budget(
    tmp_path, profile, settings
) -> None:
    paths = make_paths(tmp_path)
    connection = seed(
        paths,
        profile,
        settings,
        [
            ("Acme", "Backend Intern", "https://jobs.test/acme-1"),
            ("Acme", "ML Intern", "https://jobs.test/acme-2"),
        ],
    )
    connection.execute(
        """
        UPDATE jobs SET pipeline_status = 'applied',
                        applied_at = '2026-08-01T00:00:00+00:00'
        WHERE id = 1
        """
    )
    connection.commit()
    client = RecordingClient(
        [{"company_summary": "One slot left.", "decisions": [decision(2, "apply")]}]
    )

    review_jobs(
        paths=paths,
        profile=profile,
        settings=settings,
        db_path=paths.database,
        client=client,
    )

    prompt = client.prompts[0]
    assert "Counting the 1 submitted and the 0 in progress, **1** slot(s) are\nfree." in prompt
    history = prompt.split("## What has already happened at this company")[1]
    assert "Applications actually submitted to Acme: **1**" in history
    assert "Backend Intern" in history
    # The applied role is history, not a listing to decide on again.
    assert prompt.count("### LISTING job_id=") == 1


def test_a_second_company_review_is_skipped_while_its_inputs_are_unchanged(
    tmp_path, profile, settings
) -> None:
    paths = make_paths(tmp_path)
    seed(paths, profile, settings, [("Acme", "Backend Intern", "https://jobs.test/acme-1")])
    first = RecordingClient(
        [{"company_summary": "Reviewed.", "decisions": [decision(1, "apply")]}]
    )
    review_jobs(
        paths=paths, profile=profile, settings=settings, db_path=paths.database, client=first
    )

    second = RecordingClient([])
    result = review_jobs(
        paths=paths, profile=profile, settings=settings, db_path=paths.database, client=second
    )

    assert result["status"] == "current"
    assert second.prompts == []


def test_a_new_listing_at_a_company_re_opens_that_company_for_review(
    tmp_path, profile, settings
) -> None:
    paths = make_paths(tmp_path)
    seed(paths, profile, settings, [("Acme", "Backend Intern", "https://jobs.test/acme-1")])
    review_jobs(
        paths=paths,
        profile=profile,
        settings=settings,
        db_path=paths.database,
        client=RecordingClient(
            [{"company_summary": "Reviewed.", "decisions": [decision(1, "apply")]}]
        ),
    )

    seed(
        paths,
        profile,
        settings,
        [
            ("Acme", "Backend Intern", "https://jobs.test/acme-1"),
            ("Acme", "ML Intern", "https://jobs.test/acme-2"),
        ],
    )
    client = RecordingClient(
        [
            {
                "company_summary": "The new role is the better use of a slot.",
                "decisions": [decision(1, "skip"), decision(2, "apply")],
            }
        ]
    )
    result = review_jobs(
        paths=paths, profile=profile, settings=settings, db_path=paths.database, client=client
    )

    assert result["companies"] == 1
    assert client.prompts[0].count("### LISTING job_id=") == 2
    assert get_job(get_connection(paths.database), 1)["apply_decision"] == "skip"


def test_a_closed_posting_expires_the_listing_without_a_model_call(
    tmp_path, profile, settings, monkeypatch
) -> None:
    paths = make_paths(tmp_path)
    seed(paths, profile, settings, [("Acme", "Backend Intern", "https://jobs.test/acme-1")])
    monkeypatch.setattr(
        reviewer_module,
        "fetch_posting",
        lambda url, client=None: PostingDocument(
            "closed", detail="Employer says the role is filled"
        ),
    )
    client = RecordingClient([])

    result = review_jobs(
        paths=paths, profile=profile, settings=settings, db_path=paths.database, client=client
    )

    assert client.prompts == []
    assert result["reviewed"] == 0
    job = get_job(get_connection(paths.database), 1)
    assert job["posting_status"] == "closed"
    assert job["availability_status"] == "closed"
    assert job["pipeline_status"] == "expired"


def test_an_unreadable_posting_still_produces_a_decision_marked_low_confidence(
    tmp_path, profile, settings, monkeypatch
) -> None:
    paths = make_paths(tmp_path)
    seed(paths, profile, settings, [("Acme", "Backend Intern", "https://jobs.test/acme-1")])
    monkeypatch.setattr(
        reviewer_module,
        "fetch_posting",
        lambda url, client=None: PostingDocument("blocked", detail="HTTP 403"),
    )
    client = RecordingClient(
        [
            {
                "company_summary": "Judged from the list alone.",
                "decisions": [decision(1, "apply", confidence="low")],
            }
        ]
    )

    review_jobs(
        paths=paths, profile=profile, settings=settings, db_path=paths.database, client=client
    )

    assert "EMPLOYER POSTING TEXT: unavailable" in client.prompts[0]
    assert "lower your confidence" in client.prompts[0]
    job = get_job(get_connection(paths.database), 1)
    assert job["apply_confidence"] == "low"
    assert job["posting_status"] == "blocked"


def test_a_failed_company_is_recorded_without_inventing_a_decision(
    tmp_path, profile, settings
) -> None:
    paths = make_paths(tmp_path)
    seed(paths, profile, settings, [("Acme", "Backend Intern", "https://jobs.test/acme-1")])

    class BrokenClient(RecordingClient):
        def decide(self, **_kwargs):
            raise RuntimeError("model unavailable")

    result = review_jobs(
        paths=paths,
        profile=profile,
        settings=settings,
        db_path=paths.database,
        client=BrokenClient([]),
    )

    assert result["errors"] == 1 and result["reviewed"] == 0
    job = get_job(get_connection(paths.database), 1)
    assert job["apply_decision"] is None
    assert "model unavailable" in job["review_error"]


def test_daily_retry_only_reviews_missing_decisions_without_the_cycle_cap(
    tmp_path, profile, settings
) -> None:
    paths = make_paths(tmp_path)
    connection = seed(
        paths,
        profile,
        settings,
        [
            ("Acme", "Backend Intern", "https://jobs.test/acme-new"),
            ("Acme", "ML Intern", "https://jobs.test/acme-peer"),
            ("Yesterday Co", "Data Intern", "https://jobs.test/yesterday"),
            ("Beta", "Software Intern", "https://jobs.test/beta-new"),
        ],
    )
    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    posting_date = start.date().isoformat()
    connection.executemany(
        "UPDATE jobs SET first_seen_at = ?, posting_date = ? WHERE id = ?",
        [
            ((start + timedelta(hours=1)).isoformat(), posting_date, 1),
            ((start + timedelta(minutes=30)).isoformat(), posting_date, 2),
            ((start - timedelta(hours=2)).isoformat(), posting_date, 3),
            ((start + timedelta(hours=2)).isoformat(), posting_date, 4),
        ],
    )
    connection.execute(
        """
        UPDATE jobs SET apply_decision = 'apply', apply_headline = 'Keep this decision',
                        reviewed_at = ?, review_signature = 'existing-decision'
        WHERE id = 2
        """,
        ((start + timedelta(minutes=45)).isoformat(),),
    )
    connection.commit()
    settings["review"]["max_companies_per_cycle"] = 1
    client = RecordingClient(
        [
            {"company_summary": "Beta reviewed.", "decisions": [decision(4, "skip")]},
            {
                "company_summary": "Only the missing Acme role was reviewed.",
                "decisions": [decision(1, "apply")],
            },
        ]
    )

    result = review_jobs(
        paths=paths,
        profile=profile,
        settings=settings,
        db_path=paths.database,
        first_seen_from=start.isoformat(),
        first_seen_before=end.isoformat(),
        force=True,
        client=client,
    )

    assert result["selected"] == 2
    assert result["companies"] == 2, "an explicit daily retry is not truncated by the cycle cap"
    assert result["reviewed"] == 2
    assert len(client.prompts) == 2
    assert client.prompts[1].count("### LISTING job_id=") == 1
    assert "### LISTING job_id=2" not in "\n".join(client.prompts)
    assert "Roles previously marked Apply but not started: **1**" in client.prompts[1]
    assert (
        "Counting the 0 submitted, the 0 in progress, and the 1 previously approved, "
        "**1** slot(s) are\nfree."
    ) in client.prompts[1]
    assert get_job(connection, 2)["apply_headline"] == "Keep this decision", (
        "an existing YES/NO must never be overwritten by the daily recovery action"
    )
    assert get_job(connection, 3)["apply_decision"] is None


def test_review_can_be_turned_off(tmp_path, profile, settings) -> None:
    paths = make_paths(tmp_path)
    seed(paths, profile, settings, [("Acme", "Backend Intern", "https://jobs.test/acme-1")])
    settings["review"]["enabled"] = False
    client = RecordingClient([])

    result = review_jobs(
        paths=paths, profile=profile, settings=settings, db_path=paths.database, client=client
    )

    assert result["status"] == "disabled"
    assert client.prompts == []


def test_ineligible_listings_never_reach_the_model(tmp_path, profile, settings) -> None:
    paths = make_paths(tmp_path)
    seed(
        paths,
        profile,
        settings,
        [("Acme", "Research Intern (PhD)", "https://jobs.test/acme-phd")],
    )
    client = RecordingClient([])

    result = review_jobs(
        paths=paths, profile=profile, settings=settings, db_path=paths.database, client=client
    )

    assert result["status"] == "idle"
    assert client.prompts == []


def test_parse_rejects_ids_and_verdicts_the_model_invented() -> None:
    review = parse_company_review(
        {
            "company_summary": "x" * 900,
            "decisions": [
                {"job_id": 99, "decision": "apply", "confidence": "high", "headline": "ghost"},
                {"job_id": 1, "decision": "maybe", "confidence": "high", "headline": "bad"},
                {
                    "job_id": 2,
                    "decision": "apply",
                    "confidence": "certain",
                    "headline": "",
                    "factors": [{"label": "L", "verdict": "amazing", "detail": "d"}],
                    "blockers": ["", "real blocker"],
                },
                {"job_id": 2, "decision": "skip", "confidence": "low", "headline": "dupe"},
            ],
        },
        company="Acme",
        allowed_job_ids={1, 2},
    )

    assert [item.job_id for item in review.decisions] == [2]
    only = review.decisions[0]
    assert only.confidence == "low", "an unknown confidence is downgraded, not trusted"
    assert only.headline == "Worth an application"
    assert only.factors[0].verdict == "neutral"
    assert only.blockers == ["real blocker"]
    assert len(review.company_summary) <= 400


def test_prompt_names_every_factor_the_decision_must_weigh(profile) -> None:
    prompt = build_review_prompt(
        company="Acme",
        listings=[
            {
                "id": 1,
                "role": "Backend Intern",
                "location": "Seattle",
                "category": "Tech",
                "application_url": "https://jobs.test/1",
                "posting_date": "2026-08-01",
                "first_seen_at": "2026-08-01T00:00:00+00:00",
                "age_days": 15,
                "advanced_degree_required": True,
                "posting": {"status": "ok", "text": "Requires a master's degree."},
            }
        ],
        profile=profile,
        resumes=[{"name": "Backend", "tags": ["backend"], "text": "Python services."}],
        history={"used": 0, "entries": []},
        budget=2,
        today="2026-08-16",
    )

    for expected in (
        "Hard eligibility gates",
        "graduation-date window",
        "Whether the posting is still live",
        "Timing",
        "Term and graduation alignment",
        "Location and the candidate's stated preferences",
        "Duplicate listings",
        "Application cost",
        "Which resume to send",
    ):
        assert expected in prompt
    assert "Days since the list published it: 15" in prompt
    assert "advanced degree marked required by the source list" in prompt
    assert "Requires a master's degree." in prompt
    assert "--- RESUME: Backend (tags: backend) ---" in prompt


def test_an_explicit_recheck_reaches_a_listing_the_hard_gate_ruled_out(
    tmp_path, profile, settings
) -> None:
    paths = make_paths(tmp_path)
    seed(
        paths,
        profile,
        settings,
        [("Acme", "Research Intern (PhD)", "https://jobs.test/acme-phd")],
    )
    connection = get_connection(paths.database)
    assert get_job(connection, 1)["eligibility"] == "ineligible"
    client = RecordingClient(
        [
            {
                "company_summary": "Checked on request.",
                "decisions": [
                    decision(
                        1,
                        "skip",
                        headline="Posting states enrollment in a PhD program is required",
                        blockers=["Requires an enrolled PhD student"],
                    )
                ],
            }
        ]
    )

    result = review_jobs(
        paths=paths,
        profile=profile,
        settings=settings,
        db_path=paths.database,
        target_job_id=1,
        client=client,
    )

    assert result["reviewed"] == 1
    job = get_job(connection, 1)
    assert job["apply_decision"] == "skip"
    assert "PhD program" in job["apply_headline"]


def test_a_recheck_re_decides_the_whole_company_not_just_the_one_listing(
    tmp_path, profile, settings
) -> None:
    paths = make_paths(tmp_path)
    seed(
        paths,
        profile,
        settings,
        [
            ("Acme", "Backend Intern", "https://jobs.test/acme-1"),
            ("Acme", "ML Intern", "https://jobs.test/acme-2"),
            ("Beta", "Software Intern", "https://jobs.test/beta-1"),
        ],
    )
    client = RecordingClient(
        [
            {
                "company_summary": "Re-ranked Acme.",
                "decisions": [decision(1, "skip"), decision(2, "apply")],
            }
        ]
    )

    review_jobs(
        paths=paths,
        profile=profile,
        settings=settings,
        db_path=paths.database,
        target_job_id=1,
        client=client,
    )

    assert len(client.prompts) == 1
    prompt = client.prompts[0]
    assert prompt.count("### LISTING job_id=") == 2, "both Acme roles are compared"
    assert "Beta" not in prompt, "the other company is left alone"


def test_an_unsubmitted_attempt_holds_a_slot_without_being_called_an_application(
    tmp_path, profile, settings
) -> None:
    """The ByteDance case: one application filed, one form awaiting confirmation."""

    paths = make_paths(tmp_path)
    connection = seed(
        paths,
        profile,
        settings,
        [
            ("ByteDance", "Backend Intern", "https://jobs.test/bd-1"),
            ("ByteDance", "ML Intern", "https://jobs.test/bd-2"),
            ("ByteDance", "Infra Intern", "https://jobs.test/bd-3"),
        ],
    )
    connection.execute(
        """
        UPDATE jobs SET pipeline_status = 'applied',
                        applied_at = '2026-08-10T00:00:00+00:00'
        WHERE id = 1
        """
    )
    # Form filled by the agent, still waiting on the candidate's Submit click.
    connection.execute(
        "UPDATE jobs SET pipeline_status = 'manual_review', worker_id = 'worker-0' WHERE id = 2"
    )
    connection.commit()
    client = RecordingClient(
        [{"company_summary": "Nothing left to spend.", "decisions": [decision(3, "skip")]}]
    )

    review_jobs(
        paths=paths,
        profile=profile,
        settings=settings,
        db_path=paths.database,
        client=client,
    )

    prompt = client.prompts[0]
    history = prompt.split("## What has already happened at this company")[1]
    assert "Applications actually submitted to ByteDance: **1**" in history
    assert "Applications started but not yet submitted: **1**" in history
    assert "waiting for your Submit confirmation" in history
    assert "Counting the 1 submitted and the 1 in progress, **0** slot(s) are\nfree." in prompt
    # The model must not be able to read this as two filed applications.
    assert "submitted to ByteDance: **2**" not in prompt
    assert 'has **not** been sent' in prompt


def test_the_limit_is_never_presented_as_the_employers_own_policy(profile) -> None:
    prompt = build_review_prompt(
        company="ByteDance",
        listings=[],
        profile=profile,
        resumes=[],
        history={"submitted": 1, "in_progress": 0},
        budget=2,
        today="2026-08-16",
    )

    assert "the candidate's own rule, configured in this tool" in prompt
    assert "It is not an employer policy" in prompt
    assert "never invent a per-company application cap on the employer's behalf" in prompt


def test_a_handed_back_role_holds_no_slot(tmp_path, profile, settings) -> None:
    paths = make_paths(tmp_path)
    connection = seed(
        paths,
        profile,
        settings,
        [
            ("Acme", "Backend Intern", "https://jobs.test/acme-1"),
            ("Acme", "ML Intern", "https://jobs.test/acme-2"),
        ],
    )
    # The employer blocked the browser agent, so this is the candidate's to file.
    connection.execute(
        """
        UPDATE jobs SET pipeline_status = 'manual_review',
                        availability_status = 'manual_only',
                        apply_reason_code = 'access_blocked'
        WHERE id = 1
        """
    )
    connection.commit()
    client = RecordingClient(
        [{"company_summary": "Both slots free.", "decisions": [decision(2, "apply")]}]
    )

    review_jobs(
        paths=paths,
        profile=profile,
        settings=settings,
        db_path=paths.database,
        client=client,
    )

    assert "Counting the 0 submitted and the 0 in progress, **2** slot(s) are\nfree." in (
        client.prompts[0]
    )


def test_listings_older_than_the_window_are_never_reviewed(
    tmp_path, profile, settings
) -> None:
    paths = make_paths(tmp_path)
    connection = seed(
        paths,
        profile,
        settings,
        [
            ("Acme", "Fresh Intern", "https://jobs.test/fresh"),
            ("Backlog Co", "Stale Intern", "https://jobs.test/stale"),
        ],
    )
    connection.execute("UPDATE jobs SET posting_date = '2026-01-05' WHERE id = 2")
    connection.commit()
    client = RecordingClient(
        [{"company_summary": "Only the fresh one.", "decisions": [decision(1, "apply")]}]
    )

    result = review_jobs(
        paths=paths,
        profile=profile,
        settings=settings,
        db_path=paths.database,
        client=client,
    )

    assert len(client.prompts) == 1
    assert "Backlog Co" not in client.prompts[0]
    assert result["companies"] == 1
    assert get_job(connection, 2)["apply_decision"] is None
    assert get_job(connection, 2)["posting_status"] == "unknown", (
        "a listing outside the window costs no posting fetch either"
    )


def test_the_age_window_can_be_turned_off_for_a_backlog_pass(
    tmp_path, profile, settings
) -> None:
    paths = make_paths(tmp_path)
    connection = seed(
        paths, profile, settings, [("Backlog Co", "Stale Intern", "https://jobs.test/stale")]
    )
    connection.execute("UPDATE jobs SET posting_date = '2026-01-05'")
    connection.commit()
    settings["review"]["max_listing_age_days"] = 0
    client = RecordingClient(
        [{"company_summary": "Reviewed on request.", "decisions": [decision(1, "skip")]}]
    )

    result = review_jobs(
        paths=paths,
        profile=profile,
        settings=settings,
        db_path=paths.database,
        client=client,
    )

    assert result["reviewed"] == 1


def test_an_explicit_recheck_ignores_the_age_window(tmp_path, profile, settings) -> None:
    paths = make_paths(tmp_path)
    connection = seed(
        paths, profile, settings, [("Backlog Co", "Stale Intern", "https://jobs.test/stale")]
    )
    connection.execute("UPDATE jobs SET posting_date = '2026-01-05'")
    connection.commit()
    client = RecordingClient(
        [{"company_summary": "Checked on request.", "decisions": [decision(1, "apply")]}]
    )

    result = review_jobs(
        paths=paths,
        profile=profile,
        settings=settings,
        db_path=paths.database,
        target_job_id=1,
        client=client,
    )

    assert result["reviewed"] == 1
    assert get_job(connection, 1)["apply_decision"] == "apply"


def test_a_listing_with_no_posting_date_falls_back_to_when_it_was_first_seen(
    tmp_path, profile, settings
) -> None:
    paths = make_paths(tmp_path)
    connection = seed(
        paths, profile, settings, [("Acme", "Undated Intern", "https://jobs.test/undated")]
    )
    connection.execute(
        "UPDATE jobs SET posting_date = NULL, first_seen_at = '2026-01-05T00:00:00+00:00'"
    )
    connection.commit()
    client = RecordingClient([])

    result = review_jobs(
        paths=paths,
        profile=profile,
        settings=settings,
        db_path=paths.database,
        client=client,
    )

    assert result["status"] == "idle"
    assert client.prompts == []
