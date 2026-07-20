from __future__ import annotations

from tiaaa.config import AppPaths, ensure_dirs, save_profile, save_settings
from tiaaa.database import get_connection, init_db, set_app_state
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
