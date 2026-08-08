from __future__ import annotations

import json

from fastapi.testclient import TestClient

from tiaaa.config import SOURCE_DOCUMENTS, AppPaths, ensure_dirs
from tiaaa.dashboard.app import create_app
from tiaaa.database import ingest_listings, init_db, list_application_queue
from tiaaa.models import InternshipListing
from tiaaa.web_push import (
    push_subscription_count,
    send_auto_queue_notifications,
    store_push_subscription,
)


def test_push_subscription_api_keeps_endpoints_private_and_supports_removal(tmp_path) -> None:
    paths = AppPaths(tmp_path)
    client = TestClient(create_app(paths.database, paths=paths))

    status = client.get("/api/push")
    assert status.status_code == 200
    assert len(status.json()["public_key"]) == 87
    assert status.json()["subscription_count"] == 0

    subscription = {
        "endpoint": "https://push.example.test/subscriptions/browser-one",
        "keys": {"p256dh": "p" * 65, "auth": "a" * 24},
    }
    created = client.post("/api/push/subscriptions", json=subscription)
    assert created.status_code == 201
    assert created.json()["subscription_count"] == 1
    assert "endpoint" not in created.text

    removed = client.request(
        "DELETE",
        "/api/push/subscriptions",
        json={"endpoint": subscription["endpoint"]},
    )
    assert removed.status_code == 200
    assert removed.json()["subscription_count"] == 0

    rejected = client.post(
        "/api/push/subscriptions",
        json={**subscription, "endpoint": "http://push.example.test/not-secure"},
    )
    assert rejected.status_code == 422


def test_auto_queue_push_is_batched_and_delivered_once_per_browser(
    tmp_path, profile, settings, monkeypatch
) -> None:
    paths = ensure_dirs(AppPaths(tmp_path))
    connection = init_db(paths.database)
    store_push_subscription(
        connection,
        endpoint="https://push.example.test/subscriptions/browser-one",
        p256dh="p" * 65,
        auth="a" * 24,
        user_agent="Test browser",
    )
    settings["automation"]["auto_apply_new"] = True
    settings["automation"]["auto_apply_minimum_fit_score"] = 1
    source = SOURCE_DOCUMENTS[0]
    listings = [
        InternshipListing(
            company="Acme",
            role="Software Engineer Intern",
            location="Remote",
            application_url="https://jobs.test/acme",
            source_key=source.key,
            source_label=source.label,
            source_repo_url=source.repo_url,
            source_path=source.path,
        ),
        InternshipListing(
            company="Beta",
            role="Data Engineer Intern",
            location="Seattle",
            application_url="https://jobs.test/beta",
            source_key=source.key,
            source_label=source.label,
            source_repo_url=source.repo_url,
            source_path=source.path,
        ),
    ]
    ingest_listings(
        connection,
        source,
        listings,
        profile=profile,
        settings=settings,
        include_existing=True,
    )
    queue = list_application_queue(
        connection,
        auto_enabled=True,
        max_attempts=3,
        minimum_fit_score=1,
        profile=profile,
    )
    deliveries: list[dict] = []

    def fake_webpush(**kwargs):
        deliveries.append(kwargs)

    monkeypatch.setattr("tiaaa.web_push.webpush", fake_webpush)
    first = send_auto_queue_notifications(connection, paths=paths, queue=queue)
    second = send_auto_queue_notifications(connection, paths=paths, queue=queue)

    assert first == {
        "subscriptions": 1,
        "sent": 1,
        "jobs": 2,
        "removed": 0,
        "errors": 0,
    }
    assert second["sent"] == 0
    assert len(deliveries) == 1
    payload = json.loads(deliveries[0]["data"])
    assert payload["title"] == "2 new internships queued"
    assert "Acme · Software Engineer Intern" in payload["body"]
    assert payload["url"] == "/?view=live"
    assert push_subscription_count(connection) == 1
