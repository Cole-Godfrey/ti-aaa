from __future__ import annotations

import io

from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas

from tiaaa.config import SOURCE_DOCUMENTS, AppPaths
from tiaaa.dashboard.app import create_app
from tiaaa.database import get_connection, ingest_listings, init_db, update_worker_state
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


def test_web_onboarding_stores_write_only_keys_and_resume(tmp_path, profile, monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    paths = AppPaths(tmp_path)
    path = tmp_path / "tiaaa.db"
    client = TestClient(create_app(path, paths=paths))
    secret = "sk-ant-test-secret-1234"

    response = client.put(
        "/api/config",
        json={"profile": profile, "secrets": {"ANTHROPIC_API_KEY": secret}},
    )
    assert response.status_code == 200
    assert secret not in response.text
    assert response.json()["secrets"]["ANTHROPIC_API_KEY"] == {
        "configured": True,
        "suffix": "1234",
    }
    assert secret in paths.env.read_text(encoding="utf-8")
    assert paths.env.stat().st_mode & 0o077 == 0
    cleared = client.put(
        "/api/config", json={"clear_secrets": ["ANTHROPIC_API_KEY"]}
    )
    assert cleared.json()["secrets"]["ANTHROPIC_API_KEY"]["configured"] is False
    assert secret not in paths.env.read_text(encoding="utf-8")

    buffer = io.BytesIO()
    document = canvas.Canvas(buffer)
    document.drawString(72, 760, "Avery Student")
    document.drawString(72, 735, "Built a Python API for a university project using Docker")
    document.save()
    upload = client.post(
        "/api/resumes",
        data={"name": "Backend", "tags": "backend, python"},
        files={"file": ("resume.pdf", buffer.getvalue(), "application/pdf")},
    )
    assert upload.status_code == 201, upload.text
    assert "pdf_path" not in upload.json()
    assert client.get("/api/resumes").json()["items"][0]["name"] == "Backend"

    finish = client.put("/api/config", json={"onboarding_complete": True})
    assert finish.status_code == 200
    assert client.get("/api/onboarding").json()["complete"] is True
    assert client.delete("/api/resumes/1").status_code == 409


def test_live_worker_preview_is_served_only_from_preview_directory(tmp_path) -> None:
    paths = AppPaths(tmp_path)
    paths.previews.mkdir(parents=True)
    preview = paths.previews / "worker-0.jpg"
    preview.write_bytes(b"\xff\xd8\xff\xd9")
    connection = init_db(paths.database)
    update_worker_state(
        connection,
        "worker-0",
        status="applying",
        message="Filling the application",
        screenshot_path=str(preview),
    )
    client = TestClient(create_app(paths.database, paths=paths))

    workers = client.get("/api/workers").json()["items"]
    assert workers[0]["preview_available"] is True
    response = client.get("/api/workers/worker-0/preview")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert client.get("/api/workers/../../profile/preview").status_code == 404
