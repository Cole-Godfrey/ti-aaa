from __future__ import annotations

import io
import threading

import pytest
from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas
from starlette.websockets import WebSocketDisconnect

from tiaaa.apply.preview import browser_control_hub, preview_frame_hub
from tiaaa.config import SOURCE_DOCUMENTS, AppPaths, save_settings
from tiaaa.dashboard.app import create_app
from tiaaa.database import (
    answer_agent_inputs,
    get_connection,
    get_job,
    ingest_listings,
    init_db,
    mark_apply_result,
    request_final_submission,
    request_human_control_return,
    request_manual_application,
    retry_manual_application,
    set_app_state,
    store_agent_inputs,
    update_tracker,
    update_worker_state,
)
from tiaaa.models import InternshipListing
from tiaaa.resumes import store_resume


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
    assert client.get("/api/jobs?view=applications").json()["items"] == []
    assert client.patch("/api/jobs/1", json={"pipeline_status": "applied"}).status_code == 200
    assert [
        item["company"]
        for item in client.get("/api/jobs?view=applications").json()["items"]
    ] == ["Acme"]
    assert client.patch("/api/jobs/1", json={"outcome_status": "oa"}).status_code == 200
    stats = client.get("/api/stats").json()
    assert stats["applications"] == 1
    assert stats["oa_rate"] == 100.0
    analytics = client.get("/api/analytics").json()
    assert analytics["summary"]["applications"] == 1
    assert analytics["dimensions"]["role_family"][0]["label"] == "Software engineering"
    health = client.get("/api/health")
    assert health.json()["status"] == "ok"
    assert health.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in health.headers["content-security-policy"]
    assert get_connection(path).execute("SELECT outcome_status FROM jobs").fetchone()[0] == "oa"


def test_dashboard_blocks_untrusted_hosts_and_cross_origin_writes(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "dashboard.db"))

    assert client.get("/api/health", headers={"host": "attacker.example"}).status_code == 400
    cross_origin = client.post(
        "/api/dashboard/visit",
        headers={
            "origin": "https://attacker.example",
            "sec-fetch-site": "cross-site",
        },
    )
    assert cross_origin.status_code == 403

    same_origin = client.post(
        "/api/dashboard/visit",
        headers={"origin": "http://testserver", "sec-fetch-site": "same-origin"},
    )
    assert same_origin.status_code == 200
    assert same_origin.headers["cache-control"] == "no-store"


def test_dashboard_blocks_cross_origin_worker_websockets(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "dashboard.db"))

    with pytest.raises(WebSocketDisconnect) as error, client.websocket_connect(
        "/api/workers/worker-0/stream",
        headers={
            "origin": "https://attacker.example",
            "sec-fetch-site": "cross-site",
        },
    ):
        pass
    assert error.value.code == 1008


def test_config_api_stores_write_only_keys_and_web_onboarding_resume(
    tmp_path, profile, monkeypatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    paths = AppPaths(tmp_path)
    path = tmp_path / "tiaaa.db"
    client = TestClient(create_app(path, paths=paths))
    secret = "sk-ant-test-secret-1234"
    profile["personal"].update(
        {
            "address": "123 Pine Street",
            "address_line_2": "Apt 4",
            "city": "Seattle",
            "state": "WA",
            "county": "King County",
            "postal_code": "98101",
            "country": "United States",
        }
    )
    profile["experience"] = {"previous_internship_companies": ["Acme"]}

    response = client.put(
        "/api/config",
        json={
            "profile": profile,
            "secrets": {"ANTHROPIC_API_KEY": secret},
        },
    )
    assert response.status_code == 200
    assert secret not in response.text
    assert response.json()["profile"]["personal"]["address_line_2"] == "Apt 4"
    assert response.json()["profile"]["personal"]["county"] == "King County"
    assert response.json()["profile"]["experience"][
        "previous_internship_companies"
    ] == ["Acme"]
    assert response.json()["secrets"]["ANTHROPIC_API_KEY"] == {
        "configured": True,
        "suffix": "1234",
    }
    assert secret in paths.env.read_text(encoding="utf-8")
    assert paths.env.stat().st_mode & 0o077 == 0
    cleared = client.put(
        "/api/config",
        json={"clear_secrets": ["ANTHROPIC_API_KEY"]},
    )
    assert cleared.json()["secrets"]["ANTHROPIC_API_KEY"]["configured"] is False
    assert secret not in paths.env.read_text(encoding="utf-8")
    retired_password = client.put(
        "/api/config",
        json={"secrets": {"TIAAA_APPLICATION_PASSWORD": "do-not-store"}},
    )
    assert retired_password.status_code == 422
    assert "do-not-store" not in paths.env.read_text(encoding="utf-8")

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


def test_dashboard_visit_reports_applications_since_the_prior_visit(
    tmp_path, profile, settings
) -> None:
    paths = AppPaths(tmp_path)
    connection = init_db(paths.database)
    source = SOURCE_DOCUMENTS[0]
    listing = InternshipListing(
        company="Acme",
        role="Software Engineer Intern",
        location="Remote",
        application_url="https://jobs.test/welcome",
        source_key=source.key,
        source_label=source.label,
        source_repo_url=source.repo_url,
        source_path=source.path,
    )
    failed_listing = InternshipListing(
        company="Blocked Co",
        role="Platform Intern",
        location="Remote",
        application_url="https://jobs.test/welcome-failed",
        source_key=source.key,
        source_label=source.label,
        source_repo_url=source.repo_url,
        source_path=source.path,
    )
    ingest_listings(
        connection,
        source,
        [listing, failed_listing],
        profile=profile,
        settings=settings,
        include_existing=True,
    )
    client = TestClient(create_app(paths.database, paths=paths))

    first = client.post("/api/dashboard/visit").json()
    assert first["first_visit"] is True
    assert first["applications"] == []

    update_tracker(connection, 1, pipeline_status="applied")
    connection.execute(
        """
        UPDATE jobs SET pipeline_status = 'applying', apply_origin = 'auto',
                        last_attempted_at = updated_at
        WHERE id = 2
        """
    )
    connection.commit()
    mark_apply_result(
        connection,
        2,
        "failed",
        "Required home address is not configured",
        reason_code="missing_input",
        manual_handoff=False,
    )
    second = client.post("/api/dashboard/visit").json()
    assert second["first_visit"] is False
    assert second["application_count"] == 1
    assert second["applications"][0]["company"] == "Acme"
    assert second["failure_count"] == 1
    assert second["failures"][0]["company"] == "Blocked Co"
    assert second["failures"][0]["apply_reason_code"] == "missing_input"

    third = client.post("/api/dashboard/visit").json()
    assert third["applications"] == []
    assert third["failures"] == []


def test_live_worker_preview_is_served_only_from_preview_directory(tmp_path) -> None:
    preview_frame_hub.clear()
    browser_control_hub.clear()
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
    preview_frame_hub.set_active("worker-0", True)
    client = TestClient(create_app(paths.database, paths=paths))

    workers = client.get("/api/workers").json()["items"]
    assert workers[0]["preview_available"] is True
    assert workers[0]["stream_active"] is True
    response = client.get("/api/workers/worker-0/preview")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert client.get("/api/workers/../../profile/preview").status_code == 404
    preview_frame_hub.publish("worker-0", b"\xff\xd8stream\xff\xd9")
    with client.websocket_connect("/api/workers/worker-0/stream") as websocket:
        assert websocket.receive_bytes() == b"\xff\xd8stream\xff\xd9"
    preview_frame_hub.clear()
    browser_control_hub.clear()


def test_captcha_checkpoint_relays_input_and_returns_the_same_browser_to_agent(
    tmp_path, profile, settings
) -> None:
    preview_frame_hub.clear()
    browser_control_hub.clear()
    paths = AppPaths(tmp_path)
    paths.previews.mkdir(parents=True)
    preview_path = paths.previews / "worker-0.jpg"
    preview_path.write_bytes(b"\xff\xd8\xff\xd9")
    connection = init_db(paths.database)
    source = SOURCE_DOCUMENTS[0]
    listing = InternshipListing(
        company="Capula",
        role="Trading and Research Intern",
        location="London",
        application_url="https://jobs.test/capula",
        source_key=source.key,
        source_label=source.label,
        source_repo_url=source.repo_url,
        source_path=source.path,
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
        "Submit remained disabled on Submitting with no confirmation",
        reason_code="captcha",
        retain_worker=True,
    )
    update_worker_state(
        connection,
        "worker-0",
        status="captcha",
        job=get_job(connection, 1),
        message="Human control enabled",
        screenshot_path=str(preview_path),
    )

    class FakeController:
        def __init__(self) -> None:
            self.actions: list[dict] = []
            self.received = threading.Event()

        def enqueue_control(self, action):
            self.actions.append(action)
            self.received.set()

    controller = FakeController()
    browser_control_hub.register("worker-0", controller)
    preview_frame_hub.set_active("worker-0", True)

    class FakeService:
        def return_browser_control(self, job_id):
            return request_human_control_return(connection, job_id)

    app = create_app(paths.database, paths=paths)
    app.state.service = FakeService()
    client = TestClient(app)

    worker = client.get("/api/workers").json()["items"][0]
    assert worker["browser_interactive"] is True
    assert worker["pipeline_status"] == "manual_review"

    preview_frame_hub.publish("worker-0", b"\xff\xd8interactive\xff\xd9")
    with client.websocket_connect("/api/workers/worker-0/stream") as websocket:
        assert websocket.receive_bytes() == b"\xff\xd8interactive\xff\xd9"
        websocket.send_json({"type": "click", "x": 0.5, "y": 0.25})
        assert controller.received.wait(timeout=1)

    assert controller.actions == [{"type": "click", "x": 0.5, "y": 0.25}]
    response = client.post("/api/jobs/1/continue-agent")
    assert response.status_code == 202
    assert response.json()["status"] == "continuing"
    assert get_job(connection, 1)["human_control_returned"] == 1
    assert client.get("/api/workers").json()["items"][0]["browser_interactive"] is False

    browser_control_hub.unregister("worker-0", controller)
    preview_frame_hub.clear()


def test_latest_jobs_detail_and_manual_apply_action(tmp_path, profile, settings) -> None:
    paths = AppPaths(tmp_path)
    connection = init_db(paths.database)
    source = SOURCE_DOCUMENTS[0]
    older = InternshipListing(
        company="Older Co",
        role="Software Intern",
        location="Remote",
        application_url="https://jobs.test/older",
        source_key=source.key,
        source_label=source.label,
        source_repo_url=source.repo_url,
        source_path=source.path,
        posting_date="2026-06-01",
    )
    latest = InternshipListing(
        company="Latest Co",
        role="Backend Intern",
        location="Seattle",
        application_url="https://jobs.test/latest",
        source_key=source.key,
        source_label=source.label,
        source_repo_url=source.repo_url,
        source_path=source.path,
        posting_date="2026-07-20",
    )
    ingest_listings(
        connection, source, [older, latest], profile=profile, settings=settings
    )
    set_app_state(connection, "onboarding_complete", True)
    buffer = io.BytesIO()
    document = canvas.Canvas(buffer)
    document.drawString(72, 760, "Avery Student - Python and backend projects")
    document.save()
    store_resume(
        paths=paths,
        name="General",
        original_filename="resume.pdf",
        content=buffer.getvalue(),
        text_override="Avery Student - Python and backend projects",
        db_path=paths.database,
    )

    class ConnectedAuth:
        def status(self):
            return {"logged_in": True}

    class FakeService:
        requested: list[int] = []

        def request_application(self, job_id):
            self.requested.append(job_id)
            return request_manual_application(connection, job_id)

    app = create_app(paths.database, paths=paths)
    service = FakeService()
    app.state.claude_auth = ConnectedAuth()
    app.state.service = service
    client = TestClient(app)

    rows = client.get("/api/jobs?view=latest").json()["items"]
    assert [row["company"] for row in rows[:2]] == ["Latest Co", "Older Co"]
    detail = client.get(f"/api/jobs/{rows[0]['id']}").json()
    assert detail["application_mode"] == "manual_confirm"
    assert detail["source_labels"] == source.label
    response = client.post(f"/api/jobs/{rows[0]['id']}/apply")
    assert response.status_code == 202
    assert response.json()["mode"] == "manual_confirm"
    assert service.requested == [rows[0]["id"]]
    queue = client.get("/api/workers").json()
    assert queue["queue_summary"] == {"serial": True, "active": 0, "waiting": 1}
    assert queue["queue"][0]["id"] == rows[0]["id"]
    assert queue["queue"][0]["origin"] == "manual"

    settings["automation"]["manual_auto_submit"] = True
    save_settings(settings, paths)
    older_detail = client.get(f"/api/jobs/{rows[1]['id']}").json()
    assert older_detail["application_mode"] == "manual_auto_submit"
    auto_submit_response = client.post(f"/api/jobs/{rows[1]['id']}/apply")
    assert auto_submit_response.status_code == 202
    assert auto_submit_response.json()["mode"] == "manual_auto_submit"
    assert service.requested == [rows[0]["id"], rows[1]["id"]]


def test_latest_jobs_can_retry_reviews_for_the_browser_day(tmp_path) -> None:
    paths = AppPaths(tmp_path)
    seen: list[str] = []

    class FakeService:
        def review_todays_listings(self, timezone_name):
            seen.append(timezone_name)
            return {
                "date": "2026-09-01",
                "review": {
                    "reviewed": 4,
                    "companies": 2,
                    "apply": 2,
                    "skip": 2,
                    "errors": 0,
                    "selected": 4,
                    "status": "complete",
                },
            }

    app = create_app(paths.database, paths=paths)
    app.state.service = FakeService()
    client = TestClient(app)

    response = client.post(
        "/api/reviews/today/retry", json={"timezone": "America/Los_Angeles"}
    )

    assert response.status_code == 200
    assert response.json()["review"]["reviewed"] == 4
    assert seen == ["America/Los_Angeles"]


def test_agent_page_accepts_requested_input_and_requeues_job(
    tmp_path, profile, settings
) -> None:
    paths = AppPaths(tmp_path)
    paths.previews.mkdir(parents=True)
    connection = init_db(paths.database)
    source = SOURCE_DOCUMENTS[0]
    listing = InternshipListing(
        company="Acme",
        role="Software Engineer Intern",
        location="Remote",
        application_url="https://jobs.test/input",
        source_key=source.key,
        source_label=source.label,
        source_repo_url=source.repo_url,
        source_path=source.path,
    )
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
                "label": "Which team do you prefer?",
                "input_type": "select",
                "options": ["Platform", "Product"],
                "required": True,
            },
            {
                "key": "email_verification_code",
                "label": "Email verification code",
                "input_type": "verification_code",
                "options": [],
                "required": True,
            },
        ],
    )
    update_worker_state(
        connection,
        "worker-0",
        status="idle",
        job=get_job(connection, 1),
        message="Application needs your input",
    )

    class FakeService:
        def continue_application(self, job_id, answers):
            return answer_agent_inputs(connection, job_id, answers)

    app = create_app(paths.database, paths=paths)
    app.state.service = FakeService()
    client = TestClient(app)

    worker = client.get("/api/workers").json()["items"][0]
    assert worker["pipeline_status"] == "manual_review"
    assert worker["questions"][0]["input_key"] == "preferred_team"
    assert worker["questions"][1]["input_type"] == "verification_code"
    response = client.post(
        "/api/jobs/1/inputs",
        json={
            "answers": {
                "preferred_team": "Platform",
                "email_verification_code": "A1B2C3D4",
            }
        },
    )

    assert response.status_code == 202
    assert response.json()["job"]["pipeline_status"] == "ready"
    assert get_job(connection, 1)["manual_requested"] == 1


def test_agent_page_confirms_submission_on_the_live_completed_form(
    tmp_path, profile, settings
) -> None:
    paths = AppPaths(tmp_path)
    connection = init_db(paths.database)
    source = SOURCE_DOCUMENTS[0]
    listing = InternshipListing(
        company="Acme",
        role="Software Engineer Intern",
        location="Remote",
        application_url="https://jobs.test/submit",
        source_key=source.key,
        source_label=source.label,
        source_repo_url=source.repo_url,
        source_path=source.path,
    )
    ingest_listings(connection, source, [listing], profile=profile, settings=settings)
    connection.execute(
        """
        UPDATE jobs SET pipeline_status = 'manual_review', worker_id = 'worker-0',
                        resume_path = ?
        WHERE id = 1
        """,
        (str(tmp_path / "Avery_Student_Resume.pdf"),),
    )
    connection.commit()
    update_worker_state(
        connection,
        "worker-0",
        status="review_ready",
        job=get_job(connection, 1),
        message="Review complete; confirm Submit below",
    )

    class FakeService:
        def confirm_submission(self, job_id):
            return request_final_submission(connection, job_id)

    app = create_app(paths.database, paths=paths)
    app.state.service = FakeService()
    client = TestClient(app)

    worker_payload = client.get("/api/workers").json()
    worker = worker_payload["items"][0]
    assert worker["submission_ready"] is True
    assert worker_payload["queue"][0]["queue_state"] == "active"
    assert worker_payload["queue"][0]["worker_status"] == "review_ready"
    response = client.post("/api/jobs/1/submit")

    assert response.status_code == 202
    assert response.json()["status"] == "submitting"
    assert get_job(connection, 1)["submission_requested"] == 1


def test_applications_page_retries_a_confirm_in_agent_checkpoint(
    tmp_path, profile, settings
) -> None:
    paths = AppPaths(tmp_path)
    connection = init_db(paths.database)
    source = SOURCE_DOCUMENTS[0]
    listing = InternshipListing(
        company="Acme",
        role="Software Engineer Intern",
        location="Remote",
        application_url="https://jobs.test/retry",
        source_key=source.key,
        source_label=source.label,
        source_repo_url=source.repo_url,
        source_path=source.path,
    )
    ingest_listings(connection, source, [listing], profile=profile, settings=settings)
    connection.execute(
        """
        UPDATE jobs SET pipeline_status = 'manual_review', worker_id = 'worker-0',
                        resume_path = ?, submission_requested = 1
        WHERE id = 1
        """,
        (str(tmp_path / "Avery_Student_Resume.pdf"),),
    )
    connection.commit()
    store_agent_inputs(
        connection,
        1,
        [
            {
                "key": "email_verification_code",
                "label": "Email verification code",
                "input_type": "verification_code",
                "required": True,
            }
        ],
    )
    update_worker_state(
        connection,
        "worker-0",
        status="review_ready",
        job=get_job(connection, 1),
        message="Confirm in Agent",
    )

    class ConnectedAuth:
        def status(self):
            return {"logged_in": True}

    class FakeService:
        def retry_application(self, job_id):
            return retry_manual_application(connection, job_id)

    app = create_app(paths.database, paths=paths)
    app.state.claude_auth = ConnectedAuth()
    app.state.service = FakeService()
    client = TestClient(app)

    applications = client.get("/api/jobs?view=applications").json()["items"]
    assert [item["company"] for item in applications] == ["Acme"]
    assert applications[0]["pipeline_status"] == "manual_review"

    response = client.post("/api/jobs/1/retry")

    assert response.status_code == 202
    assert response.json()["job"]["pipeline_status"] == "ready"
    retried = get_job(connection, 1)
    assert retried["manual_requested"] == 1
    assert retried["worker_id"] is None
    assert retried["submission_requested"] == 0
    assert client.get("/api/jobs?view=applications").json()["items"] == []


def test_agent_page_records_a_bot_blocked_application_as_applied_manually(
    tmp_path, profile, settings
) -> None:
    paths = AppPaths(tmp_path)
    connection = init_db(paths.database)
    source = SOURCE_DOCUMENTS[0]
    listing = InternshipListing(
        company="Blocked Co",
        role="Software Engineer Intern",
        location="Remote",
        application_url="https://jobs.test/blocked",
        source_key=source.key,
        source_label=source.label,
        source_repo_url=source.repo_url,
        source_path=source.path,
    )
    ingest_listings(connection, source, [listing], profile=profile, settings=settings)
    mark_apply_result(
        connection,
        1,
        "needs_review",
        "Employer returned HTTP 403",
        reason_code="access_blocked",
    )
    client = TestClient(create_app(paths.database, paths=paths))

    blocked = client.get("/api/workers").json()["manual_applications"]
    assert [item["company"] for item in blocked] == ["Blocked Co"]
    assert blocked[0]["application_url"] == "https://jobs.test/blocked"
    assert blocked[0]["detail"] == "Employer returned HTTP 403"

    response = client.post("/api/jobs/1/applied-manually")

    assert response.status_code == 200
    assert response.json()["job"]["pipeline_status"] == "applied"
    assert response.json()["job"]["apply_origin"] == "self"
    assert client.get("/api/workers").json()["manual_applications"] == []
    applications = client.get("/api/jobs?view=applications&status=applied").json()["items"]
    assert [item["company"] for item in applications] == ["Blocked Co"]
    assert client.get("/api/stats").json()["applications"] == 1
    assert client.post("/api/jobs/1/applied-manually").status_code == 409
    assert client.post("/api/jobs/404/applied-manually").status_code == 404


def test_latest_jobs_records_an_application_without_starting_the_agent(
    tmp_path, profile, settings
) -> None:
    paths = AppPaths(tmp_path)
    connection = init_db(paths.database)
    source = SOURCE_DOCUMENTS[0]
    listing = InternshipListing(
        company="Ledger Co",
        role="Software Engineer Intern",
        location="Remote",
        application_url="https://jobs.test/ledger",
        source_key=source.key,
        source_label=source.label,
        source_repo_url=source.repo_url,
        source_path=source.path,
    )
    ingest_listings(connection, source, [listing], profile=profile, settings=settings)
    buffer = io.BytesIO()
    document = canvas.Canvas(buffer)
    document.drawString(72, 760, "Avery Student - backend and data engineering experience")
    document.save()
    wrong = store_resume(
        paths=paths,
        name="General resume",
        original_filename="general.pdf",
        content=buffer.getvalue(),
        text_override="Avery Student with general software engineering project experience.",
        db_path=paths.database,
    )
    chosen = store_resume(
        paths=paths,
        name="Backend resume",
        original_filename="backend.pdf",
        content=buffer.getvalue(),
        text_override="Avery Student built Python APIs, SQL services, and backend systems.",
        db_path=paths.database,
    )
    connection.execute("UPDATE jobs SET base_resume_id = ? WHERE id = 1", (wrong["id"],))
    connection.commit()
    client = TestClient(create_app(paths.database, paths=paths))
    assert [item["id"] for item in client.get("/api/jobs?view=latest").json()["items"]] == [1]

    invalid = client.post("/api/jobs/1/applied-manually", json={"resume_id": 9999})
    assert invalid.status_code == 409
    response = client.post(
        "/api/jobs/1/applied-manually", json={"resume_id": chosen["id"]}
    )

    assert response.status_code == 200
    assert response.json()["job"]["pipeline_status"] == "applied"
    assert response.json()["job"]["apply_origin"] == "self"
    assert response.json()["job"]["submitted_resume_id"] == chosen["id"]
    assert response.json()["job"]["submitted_resume_name"] == "Backend resume"
    application = client.get("/api/jobs?view=applications&status=applied").json()[
        "items"
    ][0]
    assert application["company"] == "Ledger Co"
    assert application["submitted_resume_name"] == "Backend resume"
    assert client.get("/api/stats").json()["applications"] == 1
    # The listing stays in the repository inbox, now stamped as applied.
    latest = client.get("/api/jobs?view=latest").json()["items"]
    assert [item["pipeline_status"] for item in latest] == ["applied"]

    index = client.get("/").text
    javascript = client.get("/static/app.js").text
    assert 'id="markAppliedButton"' in index
    assert 'id="manualAppliedResume"' in index
    assert 'id="confirmManualApplied"' in index
    assert "function jobCanBeMarkedApplied(job)" in javascript
    assert 'class="text-button mark-applied"' in javascript
    assert "Choose the resume you submitted" in javascript
    assert "body: JSON.stringify({ resume_id: resumeId })" in javascript


def test_latest_jobs_can_filter_to_reviews_that_said_yes(
    tmp_path, profile, settings
) -> None:
    paths = AppPaths(tmp_path)
    connection = init_db(paths.database)
    source = SOURCE_DOCUMENTS[0]
    listings = [
        InternshipListing(
            company=company,
            role=f"{company} Intern",
            location="Remote",
            application_url=f"https://jobs.test/{company.casefold()}",
            source_key=source.key,
            source_label=source.label,
            source_repo_url=source.repo_url,
            source_path=source.path,
        )
        for company in ("Yes", "No", "Pending")
    ]
    ingest_listings(connection, source, listings, profile=profile, settings=settings)
    connection.execute(
        """
        UPDATE jobs
        SET apply_decision = CASE company
            WHEN 'Yes' THEN 'apply'
            WHEN 'No' THEN 'skip'
            ELSE NULL
        END
        """
    )
    connection.commit()
    client = TestClient(create_app(paths.database, paths=paths))

    yes = client.get("/api/jobs?view=latest&decision=apply")
    no = client.get("/api/jobs?view=latest&decision=skip")
    pending = client.get("/api/jobs?view=latest&decision=pending")

    assert [item["company"] for item in yes.json()["items"]] == ["Yes"]
    assert [item["company"] for item in no.json()["items"]] == ["No"]
    assert [item["company"] for item in pending.json()["items"]] == ["Pending"]
    assert client.get("/api/jobs?view=latest&decision=maybe").status_code == 422


def test_agent_page_stops_a_looping_captcha_session_and_records_it(
    tmp_path, profile, settings
) -> None:
    paths = AppPaths(tmp_path)
    connection = init_db(paths.database)
    source = SOURCE_DOCUMENTS[0]
    listing = InternshipListing(
        company="Acme",
        role="Software Engineer Intern",
        location="Remote",
        application_url="https://jobs.test/captcha",
        source_key=source.key,
        source_label=source.label,
        source_repo_url=source.repo_url,
        source_path=source.path,
    )
    ingest_listings(connection, source, [listing], profile=profile, settings=settings)
    connection.execute(
        """
        UPDATE jobs SET pipeline_status = 'manual_review', worker_id = 'worker-0',
                        apply_reason_code = 'captcha', resume_path = ?
        WHERE id = 1
        """,
        (str(tmp_path / "resume.pdf"),),
    )
    connection.commit()
    update_worker_state(
        connection,
        "worker-0",
        status="captcha",
        job=get_job(connection, 1),
        message="Solve the CAPTCHA in this browser",
    )
    client = TestClient(create_app(paths.database, paths=paths))

    worker = client.get("/api/workers").json()["items"][0]
    assert worker["stoppable"] is True
    assert worker["stop_requested"] is False

    response = client.post("/api/jobs/1/stop")

    assert response.status_code == 202
    assert response.json()["status"] == "stopped"
    assert response.json()["job"]["pipeline_status"] == "skipped"
    payload = client.get("/api/workers").json()
    assert payload["queue"] == []
    assert [item["id"] for item in payload["manual_applications"]] == [1]
    assert payload["manual_applications"][0]["handoff"] == "stopped"

    recorded = client.post("/api/jobs/1/applied-manually")
    assert recorded.status_code == 200
    assert recorded.json()["job"]["apply_origin"] == "self"
    assert client.get("/api/workers").json()["manual_applications"] == []
    assert client.post("/api/jobs/1/stop").status_code == 409


def test_agent_ui_uses_a_persistent_stream_and_input_channel(tmp_path) -> None:
    paths = AppPaths(tmp_path)
    client = TestClient(create_app(paths.database, paths=paths))

    index = client.get("/").text
    javascript = client.get("/static/app.js").text

    assert 'id="agentInputPanel"' in index
    assert 'id="agentQueueList"' in index
    assert 'id="agentQueueCount"' in index
    assert 'id="autoMode"' in index
    assert 'id="autoModeMinimumFit"' not in index
    assert 'id="reviewBudget"' in index
    assert 'id="reviewMaxAge"' in index
    assert 'id="reviewEnabled"' in index
    assert 'id="retryTodayReviews"' in index
    assert '<th>Apply?</th>' in index
    assert '<th>Fit</th>' not in index
    assert '<th>Qualified</th>' not in index
    assert 'id="decisionModal"' in index
    assert 'id="autoModeUsePreferences"' in index
    assert 'id="manualAutoSubmit"' in index
    assert '<option value="apply">Review said Yes</option>' in index
    assert '<option value="skip">Review said No</option>' in index
    assert 'colspan="6"' in index
    assert 'id="addressLine2"' in index
    assert 'id="county"' in index
    assert 'id="previousInternshipCompanies"' in index
    assert "Applications & checkpoints" in index
    assert '<option value="manual_review">Confirm in Agent</option>' in index
    assert '<option value="applied" selected>Applied</option>' in index
    assert 'id="manualApplyList"' in index
    assert 'id="manualApplyCount"' in index
    assert "Roles you apply to yourself" in index
    assert 'id="webPushOption" class="web-push-option hidden"' in index
    assert 'id="webPushNotifications"' in index
    assert 'id="autoApplyMinimumFit"' not in index
    assert 'id="autoApplyRule"' not in index
    assert 'id="tailorResumes"' not in index
    assert 'id="autoApplyNew"' not in index
    assert 'id="allowSubmission"' not in index
    assert 'id="workerCount"' not in index
    assert 'id="welcomeBack"' in index
    assert 'id="minimumFit"' not in index
    assert "NOTIFICATIONS" not in index
    assert "OPTIONAL SECRETS" not in index
    assert 'id="anthropicKey"' not in index
    assert 'id="onboardAnthropic"' not in index
    assert "Same-session browser" in index
    assert "click, type, paste, and scroll directly" in index
    assert "new WebSocket" in javascript
    assert "data-browser-control" in javascript
    assert "Input.dispatchMouseEvent" not in javascript
    assert "This is the agent's exact retained tab—not a new job link" in javascript
    assert "/continue-agent" in javascript
    assert "data-preview-canvas" in javascript
    assert "}, 500);" not in javascript
    assert "}, 1000);" in javascript
    assert "Save answers & continue" in javascript
    assert 'autocomplete="one-time-code"' in javascript
    assert "VERIFICATION CODE NEEDED" in javascript
    assert "keeps the current form open and continues it with your answers" in javascript
    assert "restarts this application with your answers" not in javascript
    assert "function listingDate(postingDate, firstSeenAt)" in javascript
    assert "job.posting_date || shortDate(job.first_seen_at)" not in javascript
    assert javascript.count("listingDate(job.posting_date, job.first_seen_at)") == 2
    assert "review.max_applications_per_company ?? 2" in javascript
    assert 'api("/api/reviews/today/retry"' in javascript
    assert "function firstSeenToday(job)" in javascript
    assert "&& !job.apply_decision" in javascript
    assert "Existing YES/NO decisions will not be reviewed again or changed" in javascript
    assert "timezone: browserTimeZone()" in javascript
    assert 'parameters.set("decision", decision)' in javascript
    assert 'element("latestDecision").addEventListener("change", () => loadLatestJobs()' in javascript
    # The review controls are populated from saved settings, not from the
    # auto-mode toggle handler, which has no `review` in scope.
    populate = javascript.split("function populateConfiguration(")[1]
    assert 'setValue("reviewBudget"' in populate.split("function ")[0]
    toggle = javascript.split("function handleAutoModeToggle(")[1].split("function ")[0]
    assert "review." not in toggle
    assert 'api(`/api/jobs/${card.dataset.submitJob}/submit`' in javascript
    assert "Submit application" in javascript
    assert "function renderApplicationQueue" in javascript
    assert 'class="mini-apply retry-application"' in javascript
    assert 'aria-label="Retry application for ${escapeHtml(job.company)}"' in javascript
    assert "Any form currently open in Agent will close" in javascript
    assert 'api(`/api/jobs/${jobId}/retry`' in javascript
    assert 'renderApplicationQueue(workers.queue || [], workers.queue_summary || {})' in javascript
    assert 'renderManualApplications(workers.manual_applications || [])' in javascript
    assert 'api(`/api/jobs/${job.id}/applied-manually`' in javascript
    assert 'api(`/api/jobs/${jobId}/stop`' in javascript
    assert "I applied manually" in javascript
    assert "Stop session" in javascript
    assert "If the challenge keeps coming back, stop the session" in javascript
    assert 'job.apply_origin === "self"' in javascript
    assert "Notification.requestPermission()" in javascript
    assert 'navigator.serviceWorker.register("/sw.js"' in javascript
    assert 'web_push_notifications: checked("autoMode") && checked("webPushNotifications")' in javascript
    assert 'api("/api/dashboard/visit", { method: "POST" })' in javascript
    assert "stopped with a recorded reason" in javascript
    assert "decision-mark" in javascript
    assert "fit-mark" not in javascript

    service_worker = client.get("/sw.js")
    assert service_worker.status_code == 200
    assert "showNotification" in service_worker.text
    assert service_worker.headers["service-worker-allowed"] == "/"


def test_web_settings_retire_the_fit_limit_and_clamp_the_review_budget(tmp_path) -> None:
    paths = AppPaths(tmp_path)
    client = TestClient(create_app(paths.database, paths=paths))

    response = client.put(
        "/api/config",
        json={
            "settings": {
                "minimum_fit_score": 5,
                "notifications": {"email_enabled": True},
                "review": {
                    "max_applications_per_company": 99,
                    "max_listing_age_days": -5,
                    "refresh_after_days": 0,
                    "max_companies_per_cycle": 500,
                },
                "automation": {
                    "auto_apply_eligible_only": True,
                    "auto_apply_minimum_fit_score": 99,
                    "auto_apply_use_preferences": True,
                    "manual_auto_submit": 1,
                    "web_push_notifications": 1,
                },
            }
        },
    )

    assert response.status_code == 200
    review = response.json()["settings"]["review"]
    assert review["max_applications_per_company"] == 10
    assert review["max_listing_age_days"] == 0
    assert review["refresh_after_days"] == 1
    assert review["max_companies_per_cycle"] == 100
    assert (
        "auto_apply_minimum_fit_score"
        not in response.json()["settings"]["automation"]
    )
    assert "auto_apply_eligible_only" not in response.json()["settings"]["automation"]
    assert response.json()["settings"]["automation"]["auto_apply_use_preferences"] is True
    assert response.json()["settings"]["automation"]["manual_auto_submit"] is True
    assert response.json()["settings"]["automation"]["web_push_notifications"] is True
    assert "minimum_fit_score" not in response.json()["settings"]
    assert "notifications" not in response.json()["settings"]
