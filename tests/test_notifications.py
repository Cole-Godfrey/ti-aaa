from __future__ import annotations

from tiaaa.config import SOURCE_DOCUMENTS, AppPaths, save_settings
from tiaaa.database import (
    get_connection,
    ingest_listings,
    init_db,
    update_tracker,
)
from tiaaa.models import InternshipListing
from tiaaa.notifications import NotificationDispatcher


def test_email_dispatcher_respects_event_toggles_and_sends_a_test(
    tmp_path, profile, settings, monkeypatch
) -> None:
    paths = AppPaths(tmp_path)
    settings["notifications"].update(
        {
            "email_enabled": True,
            "email_to": "avery@example.com",
            "email_from": "tiaaa@example.com",
            "smtp_host": "smtp.example.com",
            "smtp_port": 587,
            "smtp_security": "starttls",
            "smtp_username": "",
        }
    )
    settings["notifications"]["events"]["oa"] = False
    save_settings(settings, paths)
    connection = init_db(paths.database)
    source = SOURCE_DOCUMENTS[0]
    listing = InternshipListing(
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
        connection,
        source,
        [listing],
        profile=profile,
        settings=settings,
        include_existing=True,
    )
    update_tracker(connection, 1, pipeline_status="applied")
    update_tracker(connection, 1, outcome_status="oa")

    sent = []
    monkeypatch.setattr(
        NotificationDispatcher,
        "_send",
        staticmethod(lambda _config, message: sent.append(message)),
    )
    dispatcher = NotificationDispatcher(paths)

    assert dispatcher.flush() == {"sent": 1, "failed": 0, "skipped": 1}
    assert sent[0]["Subject"] == "[TI-AAA] Application submitted"
    statuses = [
        row["email_status"]
        for row in get_connection(paths.database)
        .execute("SELECT email_status FROM notifications ORDER BY id")
        .fetchall()
    ]
    assert statuses == ["sent", "skipped"]

    dispatcher.send_test()
    assert sent[-1]["Subject"] == "[TI-AAA] Notification test"
