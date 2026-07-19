from __future__ import annotations

from fastapi.testclient import TestClient

from tiaaa.config import SOURCE_DOCUMENTS
from tiaaa.dashboard.app import create_app
from tiaaa.database import get_connection, ingest_listings, init_db
from tiaaa.models import InternshipListing


def test_dashboard_stats_jobs_and_tracker_updates(tmp_path, profile, settings) -> None:
    path = tmp_path / "dashboard.db"
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    job = InternshipListing(
        company="Acme",
        role="Software Engineer Intern",
        location="Remote",
        application_url="https://jobs.test/1",
        source_key=source.key,
        source_label=source.label,
        source_repo_url=source.repo_url,
        source_path=source.path,
    )
    ingest_listings(
        connection, source, [job], profile=profile, settings=settings, include_existing=True
    )

    client = TestClient(create_app(path))
    assert client.get("/").status_code == 200
    jobs = client.get("/api/jobs").json()["items"]
    assert jobs[0]["company"] == "Acme"
    assert client.patch("/api/jobs/1", json={"pipeline_status": "applied"}).status_code == 200
    assert client.patch("/api/jobs/1", json={"outcome_status": "oa"}).status_code == 200
    stats = client.get("/api/stats").json()
    assert stats["applications"] == 1
    assert stats["oa_rate"] == 100.0
    health = client.get("/api/health")
    assert health.json()["status"] == "ok"
    assert health.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in health.headers["content-security-policy"]
    assert get_connection(path).execute("SELECT outcome_status FROM jobs").fetchone()[0] == "oa"
