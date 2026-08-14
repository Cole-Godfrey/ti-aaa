from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

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
    live_human_interaction_checkpoint,
    live_submission_checkpoint,
    manual_application_ids,
    mark_applied_manually,
    mark_apply_result,
    mark_prepared,
    profile_facts_changed,
    reconcile_source_registry,
    recover_stale_work,
    refresh_qualification_scores,
    request_agent_stop,
    request_final_submission,
    request_human_control_return,
    request_manual_application,
    resume_application_after_human_control,
    resume_application_after_input,
    resume_application_for_submission,
    retry_manual_application,
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


def test_auto_apply_fit_limit_blocks_low_fit_jobs_but_manual_apply_bypasses_it(
    tmp_path, profile, settings
) -> None:
    path = tmp_path / "auto-fit-limit.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    jobs = [
        make_listing(source, "Low Fit", "Software Intern", "https://jobs.test/low"),
        make_listing(source, "High Fit", "Backend Intern", "https://jobs.test/high"),
    ]
    ingest_listings(
        connection, source, jobs, profile=profile, settings=settings, include_existing=True
    )
    connection.execute(
        """
        UPDATE jobs SET pipeline_status = 'ready', eligibility = 'eligible',
                        discovered_as_new = 1,
                        fit_score = CASE WHEN company = 'Low Fit' THEN 6 ELSE 8 END
        """
    )
    connection.commit()

    assert claimable_application_count(
        connection, max_attempts=3, minimum_fit_score=7
    ) == 1
    automatic = claim_next_job(
        connection,
        worker_id="worker-0",
        max_attempts=3,
        minimum_fit_score=7,
    )
    assert automatic is not None
    assert automatic["company"] == "High Fit"

    manual = claim_next_job(
        connection,
        worker_id="worker-1",
        max_attempts=3,
        target_job_id=1,
        minimum_fit_score=10,
    )
    assert manual is not None
    assert manual["company"] == "Low Fit"
    close_connection(path)


def test_auto_apply_claims_only_the_best_fit_role_per_company(
    tmp_path, profile, settings
) -> None:
    path = tmp_path / "company-best-fit.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    listings = [
        make_listing(source, "Acme", "Backend Intern", "https://jobs.test/acme-backend"),
        make_listing(source, "Acme", "ML Intern", "https://jobs.test/acme-ml"),
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
    connection.execute(
        """
        UPDATE jobs SET pipeline_status = 'ready', eligibility = 'eligible',
                        discovered_as_new = 1,
                        fit_score = CASE role
                            WHEN 'ML Intern' THEN 9
                            WHEN 'Backend Intern' THEN 8
                            ELSE 7 END
        """
    )
    connection.commit()

    assert claimable_application_count(
        connection, max_attempts=3, minimum_fit_score=7
    ) == 2
    acme = claim_next_job(
        connection,
        worker_id="worker-0",
        max_attempts=3,
        minimum_fit_score=7,
    )
    assert acme is not None
    assert acme["company"] == "Acme"
    assert acme["role"] == "ML Intern"
    assert acme["apply_origin"] == "auto"
    sibling = get_job(connection, 1)
    assert sibling is not None
    assert sibling["pipeline_status"] == "skipped"
    assert sibling["apply_reason_code"] == "company_role_deduplicated"

    beta = claim_next_job(
        connection,
        worker_id="worker-1",
        max_attempts=3,
        minimum_fit_score=7,
    )
    assert beta is not None
    assert beta["company"] == "Beta"
    assert claim_next_job(
        connection,
        worker_id="worker-2",
        max_attempts=3,
        minimum_fit_score=7,
    ) is None
    close_connection(path)


def test_application_queue_keeps_batch_order_and_one_role_per_company(
    tmp_path, profile, settings
) -> None:
    path = tmp_path / "application-queue.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    listings = [
        make_listing(source, "Acme", "Backend Intern", "https://jobs.test/acme-backend"),
        make_listing(source, "Acme", "ML Intern", "https://jobs.test/acme-ml"),
        make_listing(source, "Beta", "Software Intern", "https://jobs.test/beta"),
        make_listing(source, "Low Fit", "IT Intern", "https://jobs.test/low"),
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
                        fit_score = CASE role
                            WHEN 'ML Intern' THEN 9
                            WHEN 'Backend Intern' THEN 8
                            WHEN 'Software Intern' THEN 7
                            ELSE 6 END
        """
    )
    connection.commit()

    waiting = list_application_queue(
        connection,
        auto_enabled=True,
        max_attempts=3,
        minimum_fit_score=7,
        profile=profile,
    )

    assert [(item["company"], item["role"]) for item in waiting] == [
        ("Acme", "ML Intern"),
        ("Beta", "Software Intern"),
    ]
    assert [item["position"] for item in waiting] == [1, 2]
    assert all(item["queue_state"] == "preparing" for item in waiting)

    connection.execute("UPDATE jobs SET pipeline_status = 'ready' WHERE fit_score >= 7")
    connection.commit()
    claimed = claim_next_job(
        connection,
        worker_id="worker-0",
        max_attempts=3,
        minimum_fit_score=7,
    )
    assert claimed is not None
    active_queue = list_application_queue(
        connection,
        auto_enabled=True,
        max_attempts=3,
        minimum_fit_score=7,
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
                        discovered_as_new = 1, fit_score = 8
        """
    )
    connection.commit()

    assert claimable_application_count(
        connection,
        max_attempts=3,
        minimum_fit_score=7,
        profile=profile,
        use_preferences=False,
    ) == 2
    assert claimable_application_count(
        connection,
        max_attempts=3,
        minimum_fit_score=7,
        profile=profile,
        use_preferences=True,
    ) == 1
    claimed = claim_next_job(
        connection,
        worker_id="worker-0",
        max_attempts=3,
        minimum_fit_score=7,
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
                        discovered_as_new = 1, fit_score = 9,
                        apply_origin = 'auto', apply_attempts = 1,
                        apply_reason_code = 'missing_input',
                        apply_error = 'Required address is unavailable'
        """
    )
    connection.commit()

    assert claimable_application_count(
        connection,
        max_attempts=3,
        minimum_fit_score=7,
    ) == 0
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
    refresh_qualification_scores(connection, profile=profile, settings=settings)

    row = get_job(connection, 1)
    assert row is not None
    assert row["eligibility"] == "ineligible"
    assert row["eligibility_reason"] == detail
    assert row["pipeline_status"] == "failed"
    assert row["apply_reason_code"] == "eligibility_conflict"
    assert claimable_application_count(connection, max_attempts=3) == 0
    close_connection(path)


def test_qualification_refresh_preserves_llm_fit_while_updating_hard_gate(
    tmp_path, profile, settings
) -> None:
    path = tmp_path / "qualification-refresh.db"
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
        """
        UPDATE jobs SET eligibility = 'eligible', eligibility_reason = 'old',
                        fit_score = 9, score_reasoning = 'LLM score',
                        scored_at = '2026-08-10T00:00:00+00:00'
        WHERE id = 1
        """
    )
    connection.commit()

    refresh_qualification_scores(
        connection,
        profile=profile,
        settings=settings,
        preserve_scores=True,
    )

    row = get_job(connection, 1)
    assert row is not None
    assert row["eligibility"] == "ineligible"
    assert row["eligibility_reason"] == (
        "requires a doctoral degree not present in the profile"
    )
    assert row["fit_score"] == 9
    assert row["score_reasoning"] == "LLM score"
    assert row["scored_at"] == "2026-08-10T00:00:00+00:00"
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
                        apply_attempts = 1, resume_path = 'resume.pdf'
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
    assert claimable_application_count(connection, max_attempts=3, minimum_fit_score=1) == 0

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
    assert claimable_application_count(connection, max_attempts=3, minimum_fit_score=1) == 1
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
        connection, auto_enabled=True, max_attempts=3, minimum_fit_score=1
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
    refresh_qualification_scores(connection, profile=profile, settings=settings)
    assert get_job(connection, 1)["eligibility"] == "eligible"
    close_connection(path)


def test_agent_discovered_conflict_lowers_the_heuristic_fit_score(
    tmp_path, profile, settings
) -> None:
    path = tmp_path / "conflict-fit.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    listing = make_listing(source, "Acme", "Software Intern", "https://jobs.test/conflict")
    ingest_listings(connection, source, [listing], profile=profile, settings=settings)
    assert get_job(connection, 1)["fit_score"] >= 7
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

    refresh_qualification_scores(connection, profile=profile, settings=settings)

    row = get_job(connection, 1)
    assert row["eligibility"] == "ineligible"
    assert row["fit_score"] <= 2
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
