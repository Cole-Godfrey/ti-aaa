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
    get_app_state,
    get_connection,
    init_db,
    recover_stale_work,
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

            state = get_app_state(connection)
            prepared = {"prepared": 0, "errors": 0}
            applied = {"applied": 0, "review": 0, "failed": 0, "expired": 0}
            onboarding_complete = bool(state.get("onboarding_complete"))
            baseline_complete = source_baseline_complete(
                connection, expected_documents=len(SOURCE_DOCUMENTS)
            )
            service_settings = settings.get("service", {})
            if (
                onboarding_complete
                and baseline_complete
                and bool(service_settings.get("auto_prepare", True))
            ):
                set_app_state(connection, "service_status", "preparing")
                set_app_state(connection, "service_message", "Selecting and tailoring resumes")
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

            automation = settings.get("automation", {})
            if onboarding_complete and baseline_complete and bool(automation.get("enabled")):
                set_app_state(connection, "service_status", "applying")
                set_app_state(connection, "service_message", "Browser application workers are active")
                from tiaaa.apply import run_applications

                applied = run_applications(
                    profile=profile,
                    settings=settings,
                    paths=self.paths,
                    workers=int(automation.get("workers", 1)),
                    submit=bool(automation.get("allow_submission")),
                    db_path=self.db_path,
                )

            summary: dict[str, Any] = {
                "status": "complete",
                "started_at": started,
                "completed_at": datetime.now(UTC).isoformat(),
                "sync": sync_summary,
                "baseline_complete": baseline_complete,
                "preparation": prepared,
                "applications": applied,
            }
            set_app_state(connection, "last_cycle", summary)
            set_app_state(connection, "last_cycle_at", summary["completed_at"])
            if not onboarding_complete:
                message = "Baseline is protected; finish onboarding to enable preparation"
            elif not baseline_complete:
                message = "Finishing the protected first-sync baseline before preparing anything"
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
