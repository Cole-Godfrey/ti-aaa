"""FastAPI backend for the local application tracker dashboard."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from tiaaa import __version__
from tiaaa.database import (
    get_connection,
    get_job,
    get_stats,
    init_db,
    list_jobs,
    source_status,
    update_tracker,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"


class TrackerUpdate(BaseModel):
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


def create_app(db_path: str | Path | None = None) -> FastAPI:
    init_db(db_path)
    app = FastAPI(
        title="TI-AAA Dashboard",
        version=__version__,
        docs_url="/api/docs",
        redoc_url=None,
    )
    app.state.db_path = db_path
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
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
        return response

    def connection():
        return get_connection(app.state.db_path)

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/api/stats")
    def stats() -> dict:
        return get_stats(connection())

    @app.get("/api/jobs")
    def jobs(
        status: str | None = None,
        search: str | None = Query(default=None, max_length=200),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> dict:
        rows = list_jobs(
            connection(), status=status, search=search, limit=limit, offset=offset
        )
        return {"items": rows, "limit": limit, "offset": offset}

    @app.get("/api/jobs/{job_id}")
    def job(job_id: int) -> dict:
        row = get_job(connection(), job_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return row

    @app.patch("/api/jobs/{job_id}")
    def patch_job(job_id: int, update: TrackerUpdate) -> dict:
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
        return row

    @app.get("/api/sources")
    def sources() -> dict:
        return {"items": source_status(connection())}

    @app.get("/api/events")
    def events(limit: int = Query(default=20, ge=1, le=100)) -> dict:
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
