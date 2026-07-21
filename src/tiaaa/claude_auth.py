"""Persistent Claude Code account authentication for the local dashboard."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from typing import Any

from tiaaa.config import AppPaths

_LOGIN_URL = re.compile(r"https://claude\.com/[^\s]+")


class ClaudeAuthManager:
    """Coordinate one browser-based Claude subscription login at a time."""

    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths
        self._condition = threading.Condition()
        self._process: subprocess.Popen[str] | None = None
        self._login_url: str | None = None
        self._reader: threading.Thread | None = None

    @staticmethod
    def _environment(*, subscription_login: bool = False) -> dict[str, str]:
        environment = os.environ.copy()
        environment.pop("CLAUDECODE", None)
        environment.pop("CLAUDE_CODE_ENTRYPOINT", None)
        if subscription_login:
            # An API key takes precedence over Claude.ai OAuth. Remove it only from
            # the login subprocess so users can deliberately connect a subscription.
            environment.pop("ANTHROPIC_API_KEY", None)
            environment.pop("ANTHROPIC_AUTH_TOKEN", None)
        return environment

    def _pending(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def status(self) -> dict[str, Any]:
        executable = shutil.which("claude")
        with self._condition:
            pending = self._pending()
            login_url = self._login_url if pending else None
        if executable is None:
            result = {
                "installed": False,
                "logged_in": False,
                "auth_method": "none",
                "provider": "none",
                "login_pending": pending,
            }
            if login_url:
                result["login_url"] = login_url
            return result
        try:
            result = subprocess.run(
                [executable, "auth", "status", "--json"],
                cwd=self.paths.root,
                env=self._environment(),
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=10,
                check=False,
            )
            start = result.stdout.find("{")
            end = result.stdout.rfind("}")
            raw = json.loads(result.stdout[start : end + 1]) if start >= 0 and end >= start else {}
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            raw = {}
        method = str(raw.get("authMethod") or "none")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", method):
            method = "unknown"
        provider = str(raw.get("apiProvider") or "firstParty")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", provider):
            provider = "unknown"
        result = {
            "installed": True,
            "logged_in": bool(raw.get("loggedIn")),
            "auth_method": method,
            "provider": provider,
            "login_pending": pending,
        }
        if login_url:
            result["login_url"] = login_url
        return result

    def _read_login_output(self, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        try:
            for line in process.stdout:
                match = _LOGIN_URL.search(line)
                if match:
                    with self._condition:
                        if process is self._process:
                            self._login_url = match.group(0)
                            self._condition.notify_all()
        finally:
            with self._condition:
                self._condition.notify_all()

    def start_login(self) -> dict[str, Any]:
        executable = shutil.which("claude")
        if executable is None:
            raise FileNotFoundError("Claude Code CLI is not installed")
        self.cancel_login()
        try:
            process = subprocess.Popen(
                [executable, "auth", "login", "--claudeai"],
                cwd=self.paths.root,
                env=self._environment(subscription_login=True),
                text=True,
                encoding="utf-8",
                errors="replace",
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
            )
        except OSError as exc:
            raise RuntimeError("Could not start Claude Code login") from exc
        reader = threading.Thread(
            target=self._read_login_output,
            args=(process,),
            name="tiaaa-claude-login",
            daemon=True,
        )
        with self._condition:
            self._process = process
            self._login_url = None
            self._reader = reader
            reader.start()
            self._condition.wait_for(
                lambda: self._login_url is not None or process.poll() is not None,
                timeout=15,
            )
            login_url = self._login_url
        if not login_url:
            self.cancel_login()
            raise RuntimeError("Claude Code did not provide a login link")
        result = self.status()
        result.update({"login_pending": True, "login_url": login_url})
        return result

    def complete_login(self, code: str) -> dict[str, Any]:
        clean_code = code.strip()
        if not clean_code or "\n" in clean_code or "\r" in clean_code or len(clean_code) > 4096:
            raise ValueError("Paste the one-time code from the Claude login page")
        with self._condition:
            process = self._process
            if process is None or process.poll() is not None or process.stdin is None:
                raise RuntimeError("Start Claude account login before submitting a code")
            process.stdin.write(clean_code + "\n")
            process.stdin.flush()
        try:
            process.wait(timeout=120)
        except subprocess.TimeoutExpired as exc:
            self.cancel_login()
            raise RuntimeError("Claude account login timed out; please try again") from exc
        with self._condition:
            reader = self._reader
            self._process = None
            self._login_url = None
            self._reader = None
        if reader is not None:
            reader.join(timeout=2)
        result = self.status()
        if process.returncode != 0 or not result["logged_in"]:
            raise RuntimeError("Claude account login was not completed; please try again")
        return result

    def cancel_login(self) -> None:
        with self._condition:
            process = self._process
            reader = self._reader
            self._process = None
            self._login_url = None
            self._reader = None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=2)

    def logout(self) -> dict[str, Any]:
        executable = shutil.which("claude")
        if executable is None:
            raise FileNotFoundError("Claude Code CLI is not installed")
        self.cancel_login()
        try:
            subprocess.run(
                [executable, "auth", "logout"],
                cwd=self.paths.root,
                env=self._environment(subscription_login=True),
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("Could not disconnect the Claude account") from exc
        return self.status()

    def close(self) -> None:
        self.cancel_login()
