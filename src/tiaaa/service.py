"""Always-on polling and application orchestration for the local web app."""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tiaaa.config import (
    SOURCE_DOCUMENTS,
    AppPaths,
    ensure_dirs,
    initialize_user_files,
    load_environment,
    load_profile,
    load_settings,
)
from tiaaa.database import (
    answer_agent_inputs,
    get_app_state,
    get_connection,
    init_db,
    list_application_queue,
    manual_application_ids,
    recover_stale_work,
    refresh_qualification_scores,
    request_final_submission,
    request_human_control_return,
    request_manual_application,
    retry_manual_application,
    set_app_state,
    source_baseline_complete,
)

log = logging.getLogger(__name__)


class AutomationService:
    """Run safe, bounded automation cycles independently of browser clients."""

    def __init__(self, paths: AppPaths, db_path: str | Path | None = None) -> None:
        self.paths = ensure_dirs(paths)
        self.db_path = Path(db_path or paths.database).expanduser().resolve()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._force_cycle = threading.Event()
        self._cycle_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.running:
            return
        initialize_user_files(self.paths)
        load_environment(self.paths)
        connection = init_db(self.db_path)
        recovered = recover_stale_work(connection)
        set_app_state(connection, "service_status", "starting")
        set_app_state(
            connection,
            "service_message",
            f"Recovered {recovered} interrupted application(s)" if recovered else "Starting",
        )
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="tiaaa-background-service",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 15) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        connection = get_connection(self.db_path)
        set_app_state(connection, "service_status", "stopped")
        set_app_state(connection, "service_message", "Background service stopped")

    def pause(self) -> None:
        connection = get_connection(self.db_path)
        set_app_state(connection, "service_paused", True)
        set_app_state(connection, "service_status", "paused")
        set_app_state(connection, "service_message", "Polling and automation are paused")
        self._wake.set()

    def resume(self) -> None:
        connection = get_connection(self.db_path)
        set_app_state(connection, "service_paused", False)
        set_app_state(connection, "service_status", "waiting")
        set_app_state(connection, "service_message", "Resuming with a fresh repository poll")
        self._force_cycle.set()
        self._wake.set()

    def trigger(self) -> None:
        """Request a cycle without allowing overlapping runs."""

        self._force_cycle.set()
        self._wake.set()

    def nudge(self) -> None:
        """Reload scheduling/configuration without overriding a disabled service."""

        self._wake.set()

    def request_application(self, job_id: int) -> dict[str, Any]:
        """Queue one user-selected listing and wake the background worker."""

        connection = get_connection(self.db_path)
        if bool(get_app_state(connection).get("service_paused")):
            raise RuntimeError("Resume the background service before starting an application")
        job = request_manual_application(connection, job_id)
        if job is None:
            raise LookupError("Job not found")
        set_app_state(connection, "service_status", "requested")
        set_app_state(
            connection,
            "service_message",
            f"Application requested for {job['company']} · {job['role']}",
        )
        self.trigger()
        return job

    def continue_application(
        self,
        job_id: int,
        answers: dict[str, Any],
    ) -> dict[str, Any]:
        """Save candidate answers and continue the active application session."""

        connection = get_connection(self.db_path)
        if bool(get_app_state(connection).get("service_paused")):
            raise RuntimeError("Resume the background service before continuing an application")
        job = answer_agent_inputs(connection, job_id, answers)
        if job is None:
            raise LookupError("Job not found")
        set_app_state(connection, "service_status", "requested")
        set_app_state(
            connection,
            "service_message",
            f"Continuing application for {job['company']} · {job['role']}",
        )
        self.trigger()
        return job

    def retry_application(self, job_id: int) -> dict[str, Any]:
        """Cancel a review checkpoint and queue a clean browser attempt."""

        connection = get_connection(self.db_path)
        if bool(get_app_state(connection).get("service_paused")):
            raise RuntimeError("Resume the background service before retrying an application")
        job = retry_manual_application(connection, job_id)
        if job is None:
            raise LookupError("Job not found")
        set_app_state(connection, "service_status", "requested")
        set_app_state(
            connection,
            "service_message",
            f"Retry requested for {job['company']} · {job['role']}",
        )
        self.trigger()
        return job

    def confirm_submission(self, job_id: int) -> dict[str, Any]:
        """Authorize final submission in the still-open manual browser session."""

        connection = get_connection(self.db_path)
        if bool(get_app_state(connection).get("service_paused")):
            raise RuntimeError("Resume the background service before submitting")
        job = request_final_submission(connection, job_id)
        if job is None:
            raise LookupError("Job not found")
        set_app_state(connection, "service_status", "applying")
        set_app_state(
            connection,
            "service_message",
            f"Submitting application for {job['company']} · {job['role']}",
        )
        self._wake.set()
        return job

    def return_browser_control(self, job_id: int) -> dict[str, Any]:
        """Tell a retained CAPTCHA session that candidate interaction is complete."""

        connection = get_connection(self.db_path)
        if bool(get_app_state(connection).get("service_paused")):
            raise RuntimeError("Resume the background service before returning browser control")
        job = request_human_control_return(connection, job_id)
        if job is None:
            raise LookupError("Job not found")
        set_app_state(connection, "service_status", "applying")
        set_app_state(
            connection,
            "service_message",
            f"Resuming application for {job['company']} · {job['role']}",
        )
        self._wake.set()
        return job

    def snapshot(self) -> dict[str, Any]:
        state = get_app_state(get_connection(self.db_path))
        state["process_running"] = self.running
        return state

    def _loop(self) -> None:
        run_immediately = True
        while not self._stop.is_set():
            try:
                settings = load_settings(self.paths)
                state = get_app_state(get_connection(self.db_path))
                paused = bool(state.get("service_paused"))
                enabled = bool(settings.get("service", {}).get("enabled", True))
                forced = self._force_cycle.is_set()
                if not paused and (enabled or forced) and (run_immediately or forced):
                    self._force_cycle.clear()
                    self.run_cycle()
                elif paused:
                    self._set_status("paused", "Polling and automation are paused")
                elif not enabled:
                    self._set_status("disabled", "Background polling is disabled in Settings")
            except Exception as exc:  # keep the daemon alive after any cycle failure
                log.exception("Background service cycle failed")
                self._set_status("error", str(exc)[:500])

            run_immediately = False
            if self._stop.is_set():
                break
            if self._force_cycle.is_set():
                continue
            settings = load_settings(self.paths)
            interval = max(30, int(settings.get("poll_interval_seconds", 300)))
            next_cycle = datetime.now(UTC) + timedelta(seconds=interval)
            set_app_state(get_connection(self.db_path), "next_cycle_at", next_cycle.isoformat())
            self._wake.clear()
            if self._force_cycle.is_set():
                continue
            self._wake.wait(interval)
            if not self._wake.is_set():
                run_immediately = True

    def _set_status(self, status: str, message: str) -> None:
        connection = get_connection(self.db_path)
        set_app_state(connection, "service_status", status)
        set_app_state(connection, "service_message", message)

    def run_cycle(self) -> dict[str, Any]:
        """Synchronously execute one cycle; useful to both the daemon and tests."""

        if not self._cycle_lock.acquire(blocking=False):
            return {"status": "busy"}
        started = datetime.now(UTC).isoformat()
        connection = get_connection(self.db_path)
        set_app_state(connection, "service_status", "syncing")
        set_app_state(connection, "service_message", "Checking internship repositories for additions")
        set_app_state(connection, "cycle_started_at", started)
        try:
            load_environment(self.paths)
            profile = load_profile(self.paths)
            settings = load_settings(self.paths)

            from tiaaa.discovery.github import sync_repositories

            sync_results = sync_repositories(
                profile=profile,
                settings=settings,
                force=False,
                source_key=None,
                db_path=str(self.db_path),
            )
            sync_summary = {
                "documents": len(sync_results),
                "new": sum(result.new for result in sync_results),
                "queued": sum(result.queued for result in sync_results),
                "errors": sum(result.status == "error" for result in sync_results),
            }
            use_llm_scores = bool(settings.get("preparation", {}).get("use_llm"))
            refresh_qualification_scores(
                connection,
                profile=profile,
                settings=settings,
                preserve_scores=use_llm_scores,
            )

            state = get_app_state(connection)
            prepared = {"prepared": 0, "errors": 0}
            applied = {"applied": 0, "review": 0, "failed": 0, "expired": 0}
            push_delivery = {
                "subscriptions": 0,
                "sent": 0,
                "jobs": 0,
                "removed": 0,
                "errors": 0,
            }
            onboarding_complete = bool(state.get("onboarding_complete"))
            baseline_complete = source_baseline_complete(
                connection, expected_documents=len(SOURCE_DOCUMENTS)
            )
            service_settings = settings.get("service", {})
            automation = settings.get("automation", {})
            auto_mode = bool(automation.get("auto_apply_new", False))
            manual_pending = bool(manual_application_ids(connection))
            if (
                onboarding_complete
                and baseline_complete
                and (
                    manual_pending
                    or auto_mode
                    or bool(service_settings.get("auto_prepare", True))
                )
            ):
                set_app_state(connection, "service_status", "preparing")
                set_app_state(connection, "service_message", "Selecting application resumes")
                if settings.get("preparation", {}).get("use_llm"):
                    from tiaaa.preparation import score_jobs_with_llm

                    score_jobs_with_llm(paths=self.paths, db_path=self.db_path)
                from tiaaa.preparation import prepare_jobs

                prepared = prepare_jobs(
                    paths=self.paths,
                    profile=profile,
                    settings=settings,
                    db_path=self.db_path,
                )

            if (
                onboarding_complete
                and baseline_complete
                and auto_mode
                and bool(automation.get("web_push_notifications", False))
            ):
                try:
                    from tiaaa.web_push import send_auto_queue_notifications

                    queue = list_application_queue(
                        connection,
                        auto_enabled=True,
                        max_attempts=int(automation.get("max_attempts", 3)),
                        minimum_fit_score=int(
                            automation.get("auto_apply_minimum_fit_score", 7)
                        ),
                        profile=profile,
                        use_preferences=bool(
                            automation.get("auto_apply_use_preferences", False)
                        ),
                    )
                    push_delivery = send_auto_queue_notifications(
                        connection,
                        paths=self.paths,
                        queue=queue,
                    )
                except Exception as exc:
                    log.warning("Browser notification delivery failed: %s", exc)
                    push_delivery["errors"] += 1

            manual_ids = manual_application_ids(connection)
            if onboarding_complete and baseline_complete and (
                manual_ids or auto_mode
            ):
                set_app_state(connection, "service_status", "applying")
                set_app_state(connection, "service_message", "Browser application workers are active")
                from tiaaa.apply import run_applications

                manual_auto_submit = bool(
                    automation.get("manual_auto_submit", False)
                )
                for job_id in manual_ids:
                    result = run_applications(
                        profile=profile,
                        settings=settings,
                        paths=self.paths,
                        limit=1,
                        workers=1,
                        submit=manual_auto_submit,
                        unattended=False,
                        interactive_review=not manual_auto_submit,
                        manual_selection_auto_submit=manual_auto_submit,
                        target_job_id=job_id,
                        db_path=self.db_path,
                    )
                    for key in applied:
                        applied[key] += result[key]
                if auto_mode:
                    result = run_applications(
                        profile=profile,
                        settings=settings,
                        paths=self.paths,
                        workers=1,
                        submit=True,
                        unattended=True,
                        db_path=self.db_path,
                    )
                    for key in applied:
                        applied[key] += result[key]

            summary: dict[str, Any] = {
                "status": "complete",
                "started_at": started,
                "completed_at": datetime.now(UTC).isoformat(),
                "sync": sync_summary,
                "baseline_complete": baseline_complete,
                "preparation": prepared,
                "applications": applied,
                "web_push": push_delivery,
            }
            set_app_state(connection, "last_cycle", summary)
            set_app_state(connection, "last_cycle_at", summary["completed_at"])
            if not onboarding_complete:
                message = "Finish onboarding to enable preparation and application actions"
            elif not baseline_complete:
                message = "Finishing the first repository import"
            elif sync_summary["errors"]:
                message = f"Cycle finished with {sync_summary['errors']} source error(s)"
            else:
                message = (
                    f"Cycle complete · {sync_summary['new']} new · "
                    f"{prepared['prepared']} prepared · {applied['applied']} applied"
                )
            self._set_status("waiting", message)
            return summary
        except Exception as exc:
            failed = {
                "status": "error",
                "started_at": started,
                "completed_at": datetime.now(UTC).isoformat(),
                "error": str(exc)[:1000],
            }
            set_app_state(connection, "last_cycle", failed)
            set_app_state(connection, "last_cycle_at", failed["completed_at"])
            self._set_status("error", str(exc)[:500])
            raise
        finally:
            self._cycle_lock.release()


def service_for(
    paths: AppPaths, db_path: str | Path | None = None
) -> AutomationService:
    """Small factory kept separate so the dashboard can replace it in tests."""

    return AutomationService(paths, db_path=db_path)
