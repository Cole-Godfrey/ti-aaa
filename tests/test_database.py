from __future__ import annotations

from tiaaa.config import SOURCE_DOCUMENTS
from tiaaa.database import (
    add_resume_record,
    answer_agent_inputs,
    answered_agent_inputs,
    claim_next_job,
    claimable_application_count,
    close_connection,
    get_analytics,
    get_stats,
    ingest_listings,
    init_db,
    list_agent_inputs,
    list_jobs,
    list_notifications,
    mark_apply_result,
    mark_prepared,
    reconcile_source_registry,
    request_manual_application,
    store_agent_inputs,
    update_tracker,
)
from tiaaa.models import InternshipListing, SourceDocument


def make_listing(source, company: str, role: str, url: str, location: str = "Remote"):
    return InternshipListing(
        company=company,
        role=role,
        location=location,
        application_url=url,
        source_key=source.key,
        source_label=source.label,
        source_repo_url=source.repo_url,
        source_path=source.path,
    )


def test_auto_apply_new_is_off_by_default_and_only_queues_future_additions(
    tmp_path, profile, settings
) -> None:
    path = tmp_path / "tracker.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    first = make_listing(source, "Acme", "Software Engineer Intern", "https://jobs.test/acme")
    second = make_listing(source, "Beta", "Backend Intern", "https://jobs.test/beta")
    third = make_listing(source, "Gamma", "Data Intern", "https://jobs.test/gamma")

    baseline = ingest_listings(
        connection, source, [first], profile=profile, settings=settings
    )
    update = ingest_listings(
        connection, source, [first, second], profile=profile, settings=settings
    )

    assert baseline["baseline"] is True
    assert baseline["queued"] == 0
    assert update["baseline"] is False
    assert update["queued"] == 0
    rows = {row["company"]: row for row in list_jobs(connection)}
    assert rows["Acme"]["pipeline_status"] == "discovered"
    assert rows["Beta"]["pipeline_status"] == "discovered"

    settings["automation"]["auto_apply_new"] = True
    enabled_update = ingest_listings(
        connection, source, [first, second, third], profile=profile, settings=settings
    )
    rows = {row["company"]: row for row in list_jobs(connection)}
    assert enabled_update["queued"] == 1
    assert rows["Beta"]["pipeline_status"] == "discovered"
    assert rows["Gamma"]["pipeline_status"] == "queued"
    close_connection(path)


def test_first_sync_listing_can_be_explicitly_sent_to_agent(tmp_path, profile, settings) -> None:
    path = tmp_path / "manual-listing.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    baseline_job = make_listing(
        source, "Acme", "Software Engineer Intern", "https://jobs.test/protected"
    )
    ingest_listings(connection, source, [baseline_job], profile=profile, settings=settings)

    requested = request_manual_application(connection, 1)
    assert requested is not None
    assert requested["pipeline_status"] == "queued"
    assert requested["manual_requested"] == 1
    mark_prepared(
        connection,
        1,
        base_resume_id=None,
        resume_path=str(tmp_path / "resume.pdf"),
        cover_letter_path=None,
        tailoring_reason="manual test",
        notes="ready",
    )
    assert claimable_application_count(connection, max_attempts=3) == 0
    assert claimable_application_count(connection, max_attempts=3, target_job_id=1) == 1
    claimed = claim_next_job(
        connection, worker_id="worker-0", max_attempts=3, target_job_id=1
    )
    assert claimed is not None
    assert claimed["pipeline_status"] == "applying"
    close_connection(path)


def test_same_role_from_second_repo_is_deduplicated_by_fingerprint(tmp_path, profile, settings) -> None:
    path = tmp_path / "dedupe.db"
    connection = init_db(path)
    source_one, source_two = SOURCE_DOCUMENTS[0], SOURCE_DOCUMENTS[1]
    one = make_listing(
        source_one,
        "Acme",
        "Software Engineering Intern",
        "https://boards.example.com/jobs/123",
        "San Francisco",
    )
    two = make_listing(
        source_two,
        "ACME",
        "Software Engineer Intern",
        "https://careers.example.com/positions/123",
        "SF",
    )

    ingest_listings(connection, source_one, [one], profile=profile, settings=settings)
    ingest_listings(connection, source_two, [two], profile=profile, settings=settings)

    assert get_stats(connection)["total_discovered"] == 1
    row = list_jobs(connection)[0]
    assert "sndsh404" in row["source_labels"]
    assert "Vansh" in row["source_labels"]
    close_connection(path)


def test_cross_source_eligibility_change_does_not_escape_baseline(
    tmp_path, profile, settings
) -> None:
    path = tmp_path / "cross-source-baseline.db"
    connection = init_db(path)
    source_one, source_two = SOURCE_DOCUMENTS[0], SOURCE_DOCUMENTS[1]
    closed = make_listing(
        source_one,
        "Acme",
        "Software Engineer Intern",
        "https://jobs-one.test/123",
        "Remote",
    )
    closed.closed = True
    active = make_listing(
        source_two,
        "Acme",
        "Software Engineer Intern",
        "https://jobs-two.test/123",
        "Remote",
    )

    first = ingest_listings(
        connection, source_one, [closed], profile=profile, settings=settings
    )
    second = ingest_listings(
        connection, source_two, [active], profile=profile, settings=settings
    )

    row = list_jobs(connection)[0]
    assert first["queued"] == 0
    assert second["baseline"] is True
    assert second["queued"] == 0
    assert row["pipeline_status"] == "discovered"
    close_connection(path)


def test_filter_change_cannot_promote_a_baseline_listing_to_queue(
    tmp_path, profile, settings
) -> None:
    path = tmp_path / "filter-baseline.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    listing = make_listing(
        source,
        "Acme",
        "Software Engineer Intern",
        "https://jobs.test/filter-baseline",
    )
    blocked_settings = {**settings, "filters": {**settings["filters"]}}
    blocked_settings["filters"]["exclude_keywords"] = ["software"]

    first = ingest_listings(
        connection,
        source,
        [listing],
        profile=profile,
        settings=blocked_settings,
    )
    second = ingest_listings(
        connection,
        source,
        [listing],
        profile=profile,
        settings=settings,
    )

    row = list_jobs(connection)[0]
    assert first["baseline"] is True
    assert second["queued"] == 0
    assert row["pipeline_status"] == "discovered"
    assert row["discovered_as_new"] == 0
    close_connection(path)


def test_distinct_requisitions_in_same_source_are_not_collapsed(tmp_path, profile, settings) -> None:
    path = tmp_path / "requisitions.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    one = make_listing(
        source,
        "Acme",
        "Software Engineer Intern",
        "https://jobs.test/requisition-1",
        "Seattle",
    )
    two = make_listing(
        source,
        "Acme",
        "Software Engineer Intern",
        "https://jobs.test/requisition-2",
        "Seattle",
    )

    ingest_listings(connection, source, [one, two], profile=profile, settings=settings)

    assert get_stats(connection)["total_discovered"] == 2
    close_connection(path)


def test_removed_listing_expires_without_erasing_submitted_job(tmp_path, profile, settings) -> None:
    path = tmp_path / "expiry.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    one = make_listing(source, "Acme", "Software Intern", "https://jobs.test/1")
    two = make_listing(source, "Beta", "Data Intern", "https://jobs.test/2")
    ingest_listings(connection, source, [one, two], profile=profile, settings=settings)
    update_tracker(connection, 1, pipeline_status="applied")

    result = ingest_listings(connection, source, [], profile=profile, settings=settings)
    rows = {row["id"]: row for row in list_jobs(connection)}

    assert result["expired"] == 2
    assert rows[1]["pipeline_status"] == "applied"
    assert rows[2]["pipeline_status"] == "expired"
    close_connection(path)


def test_tracker_milestones_drive_rates_and_are_retained(tmp_path, profile, settings) -> None:
    path = tmp_path / "stats.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    job = make_listing(source, "Acme", "Software Intern", "https://jobs.test/1")
    ingest_listings(
        connection, source, [job], profile=profile, settings=settings, include_existing=True
    )
    update_tracker(connection, 1, pipeline_status="applied")
    update_tracker(connection, 1, outcome_status="oa")
    update_tracker(connection, 1, outcome_status="interview")

    stats = get_stats(connection)
    row = list_jobs(connection)[0]
    assert stats["applications"] == 1
    assert stats["oa_rate"] == 100.0
    assert stats["interview_rate"] == 100.0
    assert row["oa_at"] is not None
    assert row["interview_at"] is not None
    close_connection(path)


def test_analytics_break_down_submitted_applications_and_notifications_are_unique(
    tmp_path, profile, settings
) -> None:
    path = tmp_path / "analytics.db"
    connection = init_db(path)
    source_one, source_two = SOURCE_DOCUMENTS[:2]
    listings = [
        make_listing(
            source_one,
            "Acme",
            "Machine Learning Intern",
            "https://boards.greenhouse.io/acme/jobs/1",
            "Remote - US",
        ),
        make_listing(
            source_two,
            "Beta",
            "Data Engineering Intern",
            "https://beta.wd5.myworkdayjobs.com/jobs/2",
            "New York, NY",
        ),
    ]
    ingest_listings(
        connection,
        source_one,
        [listings[0]],
        profile=profile,
        settings=settings,
        include_existing=True,
    )
    ingest_listings(
        connection,
        source_two,
        [listings[1]],
        profile=profile,
        settings=settings,
        include_existing=True,
    )
    general = add_resume_record(
        connection,
        name="General",
        original_filename="general.pdf",
        pdf_path=str(tmp_path / "general.pdf"),
        text_path=str(tmp_path / "general.txt"),
    )
    data = add_resume_record(
        connection,
        name="Data",
        original_filename="data.pdf",
        pdf_path=str(tmp_path / "data.pdf"),
        text_path=str(tmp_path / "data.txt"),
    )
    connection.execute("UPDATE jobs SET base_resume_id = ? WHERE id = 1", (general["id"],))
    connection.execute("UPDATE jobs SET base_resume_id = ? WHERE id = 2", (data["id"],))
    connection.commit()

    update_tracker(connection, 1, pipeline_status="applied")
    update_tracker(connection, 1, outcome_status="oa")
    update_tracker(connection, 1, outcome_status="oa")
    update_tracker(connection, 2, pipeline_status="applied")
    update_tracker(connection, 2, outcome_status="interview")

    analytics = get_analytics(connection)
    assert analytics["summary"]["applications"] == 2
    assert analytics["summary"]["oa_rate"] == 50.0
    assert {row["label"] for row in analytics["dimensions"]["resume"]} == {
        "General",
        "Data",
    }
    assert {row["label"] for row in analytics["dimensions"]["role_family"]} == {
        "Machine learning & AI",
        "Data & analytics",
    }
    assert {row["label"] for row in analytics["dimensions"]["portal"]} == {
        "Greenhouse",
        "Workday",
    }
    assert {row["label"] for row in analytics["dimensions"]["location"]} == {
        "Remote",
        "New York, NY",
    }
    assert {row["label"] for row in analytics["dimensions"]["source"]} == {
        source_one.label,
        source_two.label,
    }
    assert [item["category"] for item in list_notifications(connection)] == [
        "application_applied",
        "oa",
        "application_applied",
        "interview",
    ]
    close_connection(path)


def test_stats_count_only_active_eligibility_and_submitted_outcomes(
    tmp_path, profile, settings
) -> None:
    path = tmp_path / "stats-guards.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    jobs = [
        make_listing(source, "Acme", "Software Intern", "https://jobs.test/1"),
        make_listing(source, "Beta", "Data Intern", "https://jobs.test/2"),
    ]
    ingest_listings(
        connection,
        source,
        jobs,
        profile=profile,
        settings=settings,
        include_existing=True,
    )
    connection.execute("UPDATE jobs SET is_active = 0, pipeline_status = 'expired' WHERE id = 1")
    connection.commit()
    update_tracker(connection, 2, outcome_status="oa")

    stats = get_stats(connection)
    assert stats["eligible"] == 1
    assert stats["applications"] == 0
    assert stats["oas"] == 0
    assert stats["oa_rate"] == 0.0
    close_connection(path)


def test_failed_application_is_retryable_until_attempt_cap(tmp_path, profile, settings) -> None:
    path = tmp_path / "retry.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    job = make_listing(source, "Acme", "Software Intern", "https://jobs.test/1")
    ingest_listings(
        connection, source, [job], profile=profile, settings=settings, include_existing=True
    )
    connection.execute("UPDATE jobs SET pipeline_status = 'ready' WHERE id = 1")
    connection.commit()

    first = claim_next_job(connection, worker_id="worker-0", max_attempts=2)
    assert first is not None
    mark_apply_result(connection, 1, "failed", "temporary page error")
    second = claim_next_job(connection, worker_id="worker-0", max_attempts=2)
    assert second is not None
    mark_apply_result(connection, 1, "failed", "temporary page error")

    assert claim_next_job(connection, worker_id="worker-0", max_attempts=2) is None
    close_connection(path)


def test_application_claim_rejects_inactive_and_ineligible_jobs(
    tmp_path, profile, settings
) -> None:
    path = tmp_path / "claim-guards.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    first = make_listing(source, "Acme", "Software Intern", "https://jobs.test/1")
    second = make_listing(source, "Beta", "Data Intern", "https://jobs.test/2")
    ingest_listings(
        connection,
        source,
        [first, second],
        profile=profile,
        settings=settings,
        include_existing=True,
    )
    connection.execute(
        "UPDATE jobs SET pipeline_status = 'ready', eligibility = 'ineligible' WHERE id = 1"
    )
    connection.execute(
        "UPDATE jobs SET pipeline_status = 'ready', eligibility = 'eligible', is_active = 0 WHERE id = 2"
    )
    connection.commit()

    assert claimable_application_count(connection, max_attempts=3) == 0
    assert claim_next_job(connection, worker_id="worker-0", max_attempts=3) is None
    close_connection(path)


def test_applications_view_contains_only_submitted_jobs(tmp_path, profile, settings) -> None:
    path = tmp_path / "applications-only.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    jobs = [
        make_listing(source, "Acme", "Software Intern", "https://jobs.test/1"),
        make_listing(source, "Beta", "Data Intern", "https://jobs.test/2"),
    ]
    ingest_listings(connection, source, jobs, profile=profile, settings=settings)
    update_tracker(connection, 2, pipeline_status="applied")

    rows = list_jobs(connection, applied_only=True)

    assert [row["company"] for row in rows] == ["Beta"]
    assert rows[0]["applied_at"] is not None
    close_connection(path)


def test_employer_closed_listing_is_not_reopened_by_unchanged_source(
    tmp_path, profile, settings
) -> None:
    path = tmp_path / "employer-closed.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    listing = make_listing(source, "Acme", "Software Intern", "https://jobs.test/closed")
    ingest_listings(connection, source, [listing], profile=profile, settings=settings)
    mark_apply_result(connection, 1, "expired", "Employer page returned 404")

    ingest_listings(connection, source, [listing], profile=profile, settings=settings)
    row = list_jobs(connection)[0]

    assert row["pipeline_status"] == "expired"
    assert row["availability_status"] == "closed"
    assert list_jobs(connection, latest=True, active_only=True) == []
    close_connection(path)


def test_access_block_becomes_manual_handoff_instead_of_retry(
    tmp_path, profile, settings
) -> None:
    path = tmp_path / "manual-handoff.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    listing = make_listing(source, "Acme", "Software Intern", "https://jobs.test/blocked")
    ingest_listings(connection, source, [listing], profile=profile, settings=settings)

    mark_apply_result(
        connection,
        1,
        "needs_review",
        "Employer returned HTTP 403",
        reason_code="access_blocked",
    )
    row = list_jobs(connection)[0]

    assert row["pipeline_status"] == "manual_review"
    assert row["availability_status"] == "manual_only"
    assert claimable_application_count(connection, max_attempts=3, target_job_id=1) == 0
    close_connection(path)


def test_removed_source_document_is_retired_without_deleting_history(
    tmp_path, profile, settings
) -> None:
    path = tmp_path / "retired-source.db"
    connection = init_db(path)
    retired = SourceDocument(
        key="retired",
        label="Retired off-season feed",
        repo_url="https://github.com/example/retired",
        branch="main",
        path="OFFSEASON.md",
        season="old",
    )
    listing = make_listing(retired, "Old Co", "Software Intern", "https://jobs.test/old")
    ingest_listings(connection, retired, [listing], profile=profile, settings=settings)

    retired_count = reconcile_source_registry(connection, SOURCE_DOCUMENTS)
    row = list_jobs(connection)[0]

    assert retired_count == 1
    assert row["is_active"] == 0
    assert row["pipeline_status"] == "expired"
    assert connection.execute(
        "SELECT enabled FROM sources WHERE document_key = ?", (retired.document_key,)
    ).fetchone()[0] == 0
    close_connection(path)


def test_candidate_agent_inputs_are_saved_and_requeue_prepared_job(
    tmp_path, profile, settings
) -> None:
    path = tmp_path / "agent-inputs.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    listing = make_listing(source, "Acme", "Software Intern", "https://jobs.test/input")
    ingest_listings(connection, source, [listing], profile=profile, settings=settings)
    connection.execute(
        """
        UPDATE jobs SET pipeline_status = 'manual_review', resume_path = ?
        WHERE id = 1
        """,
        (str(tmp_path / "resume.pdf"),),
    )
    connection.commit()
    saved = store_agent_inputs(
        connection,
        1,
        [
            {
                "key": "preferred_team",
                "label": "Which engineering team do you prefer?",
                "input_type": "select",
                "options": ["Platform", "Product"],
                "required": True,
            },
            {
                "key": "verification_code",
                "label": "Email verification code",
                "input_type": "text",
                "options": [],
                "required": True,
            },
        ],
    )

    assert [item["input_key"] for item in saved] == ["preferred_team"]
    job = answer_agent_inputs(connection, 1, {"preferred_team": "Platform"})

    assert job is not None
    assert job["pipeline_status"] == "ready"
    assert job["manual_requested"] == 1
    assert answered_agent_inputs(connection, 1)["preferred_team"]["answer"] == "Platform"
    close_connection(path)


def test_pre_feature_missing_fact_pause_gets_a_legacy_answer_channel(
    tmp_path, profile, settings
) -> None:
    path = tmp_path / "legacy-agent-input.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    listing = make_listing(source, "Acme", "Software Intern", "https://jobs.test/legacy")
    ingest_listings(connection, source, [listing], profile=profile, settings=settings)
    connection.execute(
        """
        UPDATE jobs SET pipeline_status = 'manual_review',
                        apply_error = 'Two required fields cannot be answered from the profile'
        WHERE id = 1
        """
    )
    connection.commit()
    close_connection(path)

    connection = init_db(path)
    questions = list_agent_inputs(connection, 1, pending_only=True)

    assert len(questions) == 1
    assert questions[0]["input_key"] == "legacy_follow_up"
    assert questions[0]["input_type"] == "textarea"
    close_connection(path)
