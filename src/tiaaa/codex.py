"""Isolated Codex CLI turns using the candidate's existing Codex login."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from tiaaa.apply.chrome import stop_process_tree


class CodexStopped(Exception):
    """The user stopped a running turn."""


def codex_status() -> dict[str, Any]:
    status: dict[str, Any] = {
        "installed": False, "logged_in": False,
        "login_command": (
            "docker exec -it tiaaa codex login --device-auth"
            if os.environ.get("TIAAA_DOCKER") == "1" else "codex login"
        ),
    }
    executable = shutil.which("codex")
    if not executable:
        return status
    status["installed"] = True
    try:
        result = subprocess.run(
            [executable, "login", "status"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        status["logged_in"] = result.returncode == 0
    except subprocess.TimeoutExpired:
        status["error"] = "Codex status check timed out. Try refreshing again."
    except OSError:
        status["error"] = "Could not run Codex. Check its installation and refresh again."
    return status


def codex_command(
    *, schema_path: Path, model: str = "", port: int | None = None, browser_tools: tuple[str, ...] = (),
    browser_token: str = "",
) -> list[str]:
    command = [
        "codex",
        "exec",
        "--ignore-user-config",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--json",
        "--color",
        "never",
        "--output-schema",
        str(schema_path),
    ]
    config: dict[str, Any] = {
        "approval_policy": "never",
        "features.shell_tool": False,
        "features.unified_exec": False,
        "features.multi_agent": False,
        "web_search": "disabled",
        "project_doc_max_bytes": 0,
    }
    if port is not None:
        config.update(
            {
                "mcp_servers.tiaaa_browser.url": f"http://127.0.0.1:{port}/mcp",
                "mcp_servers.tiaaa_browser.enabled_tools": list(browser_tools),
                "mcp_servers.tiaaa_browser.required": True,
                "mcp_servers.tiaaa_browser.default_tools_approval_mode": "approve",
            }
        )
    if browser_token and port is not None:
        config["mcp_servers.tiaaa_browser.http_headers.Authorization"] = f"Bearer {browser_token}"
    for key, value in config.items():
        command.extend(["-c", f"{key}={json.dumps(value)}"])
    if model:
        command.extend(["--model", model])
    return [*command, "-"]


def run_codex(
    command: list[str],
    prompt: str,
    *,
    cwd: Path,
    timeout: float,
    environment: dict[str, str] | None = None,
    should_stop: Any = None,
) -> tuple[str, int]:
    """Drain both pipes and poll cancellation without persisting prompt or model output."""
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    environment = dict(os.environ if environment is None else environment)
    environment.pop("TIAAA_EMAIL_APP_PASSWORD", None)
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
        env=environment,
        **kwargs,
    )
    deadline = time.monotonic() + timeout
    try:
        first = True
        while True:
            if should_stop is not None and should_stop():
                raise CodexStopped()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                output, _ = process.communicate(prompt if first else None, timeout=min(0.5, remaining))
                return output, int(process.returncode)
            except subprocess.TimeoutExpired as exc:
                first = False
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(command, timeout, output=exc.output) from exc
    finally:
        stop_process_tree(process)
        process.communicate()


def codex_events(output: str) -> list[dict[str, Any]]:
    events = []
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def codex_final(output: str) -> str:
    messages = [
        str(event["item"].get("text", ""))
        for event in codex_events(output)
        if event.get("type") == "item.completed"
        and isinstance(event.get("item"), dict)
        and event["item"].get("type") == "agent_message"
    ]
    return messages[-1] if messages else ""


def codex_usage_limited(output: str) -> bool:
    # Never interpret webpage/tool text as an account quota signal.
    errors = [event for event in codex_events(output) if event.get("type") in {"error", "turn.failed"}]
    text = json.dumps(errors).casefold()
    return any(
        marker in text
        for marker in (
            "usage_limit",
            "usage limit",
            "rate_limit",
            "rate limit",
            "quota",
            "429",
            "you've hit your",
            "insufficient_quota",
        )
    )
