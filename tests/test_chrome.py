from __future__ import annotations

import io
import os
from pathlib import Path

import pytest

from tiaaa.apply.chrome import _recover_stale_profile_lock, _wait_for_cdp


def _singleton_links(profile: Path, *, lock: str, socket_target: Path) -> None:
    profile.mkdir(parents=True)
    (profile / "SingletonLock").symlink_to(lock)
    (profile / "SingletonCookie").symlink_to("123456789")
    (profile / "SingletonSocket").symlink_to(socket_target)


def test_recovers_foreign_container_lock_when_socket_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "profile"
    _singleton_links(
        profile,
        lock="old-container-84",
        socket_target=tmp_path / "missing" / "SingletonSocket",
    )
    monkeypatch.setattr("tiaaa.apply.chrome.platform.node", lambda: "current-container")
    monkeypatch.setenv("TIAAA_DOCKER", "1")

    assert _recover_stale_profile_lock(profile) is True
    assert not any(
        os.path.lexists(profile / name)
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket")
    )


def test_keeps_current_process_lock_even_when_socket_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "profile"
    _singleton_links(
        profile,
        lock=f"current-container-{os.getpid()}",
        socket_target=tmp_path / "missing" / "SingletonSocket",
    )
    monkeypatch.setattr("tiaaa.apply.chrome.platform.node", lambda: "current-container")

    assert _recover_stale_profile_lock(profile) is False
    assert os.path.lexists(profile / "SingletonLock")


def test_recovers_current_host_lock_when_process_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "profile"
    _singleton_links(
        profile,
        lock="current-container-84",
        socket_target=tmp_path / "missing" / "SingletonSocket",
    )
    monkeypatch.setattr("tiaaa.apply.chrome.platform.node", lambda: "current-container")
    monkeypatch.setattr("tiaaa.apply.chrome._pid_is_running", lambda _pid: False)

    assert _recover_stale_profile_lock(profile) is True
    assert not os.path.lexists(profile / "SingletonLock")


def test_keeps_foreign_lock_when_its_socket_still_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    socket_target = tmp_path / "live" / "SingletonSocket"
    socket_target.parent.mkdir()
    socket_target.touch()
    profile = tmp_path / "profile"
    _singleton_links(profile, lock="other-host-84", socket_target=socket_target)
    monkeypatch.setattr("tiaaa.apply.chrome.platform.node", lambda: "current-container")

    assert _recover_stale_profile_lock(profile) is False
    assert os.path.lexists(profile / "SingletonLock")


def test_keeps_foreign_lock_outside_managed_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "profile"
    _singleton_links(
        profile,
        lock="other-host-84",
        socket_target=tmp_path / "missing" / "SingletonSocket",
    )
    monkeypatch.setattr("tiaaa.apply.chrome.platform.node", lambda: "current-container")
    monkeypatch.delenv("TIAAA_DOCKER", raising=False)

    assert _recover_stale_profile_lock(profile) is False
    assert os.path.lexists(profile / "SingletonLock")


def test_chrome_exit_includes_a_concise_stderr_reason() -> None:
    class ExitedProcess:
        returncode = 21

        @staticmethod
        def poll() -> int:
            return 21

    stderr = io.BytesIO(
        b"[123:123:ERROR] The profile appears to be in use by another Chromium process.\n"
    )

    with pytest.raises(
        RuntimeError,
        match="Chrome exited before opening its debug port \\(21\\): "
        "The profile appears to be in use",
    ):
        _wait_for_cdp(9330, ExitedProcess(), stderr=stderr)
