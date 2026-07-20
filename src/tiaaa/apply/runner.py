"""Claim queued internships and run isolated Claude/Playwright browser sessions."""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import suppress
from pathlib import Path
from typing import Any

from tiaaa.apply.chrome import launch_chrome, stop_chrome
from tiaaa.apply.preview import PreviewCapture
from tiaaa.apply.prompt import build_prompt
from tiaaa.config import AppPaths
from tiaaa.database import (
    applications_today,
    claim_next_job,
    claimable_application_count,
    get_connection,
    init_db,
    mark_apply_result,
    release_claim,
    update_worker_state,
)

log = logging.getLogger(__name__)
_RESULT_PATTERN = re.compile(
    r"RESULT:(APPLIED|REVIEW_READY|EXPIRED|CAPTCHA|NEEDS_REVIEW|FAILED)(?::([^\n\r]+))?",
    re.IGNORECASE,
)
_PLAYWRIGHT_TOOLS = (
    "mcp__playwright__browser_click",
    "mcp__playwright__browser_file_upload",
    "mcp__playwright__browser_fill_form",
    "mcp__playwright__browser_handle_dialog",
    "mcp__playwright__browser_hover",
    "mcp__playwright__browser_navigate",
    "mcp__playwright__browser_navigate_back",
    "mcp__playwright__browser_navigate_forward",
    "mcp__playwright__browser_press_key",
    "mcp__playwright__browser_select_option",
    "mcp__playwright__browser_snapshot",
    "mcp__playwright__browser_tabs",
    "mcp__playwright__browser_take_screenshot",
    "mcp__playwright__browser_type",
    "mcp__playwright__browser_wait_for",
)


def _mcp_config(port: int, *, windows: bool | None = None) -> dict[str, Any]:
    package = os.environ.get("TIAAA_PLAYWRIGHT_MCP_PACKAGE", "@playwright/mcp@0.0.78")
    windows = platform.system() == "Windows" if windows is None else windows
    installed_command = os.environ.get("TIAAA_PLAYWRIGHT_MCP_COMMAND")
    command = installed_command or ("cmd" if windows else "npx")
    prefix = [] if installed_command else (["/c", "npx"] if windows else [])
    package_args = [] if installed_command else ["-y", package]
    return {
        "mcpServers": {
            "playwright": {
                "command": command,
                "args": prefix
                + package_args
                + [
                    f"--cdp-endpoint=http://127.0.0.1:{port}",
                    "--viewport-size=1280x900",
                ],
            }
        }
    }


def _extract_agent_text(output: str) -> str:
    result_parts: list[str] = []
    plain_parts: list[str] = []
    for line in output.splitlines():
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            plain_parts.append(line)
            continue
        if message.get("type") == "result":
            result_parts.append(str(message.get("result", "")))
    return "\n".join(result_parts or plain_parts)


def _claude_command(*, model: str, config_path: Path) -> list[str]:
    """Expose only the browser interaction tools required by the application agent."""

    return [
        "claude",
        "--model",
        model,
        "-p",
        "--mcp-config",
        str(config_path),
        "--strict-mcp-config",
        "--setting-sources",
        "",
        "--tools",
        "",
        "--allowedTools",
        ",".join(_PLAYWRIGHT_TOOLS),
        "--permission-mode",
        "dontAsk",
        "--no-session-persistence",
        "--output-format",
        "stream-json",
        "--verbose",
        "-",
    ]


def _parse_result(text: str, *, submit: bool) -> tuple[str, str | None]:
    matches = list(_RESULT_PATTERN.finditer(text))
    if not matches:
        return "failed", "agent returned no result code"
    match = matches[-1]
    code = match.group(1).casefold()
    detail = (match.group(2) or "").strip().strip("*` .") or None
    mapping = {
        "review_ready": "review_ready",
        "needs_review": "needs_review",
        "captcha": "captcha",
        "expired": "expired",
        "failed": "failed",
        "applied": "applied" if submit else "review_ready",
    }
    return mapping[code], detail


def _run_agent(
    *,
    job: dict[str, Any],
    profile: dict[str, Any],
    paths: AppPaths,
    worker_id: int,
    port: int,
    model: str,
    timeout: int,
    submit: bool,
) -> tuple[str, str | None]:
    worker_dir = paths.workers / f"worker-{worker_id}"
    worker_dir.mkdir(parents=True, exist_ok=True)
    with suppress(OSError):
        worker_dir.chmod(0o700)
    prompt = build_prompt(
        job=job,
        profile=profile,
        paths=paths,
        worker_dir=worker_dir,
        submit=submit,
    )
    config_path = worker_dir / "playwright-mcp.json"
    config_path.write_text(json.dumps(_mcp_config(port), indent=2), encoding="utf-8")
    with suppress(OSError):
        config_path.chmod(0o600)
    command = _claude_command(model=model, config_path=config_path)
    environment = os.environ.copy()
    environment.pop("CLAUDECODE", None)
    environment.pop("CLAUDE_CODE_ENTRYPOINT", None)
    try:
        process = subprocess.run(
            command,
            input=prompt,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=worker_dir,
            env=environment,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "failed", f"agent timed out after {timeout}s"

    agent_text = _extract_agent_text(process.stdout)
    if os.environ.get("TIAAA_DEBUG_AGENT_OUTPUT") == "1":
        debug_path = paths.logs / f"agent-job-{job['id']}-worker-{worker_id}.log"
        debug_path.write_text(process.stdout, encoding="utf-8")
        with suppress(OSError):
            debug_path.chmod(0o600)
    if process.returncode != 0 and not _RESULT_PATTERN.search(agent_text):
        return "failed", f"claude exited with status {process.returncode}"
    return _parse_result(agent_text, submit=submit)


def _worker(
    *,
    worker_id: int,
    quota: int,
    target_job_id: int | None,
    profile: dict[str, Any],
    settings: dict[str, Any],
    paths: AppPaths,
    db_path: str | Path | None,
    submit: bool,
) -> dict[str, int]:
    automation = settings.get("automation", {})
    connection = get_connection(db_path)
    chrome_process = None
    preview: PreviewCapture | None = None
    worker_name = f"worker-{worker_id}"
    preview_path = paths.previews / f"{worker_name}.jpg"
    totals = {"applied": 0, "review": 0, "failed": 0, "expired": 0}
    update_worker_state(
        connection,
        worker_name,
        status="starting",
        message="Launching an isolated browser",
        screenshot_path=str(preview_path.resolve()),
    )
    try:
        chrome_process, port = launch_chrome(
            worker_id=worker_id,
            paths=paths,
            headless=(
                bool(automation.get("headless", False))
                or os.environ.get("TIAAA_FORCE_HEADLESS") == "1"
            ),
        )
        preview = PreviewCapture(port=port, output_path=preview_path)
        preview.start()
        update_worker_state(
            connection,
            worker_name,
            status="idle",
            message="Browser ready; looking for prepared applications",
            screenshot_path=str(preview_path.resolve()),
        )
        for _ in range(quota):
            job = claim_next_job(
                connection,
                worker_id=worker_name,
                max_attempts=int(automation.get("max_attempts", 3)),
                target_job_id=target_job_id,
            )
            if job is None:
                break
            update_worker_state(
                connection,
                worker_name,
                status="applying",
                job=job,
                message="Filling the application in the browser",
                screenshot_path=str(preview_path.resolve()),
            )
            try:
                result, detail = _run_agent(
                    job=job,
                    profile=profile,
                    paths=paths,
                    worker_id=worker_id,
                    port=port,
                    model=str(automation.get("claude_model", "sonnet")),
                    timeout=int(automation.get("timeout_seconds", 600)),
                    submit=submit,
                )
                mark_apply_result(connection, int(job["id"]), result, detail)
                result_message = {
                    "applied": "Application submitted",
                    "expired": "Listing is no longer available",
                    "review_ready": "Application is ready for your review",
                    "needs_review": "Application needs your review",
                    "captcha": "Paused for CAPTCHA review",
                    "failed": "Application attempt failed",
                }.get(result, result.replace("_", " ").title())
                update_worker_state(
                    connection,
                    worker_name,
                    status="complete" if result == "applied" else result,
                    job=job,
                    message=f"{result_message}{f': {detail}' if detail else ''}",
                    screenshot_path=str(preview_path.resolve()),
                )
                if result == "applied":
                    totals["applied"] += 1
                elif result == "expired":
                    totals["expired"] += 1
                elif result in {"review_ready", "needs_review", "captcha"}:
                    totals["review"] += 1
                else:
                    totals["failed"] += 1
            except KeyboardInterrupt:
                release_claim(connection, int(job["id"]), "interrupted")
                raise
            except Exception as exc:
                log.exception("Application worker failed for job %s", job["id"])
                mark_apply_result(connection, int(job["id"]), "failed", str(exc))
                update_worker_state(
                    connection,
                    worker_name,
                    status="failed",
                    job=job,
                    message=str(exc)[:500],
                    screenshot_path=str(preview_path.resolve()),
                )
                totals["failed"] += 1
            if target_job_id is not None:
                break
    except Exception as exc:
        update_worker_state(
            connection,
            worker_name,
            status="failed",
            message=str(exc)[:500],
            screenshot_path=str(preview_path.resolve()),
        )
        raise
    finally:
        if preview is not None:
            preview.stop()
        stop_chrome(chrome_process)
        current = connection.execute(
            "SELECT job_id, company, role, message FROM worker_state WHERE worker_id = ?",
            (worker_name,),
        ).fetchone()
        last_job = (
            {
                "id": current["job_id"],
                "company": current["company"],
                "role": current["role"],
            }
            if current and current["job_id"]
            else None
        )
        update_worker_state(
            connection,
            worker_name,
            status="idle",
            job=last_job,
            message=(current["message"] if current else None) or "Waiting for the next cycle",
            screenshot_path=str(preview_path.resolve()),
        )
    return totals


def run_applications(
    *,
    profile: dict[str, Any],
    settings: dict[str, Any],
    paths: AppPaths,
    limit: int | None = None,
    workers: int = 1,
    submit: bool = False,
    target_job_id: int | None = None,
    db_path: str | Path | None = None,
) -> dict[str, int]:
    """Run a bounded batch; continuous polling is orchestrated by `tiaaa watch`."""

    automation = settings.get("automation", {})
    if submit and not bool(automation.get("allow_submission")):
        raise PermissionError(
            "Submission is disabled in settings.yaml. Set automation.allow_submission: true "
            "and keep using --submit to opt in."
        )
    workers = max(1, min(int(workers), 8))
    if target_job_id is not None:
        workers = 1
        limit = 1
    cycle_cap = int(automation.get("max_applications_per_cycle", 5))
    requested = cycle_cap if limit is None else max(0, min(int(limit), cycle_cap))
    connection = init_db(db_path)
    if submit:
        remaining_today = max(
            0, int(automation.get("max_applications_per_day", 25)) - applications_today(connection)
        )
        requested = min(requested, remaining_today)
    requested = min(
        requested,
        claimable_application_count(
            connection,
            max_attempts=int(automation.get("max_attempts", 3)),
            target_job_id=target_job_id,
        ),
    )
    if requested <= 0:
        return {"applied": 0, "review": 0, "failed": 0, "expired": 0}
    if shutil.which("claude") is None:
        raise FileNotFoundError("Claude Code CLI was not found on PATH")
    if not os.environ.get("TIAAA_PLAYWRIGHT_MCP_COMMAND") and shutil.which("npx") is None:
        raise FileNotFoundError("npx was not found on PATH; Node.js is required for Playwright MCP")

    base, extra = divmod(requested, workers)
    quotas = [base + (1 if worker_id < extra else 0) for worker_id in range(workers)]
    totals = {"applied": 0, "review": 0, "failed": 0, "expired": 0}
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tiaaa-apply") as executor:
        futures = [
            executor.submit(
                _worker,
                worker_id=worker_id,
                quota=quota,
                target_job_id=target_job_id,
                profile=profile,
                settings=settings,
                paths=paths,
                db_path=db_path,
                submit=submit,
            )
            for worker_id, quota in enumerate(quotas)
            if quota > 0
        ]
        for future in as_completed(futures):
            result = future.result()
            with lock:
                for key in totals:
                    totals[key] += result[key]
    return totals
