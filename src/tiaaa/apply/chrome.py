"""Isolated Chrome lifecycle management for application workers."""

from __future__ import annotations

import logging
import os
import platform
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import BinaryIO

import httpx

from tiaaa.config import AppPaths, get_chrome_path

BASE_CDP_PORT = 9330
_SINGLETON_ARTIFACTS = ("SingletonLock", "SingletonCookie", "SingletonSocket")
log = logging.getLogger(__name__)


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _recover_stale_profile_lock(profile: Path) -> bool:
    """Remove Chromium singleton artifacts only when their owner is provably gone."""

    lock_path = profile / "SingletonLock"
    socket_path = profile / "SingletonSocket"
    if not os.path.lexists(lock_path) or socket_path.exists():
        return False
    try:
        lock_target = os.readlink(lock_path)
    except OSError:
        return False
    hostname, separator, raw_pid = lock_target.rpartition("-")
    if not separator or not hostname:
        return False
    try:
        pid = int(raw_pid)
    except ValueError:
        return False
    if pid <= 0:
        return False
    current_host = platform.node()
    if hostname != current_host and os.environ.get("TIAAA_DOCKER") != "1":
        return False
    if hostname == current_host and _pid_is_running(pid):
        return False
    for name in _SINGLETON_ARTIFACTS:
        artifact = profile / name
        if os.path.lexists(artifact):
            artifact.unlink()
    return True


def _read_stderr_reason(stderr: BinaryIO | None) -> str | None:
    if stderr is None:
        return None
    try:
        stderr.seek(0)
        raw = stderr.read()
    except (OSError, ValueError):
        return None
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines:
        marker = "The profile appears to be in use"
        if marker in line:
            return line[line.index(marker) :][:500]
    if not lines:
        return None
    line = lines[-1]
    if line.startswith("[") and "] " in line:
        line = line.split("] ", 1)[1]
    return line[:500]


def _terminate_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        if platform.system() == "Windows":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return


def _wait_for_cdp(
    port: int,
    process: subprocess.Popen[bytes],
    timeout: float = 15,
    *,
    stderr: BinaryIO | None = None,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            detail = _read_stderr_reason(stderr)
            suffix = f": {detail}" if detail else ""
            raise RuntimeError(
                f"Chrome exited before opening its debug port ({process.returncode}){suffix}"
            )
        try:
            response = httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=1)
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    raise TimeoutError(f"Chrome did not open CDP port {port} within {timeout:g}s")


def launch_chrome(
    *,
    worker_id: int,
    paths: AppPaths,
    headless: bool = False,
) -> tuple[subprocess.Popen[bytes], int]:
    port = BASE_CDP_PORT + worker_id
    try:
        response = httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=0.5)
        if response.status_code == 200:
            raise RuntimeError(f"CDP port {port} is already in use; stop that browser or use fewer workers")
    except httpx.HTTPError:
        pass

    profile = paths.browser_profiles / f"worker-{worker_id}"
    profile.mkdir(parents=True, exist_ok=True)
    if _recover_stale_profile_lock(profile):
        log.info("Recovered stale Chromium profile lock for worker-%s", worker_id)
    command = [
        get_chrome_path(),
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={profile}",
        "--profile-directory=Default",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        "--disable-notifications",
        "--deny-permission-prompts",
        "--disable-popup-blocking",
        "--window-size=1280,900",
        "about:blank",
    ]
    if headless:
        command.insert(-1, "--headless=new")
    if os.environ.get("TIAAA_CHROME_NO_SANDBOX") == "1":
        command.insert(-1, "--no-sandbox")
        command.insert(-1, "--disable-dev-shm-usage")
    with tempfile.TemporaryFile() as stderr:
        kwargs: dict[str, object] = {
            "stdout": subprocess.DEVNULL,
            "stderr": stderr,
        }
        if platform.system() == "Windows":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        process = subprocess.Popen(command, **kwargs)
        try:
            _wait_for_cdp(port, process, stderr=stderr)
        except Exception:
            _terminate_tree(process)
            raise
    return process, port


def stop_chrome(process: subprocess.Popen[bytes] | None) -> None:
    if process is not None:
        _terminate_tree(process)
