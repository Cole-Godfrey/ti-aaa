from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from tiaaa import database
from tiaaa.config import SOURCE_DOCUMENTS
from tiaaa.database import (
    add_resume_record,
    agent_stop_requested,
    answer_agent_inputs,
    answered_agent_inputs,
    claim_next_job,
    claimable_application_count,
    clear_ephemeral_agent_inputs,
    clear_missing_fact_blocks,
    close_all_connections,
    close_connection,
    company_application_history,
    get_analytics,
    get_job,
    get_stats,
    human_control_returned,
    ingest_listings,
    init_db,
    list_agent_inputs,
    list_application_queue,
    list_jobs,
    list_manual_handoff_jobs,
    listing_age_cutoff,
    live_human_interaction_checkpoint,
    live_submission_checkpoint,
    manual_application_ids,
    mark_applied_manually,
    mark_apply_result,
    mark_prepared,
    profile_facts_changed,
    reconcile_source_registry,
    recover_stale_work,
    refresh_eligibility,
    request_agent_stop,
    request_final_submission,
    request_human_control_return,
    request_manual_application,
    resume_application_after_human_control,
    resume_application_after_input,
    resume_application_for_submission,
    retry_manual_application,
    reviewable_listings,
    store_agent_inputs,
    update_tracker,
    update_worker_state,
)
from tiaaa.models import InternshipListing, SourceDocument


def test_close_all_connections_releases_registered_database_handles(tmp_path) -> None:
    path = tmp_path / "connections.db"
    connection = init_db(path)

    close_all_connections(path)

    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")
    replacement = init_db(path)
    assert replacement.execute("SELECT 1").fetchone()[0] == 1
    close_connection(path)


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


def test_service_restart_releases_a_stale_manual_review_session(
    tmp_path, profile, settings
) -> None:
    path = tmp_path / "stale-review.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    listing = make_listing(source, "Acme", "Software Intern", "https://jobs.test/stale")
    ingest_listings(connection, source, [listing], profile=profile, settings=settings)
    connection.execute(
        """
        UPDATE jobs SET pipeline_status = 'manual_review', worker_id = 'worker-0',
                        submission_requested = 1, human_control_returned = 1
        WHERE id = 1
        """
    )
    update_worker_state(
        connection,
        "worker-0",
        status="review_ready",
        job=get_job(connection, 1),
    )

    assert recover_stale_work(connection) == 1
    recovered = get_job(connection, 1)
    assert recovered["pipeline_status"] == "manual_review"
    assert recovered["worker_id"] is None
    assert recovered["submission_requested"] == 0
    assert recovered["human_control_returned"] == 0
    assert request_manual_application(connection, 1)["pipeline_status"] == "queued"
    close_connection(path)


def test_manual_request_cannot_replace_a_live_review_session(
    tmp_path, profile, settings
) -> None:
    path = tmp_path / "live-review.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    listing = make_listing(source, "Acme", "Software Intern", "https://jobs.test/live")
    ingest_listings(connection, source, [listing], profile=profile, settings=settings)
    connection.execute(
        "UPDATE jobs SET pipeline_status = 'manual_review', worker_id = 'worker-0' WHERE id = 1"
    )
    update_worker_state(
        connection,
        "worker-0",
        status="review_ready",
        job=get_job(connection, 1),
    )

    try:
        request_manual_application(connection, 1)
    except ValueError as exc:
        assert "already manual review" in str(exc)
    else:
        raise AssertionError("A live review session must not be replaced")
    close_connection(path)


def test_human_captcha_checkpoint_retains_and_resumes_the_same_worker(
    tmp_path, profile, settings
) -> None:
    path = tmp_path / "human-captcha.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    listing = make_listing(
        source,
        "Capula",
        "Trading and Research Intern",
        "https://jobs.test/capula",
    )
    ingest_listings(connection, source, [listing], profile=profile, settings=settings)
    connection.execute(
        "UPDATE jobs SET pipeline_status = 'applying', worker_id = 'worker-0' WHERE id = 1"
    )
    connection.commit()
    mark_apply_result(
        connection,
        1,
        "captcha",
        "Submit is stuck on Submitting with no receipt",
        reason_code="captcha",
        retain_worker=True,
    )
    update_worker_state(
        connection,
        "worker-0",
        status="captcha",
        job=get_job(connection, 1),
    )

    assert live_human_interaction_checkpoint(connection, 1, "worker-0") is True
    with pytest.raises(ValueError, match="already manual review"):
        request_manual_application(connection, 1)

    returned = request_human_control_return(connection, 1)

    assert returned is not None
    assert returned["human_control_returned"] == 1
    assert human_control_returned(connection, 1, "worker-0") is True
    with pytest.raises(ValueError, match="already been returned"):
        request_human_control_return(connection, 1)
    assert resume_application_after_human_control(
        connection, 1, "worker-0"
    ) is True
    resumed = get_job(connection, 1)
    assert resumed["pipeline_status"] == "applying"
    assert resumed["worker_id"] == "worker-0"
    assert resumed["human_control_returned"] == 0
    assert resumed["apply_error"] is None
    assert resumed["apply_reason_code"] is None
    event = connection.execute(
        "SELECT event_type FROM events WHERE job_id = 1 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert event["event_type"] == "agent_resumed_after_human_control"
    close_connection(path)


def test_retry_manual_application_cancels_live_checkpoint_and_requeues(
    tmp_path, profile, settings
) -> None:
    path = tmp_path / "retry-live-review.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    listing = make_listing(source, "Acme", "Software Intern", "https://jobs.test/retry")
    ingest_listings(connection, source, [listing], profile=profile, settings=settings)
    connection.execute(
        "UPDATE jobs SET pipeline_status = 'manual_review', resume_path = ? WHERE id = 1",
        (str(tmp_path / "resume.pdf"),),
    )
    connection.commit()
    store_agent_inputs(
        connection,
        1,
        [
            {
                "key": "preferred_team",
                "label": "Preferred team",
                "input_type": "text",
                "required": True,
            },
            {
                "key": "email_verification_code",
                "label": "Email verification code",
                "input_type": "verification_code",
                "required": True,
            },
        ],
    )
    answer_agent_inputs(
        connection,
        1,
        {
            "preferred_team": "Platform",
            "email_verification_code": "A1B2C3D4",
        },
    )
    connection.execute(
        """
        UPDATE jobs SET pipeline_status = 'manual_review', worker_id = 'worker-0',
                        apply_attempts = 3, apply_error = 'Needs confirmation',
                        submission_requested = 1
        WHERE id = 1
        """
    )
    connection.commit()
    update_worker_state(
        connection,
        "worker-0",
        status="review_ready",
        job=get_job(connection, 1),
    )

    assert live_submission_checkpoint(connection, 1, "worker-0") is True

    retried = retry_manual_application(connection, 1)

    assert retried is not None
    assert retried["pipeline_status"] == "ready"
    assert retried["manual_requested"] == 1
    assert retried["worker_id"] is None
    assert retried["apply_attempts"] == 0
    assert retried["apply_error"] is None
    assert retried["submission_requested"] == 0
    assert live_submission_checkpoint(connection, 1, "worker-0") is False
    inputs = {item["input_key"]: item for item in list_agent_inputs(connection, 1)}
    assert inputs["preferred_team"]["status"] == "resolved"
    assert inputs["preferred_team"]["answer"] == "Platform"
    assert inputs["email_verification_code"]["status"] == "resolved"
    assert inputs["email_verification_code"]["answer"] is None
    event = connection.execute(
        "SELECT event_type FROM events WHERE job_id = 1 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert event["event_type"] == "manual_retry_requested"
    close_connection(path)


def test_auto_apply_requires_an_apply_decision_but_manual_apply_bypasses_it(
    tmp_path, profile, settings
) -> None:
    path = tmp_path / "auto-decision-gate.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    jobs = [
        make_listing(source, "Skipped Co", "Software Intern", "https://jobs.test/low"),
        make_listing(source, "Chosen Co", "Backend Intern", "https://jobs.test/high"),
    ]
    ingest_listings(
        connection, source, jobs, profile=profile, settings=settings, include_existing=True
    )
    connection.execute(
        """
        UPDATE jobs SET pipeline_status = 'ready', eligibility = 'eligible',
                        discovered_as_new = 1,
                        apply_decision = CASE WHEN company = 'Chosen Co'
                            THEN 'apply' ELSE 'skip' END,
                        apply_confidence = 'high'
        """
    )
    connection.commit()

    assert claimable_application_count(connection, max_attempts=3) == 1
    automatic = claim_next_job(connection, worker_id="worker-0", max_attempts=3)
    assert automatic is not None
    assert automatic["company"] == "Chosen Co"

    manual = claim_next_job(
        connection,
        worker_id="worker-1",
        max_attempts=3,
        target_job_id=1,
    )
    assert manual is not None
    assert manual["company"] == "Skipped Co"
    close_connection(path)


def test_auto_apply_never_claims_an_unreviewed_listing(tmp_path, profile, settings) -> None:
    path = tmp_path / "unreviewed.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    ingest_listings(
        connection,
        source,
        [make_listing(source, "Acme", "Software Intern", "https://jobs.test/new")],
        profile=profile,
        settings=settings,
        include_existing=True,
    )
    connection.execute(
        """
        UPDATE jobs SET pipeline_status = 'ready', eligibility = 'eligible',
                        discovered_as_new = 1
        """
    )
    connection.commit()

    assert claimable_application_count(connection, max_attempts=3) == 0
    assert claim_next_job(connection, worker_id="worker-0", max_attempts=3) is None
    close_connection(path)


def test_auto_apply_honours_the_reviewed_company_budget(tmp_path, profile, settings) -> None:
    path = tmp_path / "company-budget.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    listings = [
        make_listing(source, "Acme", "Backend Intern", "https://jobs.test/acme-backend"),
        make_listing(source, "Acme", "ML Intern", "https://jobs.test/acme-ml"),
        make_listing(source, "Acme", "IT Intern", "https://jobs.test/acme-it"),
        make_listing(source, "Beta", "Software Intern", "https://jobs.test/beta-swe"),
    ]
    ingest_listings(
        connection,
        source,
        listings,
        profile=profile,
        settings=settings,
        include_existing=True,
    )
    # The reviewer spent Acme's two-application budget on ML and Backend.
    connection.execute(
        """
        UPDATE jobs SET pipeline_status = 'ready', eligibility = 'eligible',
                        discovered_as_new = 1,
                        apply_decision = CASE role
                            WHEN 'IT Intern' THEN 'skip' ELSE 'apply' END,
                        apply_confidence = CASE role
                            WHEN 'ML Intern' THEN 'high' ELSE 'medium' END
        """
    )
    connection.commit()

    assert claimable_application_count(connection, max_attempts=3) == 3
    first = claim_next_job(connection, worker_id="worker-0", max_attempts=3)
    assert first is not None
    assert (first["company"], first["role"]) == ("Acme", "ML Intern")
    assert first["apply_origin"] == "auto"

    # The unchosen Acme role is left alone rather than force-skipped: the
    # decision already says No, and a manual Apply must still reach it.
    unchosen = get_job(connection, 3)
    assert unchosen is not None
    assert unchosen["pipeline_status"] == "ready"
    assert unchosen["apply_decision"] == "skip"

    # Acme's second approved role waits while its first is mid-application.
    second = claim_next_job(connection, worker_id="worker-1", max_attempts=3)
    assert second is not None
    assert second["company"] == "Beta"
    close_connection(path)


def test_application_queue_lists_every_reviewed_apply_in_order(
    tmp_path, profile, settings
) -> None:
    path = tmp_path / "application-queue.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    listings = [
        make_listing(source, "Acme", "Backend Intern", "https://jobs.test/acme-backend"),
        make_listing(source, "Acme", "ML Intern", "https://jobs.test/acme-ml"),
        make_listing(source, "Beta", "Software Intern", "https://jobs.test/beta"),
        make_listing(source, "Declined Co", "IT Intern", "https://jobs.test/low"),
    ]
    ingest_listings(
        connection,
        source,
        listings,
        profile=profile,
        settings=settings,
        include_existing=True,
    )
    connection.execute(
        """
        UPDATE jobs SET pipeline_status = 'queued', eligibility = 'eligible',
                        discovered_as_new = 1,
                        apply_decision = CASE company
                            WHEN 'Declined Co' THEN 'skip' ELSE 'apply' END,
                        apply_confidence = CASE role
                            WHEN 'ML Intern' THEN 'high' ELSE 'medium' END
        """
    )
    connection.commit()

    waiting = list_application_queue(
        connection,
        auto_enabled=True,
        max_attempts=3,
        profile=profile,
    )

    assert [(item["company"], item["role"]) for item in waiting] == [
        ("Acme", "ML Intern"),
        ("Acme", "Backend Intern"),
        ("Beta", "Software Intern"),
    ]
    assert [item["position"] for item in waiting] == [1, 2, 3]
    assert all(item["queue_state"] == "preparing" for item in waiting)

    connection.execute(
        "UPDATE jobs SET pipeline_status = 'ready' WHERE apply_decision = 'apply'"
    )
    connection.commit()
    claimed = claim_next_job(connection, worker_id="worker-0", max_attempts=3)
    assert claimed is not None
    active_queue = list_application_queue(
        connection,
        auto_enabled=True,
        max_attempts=3,
        profile=profile,
    )
    assert active_queue[0]["id"] == claimed["id"]
    assert active_queue[0]["queue_state"] == "active"
    assert active_queue[1]["company"] == "Beta"
    close_connection(path)


def test_auto_apply_preferences_are_an_optional_gate(tmp_path, profile, settings) -> None:
    path = tmp_path / "preference-gate.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    listings = [
        make_listing(source, "Hardware Co", "Hardware Intern", "https://jobs.test/hw"),
        make_listing(source, "Software Co", "Software Intern", "https://jobs.test/swe"),
    ]
    ingest_listings(
        connection,
        source,
        listings,
        profile=profile,
        settings=settings,
        include_existing=True,
    )
    connection.execute(
        """
        UPDATE jobs SET pipeline_status = 'ready', eligibility = 'eligible',
                        discovered_as_new = 1, apply_decision = 'apply',
                        apply_confidence = 'high'
        """
    )
    connection.commit()

    assert claimable_application_count(
        connection,
        max_attempts=3,
        profile=profile,
        use_preferences=False,
    ) == 2
    assert claimable_application_count(
        connection,
        max_attempts=3,
        profile=profile,
        use_preferences=True,
    ) == 1
    claimed = claim_next_job(
        connection,
        worker_id="worker-0",
        max_attempts=3,
        profile=profile,
        use_preferences=True,
    )
    assert claimed is not None
    assert claimed["company"] == "Software Co"
    close_connection(path)


def test_auto_apply_does_not_retry_a_terminal_missing_fact(tmp_path, profile, settings) -> None:
    path = tmp_path / "terminal-auto-error.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    listing = make_listing(source, "Acme", "Software Intern", "https://jobs.test/missing")
    ingest_listings(
        connection,
        source,
        [listing],
        profile=profile,
        settings=settings,
        include_existing=True,
    )
    connection.execute(
        """
        UPDATE jobs SET pipeline_status = 'failed', eligibility = 'eligible',
                        discovered_as_new = 1, apply_decision = 'apply',
                        apply_origin = 'auto', apply_attempts = 1,
                        apply_reason_code = 'missing_input',
                        apply_error = 'Required address is unavailable'
        """
    )
    connection.commit()

    assert claimable_application_count(connection, max_attempts=3) == 0
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


def test_analytics_break_down_submitted_applications(
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

    mark_applied_manually(connection, 1, resume_id=int(general["id"]))
    update_tracker(connection, 1, outcome_status="oa")
    update_tracker(connection, 1, outcome_status="oa")
    mark_applied_manually(connection, 2, resume_id=int(data["id"]))
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
    connection.execute(
        """
        UPDATE jobs SET pipeline_status = 'ready', apply_decision = 'apply'
        WHERE id = 1
        """
    )
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


def test_agent_discovered_qualification_conflict_persists_and_blocks_auto_apply(
    tmp_path, profile, settings
) -> None:
    path = tmp_path / "agent-qualification-conflict.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    job = make_listing(source, "Acme", "Research Intern", "https://jobs.test/research")
    ingest_listings(
        connection, source, [job], profile=profile, settings=settings, include_existing=True
    )
    connection.execute(
        "UPDATE jobs SET pipeline_status = 'ready', discovered_as_new = 1 WHERE id = 1"
    )
    connection.commit()

    detail = "Applicants must have previously interned at Acme"
    mark_apply_result(
        connection,
        1,
        "failed",
        detail,
        reason_code="eligibility_conflict",
    )
    ingest_listings(connection, source, [job], profile=profile, settings=settings)
    refresh_eligibility(connection, profile=profile, settings=settings)

    row = get_job(connection, 1)
    assert row is not None
    assert row["eligibility"] == "ineligible"
    assert row["eligibility_reason"] == detail
    assert row["pipeline_status"] == "failed"
    assert row["apply_reason_code"] == "eligibility_conflict"
    assert claimable_application_count(connection, max_attempts=3) == 0
    close_connection(path)


def test_eligibility_refresh_updates_the_hard_gate_from_the_profile(
    tmp_path, profile, settings
) -> None:
    path = tmp_path / "eligibility-refresh.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    job = make_listing(
        source,
        "Acme",
        "Research Intern (PhD)",
        "https://jobs.test/phd-research",
    )
    ingest_listings(connection, source, [job], profile=profile, settings=settings)
    connection.execute(
        "UPDATE jobs SET eligibility = 'eligible', eligibility_reason = 'old' WHERE id = 1"
    )
    connection.commit()

    refresh_eligibility(connection, profile=profile, settings=settings)

    row = get_job(connection, 1)
    assert row is not None
    assert row["eligibility"] == "ineligible"
    assert row["eligibility_reason"] == (
        "requires a doctoral degree not present in the profile"
    )
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


def test_list_jobs_filters_review_decisions_before_applying_the_limit(
    tmp_path, profile, settings
) -> None:
    path = tmp_path / "decision-filter.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    jobs = [
        replace(
            make_listing(source, "No Co", "Newest Intern", "https://jobs.test/no"),
            posting_date="2026-09-03",
        ),
        replace(
            make_listing(
                source, "Pending Co", "Middle Intern", "https://jobs.test/pending"
            ),
            posting_date="2026-09-02",
        ),
        replace(
            make_listing(source, "Yes Co", "Oldest Intern", "https://jobs.test/yes"),
            posting_date="2026-09-01",
        ),
    ]
    ingest_listings(connection, source, jobs, profile=profile, settings=settings)
    connection.execute(
        """
        UPDATE jobs
        SET apply_decision = CASE company
            WHEN 'Yes Co' THEN 'apply'
            WHEN 'No Co' THEN 'skip'
            ELSE NULL
        END
        """
    )
    connection.commit()

    assert [
        row["company"]
        for row in list_jobs(
            connection, decision="apply", latest=True, active_only=True, limit=1
        )
    ] == ["Yes Co"]
    assert [row["company"] for row in list_jobs(connection, decision="skip")] == ["No Co"]
    assert [row["company"] for row in list_jobs(connection, decision="pending")] == [
        "Pending Co"
    ]
    close_connection(path)


def test_application_ledger_includes_agent_checkpoints(tmp_path, profile, settings) -> None:
    path = tmp_path / "application-ledger.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    jobs = [
        make_listing(source, "Discovered", "Software Intern", "https://jobs.test/1"),
        make_listing(source, "Submitted", "Data Intern", "https://jobs.test/2"),
        make_listing(source, "Checkpoint", "Security Intern", "https://jobs.test/3"),
    ]
    ingest_listings(connection, source, jobs, profile=profile, settings=settings)
    update_tracker(connection, 2, pipeline_status="applied")
    connection.execute(
        "UPDATE jobs SET pipeline_status = 'manual_review' WHERE id = 3"
    )
    connection.commit()

    rows = list_jobs(connection, application_ledger=True)

    assert [row["company"] for row in rows] == ["Checkpoint", "Submitted"]
    assert all(row["company"] != "Discovered" for row in rows)
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


def test_upgrade_reimports_sources_once_for_the_advanced_degree_flag(
    tmp_path, profile, settings
) -> None:
    path = tmp_path / "flag-backfill.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    listing = make_listing(source, "Acme", "Research Intern", "https://jobs.test/flag")
    ingest_listings(connection, source, [listing], profile=profile, settings=settings)
    connection.execute(
        "UPDATE sources SET etag = 'W/\"cached\"', content_sha256 = 'unchanged'"
    )
    connection.commit()
    # An installation that already imported the flag keeps its source cache.
    init_db(path)
    assert connection.execute(
        "SELECT content_sha256 FROM sources WHERE document_key = ?",
        (source.document_key,),
    ).fetchone()[0] == "unchanged"

    connection.execute(
        "DELETE FROM app_state WHERE key = 'advanced_degree_flag_imported'"
    )
    connection.commit()
    init_db(path)

    cached = connection.execute(
        "SELECT etag, content_sha256 FROM sources WHERE document_key = ?",
        (source.document_key,),
    ).fetchone()
    assert cached["etag"] is None
    assert cached["content_sha256"] is None
    close_connection(path)


def test_supplying_a_missing_fact_releases_blocked_applications(
    tmp_path, profile, settings
) -> None:
    path = tmp_path / "missing-fact.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    listings = [
        make_listing(source, "Acme", "Software Intern", "https://jobs.test/blocked"),
        make_listing(source, "Beta", "Backend Intern", "https://jobs.test/other"),
    ]
    settings["automation"]["auto_apply_new"] = True
    ingest_listings(
        connection, source, listings, profile=profile, settings=settings, include_existing=True
    )
    connection.execute(
        """
        UPDATE jobs SET pipeline_status = 'applying', apply_origin = 'auto',
                        apply_attempts = 1, resume_path = 'resume.pdf',
                        apply_decision = 'apply'
        WHERE id = 1
        """
    )
    connection.execute(
        "UPDATE jobs SET pipeline_status = 'expired', apply_reason_code = 'missing_input' WHERE id = 2"
    )
    connection.commit()
    mark_apply_result(
        connection,
        1,
        "failed",
        "Required home address is not configured",
        reason_code="missing_input",
        manual_handoff=False,
    )
    assert claimable_application_count(connection, max_attempts=3) == 0

    assert profile_facts_changed(connection, profile) is True
    assert profile_facts_changed(connection, profile) is False

    profile["personal"]["address"] = "123 Pine Street"
    assert profile_facts_changed(connection, profile) is True
    assert clear_missing_fact_blocks(connection) == 1

    released = get_job(connection, 1)
    assert released["pipeline_status"] == "ready"
    assert released["apply_reason_code"] is None
    assert released["apply_error"] is None
    assert released["apply_attempts"] == 0
    assert claimable_application_count(connection, max_attempts=3) == 1
    # A listing that left the source stays expired.
    assert get_job(connection, 2)["pipeline_status"] == "expired"
    assert clear_missing_fact_blocks(connection) == 0
    close_connection(path)


def test_stopping_a_live_checkpoint_releases_it_for_a_manual_record(
    tmp_path, profile, settings
) -> None:
    path = tmp_path / "stop-session.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    listing = make_listing(source, "Acme", "Software Intern", "https://jobs.test/captcha")
    ingest_listings(connection, source, [listing], profile=profile, settings=settings)
    connection.execute(
        """
        UPDATE jobs SET pipeline_status = 'manual_review', worker_id = 'worker-0',
                        apply_reason_code = 'captcha', apply_attempts = 1,
                        resume_path = 'resume.pdf'
        WHERE id = 1
        """
    )
    connection.commit()
    update_worker_state(
        connection, "worker-0", status="captcha", job=get_job(connection, 1)
    )
    assert live_human_interaction_checkpoint(connection, 1, "worker-0") is True

    row = request_agent_stop(connection, 1)

    assert row["pipeline_status"] == "skipped"
    assert row["apply_reason_code"] == "cancelled"
    assert row["worker_id"] is None
    assert row["manual_requested"] == 0
    assert row["stop_requested"] == 0
    # The waiting worker sees its checkpoint disappear and can end the run.
    assert live_human_interaction_checkpoint(connection, 1, "worker-0") is False
    assert live_submission_checkpoint(connection, 1, "worker-0") is False
    assert list_application_queue(
        connection, auto_enabled=True, max_attempts=3
    ) == []
    assert [item["id"] for item in list_manual_handoff_jobs(connection)] == [1]
    assert list_manual_handoff_jobs(connection)[0]["handoff"] == "stopped"

    applied = mark_applied_manually(connection, 1)
    assert applied["pipeline_status"] == "applied"
    assert applied["apply_origin"] == "self"
    close_connection(path)


def test_stop_flags_a_running_agent_and_survives_a_service_restart(
    tmp_path, profile, settings
) -> None:
    path = tmp_path / "stop-running.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    listing = make_listing(source, "Acme", "Software Intern", "https://jobs.test/live")
    ingest_listings(connection, source, [listing], profile=profile, settings=settings)
    connection.execute(
        "UPDATE jobs SET pipeline_status = 'applying', worker_id = 'worker-0' WHERE id = 1"
    )
    connection.commit()

    row = request_agent_stop(connection, 1)

    # A live agent turn owns the browser, so only the flag its worker polls is set.
    assert row["pipeline_status"] == "applying"
    assert row["stop_requested"] == 1
    assert agent_stop_requested(connection, 1) is True

    recovered = recover_stale_work(connection)

    assert recovered == 1
    stopped = get_job(connection, 1)
    assert stopped["pipeline_status"] == "skipped"
    assert stopped["apply_reason_code"] == "cancelled"
    assert stopped["stop_requested"] == 0
    assert claimable_application_count(connection, max_attempts=3) == 0
    close_connection(path)


def test_stop_is_rejected_for_listings_without_a_session(
    tmp_path, profile, settings
) -> None:
    path = tmp_path / "stop-idle.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    listing = make_listing(source, "Acme", "Software Intern", "https://jobs.test/idle")
    ingest_listings(connection, source, [listing], profile=profile, settings=settings)

    with pytest.raises(ValueError, match="no queued or running agent session"):
        request_agent_stop(connection, 1)
    assert request_agent_stop(connection, 404) is None

    # A queued request that has not started yet leaves the queue immediately.
    request_manual_application(connection, 1)
    row = request_agent_stop(connection, 1)

    assert row["pipeline_status"] == "skipped"
    assert row["manual_requested"] == 0
    assert manual_application_ids(connection) == []
    # Re-applying clears the stop so the fresh attempt is not cancelled.
    assert request_manual_application(connection, 1)["stop_requested"] == 0
    close_connection(path)


def test_bot_blocked_listing_can_be_recorded_as_applied_by_the_candidate(
    tmp_path, profile, settings
) -> None:
    path = tmp_path / "applied-manually.db"
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
    store_agent_inputs(
        connection,
        1,
        [{"key": "preferred_team", "label": "Which team?", "input_type": "text"}],
    )

    handoffs = list_manual_handoff_jobs(connection)
    assert [item["company"] for item in handoffs] == ["Acme"]
    assert handoffs[0]["detail"] == "Employer returned HTTP 403"

    row = mark_applied_manually(connection, 1)

    assert row["pipeline_status"] == "applied"
    assert row["applied_at"] is not None
    assert row["apply_origin"] == "self"
    assert row["manual_requested"] == 0
    assert row["worker_id"] is None
    assert list_agent_inputs(connection, 1, pending_only=True) == []
    assert list_manual_handoff_jobs(connection) == []
    assert claimable_application_count(connection, max_attempts=3, target_job_id=1) == 0
    assert get_stats(connection)["applications"] == 1
    assert [item["company"] for item in list_jobs(connection, applied_only=True)] == ["Acme"]
    with pytest.raises(ValueError, match="already recorded"):
        mark_applied_manually(connection, 1)
    close_connection(path)


def test_manual_application_records_the_chosen_resume_without_a_base_resume_fallback(
    tmp_path, profile, settings
) -> None:
    path = tmp_path / "manual-resume-choice.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    listings = [
        make_listing(source, "Chosen", "Backend Intern", "https://jobs.test/chosen"),
        make_listing(source, "Unrecorded", "Data Intern", "https://jobs.test/unrecorded"),
        make_listing(source, "Tracker", "Frontend Intern", "https://jobs.test/tracker"),
    ]
    ingest_listings(connection, source, listings, profile=profile, settings=settings)
    wrong = add_resume_record(
        connection,
        name="Wrong resume",
        original_filename="wrong.pdf",
        pdf_path=str(tmp_path / "wrong.pdf"),
        text_path=str(tmp_path / "wrong.txt"),
    )
    chosen = add_resume_record(
        connection,
        name="Backend resume",
        original_filename="backend.pdf",
        pdf_path=str(tmp_path / "backend.pdf"),
        text_path=str(tmp_path / "backend.txt"),
    )
    connection.execute("UPDATE jobs SET base_resume_id = ?", (wrong["id"],))
    connection.commit()

    with pytest.raises(ValueError, match="active resume"):
        mark_applied_manually(connection, 1, resume_id=9999)
    selected = mark_applied_manually(connection, 1, resume_id=int(chosen["id"]))
    unrecorded = mark_applied_manually(connection, 2)
    tracker = update_tracker(connection, 3, pipeline_status="applied")

    assert selected["submitted_resume_id"] == chosen["id"]
    assert selected["submitted_resume_name"] == "Backend resume"
    assert selected["submitted_resume_path"] == chosen["pdf_path"]
    assert unrecorded["base_resume_id"] == wrong["id"]
    assert unrecorded["submitted_resume_id"] is None
    assert tracker["base_resume_id"] == wrong["id"]
    assert tracker["submitted_resume_id"] is None
    close_connection(path)


def test_manual_apply_record_is_refused_while_the_agent_is_applying(
    tmp_path, profile, settings
) -> None:
    path = tmp_path / "applied-manually-busy.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    listing = make_listing(source, "Acme", "Software Intern", "https://jobs.test/live")
    ingest_listings(connection, source, [listing], profile=profile, settings=settings)
    connection.execute(
        "UPDATE jobs SET pipeline_status = 'applying', worker_id = 'worker-0' WHERE id = 1"
    )
    connection.commit()

    with pytest.raises(ValueError, match="applying to this listing right now"):
        mark_applied_manually(connection, 1)

    assert mark_applied_manually(connection, 404) is None
    assert get_job(connection, 1)["applied_at"] is None
    close_connection(path)


def test_source_advanced_degree_flag_survives_sync_and_blocks_auto_apply(
    tmp_path, profile, settings
) -> None:
    path = tmp_path / "advanced-degree-flag.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    listing = make_listing(
        source, "Acme", "Research Scientist Intern", "https://jobs.test/research"
    )
    ingest_listings(connection, source, [listing], profile=profile, settings=settings)
    row = get_job(connection, 1)
    assert row["eligibility"] == "eligible"

    flagged = replace(listing, advanced_degree_required=True)
    ingest_listings(connection, source, [flagged], profile=profile, settings=settings)
    row = get_job(connection, 1)

    assert row["advanced_degree_required"] == 1
    assert row["eligibility"] == "ineligible"
    assert row["eligibility_reason"] == (
        "source list marks this role advanced-degree only (master's, PhD, or MBA)"
    )
    assert row["pipeline_status"] == "skipped"
    assert claimable_application_count(connection, max_attempts=3) == 0

    # A later poll of a source that does not repeat the flag keeps the gate.
    ingest_listings(connection, source, [listing], profile=profile, settings=settings)
    assert get_job(connection, 1)["eligibility"] == "ineligible"

    profile["education"]["degree"] = "Master of Science"
    refresh_eligibility(connection, profile=profile, settings=settings)
    assert get_job(connection, 1)["eligibility"] == "eligible"
    close_connection(path)


def test_agent_discovered_conflict_keeps_the_listing_ineligible(
    tmp_path, profile, settings
) -> None:
    path = tmp_path / "conflict-eligibility.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    listing = make_listing(source, "Acme", "Software Intern", "https://jobs.test/conflict")
    ingest_listings(connection, source, [listing], profile=profile, settings=settings)
    assert get_job(connection, 1)["eligibility"] == "eligible"
    connection.execute(
        """
        UPDATE jobs SET pipeline_status = 'applying', apply_origin = 'auto',
                        apply_attempts = 1
        WHERE id = 1
        """
    )
    connection.commit()
    mark_apply_result(
        connection,
        1,
        "failed",
        "Posting requires an enrolled master's student",
        reason_code="eligibility_conflict",
        manual_handoff=False,
    )

    refresh_eligibility(connection, profile=profile, settings=settings)

    row = get_job(connection, 1)
    assert row["eligibility"] == "ineligible"
    assert "master" in str(row["eligibility_reason"])
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
        UPDATE jobs SET pipeline_status = 'manual_review', resume_path = ?, apply_attempts = 1
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
            {
                "key": "email_verification_code",
                "label": "8-character email verification code",
                "input_type": "verification_code",
                "options": [],
                "required": True,
            },
            {
                "key": "account_password",
                "label": "Account password",
                "input_type": "verification_code",
                "options": [],
                "required": True,
            },
        ],
    )

    assert [item["input_key"] for item in saved] == [
        "preferred_team",
        "email_verification_code",
    ]
    job = answer_agent_inputs(
        connection,
        1,
        {"preferred_team": "Platform", "email_verification_code": "A1B2C3D4"},
    )

    assert job is not None
    assert job["pipeline_status"] == "ready"
    assert job["manual_requested"] == 1
    assert answered_agent_inputs(connection, 1)["preferred_team"]["answer"] == "Platform"
    assert answered_agent_inputs(connection, 1)["email_verification_code"]["answer"] == "A1B2C3D4"

    assert clear_ephemeral_agent_inputs(connection, 1) == 1
    assert "email_verification_code" not in answered_agent_inputs(connection, 1)
    verification = next(
        item
        for item in list_agent_inputs(connection, 1)
        if item["input_key"] == "email_verification_code"
    )
    assert verification["answer"] is None
    assert verification["status"] == "resolved"

    assert resume_application_after_input(connection, 1, "worker-0") is True
    resumed = get_job(connection, 1)
    assert resumed is not None
    assert resumed["pipeline_status"] == "applying"
    assert resumed["worker_id"] == "worker-0"
    assert resumed["manual_requested"] == 0
    assert resumed["apply_attempts"] == 1
    close_connection(path)


def test_review_ready_form_can_be_confirmed_and_resumed_on_same_worker(
    tmp_path, profile, settings
) -> None:
    path = tmp_path / "submission-confirmation.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    listing = make_listing(source, "Acme", "Software Intern", "https://jobs.test/submit")
    ingest_listings(connection, source, [listing], profile=profile, settings=settings)
    connection.execute(
        """
        UPDATE jobs SET pipeline_status = 'applying', worker_id = 'worker-0',
                        apply_origin = 'manual', apply_attempts = 1
        WHERE id = 1
        """
    )
    connection.commit()
    mark_apply_result(
        connection,
        1,
        "review_ready",
        "All required fields are complete",
        retain_worker=True,
    )
    update_worker_state(
        connection,
        "worker-0",
        status="review_ready",
        job=get_job(connection, 1),
        message="Confirm Submit",
    )

    confirmed = request_final_submission(connection, 1)
    assert confirmed is not None
    assert confirmed["submission_requested"] == 1
    assert resume_application_for_submission(connection, 1, "worker-0") is True

    resumed = get_job(connection, 1)
    assert resumed is not None
    assert resumed["pipeline_status"] == "applying"
    assert resumed["worker_id"] == "worker-0"
    assert resumed["apply_attempts"] == 1
    assert resumed["submission_requested"] == 0
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


def test_upgrading_drops_the_retired_fit_score_columns(tmp_path, profile, settings) -> None:
    path = tmp_path / "retired-columns.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    ingest_listings(
        connection,
        source,
        [make_listing(source, "Acme", "Software Intern", "https://jobs.test/1")],
        profile=profile,
        settings=settings,
        include_existing=True,
    )
    # Recreate the pre-review shape: the three score columns plus the index that
    # covered one of them, which has to be dropped before the columns can be.
    connection.executescript(
        """
        ALTER TABLE jobs ADD COLUMN fit_score INTEGER;
        ALTER TABLE jobs ADD COLUMN score_reasoning TEXT;
        ALTER TABLE jobs ADD COLUMN scored_at TEXT;
        UPDATE jobs SET fit_score = 6, score_reasoning = 'stale heuristic';
        CREATE INDEX idx_jobs_pipeline ON jobs(pipeline_status, fit_score);
        """
    )
    connection.commit()
    close_connection(path)

    connection = init_db(path)

    columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}
    assert not columns & {"fit_score", "score_reasoning", "scored_at"}
    assert {"apply_decision", "apply_headline", "review_signature"} <= columns
    assert connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'idx_jobs_pipeline'"
    ).fetchone() is None
    # The listing itself survives the column drop.
    job = get_job(connection, 1)
    assert job["company"] == "Acme"
    assert job["apply_decision"] is None
    close_connection(path)


def test_an_old_sqlite_leaves_retired_columns_in_place_instead_of_failing(
    tmp_path, profile, settings, monkeypatch
) -> None:
    path = tmp_path / "old-sqlite.db"
    connection = init_db(path)
    connection.execute("ALTER TABLE jobs ADD COLUMN fit_score INTEGER")
    connection.commit()
    close_connection(path)
    monkeypatch.setattr(database.sqlite3, "sqlite_version_info", (3, 34, 0))

    connection = init_db(path)

    columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}
    assert "fit_score" in columns, "an old SQLite keeps the column rather than erroring"
    assert "apply_decision" in columns, "the upgrade still completes"
    close_connection(path)


def test_company_history_separates_submitted_from_unsubmitted_attempts(
    tmp_path, profile, settings
) -> None:
    path = tmp_path / "company-history.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    ingest_listings(
        connection,
        source,
        [
            make_listing(source, "ByteDance", "Backend Intern", "https://jobs.test/bd-1"),
            make_listing(source, "ByteDance", "ML Intern", "https://jobs.test/bd-2"),
            make_listing(source, "ByteDance", "Infra Intern", "https://jobs.test/bd-3"),
            make_listing(source, "ByteDance", "Data Intern", "https://jobs.test/bd-4"),
            make_listing(source, "Other Co", "Software Intern", "https://jobs.test/other"),
        ],
        profile=profile,
        settings=settings,
        include_existing=True,
    )
    connection.execute(
        """
        UPDATE jobs SET pipeline_status = 'applied',
                        applied_at = '2026-08-10T00:00:00+00:00'
        WHERE id = 1
        """
    )
    connection.execute("UPDATE jobs SET pipeline_status = 'manual_review' WHERE id = 2")
    connection.execute("UPDATE jobs SET pipeline_status = 'applying' WHERE id = 3")
    # Handed back to the candidate; the agent will never file it.
    connection.execute(
        """
        UPDATE jobs SET pipeline_status = 'manual_review',
                        availability_status = 'manual_only'
        WHERE id = 4
        """
    )
    connection.execute(
        """
        UPDATE jobs SET pipeline_status = 'applied',
                        applied_at = '2026-08-11T00:00:00+00:00'
        WHERE id = 5
        """
    )
    connection.commit()

    history = company_application_history(connection, "ByteDance")

    assert history["submitted"] == 1, "only one ByteDance application was actually sent"
    assert history["in_progress"] == 2
    assert history["used"] == 3, "in-flight attempts still reserve a slot"
    assert len(history["submitted_entries"]) == 1
    assert "submitted on 2026-08-10" in history["submitted_entries"][0]
    assert any("Submit confirmation" in line for line in history["in_progress_entries"])
    assert any("filling this in now" in line for line in history["in_progress_entries"])
    assert not any("Data Intern" in line for line in history["in_progress_entries"])
    # Another company's application never counts against this one.
    assert not any("Software Intern" in line for line in history["submitted_entries"])
    close_connection(path)


def test_reviewable_listings_skip_the_backlog_outside_the_age_window(
    tmp_path, profile, settings
) -> None:
    path = tmp_path / "age-window.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    ingest_listings(
        connection,
        source,
        [
            make_listing(source, "Fresh Co", "Backend Intern", "https://jobs.test/fresh"),
            make_listing(source, "Backlog Co", "Old Intern", "https://jobs.test/old"),
        ],
        profile=profile,
        settings=settings,
        include_existing=True,
    )
    connection.execute("UPDATE jobs SET posting_date = '2026-01-05' WHERE id = 2")
    connection.commit()

    assert len(reviewable_listings(connection)) == 2, "0 means no age limit"
    within = reviewable_listings(connection, max_age_days=2)
    assert [row["company"] for row in within] == ["Fresh Co"]
    # An explicit request still reaches the old listing.
    assert len(reviewable_listings(connection, target_job_id=2)) == 1
    close_connection(path)


def test_reviewable_listings_can_select_one_first_seen_day(tmp_path, profile, settings) -> None:
    path = tmp_path / "first-seen-window.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    ingest_listings(
        connection,
        source,
        [
            make_listing(source, "Start Co", "Backend Intern", "https://jobs.test/start"),
            make_listing(source, "Inside Co", "ML Intern", "https://jobs.test/inside"),
            make_listing(source, "End Co", "Data Intern", "https://jobs.test/end"),
        ],
        profile=profile,
        settings=settings,
        include_existing=True,
    )
    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    connection.executemany(
        "UPDATE jobs SET first_seen_at = ? WHERE id = ?",
        [
            (start.isoformat(), 1),
            ((end - timedelta(seconds=1)).isoformat(), 2),
            (end.isoformat(), 3),
        ],
    )
    connection.execute("UPDATE jobs SET apply_decision = 'apply' WHERE id = 2")
    connection.commit()

    rows = reviewable_listings(
        connection,
        first_seen_from=start.isoformat(),
        first_seen_before=end.isoformat(),
    )

    assert {row["company"] for row in rows} == {"Start Co", "Inside Co"}
    undecided = reviewable_listings(
        connection,
        first_seen_from=start.isoformat(),
        first_seen_before=end.isoformat(),
        undecided_only=True,
    )
    assert [row["company"] for row in undecided] == ["Start Co"]
    close_connection(path)


def test_listing_age_cutoff_counts_back_from_the_reference_day() -> None:
    assert listing_age_cutoff(2, today="2026-08-16") == "2026-08-14"
    assert listing_age_cutoff(0, today="2026-08-16") == "2026-08-16"
