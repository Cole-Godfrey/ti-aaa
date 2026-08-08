from __future__ import annotations

import yaml

from tiaaa.config import AppPaths, load_settings, save_settings


def test_legacy_auto_discovery_does_not_become_unattended_submission(tmp_path) -> None:
    paths = AppPaths(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    paths.settings.write_text(
        yaml.safe_dump(
            {
                "automation": {
                    "auto_apply_new": True,
                    "auto_apply_eligible_only": False,
                    "allow_submission": False,
                    "enabled": True,
                }
            }
        ),
        encoding="utf-8",
    )

    automation = load_settings(paths)["automation"]

    assert automation["auto_apply_new"] is False
    assert "auto_apply_eligible_only" not in automation
    assert "enabled" not in automation


def test_new_auto_mode_can_be_enabled_without_the_cli_submit_switch(tmp_path) -> None:
    paths = AppPaths(tmp_path)

    save_settings(
        {
            "automation": {
                "auto_apply_new": True,
                "allow_submission": False,
            }
        },
        paths,
    )

    assert load_settings(paths)["automation"]["auto_apply_new"] is True
