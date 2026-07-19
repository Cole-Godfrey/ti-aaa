from __future__ import annotations

from tiaaa.config import SOURCE_DOCUMENTS
from tiaaa.database import (
    claim_next_job,
    claimable_application_count,
    close_connection,
    get_stats,
    ingest_listings,
    init_db,
    list_jobs,
    mark_apply_result,
    update_tracker,
)
from tiaaa.models import InternshipListing


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


def test_first_sync_is_baseline_and_later_addition_is_queued(tmp_path, profile, settings) -> None:
    path = tmp_path / "tracker.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    first = make_listing(source, "Acme", "Software Engineer Intern", "https://jobs.test/acme")
    second = make_listing(source, "Beta", "Backend Intern", "https://jobs.test/beta")

    baseline = ingest_listings(
        connection, source, [first], profile=profile, settings=settings
    )
    update = ingest_listings(
        connection, source, [first, second], profile=profile, settings=settings
    )

    assert baseline["baseline"] is True
    assert baseline["queued"] == 0
    assert update["baseline"] is False
    assert update["queued"] == 1
    rows = {row["company"]: row for row in list_jobs(connection)}
    assert rows["Acme"]["pipeline_status"] == "discovered"
    assert rows["Beta"]["pipeline_status"] == "queued"
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
