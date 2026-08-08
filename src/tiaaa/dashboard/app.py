"""FastAPI backend for the local TI-AAA application."""

from __future__ import annotations

import asyncio
import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlparse, urlsplit

from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from tiaaa import __version__
from tiaaa.apply.preview import preview_frame_hub
from tiaaa.claude_auth import ClaudeAuthManager
from tiaaa.config import (
    SOURCE_DOCUMENTS,
    AppPaths,
    ensure_dirs,
    get_chrome_path,
    get_paths,
    initialize_user_files,
    load_environment,
    load_profile,
    load_settings,
    save_profile,
    save_settings,
    secret_status,
    update_secrets,
)
from tiaaa.database import (
    archive_resume,
    close_all_connections,
    get_analytics,
    get_app_state,
    get_connection,
    get_job,
    get_stats,
    get_worker_states,
    init_db,
    list_agent_inputs,
    list_application_queue,
    list_jobs,
    list_resumes,
    record_dashboard_visit,
    set_app_state,
    source_baseline_complete,
    source_status,
    update_tracker,
)
from tiaaa.resumes import MAX_RESUME_BYTES, store_resume
from tiaaa.service import AutomationService, service_for
from tiaaa.web_push import (
    push_subscription_count,
    remove_push_subscription,
    store_push_subscription,
    vapid_public_key,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
UNSAFE_HTTP_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
DEFAULT_TRUSTED_HOSTS = {"127.0.0.1", "::1", "localhost", "testserver"}


def _authority(value: str) -> tuple[str, int | None] | None:
    """Return a normalized Host/Origin authority without accepting URL components."""

    try:
        parsed = urlsplit(f"//{value}")
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if (
        not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return None
    return hostname.casefold().rstrip("."), port


def _trusted_host_patterns(extra: tuple[str, ...] | None) -> tuple[str, ...]:
    configured = os.environ.get("TIAAA_TRUSTED_HOSTS", "")
    values = set(DEFAULT_TRUSTED_HOSTS)
    values.update(item.strip() for item in configured.split(",") if item.strip())
    values.update(item.strip() for item in (extra or ()) if item.strip())
    return tuple(sorted(values))


def _host_matches(hostname: str, patterns: tuple[str, ...]) -> bool:
    for raw_pattern in patterns:
        pattern = raw_pattern.casefold().strip().strip("[]").rstrip(".")
        if pattern.startswith("*."):
            suffix = pattern[1:]
            if hostname.endswith(suffix) and hostname != suffix[1:]:
                return True
        elif hostname == pattern:
            return True
    return False


def _host_is_trusted(host_header: str, patterns: tuple[str, ...]) -> bool:
    authority = _authority(host_header)
    return bool(authority and _host_matches(authority[0], patterns))


def _origin_matches_host(origin: str, host_header: str) -> bool:
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return False
    return _authority(parsed.netloc) == _authority(host_header)


def _browser_request_is_same_origin(
    *, host_header: str, origin: str | None, fetch_site: str | None
) -> bool:
    if (fetch_site or "").casefold() == "cross-site":
        return False
    return origin is None or _origin_matches_host(origin, host_header)


class TrackerUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pipeline_status: Literal[
        "discovered",
        "queued",
        "ready",
        "applying",
        "manual_review",
        "applied",
        "failed",
        "skipped",
        "expired",
        "withdrawn",
    ] | None = None
    outcome_status: Literal["none", "oa", "interview", "offer", "rejected", "withdrawn"] | None = None
    notes: str | None = Field(default=None, max_length=10000)


class ConfigurationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: dict[str, Any] | None = None
    settings: dict[str, Any] | None = None
    secrets: dict[str, str | None] = Field(default_factory=dict)
    clear_secrets: list[str] = Field(default_factory=list)
    onboarding_complete: bool | None = None


class ClaudeAuthCode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=4096)


class AgentInputAnswers(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answers: dict[str, Any] = Field(default_factory=dict)


class WebPushKeys(BaseModel):
    model_config = ConfigDict(extra="forbid")

    p256dh: str = Field(min_length=16, max_length=1024)
    auth: str = Field(min_length=8, max_length=512)


class WebPushSubscription(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint: str = Field(min_length=16, max_length=4096)
    keys: WebPushKeys


class WebPushRemoval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint: str = Field(min_length=16, max_length=4096)


def _public_resume(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in row.items()
        if key not in {"pdf_path", "text_path"}
    }


def _public_job(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for key in (
        "base_resume_text_path",
        "resume_path",
        "submitted_resume_path",
        "cover_letter_path",
    ):
        result.pop(key, None)
    return result


def _safe_file(path_value: str | None, roots: tuple[Path, ...]) -> Path:
    if not path_value:
        raise HTTPException(status_code=404, detail="File not found")
    path = Path(path_value).expanduser().resolve()
    allowed = any(path.is_relative_to(root.resolve()) for root in roots)
    if not allowed or not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return path


def create_app(
    db_path: str | Path | None = None,
    *,
    paths: AppPaths | None = None,
    start_service: bool = False,
    trusted_hosts: tuple[str, ...] | None = None,
) -> FastAPI:
    if paths is None:
        paths = get_paths(Path(db_path).parent if db_path is not None else None)
    paths = ensure_dirs(paths)
    initialize_user_files(paths)
    load_environment(paths)
    database_path = Path(db_path).resolve() if db_path is not None else paths.database
    init_db(database_path)
    claude_auth = ClaudeAuthManager(paths)
    background_service: AutomationService | None = (
        service_for(paths, database_path) if start_service else None
    )
    allowed_hosts = _trusted_host_patterns(trusted_hosts)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if background_service is not None:
            background_service.start()
        try:
            yield
        finally:
            if background_service is not None:
                background_service.stop()
            claude_auth.close()
            close_all_connections(database_path)

    app = FastAPI(
        title="TI-AAA Dashboard",
        version=__version__,
        docs_url="/api/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.db_path = database_path
    app.state.paths = paths
    app.state.service = background_service
    app.state.claude_auth = claude_auth
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        host_header = request.headers.get("host", "")
        if not _host_is_trusted(host_header, allowed_hosts):
            return JSONResponse(status_code=400, content={"detail": "Invalid host header"})
        if request.method in UNSAFE_HTTP_METHODS and not _browser_request_is_same_origin(
            host_header=host_header,
            origin=request.headers.get("origin"),
            fetch_site=request.headers.get("sec-fetch-site"),
        ):
            return JSONResponse(status_code=403, content={"detail": "Cross-origin request blocked"})
        response = await call_next(request)
        if request.url.path == "/api/docs":
            script_sources = "'self' 'unsafe-inline' https://cdn.jsdelivr.net"
        else:
            script_sources = "'self'"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; "
            f"script-src {script_sources}; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "img-src 'self' data:; connect-src 'self'; form-action 'self'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith("/api/") and "cache-control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        return response

    def connection():
        return get_connection(app.state.db_path)

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/sw.js", include_in_schema=False)
    def service_worker() -> FileResponse:
        response = FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript")
        response.headers["Cache-Control"] = "no-cache"
        response.headers["Service-Worker-Allowed"] = "/"
        return response

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/api/onboarding")
    def onboarding() -> dict[str, Any]:
        profile = load_profile(paths)
        state = get_app_state(connection())
        resumes = list_resumes(connection())
        full_name = str(profile.get("personal", {}).get("full_name", ""))
        try:
            chrome = get_chrome_path()
        except FileNotFoundError:
            chrome = None
        return {
            "complete": bool(state.get("onboarding_complete")),
            "profile_ready": bool(full_name and not full_name.startswith("YOUR ")),
            "resume_count": len(resumes),
            "baseline_complete": source_baseline_complete(
                connection(), expected_documents=len(SOURCE_DOCUMENTS)
            ),
            "secrets": secret_status(paths),
            "tools": {
                "claude": bool(shutil.which("claude")),
                "npx": bool(shutil.which("npx")),
                "chrome": bool(chrome),
            },
        }

    @app.get("/api/config")
    def configuration() -> dict[str, Any]:
        return {
            "profile": load_profile(paths),
            "settings": load_settings(paths),
            "secrets": secret_status(paths),
        }

    def push_status() -> dict[str, Any]:
        automation = load_settings(paths).get("automation", {})
        return {
            "public_key": vapid_public_key(paths),
            "subscription_count": push_subscription_count(connection()),
            "enabled": bool(
                automation.get("auto_apply_new", False)
                and automation.get("web_push_notifications", False)
            ),
        }

    @app.get("/api/push")
    def get_push_status() -> dict[str, Any]:
        return push_status()

    @app.post("/api/push/subscriptions", status_code=201)
    def subscribe_to_push(
        payload: WebPushSubscription, request: Request
    ) -> dict[str, Any]:
        endpoint = payload.endpoint.strip()
        parsed = urlparse(endpoint)
        if parsed.scheme != "https" or not parsed.netloc:
            raise HTTPException(status_code=422, detail="Push endpoint must use HTTPS")
        store_push_subscription(
            connection(),
            endpoint=endpoint,
            p256dh=payload.keys.p256dh,
            auth=payload.keys.auth,
            user_agent=request.headers.get("user-agent", "")[:500],
        )
        return push_status()

    @app.delete("/api/push/subscriptions")
    def unsubscribe_from_push(payload: WebPushRemoval) -> dict[str, Any]:
        remove_push_subscription(connection(), payload.endpoint.strip())
        return push_status()

    @app.get("/api/claude-auth")
    def claude_auth_status() -> dict[str, Any]:
        return app.state.claude_auth.status()

    @app.post("/api/claude-auth/login")
    def start_claude_auth() -> dict[str, Any]:
        try:
            return app.state.claude_auth.start_login()
        except (FileNotFoundError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/claude-auth/complete")
    def complete_claude_auth(payload: ClaudeAuthCode) -> dict[str, Any]:
        try:
            return app.state.claude_auth.complete_login(payload.code)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.delete("/api/claude-auth")
    def disconnect_claude_auth() -> dict[str, Any]:
        try:
            return app.state.claude_auth.logout()
        except (FileNotFoundError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.put("/api/config")
    def put_configuration(update: ConfigurationUpdate) -> dict[str, Any]:
        try:
            if update.profile is not None:
                save_profile(update.profile, paths)
            if update.settings is not None:
                save_settings(update.settings, paths)
            if update.secrets or update.clear_secrets:
                update_secrets(
                    update.secrets,
                    clear=update.clear_secrets,
                    paths=paths,
                )
            if update.onboarding_complete is not None:
                if update.onboarding_complete:
                    profile = load_profile(paths)
                    full_name = str(profile.get("personal", {}).get("full_name", ""))
                    if not full_name or full_name.startswith("YOUR "):
                        raise ValueError("Add your real name before finishing onboarding")
                    if not list_resumes(connection()):
                        raise ValueError("Upload at least one resume before finishing onboarding")
                set_app_state(connection(), "onboarding_complete", update.onboarding_complete)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if background_service is not None:
            if bool(load_settings(paths).get("service", {}).get("enabled", True)):
                background_service.trigger()
            else:
                background_service.nudge()
        return configuration()

    @app.get("/api/stats")
    def stats() -> dict[str, Any]:
        return get_stats(connection())

    @app.get("/api/analytics")
    def analytics() -> dict[str, Any]:
        return get_analytics(connection())

    @app.post("/api/dashboard/visit")
    def dashboard_visit() -> dict[str, Any]:
        return record_dashboard_visit(connection())

    @app.get("/api/jobs")
    def jobs(
        status: str | None = None,
        search: str | None = Query(default=None, max_length=200),
        view: Literal["tracker", "latest", "applications"] = "tracker",
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        rows = list_jobs(
            connection(),
            status=status,
            search=search,
            limit=limit,
            offset=offset,
            latest=view == "latest",
            active_only=view == "latest",
            applied_only=view == "applications",
        )
        return {"items": [_public_job(row) for row in rows], "limit": limit, "offset": offset}

    @app.get("/api/jobs/{job_id}")
    def job(job_id: int) -> dict[str, Any]:
        row = get_job(connection(), job_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Job not found")
        result = _public_job(row)
        events = connection().execute(
            """
            SELECT event_type, detail, created_at FROM events
            WHERE job_id = ? ORDER BY created_at DESC LIMIT 20
            """,
            (job_id,),
        ).fetchall()
        result["events"] = [dict(item) for item in events]
        result["application_mode"] = (
            "auto"
            if result.get("apply_origin") == "auto"
            else "manual_confirm"
        )
        return result

    @app.post("/api/jobs/{job_id}/apply", status_code=202)
    def apply_to_job(job_id: int) -> dict[str, Any]:
        state = get_app_state(connection())
        if not bool(state.get("onboarding_complete")):
            raise HTTPException(status_code=409, detail="Finish onboarding before applying")
        if not list_resumes(connection()):
            raise HTTPException(status_code=409, detail="Upload a resume before applying")
        if not bool(app.state.claude_auth.status().get("logged_in")):
            raise HTTPException(
                status_code=409,
                detail="Connect Claude Code in Settings before starting the browser agent",
            )
        service = require_service()
        try:
            row = service.request_application(job_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "status": "queued",
            "job": _public_job(row),
            "mode": "manual_confirm",
        }

    @app.post("/api/jobs/{job_id}/inputs", status_code=202)
    def submit_agent_inputs(job_id: int, payload: AgentInputAnswers) -> dict[str, Any]:
        service = require_service()
        try:
            row = service.continue_application(job_id, payload.answers)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"status": "queued", "job": _public_job(row)}

    @app.post("/api/jobs/{job_id}/submit", status_code=202)
    def confirm_job_submission(job_id: int) -> dict[str, Any]:
        service = require_service()
        try:
            row = service.confirm_submission(job_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"status": "submitting", "job": _public_job(row)}

    @app.patch("/api/jobs/{job_id}")
    def patch_job(
        job_id: int,
        update: TrackerUpdate,
    ) -> dict[str, Any]:
        try:
            row = update_tracker(
                connection(),
                job_id,
                pipeline_status=update.pipeline_status,
                outcome_status=update.outcome_status,
                notes=update.notes,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if row is None:
            raise HTTPException(status_code=404, detail="Job not found")
        if update.pipeline_status == "queued" and app.state.service is not None:
            app.state.service.trigger()
        return _public_job(row)

    @app.get("/api/jobs/{job_id}/resume")
    def job_resume(job_id: int) -> FileResponse:
        row = get_job(connection(), job_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Job not found")
        path = _safe_file(
            row.get("submitted_resume_path") or row.get("resume_path"),
            (paths.resumes, paths.packets),
        )
        return FileResponse(
            path,
            media_type="application/pdf",
            filename=path.name,
        )

    @app.get("/api/resumes")
    def resumes() -> dict[str, Any]:
        return {"items": [_public_resume(row) for row in list_resumes(connection())]}

    @app.post("/api/resumes", status_code=201)
    async def upload_resume(
        file: Annotated[UploadFile, File()],
        name: Annotated[str, Form(min_length=1, max_length=100)],
        tags: Annotated[str, Form()] = "",
        text: Annotated[str, Form()] = "",
        notes: Annotated[str, Form(max_length=1000)] = "",
    ) -> dict[str, Any]:
        if not file.filename or not file.filename.casefold().endswith(".pdf"):
            raise HTTPException(status_code=422, detail="Upload a PDF resume")
        content = await file.read(MAX_RESUME_BYTES + 1)
        try:
            row = store_resume(
                paths=paths,
                name=name,
                original_filename=file.filename,
                content=content,
                text_override=text,
                tags=[item.strip() for item in tags.split(",") if item.strip()],
                notes=notes,
                db_path=app.state.db_path,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _public_resume(row)

    @app.delete("/api/resumes/{resume_id}", status_code=204)
    def delete_resume(resume_id: int) -> None:
        active = list_resumes(connection())
        state = get_app_state(connection())
        if bool(state.get("onboarding_complete")) and len(active) <= 1:
            raise HTTPException(
                status_code=409,
                detail="Upload a replacement before archiving your only active resume",
            )
        if not archive_resume(connection(), resume_id):
            raise HTTPException(status_code=404, detail="Active resume not found")

    @app.get("/api/resumes/{resume_id}/download")
    def download_resume(resume_id: int) -> FileResponse:
        row = next((item for item in list_resumes(connection()) if item["id"] == resume_id), None)
        if row is None:
            raise HTTPException(status_code=404, detail="Resume not found")
        path = _safe_file(row.get("pdf_path"), (paths.resumes,))
        return FileResponse(path, media_type="application/pdf", filename=row["original_filename"])

    @app.get("/api/service")
    def service_status() -> dict[str, Any]:
        if background_service is not None:
            return background_service.snapshot()
        state = get_app_state(connection())
        state["process_running"] = False
        return state

    def require_service() -> AutomationService:
        service = app.state.service
        if service is None:
            raise HTTPException(
                status_code=409,
                detail="This dashboard was started without the background service; use `tiaaa serve`.",
            )
        return service

    @app.post("/api/service/run", status_code=202)
    def run_service() -> dict[str, str]:
        require_service().trigger()
        return {"status": "scheduled"}

    @app.post("/api/service/pause", status_code=202)
    def pause_service() -> dict[str, str]:
        require_service().pause()
        return {"status": "paused"}

    @app.post("/api/service/resume", status_code=202)
    def resume_service() -> dict[str, str]:
        require_service().resume()
        return {"status": "resuming"}

    @app.get("/api/workers")
    def workers() -> dict[str, Any]:
        database = connection()
        items = get_worker_states(database)
        for item in items:
            screenshot = item.pop("screenshot_path", None)
            item["preview_available"] = bool(screenshot and Path(screenshot).is_file())
            item["preview_url"] = (
                f"/api/workers/{item['worker_id']}/preview" if item["preview_available"] else None
            )
            item["stream_active"] = preview_frame_hub.is_active(str(item["worker_id"]))
            job_row = get_job(database, int(item["job_id"])) if item.get("job_id") else None
            item["questions"] = (
                list_agent_inputs(database, int(item["job_id"]), pending_only=True)
                if item.get("job_id")
                else []
            )
            if job_row is not None:
                item["pipeline_status"] = job_row.get("pipeline_status")
                item["application_url"] = job_row.get("application_url")
                item["availability_status"] = job_row.get("availability_status")
                item["availability_detail"] = job_row.get("availability_detail")
                item["review_detail"] = job_row.get("apply_error")
                item["base_resume_name"] = job_row.get("base_resume_name")
                item["submitted_resume_name"] = job_row.get("submitted_resume_name")
                item["resume_url"] = (
                    f"/api/jobs/{item['job_id']}/resume"
                    if job_row.get("resume_path") or job_row.get("submitted_resume_path")
                    else None
                )
                item["apply_origin"] = job_row.get("apply_origin")
                item["submission_ready"] = bool(
                    job_row.get("pipeline_status") == "manual_review"
                    and job_row.get("worker_id") == item.get("worker_id")
                    and item.get("status") == "review_ready"
                    and not item["questions"]
                    and job_row.get("availability_status") != "manual_only"
                )
        settings = load_settings(paths)
        automation = settings.get("automation", {})
        queue = list_application_queue(
            database,
            auto_enabled=bool(automation.get("auto_apply_new", False)),
            max_attempts=int(automation.get("max_attempts", 3)),
            minimum_fit_score=int(
                automation.get("auto_apply_minimum_fit_score", 7)
            ),
            profile=load_profile(paths),
            use_preferences=bool(
                automation.get("auto_apply_use_preferences", False)
            ),
        )
        return {
            "items": items,
            "queue": queue,
            "queue_summary": {
                "serial": True,
                "active": sum(item["queue_state"] == "active" for item in queue),
                "waiting": sum(item["queue_state"] != "active" for item in queue),
            },
        }

    @app.get("/api/workers/{worker_id}/preview")
    def worker_preview(worker_id: str) -> FileResponse:
        if not worker_id.startswith("worker-") or not worker_id[7:].isdigit():
            raise HTTPException(status_code=404, detail="Worker not found")
        path = _safe_file(str(paths.previews / f"{worker_id}.jpg"), (paths.previews,))
        response = FileResponse(path, media_type="image/jpeg")
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @app.websocket("/api/workers/{worker_id}/stream")
    async def worker_stream(websocket: WebSocket, worker_id: str) -> None:
        host_header = websocket.headers.get("host", "")
        if (
            not _host_is_trusted(host_header, allowed_hosts)
            or not _browser_request_is_same_origin(
                host_header=host_header,
                origin=websocket.headers.get("origin"),
                fetch_site=websocket.headers.get("sec-fetch-site"),
            )
            or not worker_id.startswith("worker-")
            or not worker_id[7:].isdigit()
        ):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        sequence = -1
        try:
            while True:
                frame = await asyncio.to_thread(
                    preview_frame_hub.wait_for_frame,
                    worker_id,
                    sequence,
                    10,
                )
                if frame is None:
                    await websocket.send_json({"type": "heartbeat"})
                    continue
                sequence, data = frame
                await websocket.send_bytes(data)
        except (RuntimeError, WebSocketDisconnect):
            return

    @app.get("/api/sources")
    def sources() -> dict[str, Any]:
        return {"items": source_status(connection())}

    @app.get("/api/events")
    def events(limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
        rows = connection().execute(
            """
            SELECT e.id, e.job_id, e.event_type, e.detail, e.created_at,
                   j.company, j.role
            FROM events e LEFT JOIN jobs j ON j.id = e.job_id
            ORDER BY e.created_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return {"items": [dict(row) for row in rows]}

    return app
