from __future__ import annotations

from tiaaa.config import AppPaths, ensure_dirs, save_profile, save_settings
from tiaaa.database import get_app_state, get_connection, init_db, set_app_state
from tiaaa.discovery.github import SyncResult
from tiaaa.service import AutomationService


def test_background_cycle_syncs_before_and_only_prepares_after_onboarding_and_baseline(
    tmp_path, profile, settings, monkeypatch
) -> None:
    paths = ensure_dirs(AppPaths(tmp_path))
    save_profile(profile, paths)
    save_settings(settings, paths)
    connection = init_db(paths.database)
    calls: dict[str, object] = {"sync": 0, "prepare": 0}

    def fake_sync(**kwargs):
        assert "include_existing" not in kwargs
        calls["sync"] += 1
        return [SyncResult("source/readme", "Source", "synced", parsed=10, baseline=True)]

    def fake_prepare(**_kwargs):
        calls["prepare"] += 1
        return {"prepared": 0, "errors": 0}

    monkeypatch.setattr("tiaaa.discovery.github.sync_repositories", fake_sync)
    monkeypatch.setattr("tiaaa.preparation.prepare_jobs", fake_prepare)
    monkeypatch.setattr("tiaaa.service.source_baseline_complete", lambda *_args, **_kwargs: True)
    service = AutomationService(paths)

    service.run_cycle()
    assert calls == {"sync": 1, "prepare": 0}

    set_app_state(connection, "onboarding_complete", True)
    summary = service.run_cycle()
    assert calls == {"sync": 2, "prepare": 1}
    assert summary["baseline_complete"] is True
    assert summary["status"] == "complete"
    assert get_connection(paths.database).execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_manual_application_runs_when_automatic_new_job_setting_is_off(
    tmp_path, profile, settings, monkeypatch
) -> None:
    paths = ensure_dirs(AppPaths(tmp_path))
    save_profile(profile, paths)
    settings["automation"]["auto_apply_new"] = False
    save_settings(settings, paths)
    connection = init_db(paths.database)
    set_app_state(connection, "onboarding_complete", True)
    calls: list[int | None] = []

    monkeypatch.setattr(
        "tiaaa.discovery.github.sync_repositories",
        lambda **_kwargs: [
            SyncResult("source/readme", "Source", "unchanged", parsed=10, baseline=False)
        ],
    )
    monkeypatch.setattr("tiaaa.service.source_baseline_complete", lambda *_a, **_k: True)
    monkeypatch.setattr("tiaaa.service.manual_application_ids", lambda _connection: [42])
    monkeypatch.setattr(
        "tiaaa.preparation.prepare_jobs",
        lambda **_kwargs: {"prepared": 1, "errors": 0},
    )

    def fake_apply(**kwargs):
        assert kwargs["submit"] is False
        assert kwargs["unattended"] is False
        assert kwargs["interactive_review"] is True
        calls.append(kwargs.get("target_job_id"))
        return {"applied": 0, "review": 1, "failed": 0, "expired": 0}

    monkeypatch.setattr("tiaaa.apply.run_applications", fake_apply)
    summary = AutomationService(paths).run_cycle()

    assert calls == [42]
    assert summary["applications"]["review"] == 1


def test_retry_application_requests_a_fresh_service_cycle(tmp_path, monkeypatch) -> None:
    paths = ensure_dirs(AppPaths(tmp_path))
    init_db(paths.database)
    service = AutomationService(paths)
    monkeypatch.setattr(
        "tiaaa.service.retry_manual_application",
        lambda _connection, job_id: {
            "id": job_id,
            "company": "Acme",
            "role": "Software Intern",
        },
    )

    job = service.retry_application(42)

    assert job["id"] == 42
    assert service._force_cycle.is_set()
    assert service._wake.is_set()
    state = get_app_state(get_connection(paths.database))
    assert state["service_status"] == "requested"
    assert state["service_message"] == "Retry requested for Acme · Software Intern"


def test_return_browser_control_wakes_the_retained_agent(tmp_path, monkeypatch) -> None:
    paths = ensure_dirs(AppPaths(tmp_path))
    init_db(paths.database)
    service = AutomationService(paths)
    monkeypatch.setattr(
        "tiaaa.service.request_human_control_return",
        lambda _connection, job_id: {
            "id": job_id,
            "company": "Capula",
            "role": "Trading and Research Intern",
        },
    )

    job = service.return_browser_control(42)

    assert job["id"] == 42
    assert service._wake.is_set()
    state = get_app_state(get_connection(paths.database))
    assert state["service_status"] == "applying"
    assert state["service_message"] == (
        "Resuming application for Capula · Trading and Research Intern"
    )


def test_auto_mode_submits_unattended_in_the_same_cycle(
    tmp_path, profile, settings, monkeypatch
) -> None:
    paths = ensure_dirs(AppPaths(tmp_path))
    save_profile(profile, paths)
    settings["automation"]["auto_apply_new"] = True
    settings["automation"]["web_push_notifications"] = True
    save_settings(settings, paths)
    connection = init_db(paths.database)
    set_app_state(connection, "onboarding_complete", True)
    calls: list[dict] = []
    push_calls: list[list[dict]] = []

    monkeypatch.setattr(
        "tiaaa.discovery.github.sync_repositories",
        lambda **_kwargs: [
            SyncResult("source/readme", "Source", "synced", parsed=1, new=1, queued=1)
        ],
    )
    monkeypatch.setattr("tiaaa.service.source_baseline_complete", lambda *_a, **_k: True)
    monkeypatch.setattr("tiaaa.service.manual_application_ids", lambda _connection: [])
    monkeypatch.setattr(
        "tiaaa.preparation.prepare_jobs",
        lambda **_kwargs: {"prepared": 1, "errors": 0},
    )

    def fake_push(_connection, *, paths, queue):
        assert paths == service_paths
        push_calls.append(queue)
        return {"subscriptions": 1, "sent": 1, "jobs": 1, "removed": 0, "errors": 0}

    service_paths = paths
    monkeypatch.setattr("tiaaa.web_push.send_auto_queue_notifications", fake_push)

    def fake_apply(**kwargs):
        calls.append(kwargs)
        return {"applied": 1, "review": 0, "failed": 0, "expired": 0}

    monkeypatch.setattr("tiaaa.apply.run_applications", fake_apply)
    summary = AutomationService(paths).run_cycle()

    assert len(calls) == 1
    assert calls[0].get("target_job_id") is None
    assert calls[0]["submit"] is True
    assert calls[0]["unattended"] is True
    assert summary["applications"]["applied"] == 1
    assert push_calls == [[]]
    assert summary["web_push"]["sent"] == 1


def test_manual_auto_submit_submits_selected_job_but_keeps_input_interactive(
    tmp_path, profile, settings, monkeypatch
) -> None:
    paths = ensure_dirs(AppPaths(tmp_path))
    save_profile(profile, paths)
    settings["automation"]["auto_apply_new"] = False
    settings["automation"]["manual_auto_submit"] = True
    save_settings(settings, paths)
    connection = init_db(paths.database)
    set_app_state(connection, "onboarding_complete", True)
    calls: list[dict] = []

    monkeypatch.setattr(
        "tiaaa.discovery.github.sync_repositories",
        lambda **_kwargs: [
            SyncResult("source/readme", "Source", "unchanged", parsed=10, baseline=False)
        ],
    )
    monkeypatch.setattr("tiaaa.service.source_baseline_complete", lambda *_a, **_k: True)
    monkeypatch.setattr("tiaaa.service.manual_application_ids", lambda _connection: [42])
    monkeypatch.setattr(
        "tiaaa.preparation.prepare_jobs",
        lambda **_kwargs: {"prepared": 1, "errors": 0},
    )

    def fake_apply(**kwargs):
        calls.append(kwargs)
        return {"applied": 1, "review": 0, "failed": 0, "expired": 0}

    monkeypatch.setattr("tiaaa.apply.run_applications", fake_apply)
    summary = AutomationService(paths).run_cycle()

    assert len(calls) == 1
    assert calls[0]["target_job_id"] == 42
    assert calls[0]["submit"] is True
    assert calls[0]["unattended"] is False
    assert calls[0]["interactive_review"] is False
    assert calls[0]["manual_selection_auto_submit"] is True
    assert summary["applications"]["applied"] == 1
