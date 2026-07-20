"""Isolated Chrome lifecycle management for application workers."""

from __future__ import annotations

import os
import platform
import signal
import subprocess
import time

import httpx

from tiaaa.config import AppPaths, get_chrome_path

BASE_CDP_PORT = 9330


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


def _wait_for_cdp(port: int, process: subprocess.Popen[bytes], timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Chrome exited before opening its debug port ({process.returncode})")
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
    kwargs: dict[str, object] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if platform.system() == "Windows":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(command, **kwargs)
    try:
        _wait_for_cdp(port, process)
    except Exception:
        _terminate_tree(process)
        raise
    return process, port


def stop_chrome(process: subprocess.Popen[bytes] | None) -> None:
    if process is not None:
        _terminate_tree(process)
