"""SQLite persistence for discovery, application state, and tracker analytics."""

from __future__ import annotations

import atexit
import json
import re
import sqlite3
import threading
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from tiaaa.config import SOURCE_DOCUMENTS, get_paths
from tiaaa.discovery.parser import canonicalize_url, listing_fingerprint
from tiaaa.eligibility import evaluate_listing, matches_preferences
from tiaaa.models import InternshipListing, SourceDocument

_local = threading.local()
_connection_registry: dict[str, list[sqlite3.Connection]] = {}
_connection_registry_lock = threading.Lock()

PIPELINE_STATUSES = {
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
}
OUTCOME_STATUSES = {"none", "oa", "interview", "offer", "rejected", "withdrawn"}
TERMINAL_PIPELINE_STATUSES = {"applied", "skipped", "withdrawn"}
AVAILABILITY_STATUSES = {"unknown", "open", "closed", "manual_only"}
AGENT_INPUT_TYPES = {
    "text",
    "textarea",
    "email",
    "tel",
    "number",
    "date",
    "select",
    "boolean",
}
_SENSITIVE_INPUT_PATTERN = re.compile(
    r"\b(?:password|passcode|one.?time code|verification code|mfa|otp|captcha|"
    r"social security|ssn|bank|routing|credit card|debit card|passport|"
    r"driver.?s license|government id|biometric)\b",
    re.IGNORECASE,
)
_AUTO_TERMINAL_REASON_CODES = {
    "access_blocked",
    "assessment_required",
    "captcha",
    "eligibility_conflict",
    "login_required",
    "missing_input",
    "sensitive_information",
    "verification_required",
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = str(Path(db_path or get_paths().database).expanduser().resolve())
    if not hasattr(_local, "connections"):
        _local.connections = {}
    connection = _local.connections.get(path)
    if connection is not None:
        try:
            connection.execute("SELECT 1")
            return connection
        except sqlite3.ProgrammingError:
            pass

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=10000")
    connection.execute("PRAGMA foreign_keys=ON")
    with suppress(OSError):
        Path(path).chmod(0o600)
    _local.connections[path] = connection
    with _connection_registry_lock:
        _connection_registry.setdefault(path, []).append(connection)
    return connection


def close_connection(db_path: Path | str | None = None) -> None:
    path = str(Path(db_path or get_paths().database).expanduser().resolve())
    if hasattr(_local, "connections"):
        connection = _local.connections.pop(path, None)
        if connection is not None:
            connection.close()
            with _connection_registry_lock:
                registered = _connection_registry.get(path, [])
                if connection in registered:
                    registered.remove(connection)
                if not registered:
                    _connection_registry.pop(path, None)


def close_all_connections(db_path: Path | str | None = None) -> None:
    """Close registered connections for one database, or every database at process exit."""

    requested_path = (
        str(Path(db_path).expanduser().resolve()) if db_path is not None else None
    )
    with _connection_registry_lock:
        if requested_path is None:
            registered = [
                connection
                for connections in _connection_registry.values()
                for connection in connections
            ]
            _connection_registry.clear()
        else:
            registered = _connection_registry.pop(requested_path, [])
    for connection in registered:
        with suppress(sqlite3.ProgrammingError):
            connection.close()
    if hasattr(_local, "connections"):
        if requested_path is None:
            _local.connections.clear()
        else:
            _local.connections.pop(requested_path, None)


atexit.register(close_all_connections)


def _ensure_column(
    connection: sqlite3.Connection, table: str, column: str, declaration: str
) -> None:
    columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def init_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    connection = get_connection(db_path)
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS sources (
            document_key       TEXT PRIMARY KEY,
            source_key         TEXT NOT NULL,
            label              TEXT NOT NULL,
            repo_url           TEXT NOT NULL,
            branch             TEXT NOT NULL,
            path               TEXT NOT NULL,
            raw_url            TEXT NOT NULL,
            etag               TEXT,
            last_modified      TEXT,
            content_sha256     TEXT,
            initialized        INTEGER NOT NULL DEFAULT 0,
            enabled            INTEGER NOT NULL DEFAULT 1,
            last_polled_at     TEXT,
            last_success_at    TEXT,
            last_error         TEXT
        );

        CREATE TABLE IF NOT EXISTS resumes (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            name              TEXT NOT NULL,
            original_filename TEXT NOT NULL,
            pdf_path          TEXT NOT NULL UNIQUE,
            text_path         TEXT NOT NULL,
            tags              TEXT NOT NULL DEFAULT '',
            notes             TEXT NOT NULL DEFAULT '',
            is_active         INTEGER NOT NULL DEFAULT 1,
            created_at        TEXT NOT NULL,
            updated_at        TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS jobs (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            fingerprint           TEXT NOT NULL,
            canonical_url         TEXT NOT NULL UNIQUE,
            application_url       TEXT NOT NULL,
            company               TEXT NOT NULL,
            role                  TEXT NOT NULL,
            location              TEXT NOT NULL DEFAULT '',
            category              TEXT NOT NULL DEFAULT 'Tech',
            posting_date          TEXT,
            first_seen_at         TEXT NOT NULL,
            last_seen_at          TEXT NOT NULL,
            is_active             INTEGER NOT NULL DEFAULT 1,
            availability_status   TEXT NOT NULL DEFAULT 'unknown',
            availability_detail   TEXT,
            availability_checked_at TEXT,
            no_sponsorship        INTEGER NOT NULL DEFAULT 0,
            citizenship_required  INTEGER NOT NULL DEFAULT 0,
            eligibility           TEXT NOT NULL DEFAULT 'eligible',
            eligibility_reason    TEXT,
            fit_score             INTEGER,
            score_reasoning       TEXT,
            scored_at             TEXT,
            pipeline_status       TEXT NOT NULL DEFAULT 'discovered',
            discovered_as_new     INTEGER NOT NULL DEFAULT 0,
            manual_requested      INTEGER NOT NULL DEFAULT 0,
            manual_requested_at   TEXT,
            base_resume_id        INTEGER REFERENCES resumes(id),
            submitted_resume_id   INTEGER REFERENCES resumes(id),
            resume_path           TEXT,
            submitted_resume_path TEXT,
            tailoring_reason      TEXT,
            cover_letter_path     TEXT,
            preparation_notes     TEXT,
            prepared_at           TEXT,
            apply_attempts        INTEGER NOT NULL DEFAULT 0,
            apply_origin          TEXT,
            apply_reason_code     TEXT,
            submission_requested  INTEGER NOT NULL DEFAULT 0,
            applied_at            TEXT,
            apply_error           TEXT,
            last_attempted_at      TEXT,
            worker_id             TEXT,
            oa_at                 TEXT,
            interview_at          TEXT,
            offer_at              TEXT,
            rejected_at           TEXT,
            withdrawn_at          TEXT,
            outcome_status        TEXT NOT NULL DEFAULT 'none',
            notes                 TEXT NOT NULL DEFAULT '',
            created_at            TEXT NOT NULL,
            updated_at            TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_jobs_pipeline ON jobs(pipeline_status, fit_score);
        CREATE INDEX IF NOT EXISTS idx_jobs_fingerprint ON jobs(fingerprint);
        CREATE INDEX IF NOT EXISTS idx_jobs_applied ON jobs(applied_at);
        CREATE INDEX IF NOT EXISTS idx_jobs_last_seen ON jobs(last_seen_at);

        CREATE TABLE IF NOT EXISTS job_sources (
            job_id              INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            document_key        TEXT NOT NULL REFERENCES sources(document_key) ON DELETE CASCADE,
            source_key          TEXT NOT NULL,
            source_label        TEXT NOT NULL,
            source_repo_url     TEXT NOT NULL,
            source_path         TEXT NOT NULL,
            raw_date            TEXT,
            first_seen_at       TEXT NOT NULL,
            last_seen_at        TEXT NOT NULL,
            active              INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (job_id, document_key)
        );

        CREATE INDEX IF NOT EXISTS idx_job_sources_document ON job_sources(document_key, active);

        CREATE TABLE IF NOT EXISTS events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id      INTEGER REFERENCES jobs(id) ON DELETE CASCADE,
            event_type  TEXT NOT NULL,
            detail      TEXT,
            created_at  TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS app_state (
            key         TEXT PRIMARY KEY,
            value       TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS worker_state (
            worker_id       TEXT PRIMARY KEY,
            status          TEXT NOT NULL,
            job_id          INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
            company         TEXT,
            role            TEXT,
            message         TEXT,
            screenshot_path TEXT,
            started_at      TEXT,
            updated_at      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS agent_inputs (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id         INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            input_key      TEXT NOT NULL,
            label          TEXT NOT NULL,
            input_type     TEXT NOT NULL DEFAULT 'text',
            options_json   TEXT NOT NULL DEFAULT '[]',
            required       INTEGER NOT NULL DEFAULT 1,
            answer         TEXT,
            status         TEXT NOT NULL DEFAULT 'pending',
            created_at     TEXT NOT NULL,
            answered_at    TEXT,
            updated_at     TEXT NOT NULL,
            UNIQUE(job_id, input_key)
        );

        CREATE INDEX IF NOT EXISTS idx_agent_inputs_job
            ON agent_inputs(job_id, status, id);

        CREATE TABLE IF NOT EXISTS web_push_subscriptions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            endpoint        TEXT NOT NULL UNIQUE,
            p256dh           TEXT NOT NULL,
            auth             TEXT NOT NULL,
            user_agent       TEXT,
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL,
            last_success_at  TEXT,
            last_error       TEXT
        );

        CREATE TABLE IF NOT EXISTS web_push_deliveries (
            subscription_id INTEGER NOT NULL
                            REFERENCES web_push_subscriptions(id) ON DELETE CASCADE,
            job_id           INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            delivered_at     TEXT NOT NULL,
            PRIMARY KEY (subscription_id, job_id)
        );

        CREATE INDEX IF NOT EXISTS idx_web_push_deliveries_job
            ON web_push_deliveries(job_id, subscription_id);
        """
    )
    _ensure_column(connection, "sources", "enabled", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(connection, "jobs", "base_resume_id", "INTEGER REFERENCES resumes(id)")
    _ensure_column(connection, "jobs", "submitted_resume_id", "INTEGER REFERENCES resumes(id)")
    _ensure_column(connection, "jobs", "submitted_resume_path", "TEXT")
    _ensure_column(connection, "jobs", "tailoring_reason", "TEXT")
    _ensure_column(connection, "jobs", "manual_requested", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(connection, "jobs", "manual_requested_at", "TEXT")
    _ensure_column(connection, "jobs", "apply_origin", "TEXT")
    _ensure_column(connection, "jobs", "apply_reason_code", "TEXT")
    _ensure_column(
        connection, "jobs", "submission_requested", "INTEGER NOT NULL DEFAULT 0"
    )
    _ensure_column(
        connection, "jobs", "availability_status", "TEXT NOT NULL DEFAULT 'unknown'"
    )
    _ensure_column(connection, "jobs", "availability_detail", "TEXT")
    _ensure_column(connection, "jobs", "availability_checked_at", "TEXT")
    connection.execute(
        """
        UPDATE jobs
        SET availability_status = 'closed',
            availability_detail = COALESCE(availability_detail, apply_error),
            availability_checked_at = COALESCE(
                availability_checked_at, last_attempted_at, updated_at
            )
        WHERE availability_status = 'unknown'
          AND pipeline_status = 'expired'
          AND apply_error IS NOT NULL
        """
    )
    connection.execute(
        """
        UPDATE jobs SET pipeline_status = 'queued', resume_path = NULL,
                        tailoring_reason = NULL, prepared_at = NULL,
                        updated_at = ?
        WHERE applied_at IS NULL
          AND pipeline_status IN ('ready', 'failed')
          AND (
              lower(COALESCE(resume_path, '')) LIKE '%tailored-resume.pdf'
              OR lower(COALESCE(tailoring_reason, '')) LIKE '%reprioritized%verbatim%'
          )
        """,
        (utc_now(),),
    )
    connection.execute(
        """
        UPDATE jobs
        SET availability_status = 'manual_only',
            availability_detail = COALESCE(availability_detail, apply_error),
            availability_checked_at = COALESCE(
                availability_checked_at, last_attempted_at, updated_at
            ),
            pipeline_status = CASE
                WHEN pipeline_status = 'failed' THEN 'manual_review'
                ELSE pipeline_status
            END
        WHERE availability_status = 'unknown'
          AND apply_error IS NOT NULL
          AND (
              lower(apply_error) LIKE '%403%'
              OR lower(apply_error) LIKE '%access denied%'
              OR lower(apply_error) LIKE '%access blocked%'
          )
        """
    )
    now = utc_now()
    for key, value in (
        ("onboarding_complete", "false"),
        ("service_paused", "false"),
        ("service_status", "starting"),
        ("service_message", "Waiting for the background service"),
    ):
        connection.execute(
            "INSERT OR IGNORE INTO app_state (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, now),
        )
    backfill_legacy_agent_inputs(connection)
    reconcile_source_registry(connection, SOURCE_DOCUMENTS)
    connection.commit()
    return connection


def _as_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def set_app_state(connection: sqlite3.Connection, key: str, value: Any) -> None:
    connection.execute(
        """
        INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, json.dumps(value), utc_now()),
    )
    connection.commit()


def get_app_state(connection: sqlite3.Connection) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for row in connection.execute("SELECT key, value FROM app_state").fetchall():
        try:
            result[row["key"]] = json.loads(row["value"])
        except (TypeError, json.JSONDecodeError):
            result[row["key"]] = row["value"]
    return result


def record_dashboard_visit(connection: sqlite3.Connection) -> dict[str, Any]:
    """Return unattended results since the prior visit, then record this visit."""

    now = utc_now()
    row = connection.execute(
        "SELECT value FROM app_state WHERE key = 'dashboard_last_viewed_at'"
    ).fetchone()
    previous_visit_at: str | None = None
    if row is not None:
        try:
            stored = json.loads(row["value"])
        except (TypeError, json.JSONDecodeError):
            stored = row["value"]
        if isinstance(stored, str) and stored:
            previous_visit_at = stored

    applications: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    if previous_visit_at is not None:
        applications = [
            dict(item)
            for item in connection.execute(
                """
                SELECT j.id, j.company, j.role, j.location, j.application_url,
                       j.applied_at, r.name AS submitted_resume_name
                FROM jobs j
                LEFT JOIN resumes r ON r.id = j.submitted_resume_id
                WHERE j.applied_at > ? AND j.applied_at <= ?
                ORDER BY j.applied_at DESC, j.id DESC
                """,
                (previous_visit_at, now),
            ).fetchall()
        ]
        failures = [
            dict(item)
            for item in connection.execute(
                """
                SELECT j.id, j.company, j.role, j.location, j.application_url,
                       j.pipeline_status, j.apply_error, j.apply_reason_code,
                       j.last_attempted_at, j.updated_at,
                       r.name AS base_resume_name
                FROM jobs j
                LEFT JOIN resumes r ON r.id = j.base_resume_id
                WHERE j.apply_origin = 'auto'
                  AND j.applied_at IS NULL
                  AND j.pipeline_status IN ('failed', 'expired', 'manual_review')
                  AND j.updated_at > ? AND j.updated_at <= ?
                ORDER BY j.updated_at DESC, j.id DESC
                """,
                (previous_visit_at, now),
            ).fetchall()
        ]
    set_app_state(connection, "dashboard_last_viewed_at", now)
    return {
        "first_visit": previous_visit_at is None,
        "previous_visit_at": previous_visit_at,
        "visited_at": now,
        "applications": applications,
        "application_count": len(applications),
        "failures": failures,
        "failure_count": len(failures),
    }


def source_baseline_complete(connection: sqlite3.Connection, expected_documents: int = 3) -> bool:
    row = connection.execute(
        "SELECT COUNT(*) FROM sources WHERE initialized = 1 AND enabled = 1"
    ).fetchone()
    return int(row[0]) >= expected_documents


def add_resume_record(
    connection: sqlite3.Connection,
    *,
    name: str,
    original_filename: str,
    pdf_path: str,
    text_path: str,
    tags: list[str] | None = None,
    notes: str = "",
) -> dict[str, Any]:
    now = utc_now()
    cursor = connection.execute(
        """
        INSERT INTO resumes (
            name, original_filename, pdf_path, text_path, tags, notes,
            is_active, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (
            name.strip(),
            original_filename,
            pdf_path,
            text_path,
            json.dumps(tags or []),
            notes.strip(),
            now,
            now,
        ),
    )
    connection.commit()
    return get_resume(connection, int(cursor.lastrowid)) or {}


def get_resume(connection: sqlite3.Connection, resume_id: int) -> dict[str, Any] | None:
    row = connection.execute("SELECT * FROM resumes WHERE id = ?", (resume_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    try:
        result["tags"] = json.loads(result.get("tags") or "[]")
    except json.JSONDecodeError:
        result["tags"] = []
    return result


def list_resumes(connection: sqlite3.Connection, *, active_only: bool = True) -> list[dict[str, Any]]:
    clause = "WHERE r.is_active = 1" if active_only else ""
    rows = connection.execute(
        f"""
        SELECT r.*,
               COUNT(DISTINCT CASE WHEN j.base_resume_id = r.id THEN j.id END) AS selected_count,
               COUNT(DISTINCT CASE WHEN j.submitted_resume_id = r.id THEN j.id END) AS submitted_count
        FROM resumes r
        LEFT JOIN jobs j ON j.base_resume_id = r.id OR j.submitted_resume_id = r.id
        {clause}
        GROUP BY r.id ORDER BY r.created_at DESC
        """
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        try:
            item["tags"] = json.loads(item.get("tags") or "[]")
        except json.JSONDecodeError:
            item["tags"] = []
        result.append(item)
    return result


def archive_resume(connection: sqlite3.Connection, resume_id: int) -> bool:
    cursor = connection.execute(
        "UPDATE resumes SET is_active = 0, updated_at = ? WHERE id = ? AND is_active = 1",
        (utc_now(), resume_id),
    )
    connection.commit()
    return cursor.rowcount > 0


def update_worker_state(
    connection: sqlite3.Connection,
    worker_id: str,
    *,
    status: str,
    job: dict[str, Any] | None = None,
    message: str | None = None,
    screenshot_path: str | None = None,
) -> None:
    now = utc_now()
    current = connection.execute(
        "SELECT started_at, screenshot_path FROM worker_state WHERE worker_id = ?", (worker_id,)
    ).fetchone()
    started_at = (
        current["started_at"]
        if current and current["started_at"] and status not in {"starting", "applying"}
        else now
    )
    retained_screenshot = screenshot_path or (current["screenshot_path"] if current else None)
    connection.execute(
        """
        INSERT INTO worker_state (
            worker_id, status, job_id, company, role, message,
            screenshot_path, started_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(worker_id) DO UPDATE SET
            status = excluded.status,
            job_id = excluded.job_id,
            company = excluded.company,
            role = excluded.role,
            message = excluded.message,
            screenshot_path = excluded.screenshot_path,
            started_at = excluded.started_at,
            updated_at = excluded.updated_at
        """,
        (
            worker_id,
            status,
            job.get("id") if job else None,
            job.get("company") if job else None,
            job.get("role") if job else None,
            message,
            retained_screenshot,
            started_at,
            now,
        ),
    )
    connection.commit()


def get_worker_states(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM worker_state ORDER BY worker_id"
        ).fetchall()
    ]


def _decode_agent_answer(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def backfill_legacy_agent_inputs(connection: sqlite3.Connection) -> int:
    """Give pre-0.4 pauses an answer channel when their reason clearly requests facts."""

    rows = connection.execute(
        """
        SELECT j.id, j.apply_error
        FROM jobs j
        WHERE j.pipeline_status = 'manual_review'
          AND j.availability_status NOT IN ('closed', 'manual_only')
          AND j.apply_error IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM agent_inputs ai WHERE ai.job_id = j.id)
        """
    ).fetchall()
    now = utc_now()
    created = 0
    for row in rows:
        detail = str(row["apply_error"] or "")
        lowered = detail.casefold()
        requests_candidate_facts = any(
            marker in lowered
            for marker in (
                "cannot be answered",
                "can't be answered",
                "unanswered question",
                "missing required",
                "required field",
                "needs your input",
            )
        )
        if not requests_candidate_facts or _SENSITIVE_INPUT_PATTERN.search(
            re.sub(r"[_-]+", " ", detail)
        ):
            continue
        connection.execute(
            """
            INSERT OR IGNORE INTO agent_inputs (
                job_id, input_key, label, input_type, options_json, required,
                answer, status, created_at, answered_at, updated_at
            ) VALUES (
                ?, 'legacy_follow_up',
                'Provide the missing information described in the checkpoint note',
                'textarea', '[]', 1, NULL, 'pending', ?, NULL, ?
            )
            """,
            (row["id"], now, now),
        )
        created += 1
    return created


def list_agent_inputs(
    connection: sqlite3.Connection,
    job_id: int,
    *,
    pending_only: bool = False,
) -> list[dict[str, Any]]:
    clause = "AND status = 'pending'" if pending_only else ""
    rows = connection.execute(
        f"""
        SELECT id, job_id, input_key, label, input_type, options_json,
               required, answer, status, created_at, answered_at, updated_at
        FROM agent_inputs
        WHERE job_id = ? {clause}
        ORDER BY id
        """,
        (job_id,),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        try:
            item["options"] = json.loads(item.pop("options_json") or "[]")
        except json.JSONDecodeError:
            item["options"] = []
        item["required"] = bool(item["required"])
        item["answer"] = _decode_agent_answer(item["answer"])
        result.append(item)
    return result


def answered_agent_inputs(
    connection: sqlite3.Connection,
    job_id: int,
) -> dict[str, dict[str, Any]]:
    return {
        item["input_key"]: {
            "question": item["label"],
            "answer": item["answer"],
        }
        for item in list_agent_inputs(connection, job_id)
        if item["status"] == "answered" and item["answer"] is not None
    }


def store_agent_inputs(
    connection: sqlite3.Connection,
    job_id: int,
    questions: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Persist only ordinary, non-sensitive questions returned by the agent."""

    now = utc_now()
    connection.execute(
        "UPDATE agent_inputs SET status = 'superseded', updated_at = ? "
        "WHERE job_id = ? AND status = 'pending'",
        (now, job_id),
    )
    saved = 0
    seen: set[str] = set()
    for raw in questions:
        key = str(raw.get("key") or "").strip().casefold()
        label = str(raw.get("label") or "").strip()
        input_type = str(raw.get("input_type") or "text").strip().casefold()
        if (
            not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", key)
            or key in seen
            or not label
            or len(label) > 240
            or input_type not in AGENT_INPUT_TYPES
            or _SENSITIVE_INPUT_PATTERN.search(
                re.sub(r"[_-]+", " ", f"{key} {label}")
            )
        ):
            continue
        options = raw.get("options")
        if not isinstance(options, list):
            options = []
        options = [str(option).strip()[:120] for option in options if str(option).strip()][
            :50
        ]
        if input_type == "select" and not options:
            input_type = "text"
        seen.add(key)
        connection.execute(
            """
            INSERT INTO agent_inputs (
                job_id, input_key, label, input_type, options_json, required,
                answer, status, created_at, answered_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, 'pending', ?, NULL, ?)
            ON CONFLICT(job_id, input_key) DO UPDATE SET
                label = excluded.label,
                input_type = excluded.input_type,
                options_json = excluded.options_json,
                required = excluded.required,
                status = 'pending',
                updated_at = excluded.updated_at
            """,
            (
                job_id,
                key,
                label,
                input_type,
                json.dumps(options),
                int(bool(raw.get("required", True))),
                now,
                now,
            ),
        )
        saved += 1
    connection.commit()
    return list_agent_inputs(connection, job_id, pending_only=True) if saved else []


def resolve_agent_inputs(connection: sqlite3.Connection, job_id: int) -> None:
    connection.execute(
        "UPDATE agent_inputs SET status = 'resolved', updated_at = ? "
        "WHERE job_id = ? AND status = 'pending'",
        (utc_now(), job_id),
    )
    connection.commit()


def answer_agent_inputs(
    connection: sqlite3.Connection,
    job_id: int,
    answers: dict[str, Any],
) -> dict[str, Any] | None:
    """Save candidate-provided answers and safely requeue the same prepared job."""

    job = get_job(connection, job_id)
    if job is None:
        return None
    if job["pipeline_status"] != "manual_review":
        raise ValueError("This application is not waiting for your input")
    if job["availability_status"] in {"closed", "manual_only"}:
        raise ValueError("This application requires a manual browser handoff")
    pending = list_agent_inputs(connection, job_id, pending_only=True)
    if not pending:
        raise ValueError("The agent did not request any answerable fields")
    by_key = {item["input_key"]: item for item in pending}
    unknown = sorted(set(answers) - set(by_key))
    if unknown:
        raise ValueError(f"Unknown application field: {unknown[0]}")

    normalized: dict[str, Any] = {}
    for key, value in answers.items():
        if not isinstance(value, (str, bool, int, float)):
            raise ValueError(f"Invalid value for {by_key[key]['label']}")
        if isinstance(value, str):
            value = value.strip()
            if len(value) > 4000:
                raise ValueError(f"Answer is too long for {by_key[key]['label']}")
        normalized[key] = value
    missing = [
        item["label"]
        for item in pending
        if item["required"] and (item["input_key"] not in normalized or normalized[item["input_key"]] == "")
    ]
    if missing:
        raise ValueError(f"Answer required: {missing[0]}")

    now = utc_now()
    for item in pending:
        key = item["input_key"]
        value = normalized.get(key, "")
        connection.execute(
            """
            UPDATE agent_inputs SET answer = ?, status = 'answered',
                                    answered_at = ?, updated_at = ?
            WHERE job_id = ? AND input_key = ? AND status = 'pending'
            """,
            (json.dumps(value), now, now, job_id, key),
        )
    connection.execute(
        """
        UPDATE jobs SET pipeline_status = CASE
                            WHEN resume_path IS NULL THEN 'queued' ELSE 'ready' END,
                        manual_requested = 1, manual_requested_at = ?,
                        apply_error = NULL, worker_id = NULL,
                        apply_attempts = CASE WHEN apply_attempts > 0
                            THEN apply_attempts - 1 ELSE 0 END,
                        updated_at = ?
        WHERE id = ?
        """,
        (now, now, job_id),
    )
    add_event(
        connection,
        job_id,
        "agent_input_supplied",
        f"{len(normalized)} answer(s) supplied; application requeued",
    )
    connection.commit()
    return get_job(connection, job_id)


def resume_application_after_input(
    connection: sqlite3.Connection,
    job_id: int,
    worker_id: str,
) -> bool:
    """Return an answered checkpoint to its still-live browser worker."""

    now = utc_now()
    cursor = connection.execute(
        """
        UPDATE jobs SET pipeline_status = 'applying', worker_id = ?,
                        manual_requested = 0, apply_error = NULL, updated_at = ?,
                        apply_attempts = apply_attempts + 1
        WHERE id = ?
          AND pipeline_status IN ('queued', 'ready')
          AND NOT EXISTS (
              SELECT 1 FROM agent_inputs
              WHERE job_id = ? AND status = 'pending'
          )
        """,
        (worker_id, now, job_id, job_id),
    )
    if cursor.rowcount:
        add_event(
            connection,
            job_id,
            "agent_resumed",
            f"Continued in the live browser on {worker_id}",
        )
    connection.commit()
    return bool(cursor.rowcount)


def recover_stale_work(connection: sqlite3.Connection) -> int:
    now = utc_now()
    applying = connection.execute(
        """
        UPDATE jobs SET pipeline_status = 'ready', worker_id = NULL,
                        apply_error = 'Recovered after service restart', updated_at = ?
        WHERE pipeline_status = 'applying'
        """,
        (now,),
    )
    checkpoints = connection.execute(
        """
        UPDATE jobs SET worker_id = NULL, submission_requested = 0, updated_at = ?
        WHERE pipeline_status = 'manual_review' AND worker_id IS NOT NULL
        """,
        (now,),
    )
    connection.execute(
        "UPDATE worker_state SET status = 'stopped', message = 'Service restarted', updated_at = ?",
        (now,),
    )
    connection.commit()
    return applying.rowcount + checkpoints.rowcount


def add_event(
    connection: sqlite3.Connection,
    job_id: int | None,
    event_type: str,
    detail: str | None = None,
) -> None:
    connection.execute(
        "INSERT INTO events (job_id, event_type, detail, created_at) VALUES (?, ?, ?, ?)",
        (job_id, event_type, detail, utc_now()),
    )


def reconcile_source_registry(
    connection: sqlite3.Connection,
    configured_sources: Iterable[SourceDocument],
) -> int:
    """Retire documents removed from the configured feed without deleting history."""

    document_keys = tuple(source.document_key for source in configured_sources)
    now = utc_now()
    if document_keys:
        placeholders = ", ".join("?" for _ in document_keys)
        connection.execute(
            f"UPDATE sources SET enabled = (document_key IN ({placeholders}))",
            document_keys,
        )
        connection.execute(
            f"""
            UPDATE job_sources SET active = 0
            WHERE document_key NOT IN ({placeholders}) AND active = 1
            """,
            document_keys,
        )
    else:
        connection.execute("UPDATE sources SET enabled = 0")
        connection.execute("UPDATE job_sources SET active = 0 WHERE active = 1")

    inactive = connection.execute(
        """
        SELECT j.id, j.pipeline_status
        FROM jobs j
        WHERE j.is_active = 1
          AND NOT EXISTS (
              SELECT 1 FROM job_sources js
              JOIN sources s ON s.document_key = js.document_key
              WHERE js.job_id = j.id AND js.active = 1 AND s.enabled = 1
          )
        """
    ).fetchall()
    for row in inactive:
        status = row["pipeline_status"]
        next_status = status if status in TERMINAL_PIPELINE_STATUSES else "expired"
        connection.execute(
            """
            UPDATE jobs SET is_active = 0, pipeline_status = ?,
                            worker_id = NULL, manual_requested = 0, updated_at = ?
            WHERE id = ?
            """,
            (next_status, now, row["id"]),
        )
        add_event(
            connection,
            int(row["id"]),
            "expired",
            "Removed from the configured repository feed",
        )
    return len(inactive)


def ensure_source(connection: sqlite3.Connection, source: SourceDocument) -> dict[str, Any]:
    connection.execute(
        """
        INSERT INTO sources (
            document_key, source_key, label, repo_url, branch, path, raw_url, enabled
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(document_key) DO UPDATE SET
            source_key = excluded.source_key,
            label = excluded.label,
            repo_url = excluded.repo_url,
            branch = excluded.branch,
            path = excluded.path,
            raw_url = excluded.raw_url,
            enabled = 1
        """,
        (
            source.document_key,
            source.key,
            source.label,
            source.repo_url,
            source.branch,
            source.path,
            source.raw_url,
        ),
    )
    connection.commit()
    row = connection.execute(
        "SELECT * FROM sources WHERE document_key = ?", (source.document_key,)
    ).fetchone()
    return dict(row)


def source_headers(connection: sqlite3.Connection, source: SourceDocument) -> dict[str, str]:
    state = ensure_source(connection, source)
    headers: dict[str, str] = {}
    if state.get("etag"):
        headers["If-None-Match"] = state["etag"]
    if state.get("last_modified"):
        headers["If-Modified-Since"] = state["last_modified"]
    return headers


def mark_source_polled(
    connection: sqlite3.Connection,
    source: SourceDocument,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
    content_sha256: str | None = None,
    success: bool = True,
    error: str | None = None,
) -> None:
    now = utc_now()
    ensure_source(connection, source)
    connection.execute(
        """
        UPDATE sources SET
            etag = COALESCE(?, etag),
            last_modified = COALESCE(?, last_modified),
            content_sha256 = COALESCE(?, content_sha256),
            last_polled_at = ?,
            last_success_at = CASE WHEN ? THEN ? ELSE last_success_at END,
            last_error = ?
        WHERE document_key = ?
        """,
        (
            etag,
            last_modified,
            content_sha256,
            now,
            int(success),
            now,
            None if success else (error or "unknown source error"),
            source.document_key,
        ),
    )
    connection.commit()


def _find_job(
    connection: sqlite3.Connection,
    canonical_url: str,
    fingerprint: str,
    source_key: str,
) -> sqlite3.Row | None:
    exact = connection.execute(
        "SELECT * FROM jobs WHERE canonical_url = ? LIMIT 1", (canonical_url,)
    ).fetchone()
    if exact is not None:
        return exact
    return connection.execute(
        """
        SELECT j.* FROM jobs j
        WHERE j.fingerprint = ?
          AND EXISTS (
              SELECT 1 FROM job_sources other
              WHERE other.job_id = j.id AND other.source_key != ?
          )
          AND NOT EXISTS (
              SELECT 1 FROM job_sources same
              WHERE same.job_id = j.id AND same.source_key = ?
          )
        ORDER BY j.first_seen_at LIMIT 1
        """,
        (fingerprint, source_key, source_key),
    ).fetchone()


def ingest_listings(
    connection: sqlite3.Connection,
    source: SourceDocument,
    listings: Iterable[InternshipListing],
    *,
    profile: dict[str, Any],
    settings: dict[str, Any],
    include_existing: bool = False,
) -> dict[str, int | bool]:
    """Atomically reconcile one fetched source document with the local queue."""

    source_state = ensure_source(connection, source)
    baseline = not bool(source_state["initialized"]) and not include_existing
    auto_apply_new = bool(settings.get("automation", {}).get("auto_apply_new", False))
    now = utc_now()
    stats: dict[str, int | bool] = {
        "parsed": 0,
        "new": 0,
        "existing": 0,
        "queued": 0,
        "skipped": 0,
        "expired": 0,
        "baseline": baseline,
    }

    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            "UPDATE job_sources SET active = 0 WHERE document_key = ?", (source.document_key,)
        )
        for listing in listings:
            stats["parsed"] = int(stats["parsed"]) + 1
            canonical_url = canonicalize_url(listing.application_url)
            if not canonical_url:
                continue
            fingerprint = listing_fingerprint(listing.company, listing.role, listing.location)
            row = _find_job(connection, canonical_url, fingerprint, source.key)
            eligibility_listing = listing
            if row is not None:
                eligibility_listing = replace(
                    listing,
                    no_sponsorship=bool(listing.no_sponsorship or row["no_sponsorship"]),
                    citizenship_required=bool(
                        listing.citizenship_required or row["citizenship_required"]
                    ),
                )
            eligibility = evaluate_listing(eligibility_listing, profile, settings)

            if row is None:
                is_new = not baseline
                availability_status = "closed" if listing.closed else "unknown"
                availability_detail = (
                    "Marked closed in the repository source" if listing.closed else None
                )
                if not eligibility.eligible:
                    status = "skipped"
                    stats["skipped"] = int(stats["skipped"]) + 1
                elif is_new and auto_apply_new:
                    status = "queued"
                    stats["queued"] = int(stats["queued"]) + 1
                else:
                    status = "discovered"
                cursor = connection.execute(
                    """
                    INSERT INTO jobs (
                        fingerprint, canonical_url, application_url, company, role, location,
                        category, posting_date, first_seen_at, last_seen_at, is_active,
                        availability_status, availability_detail, availability_checked_at,
                        no_sponsorship, citizenship_required, eligibility, eligibility_reason,
                        fit_score, score_reasoning, scored_at, pipeline_status, discovered_as_new,
                        created_at, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        fingerprint,
                        canonical_url,
                        listing.application_url,
                        listing.company,
                        listing.role,
                        listing.location,
                        listing.category,
                        listing.posting_date,
                        now,
                        now,
                        availability_status,
                        availability_detail,
                        now if listing.closed else None,
                        int(listing.no_sponsorship),
                        int(listing.citizenship_required),
                        "eligible" if eligibility.eligible else "ineligible",
                        eligibility.reason,
                        eligibility.score,
                        eligibility.score_reasoning,
                        now,
                        status,
                        int(is_new),
                        now,
                        now,
                    ),
                )
                job_id = int(cursor.lastrowid)
                stats["new"] = int(stats["new"]) + 1
                add_event(connection, job_id, "discovered", f"Imported from {source.label}")
                if status == "queued":
                    add_event(connection, job_id, "queued", "New eligible internship")
            else:
                job_id = int(row["id"])
                stats["existing"] = int(stats["existing"]) + 1
                next_status = row["pipeline_status"]
                url_changed = canonical_url != row["canonical_url"]
                availability_status = str(row["availability_status"] or "unknown")
                availability_detail = row["availability_detail"]
                availability_checked_at = row["availability_checked_at"]
                if listing.closed:
                    availability_status = "closed"
                    availability_detail = "Marked closed in the repository source"
                    availability_checked_at = now
                    if next_status not in TERMINAL_PIPELINE_STATUSES:
                        next_status = "expired"
                elif availability_status == "closed" and (
                    url_changed
                    or availability_detail == "Marked closed in the repository source"
                ):
                    availability_status = "unknown"
                    availability_detail = None
                    availability_checked_at = None
                if not eligibility.eligible and next_status in {
                    "discovered",
                    "queued",
                    "ready",
                    "failed",
                }:
                    next_status = "skipped"
                    add_event(connection, job_id, "ineligible", eligibility.reason)
                elif (
                    eligibility.eligible
                    and row["eligibility"] == "ineligible"
                    and next_status == "skipped"
                ):
                    if baseline or not bool(row["discovered_as_new"]) or not auto_apply_new:
                        next_status = "discovered"
                        add_event(connection, job_id, "eligible", "Eligible repository listing")
                    else:
                        next_status = "queued"
                        stats["queued"] = int(stats["queued"]) + 1
                        add_event(connection, job_id, "queued", "Listing became eligible")
                elif (
                    next_status == "expired"
                    and eligibility.eligible
                    and availability_status != "closed"
                ):
                    if baseline or not auto_apply_new:
                        next_status = "discovered"
                        add_event(connection, job_id, "reopened", "Active in repository source")
                    else:
                        next_status = "queued"
                        stats["queued"] = int(stats["queued"]) + 1
                        add_event(connection, job_id, "reopened", f"Reappeared in {source.label}")
                connection.execute(
                    """
                    UPDATE jobs SET
                        canonical_url = ?, application_url = ?, company = ?, role = ?,
                        location = ?, category = ?,
                        posting_date = COALESCE(?, posting_date), last_seen_at = ?, is_active = 1,
                        availability_status = ?, availability_detail = ?,
                        availability_checked_at = ?,
                        no_sponsorship = no_sponsorship OR ?,
                        citizenship_required = citizenship_required OR ?,
                        eligibility = ?, eligibility_reason = ?,
                        fit_score = COALESCE(fit_score, ?),
                        score_reasoning = COALESCE(score_reasoning, ?),
                        pipeline_status = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        canonical_url,
                        listing.application_url,
                        listing.company,
                        listing.role,
                        listing.location,
                        listing.category,
                        listing.posting_date,
                        now,
                        availability_status,
                        availability_detail,
                        availability_checked_at,
                        int(listing.no_sponsorship),
                        int(listing.citizenship_required),
                        "eligible" if eligibility.eligible else "ineligible",
                        eligibility.reason,
                        eligibility.score,
                        eligibility.score_reasoning,
                        next_status,
                        now,
                        job_id,
                    ),
                )

            connection.execute(
                """
                INSERT INTO job_sources (
                    job_id, document_key, source_key, source_label, source_repo_url,
                    source_path, raw_date, first_seen_at, last_seen_at, active
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(job_id, document_key) DO UPDATE SET
                    source_label = excluded.source_label,
                    source_repo_url = excluded.source_repo_url,
                    source_path = excluded.source_path,
                    raw_date = excluded.raw_date,
                    last_seen_at = excluded.last_seen_at,
                    active = 1
                """,
                (
                    job_id,
                    source.document_key,
                    source.key,
                    source.label,
                    source.repo_url,
                    source.path,
                    listing.raw_date,
                    now,
                    now,
                ),
            )

        inactive = connection.execute(
            """
            SELECT j.id, j.pipeline_status
            FROM jobs j
            WHERE j.is_active = 1
              AND NOT EXISTS (SELECT 1 FROM job_sources js WHERE js.job_id = j.id AND js.active = 1)
            """
        ).fetchall()
        for row in inactive:
            status = row["pipeline_status"]
            next_status = status if status in TERMINAL_PIPELINE_STATUSES else "expired"
            connection.execute(
                "UPDATE jobs SET is_active = 0, pipeline_status = ?, updated_at = ? WHERE id = ?",
                (next_status, now, row["id"]),
            )
            add_event(connection, int(row["id"]), "expired", "Removed from all active source documents")
        stats["expired"] = len(inactive)

        if include_existing:
            cursor = connection.execute(
                """
                UPDATE jobs SET pipeline_status = 'queued', discovered_as_new = 1, updated_at = ?
                WHERE pipeline_status = 'discovered' AND eligibility = 'eligible'
                  AND is_active = 1 AND availability_status != 'closed'
                """,
                (now,),
            )
            stats["queued"] = int(stats["queued"]) + cursor.rowcount

        connection.execute(
            """
            UPDATE sources SET initialized = 1, last_success_at = ?, last_polled_at = ?, last_error = NULL
            WHERE document_key = ?
            """,
            (now, now, source.document_key),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return stats


def get_job(connection: sqlite3.Connection, job_id: int) -> dict[str, Any] | None:
    return _as_dict(
        connection.execute(
            """
            SELECT j.*, br.name AS base_resume_name,
                   br.text_path AS base_resume_text_path,
                   sr.name AS submitted_resume_name,
                   GROUP_CONCAT(DISTINCT js.source_label) AS source_labels,
                   GROUP_CONCAT(DISTINCT js.source_repo_url) AS source_repo_urls
            FROM jobs j
            LEFT JOIN resumes br ON br.id = j.base_resume_id
            LEFT JOIN resumes sr ON sr.id = j.submitted_resume_id
            LEFT JOIN job_sources js ON js.job_id = j.id AND js.active = 1
            WHERE j.id = ?
            GROUP BY j.id
            """,
            (job_id,),
        ).fetchone()
    )


def refresh_qualification_scores(
    connection: sqlite3.Connection,
    *,
    profile: dict[str, Any],
    settings: dict[str, Any],
) -> int:
    """Recompute active heuristic scores without role or location preferences."""

    rows = connection.execute(
        """
        SELECT j.*,
               COALESCE(
                   (SELECT js.source_key FROM job_sources js
                    WHERE js.job_id = j.id ORDER BY js.first_seen_at LIMIT 1),
                   'local'
               ) AS source_key,
               COALESCE(
                   (SELECT js.source_label FROM job_sources js
                    WHERE js.job_id = j.id ORDER BY js.first_seen_at LIMIT 1),
                   'Repository source'
               ) AS source_label,
               COALESCE(
                   (SELECT js.source_repo_url FROM job_sources js
                    WHERE js.job_id = j.id ORDER BY js.first_seen_at LIMIT 1),
                   'https://github.com'
               ) AS source_repo_url,
               COALESCE(
                   (SELECT js.source_path FROM job_sources js
                    WHERE js.job_id = j.id ORDER BY js.first_seen_at LIMIT 1),
                   'README.md'
               ) AS source_path
        FROM jobs j WHERE j.is_active = 1
        """
    ).fetchall()
    now = utc_now()
    for row in rows:
        listing = InternshipListing(
            company=str(row["company"]),
            role=str(row["role"]),
            location=str(row["location"] or ""),
            application_url=str(row["application_url"]),
            source_key=str(row["source_key"]),
            source_label=str(row["source_label"]),
            source_repo_url=str(row["source_repo_url"]),
            source_path=str(row["source_path"]),
            category=str(row["category"] or "Tech"),
            posting_date=row["posting_date"],
            closed=bool(row["availability_status"] == "closed"),
            no_sponsorship=bool(row["no_sponsorship"]),
            citizenship_required=bool(row["citizenship_required"]),
        )
        evaluation = evaluate_listing(listing, profile, settings)
        connection.execute(
            """
            UPDATE jobs SET eligibility = ?, eligibility_reason = ?,
                            fit_score = ?, score_reasoning = ?, scored_at = ?,
                            updated_at = CASE
                                WHEN fit_score != ? OR score_reasoning != ? THEN ?
                                ELSE updated_at END
            WHERE id = ?
            """,
            (
                "eligible" if evaluation.eligible else "ineligible",
                evaluation.reason,
                evaluation.score,
                evaluation.score_reasoning,
                now,
                evaluation.score,
                evaluation.score_reasoning,
                now,
                row["id"],
            ),
        )
    connection.commit()
    return len(rows)


def list_jobs(
    connection: sqlite3.Connection,
    *,
    status: str | None = None,
    search: str | None = None,
    limit: int = 100,
    offset: int = 0,
    latest: bool = False,
    active_only: bool = False,
    applied_only: bool = False,
) -> list[dict[str, Any]]:
    clauses = ["1 = 1"]
    parameters: list[Any] = []
    if status and status != "all":
        if status in OUTCOME_STATUSES - {"none"}:
            clauses.append("j.outcome_status = ?")
        else:
            clauses.append("j.pipeline_status = ?")
        parameters.append(status)
    if search:
        clauses.append("(j.company LIKE ? OR j.role LIKE ? OR j.location LIKE ?)")
        token = f"%{search}%"
        parameters.extend((token, token, token))
    if active_only:
        clauses.append("j.is_active = 1")
    if latest:
        clauses.append("j.availability_status != 'closed'")
    if applied_only:
        clauses.append("j.applied_at IS NOT NULL")
    ordering = (
        "COALESCE(j.posting_date, substr(j.first_seen_at, 1, 10)) DESC, "
        "j.first_seen_at DESC, j.id DESC"
        if latest
        else "(j.applied_at IS NOT NULL) DESC, COALESCE(j.applied_at, j.last_seen_at) DESC"
    )
    parameters.extend((max(1, min(limit, 500)), max(0, offset)))
    rows = connection.execute(
        f"""
        SELECT j.*,
               GROUP_CONCAT(DISTINCT js.source_label) AS source_labels,
               GROUP_CONCAT(DISTINCT js.source_repo_url) AS source_repo_urls,
               br.name AS base_resume_name,
               sr.name AS submitted_resume_name
        FROM jobs j
        LEFT JOIN job_sources js ON js.job_id = j.id AND js.active = 1
        LEFT JOIN resumes br ON br.id = j.base_resume_id
        LEFT JOIN resumes sr ON sr.id = j.submitted_resume_id
        WHERE {' AND '.join(clauses)}
        GROUP BY j.id
        ORDER BY {ordering}
        LIMIT ? OFFSET ?
        """,
        parameters,
    ).fetchall()
    return [dict(row) for row in rows]


def get_stats(connection: sqlite3.Connection) -> dict[str, Any]:
    aggregate = connection.execute(
        """
        SELECT
            COUNT(*) AS total_discovered,
            SUM(CASE WHEN is_active = 1 AND availability_status != 'closed'
                     THEN 1 ELSE 0 END) AS active,
            SUM(CASE WHEN eligibility = 'eligible' AND is_active = 1
                          AND availability_status != 'closed'
                     THEN 1 ELSE 0 END) AS eligible,
            SUM(CASE WHEN pipeline_status = 'queued' THEN 1 ELSE 0 END) AS queued,
            SUM(CASE WHEN pipeline_status = 'ready' THEN 1 ELSE 0 END) AS ready,
            SUM(CASE WHEN applied_at IS NOT NULL THEN 1 ELSE 0 END) AS applications,
            SUM(CASE WHEN applied_at IS NOT NULL AND oa_at IS NOT NULL THEN 1 ELSE 0 END) AS oas,
            SUM(CASE WHEN applied_at IS NOT NULL AND interview_at IS NOT NULL
                     THEN 1 ELSE 0 END) AS interviews,
            SUM(CASE WHEN applied_at IS NOT NULL AND offer_at IS NOT NULL THEN 1 ELSE 0 END) AS offers,
            SUM(CASE WHEN applied_at IS NOT NULL AND rejected_at IS NOT NULL THEN 1 ELSE 0 END) AS rejected
        FROM jobs
        """
    ).fetchone()
    data = {key: int(value or 0) for key, value in dict(aggregate).items()}
    applications = data["applications"]
    data["oa_rate"] = round(data["oas"] / applications * 100, 1) if applications else 0.0
    data["interview_rate"] = (
        round(data["interviews"] / applications * 100, 1) if applications else 0.0
    )

    status_rows = connection.execute(
        "SELECT pipeline_status, COUNT(*) AS count FROM jobs GROUP BY pipeline_status"
    ).fetchall()
    data["status_counts"] = {row["pipeline_status"]: row["count"] for row in status_rows}
    source_rows = connection.execute(
        """
        SELECT source_label, COUNT(DISTINCT job_id) AS count
        FROM job_sources WHERE active = 1 GROUP BY source_label ORDER BY count DESC
        """
    ).fetchall()
    data["source_counts"] = [dict(row) for row in source_rows]
    recent = connection.execute(
        """
        SELECT j.id, j.company, j.role, j.location, j.application_url,
               j.applied_at, j.outcome_status, r.name AS submitted_resume_name
        FROM jobs j
        LEFT JOIN resumes r ON r.id = j.submitted_resume_id
        WHERE j.applied_at IS NOT NULL ORDER BY j.applied_at DESC LIMIT 8
        """
    ).fetchall()
    data["recent_applications"] = [dict(row) for row in recent]
    return data


def _role_family(role: str) -> str:
    lowered = role.casefold()
    if any(term in lowered for term in ("quant", "trading", "research scientist")):
        return "Quantitative & research"
    if any(
        term in lowered
        for term in (
            "machine learning",
            "artificial intelligence",
            " ai ",
            "ai/",
            "ai intern",
            "computer vision",
        )
    ) or re.search(r"\b(?:ai|ml|nlp)\b", lowered):
        return "Machine learning & AI"
    if any(term in lowered for term in ("data ", "analytics", "business intelligence")):
        return "Data & analytics"
    if any(term in lowered for term in ("security", "cyber", "privacy")):
        return "Security"
    if any(
        term in lowered
        for term in ("hardware", "embedded", "firmware", "fpga", "silicon", "electrical")
    ):
        return "Hardware & embedded"
    if any(term in lowered for term in ("product manager", "product management")):
        return "Product"
    if any(
        term in lowered
        for term in (
            "software",
            "developer",
            "frontend",
            "front end",
            "backend",
            "back end",
            "full stack",
            "web ",
            "mobile",
            "platform",
            "devops",
            "site reliability",
            "cloud",
        )
    ) or re.search(r"\bswe\b", lowered):
        return "Software engineering"
    if any(term in lowered for term in ("information technology", " it ", "systems")):
        return "IT & infrastructure"
    return "Other tech"


def _application_portal(url: str) -> str:
    hostname = (urlparse(url).hostname or "").casefold()
    known_portals = (
        (
            (
                "myworkdayjobs.com",
                "myworkdaysite.com",
                "workday.com",
                "workdayjobs.com",
            ),
            "Workday",
        ),
        (("greenhouse.io", "greenhouse.com"), "Greenhouse"),
        (("lever.co",), "Lever"),
        (("ashbyhq.com",), "Ashby"),
        (("smartrecruiters.com",), "SmartRecruiters"),
        (("icims.com",), "iCIMS"),
        (("taleo.net",), "Taleo"),
        (("oraclecloud.com",), "Oracle Recruiting"),
        (("successfactors.com",), "SAP SuccessFactors"),
        (("jobvite.com",), "Jobvite"),
        (("eightfold.ai",), "Eightfold"),
        (("ripplematch.com",), "RippleMatch"),
    )
    for domains, label in known_portals:
        if any(hostname == domain or hostname.endswith(f".{domain}") for domain in domains):
            return label
    return "Company site" if hostname else "Portal not recorded"


def _analytics_segment_rows(
    applications: list[dict[str, Any]],
    label_for: Any,
) -> list[dict[str, Any]]:
    segments: dict[str, dict[str, Any]] = {}
    for application in applications:
        label = str(label_for(application) or "Not recorded").strip() or "Not recorded"
        segment = segments.setdefault(
            label,
            {
                "label": label,
                "applications": 0,
                "oas": 0,
                "interviews": 0,
                "offers": 0,
                "rejections": 0,
            },
        )
        segment["applications"] += 1
        segment["oas"] += int(bool(application.get("oa_at")))
        segment["interviews"] += int(bool(application.get("interview_at")))
        segment["offers"] += int(bool(application.get("offer_at")))
        segment["rejections"] += int(bool(application.get("rejected_at")))
    for segment in segments.values():
        denominator = segment["applications"]
        segment["oa_rate"] = round(segment["oas"] / denominator * 100, 1)
        segment["interview_rate"] = round(
            segment["interviews"] / denominator * 100, 1
        )
        segment["offer_rate"] = round(segment["offers"] / denominator * 100, 1)
    return sorted(
        segments.values(),
        key=lambda item: (-item["applications"], item["label"].casefold()),
    )


def get_analytics(connection: sqlite3.Connection) -> dict[str, Any]:
    """Break response rates down across only applications that were submitted."""

    rows = connection.execute(
        """
        SELECT j.id, j.role, j.location, j.application_url, j.applied_at,
               j.oa_at, j.interview_at, j.offer_at, j.rejected_at,
               COALESCE(sr.name, 'Resume not recorded') AS resume_name,
               COALESCE(
                   (
                       SELECT js.source_label
                       FROM job_sources js
                       WHERE js.job_id = j.id
                       ORDER BY js.first_seen_at, js.document_key
                       LIMIT 1
                   ),
                   'Source not recorded'
               ) AS source_label
        FROM jobs j
        LEFT JOIN resumes sr ON sr.id = j.submitted_resume_id
        WHERE j.applied_at IS NOT NULL
        ORDER BY j.applied_at DESC
        """
    ).fetchall()
    applications = [dict(row) for row in rows]
    total = len(applications)
    summary = {
        "applications": total,
        "oas": sum(bool(row["oa_at"]) for row in applications),
        "interviews": sum(bool(row["interview_at"]) for row in applications),
        "offers": sum(bool(row["offer_at"]) for row in applications),
        "rejections": sum(bool(row["rejected_at"]) for row in applications),
    }
    summary["oa_rate"] = round(summary["oas"] / total * 100, 1) if total else 0.0
    summary["interview_rate"] = (
        round(summary["interviews"] / total * 100, 1) if total else 0.0
    )
    summary["offer_rate"] = (
        round(summary["offers"] / total * 100, 1) if total else 0.0
    )
    return {
        "summary": summary,
        "dimensions": {
            "resume": _analytics_segment_rows(
                applications, lambda row: row["resume_name"]
            ),
            "role_family": _analytics_segment_rows(
                applications, lambda row: _role_family(row["role"])
            ),
            "source": _analytics_segment_rows(
                applications, lambda row: row["source_label"]
            ),
            "location": _analytics_segment_rows(
                applications,
                lambda row: (
                    "Remote"
                    if "remote" in str(row["location"] or "").casefold()
                    else str(row["location"] or "Location not recorded").strip()
                ),
            ),
            "portal": _analytics_segment_rows(
                applications, lambda row: _application_portal(row["application_url"])
            ),
        },
    }


def update_tracker(
    connection: sqlite3.Connection,
    job_id: int,
    *,
    pipeline_status: str | None = None,
    outcome_status: str | None = None,
    notes: str | None = None,
) -> dict[str, Any] | None:
    row = get_job(connection, job_id)
    if row is None:
        return None
    if pipeline_status is not None and pipeline_status not in PIPELINE_STATUSES:
        raise ValueError(f"Unknown pipeline status: {pipeline_status}")
    if outcome_status is not None and outcome_status not in OUTCOME_STATUSES:
        raise ValueError(f"Unknown outcome status: {outcome_status}")

    updates: list[str] = []
    values: list[Any] = []
    now = utc_now()
    pipeline_changed = (
        pipeline_status is not None and pipeline_status != row.get("pipeline_status")
    )
    if pipeline_status is not None and (
        pipeline_changed
        or (pipeline_status == "applied" and not row.get("applied_at"))
    ):
        updates.append("pipeline_status = ?")
        values.append(pipeline_status)
        if pipeline_status == "applied" and not row.get("applied_at"):
            updates.append("applied_at = ?")
            values.append(now)
        if pipeline_status == "applied":
            updates.append("submitted_resume_id = COALESCE(submitted_resume_id, base_resume_id)")
            updates.append("submitted_resume_path = COALESCE(submitted_resume_path, resume_path)")
        if pipeline_status == "queued":
            updates.append("manual_requested = 1")
            updates.append("manual_requested_at = ?")
            values.append(now)
        if pipeline_status == "withdrawn":
            updates.append("outcome_status = 'withdrawn'")
            updates.append("withdrawn_at = COALESCE(withdrawn_at, ?)")
            values.append(now)
        if pipeline_changed:
            add_event(connection, job_id, "status", pipeline_status)
    outcome_changed = (
        outcome_status is not None and outcome_status != row.get("outcome_status")
    )
    if outcome_status is not None and outcome_changed:
        updates.append("outcome_status = ?")
        values.append(outcome_status)
        milestone_columns = {
            "oa": "oa_at",
            "interview": "interview_at",
            "offer": "offer_at",
            "rejected": "rejected_at",
            "withdrawn": "withdrawn_at",
        }
        if column := milestone_columns.get(outcome_status):
            updates.append(f"{column} = COALESCE({column}, ?)")
            values.append(now)
        add_event(connection, job_id, "outcome", outcome_status)
    if notes is not None:
        updates.append("notes = ?")
        values.append(notes)
    if updates:
        updates.append("updated_at = ?")
        values.append(now)
        values.append(job_id)
        connection.execute(f"UPDATE jobs SET {', '.join(updates)} WHERE id = ?", values)
        connection.commit()
    return get_job(connection, job_id)


def pending_preparation(
    connection: sqlite3.Connection,
    limit: int = 0,
    target_job_id: int | None = None,
) -> list[dict[str, Any]]:
    query = """
        SELECT * FROM jobs
        WHERE pipeline_status = 'queued' AND is_active = 1
          AND availability_status NOT IN ('closed', 'manual_only')
          AND (
            manual_requested = 1
            OR (
              discovered_as_new = 1 AND eligibility = 'eligible'
            )
          )
    """
    parameters: list[Any] = []
    if target_job_id is not None:
        query += " AND id = ?"
        parameters.append(target_job_id)
    query += """
        ORDER BY manual_requested DESC, manual_requested_at ASC,
                 posting_date DESC, first_seen_at DESC
    """
    if limit > 0:
        query += " LIMIT ?"
        parameters.append(limit)
    return [dict(row) for row in connection.execute(query, parameters).fetchall()]


def mark_prepared(
    connection: sqlite3.Connection,
    job_id: int,
    *,
    base_resume_id: int | None,
    resume_path: str,
    cover_letter_path: str | None,
    tailoring_reason: str,
    notes: str,
) -> None:
    now = utc_now()
    connection.execute(
        """
        UPDATE jobs SET base_resume_id = ?, resume_path = ?, cover_letter_path = ?,
                        tailoring_reason = ?, preparation_notes = ?, prepared_at = ?,
                        pipeline_status = 'ready', updated_at = ?
        WHERE id = ?
        """,
        (
            base_resume_id,
            resume_path,
            cover_letter_path,
            tailoring_reason,
            notes,
            now,
            now,
            job_id,
        ),
    )
    add_event(connection, job_id, "prepared", notes)
    connection.commit()


def request_manual_application(
    connection: sqlite3.Connection, job_id: int
) -> dict[str, Any] | None:
    """Explicitly place any active repository listing into the browser-agent queue."""

    row = get_job(connection, job_id)
    if row is None:
        return None
    if (
        not bool(row["is_active"])
        or row["pipeline_status"] == "expired"
        or row["availability_status"] == "closed"
    ):
        raise ValueError("This listing is no longer active")
    if row["availability_status"] == "manual_only":
        raise ValueError(
            "This employer blocks the automated browser; open the application manually"
        )
    worker_id = str(row.get("worker_id") or "")
    worker = (
        connection.execute(
            "SELECT status, job_id FROM worker_state WHERE worker_id = ?",
            (worker_id,),
        ).fetchone()
        if worker_id
        else None
    )
    live_review = bool(
        row["pipeline_status"] == "manual_review"
        and worker is not None
        and worker["status"] in {"needs_review", "review_ready"}
        and int(worker["job_id"] or 0) == job_id
    )
    if row["pipeline_status"] in {"applied", "applying", "withdrawn"} or live_review:
        raise ValueError(
            f"This listing is already {str(row['pipeline_status']).replace('_', ' ')}"
        )
    now = utc_now()
    next_status = "queued"
    connection.execute(
        """
        UPDATE jobs SET pipeline_status = ?, manual_requested = 1,
                        manual_requested_at = ?, apply_error = NULL, updated_at = ?,
                        apply_reason_code = NULL, submission_requested = 0,
                        apply_attempts = CASE WHEN pipeline_status = 'failed'
                            THEN 0 ELSE apply_attempts END
        WHERE id = ?
        """,
        (next_status, now, now, job_id),
    )
    add_event(connection, job_id, "manual_apply_requested", "Requested from Latest jobs")
    connection.commit()
    return get_job(connection, job_id)


def manual_application_ids(connection: sqlite3.Connection) -> list[int]:
    return [
        int(row["id"])
        for row in connection.execute(
            """
            SELECT id FROM jobs
            WHERE manual_requested = 1 AND is_active = 1
              AND availability_status NOT IN ('closed', 'manual_only')
              AND pipeline_status IN ('queued', 'ready', 'failed')
            ORDER BY manual_requested_at ASC, id ASC
            """
        ).fetchall()
    ]


def _company_key(company: Any) -> str:
    """Return a stable comparison key for company-level auto-apply limits."""

    return re.sub(r"[^a-z0-9]+", "", str(company or "").casefold())


def _claim_candidates(
    connection: sqlite3.Connection,
    *,
    max_attempts: int,
    target_job_id: int | None,
    minimum_fit_score: int,
    eligible_only: bool,
    profile: dict[str, Any] | None,
    use_preferences: bool,
    include_queued: bool = False,
) -> list[dict[str, Any]]:
    candidate_statuses = ("queued", "ready", "failed") if include_queued else ("ready", "failed")
    status_placeholders = ", ".join("?" for _ in candidate_statuses)
    clauses = [
        f"j.pipeline_status IN ({status_placeholders})",
        "j.is_active = 1",
        "j.availability_status NOT IN ('closed', 'manual_only')",
        "j.apply_attempts < ?",
    ]
    parameters: list[Any] = [*candidate_statuses, max_attempts]
    if target_job_id is None:
        clauses.extend(["j.eligibility = 'eligible'", "j.discovered_as_new = 1"])
        terminal_reasons = ", ".join(
            f"'{reason}'" for reason in sorted(_AUTO_TERMINAL_REASON_CODES)
        )
        clauses.append(
            "NOT (j.apply_origin = 'auto' AND j.pipeline_status = 'failed' "
            f"AND COALESCE(j.apply_reason_code, '') IN ({terminal_reasons}))"
        )
        if not eligible_only:
            clauses.append("COALESCE(j.fit_score, 0) >= ?")
            parameters.append(max(1, min(10, int(minimum_fit_score))))
    else:
        clauses.extend(
            [
                "(j.eligibility = 'eligible' OR j.manual_requested = 1)",
                "(j.discovered_as_new = 1 OR j.manual_requested = 1)",
                "j.id = ?",
            ]
        )
        parameters.append(target_job_id)
    rows = [
        dict(row)
        for row in connection.execute(
            f"""
            SELECT j.*,
                   GROUP_CONCAT(DISTINCT js.source_label) AS source_labels
            FROM jobs j
            LEFT JOIN job_sources js ON js.job_id = j.id AND js.active = 1
            WHERE {' AND '.join(clauses)}
            GROUP BY j.id
            ORDER BY j.fit_score DESC, (j.pipeline_status = 'ready') DESC,
                     j.posting_date DESC,
                     j.first_seen_at ASC, j.id ASC
            """,
            parameters,
        ).fetchall()
    ]
    if target_job_id is not None:
        return rows[:1]

    history = [
        dict(row)
        for row in connection.execute(
            """
            SELECT id, company, pipeline_status, applied_at, apply_origin,
                   apply_attempts
            FROM jobs
            WHERE applied_at IS NOT NULL
               OR pipeline_status IN ('applying', 'manual_review')
               OR (apply_origin = 'auto' AND apply_attempts > 0)
            """
        ).fetchall()
    ]
    history_by_company: dict[str, list[dict[str, Any]]] = {}
    for item in history:
        history_by_company.setdefault(_company_key(item["company"]), []).append(item)

    selected_companies: set[str] = set()
    result: list[dict[str, Any]] = []
    for row in rows:
        company = _company_key(row["company"])
        if company in selected_companies:
            continue
        prior = history_by_company.get(company, [])
        auto_selected_ids = {
            int(item["id"])
            for item in prior
            if item.get("apply_origin") == "auto" and int(item.get("apply_attempts") or 0) > 0
        }
        blocked_by_other = any(
            int(item["id"]) != int(row["id"])
            and (
                bool(item.get("applied_at"))
                or item.get("pipeline_status") in {"applying", "manual_review"}
            )
            for item in prior
        )
        if blocked_by_other or (auto_selected_ids and int(row["id"]) not in auto_selected_ids):
            continue
        if use_preferences and not matches_preferences(row, profile or {})[0]:
            continue
        selected_companies.add(company)
        result.append(row)
    return result


def _skip_auto_company_siblings(
    connection: sqlite3.Connection,
    selected: dict[str, Any],
) -> None:
    """Keep one best-fit auto application for a company while retaining manual access."""

    company = _company_key(selected["company"])
    detail = (
        f"Auto mode selected {selected['role']} as this company's best-fit role"
    )
    siblings = connection.execute(
        """
        SELECT id, company FROM jobs
        WHERE id != ? AND discovered_as_new = 1 AND manual_requested = 0
          AND pipeline_status IN ('queued', 'ready', 'failed')
        """,
        (selected["id"],),
    ).fetchall()
    now = utc_now()
    for sibling in siblings:
        if _company_key(sibling["company"]) != company:
            continue
        connection.execute(
            """
            UPDATE jobs SET pipeline_status = 'skipped', apply_error = ?,
                            apply_reason_code = 'company_role_deduplicated',
                            updated_at = ?
            WHERE id = ?
            """,
            (detail, now, sibling["id"]),
        )
        add_event(
            connection,
            int(sibling["id"]),
            "auto_role_skipped",
            detail,
        )


def claim_next_job(
    connection: sqlite3.Connection,
    *,
    worker_id: str,
    max_attempts: int,
    target_job_id: int | None = None,
    minimum_fit_score: int = 1,
    eligible_only: bool = False,
    profile: dict[str, Any] | None = None,
    use_preferences: bool = False,
) -> dict[str, Any] | None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        candidates = _claim_candidates(
            connection,
            max_attempts=max_attempts,
            target_job_id=target_job_id,
            minimum_fit_score=minimum_fit_score,
            eligible_only=eligible_only,
            profile=profile,
            use_preferences=use_preferences,
        )
        if not candidates:
            connection.rollback()
            return None
        row = candidates[0]
        if target_job_id is None:
            _skip_auto_company_siblings(connection, row)
        now = utc_now()
        origin = "manual" if target_job_id is not None else "auto"
        connection.execute(
            """
            UPDATE jobs SET pipeline_status = 'applying', worker_id = ?,
                            last_attempted_at = ?, apply_attempts = apply_attempts + 1,
                            apply_origin = ?, apply_reason_code = NULL,
                            submission_requested = 0, updated_at = ? WHERE id = ?
            """,
            (worker_id, now, origin, now, row["id"]),
        )
        add_event(connection, int(row["id"]), "applying", worker_id)
        connection.commit()
        return get_job(connection, int(row["id"]))
    except Exception:
        connection.rollback()
        raise


def claimable_application_count(
    connection: sqlite3.Connection,
    *,
    max_attempts: int,
    target_job_id: int | None = None,
    minimum_fit_score: int = 1,
    eligible_only: bool = False,
    profile: dict[str, Any] | None = None,
    use_preferences: bool = False,
) -> int:
    """Count jobs that an application worker could atomically claim."""

    return len(
        _claim_candidates(
            connection,
            max_attempts=max_attempts,
            target_job_id=target_job_id,
            minimum_fit_score=minimum_fit_score,
            eligible_only=eligible_only,
            profile=profile,
            use_preferences=use_preferences,
        )
    )


def list_application_queue(
    connection: sqlite3.Connection,
    *,
    auto_enabled: bool,
    max_attempts: int,
    minimum_fit_score: int,
    profile: dict[str, Any] | None = None,
    use_preferences: bool = False,
) -> list[dict[str, Any]]:
    """Return the persisted serial application order shown on the Agent page."""

    active = [
        dict(row)
        for row in connection.execute(
            """
            SELECT j.*, w.status AS worker_status
            FROM jobs j
            LEFT JOIN worker_state w ON w.worker_id = j.worker_id
            WHERE j.pipeline_status = 'applying'
               OR (
                    j.pipeline_status = 'manual_review'
                    AND j.worker_id IS NOT NULL
                    AND w.status IN ('needs_review', 'review_ready')
               )
            ORDER BY j.last_attempted_at ASC, j.id ASC
            """
        ).fetchall()
    ]
    manual = [
        dict(row)
        for row in connection.execute(
            """
            SELECT j.* FROM jobs j
            WHERE j.manual_requested = 1 AND j.is_active = 1
              AND j.availability_status NOT IN ('closed', 'manual_only')
              AND j.pipeline_status IN ('queued', 'ready', 'failed')
            ORDER BY j.manual_requested_at ASC, j.id ASC
            """
        ).fetchall()
    ]
    automatic = (
        _claim_candidates(
            connection,
            max_attempts=max_attempts,
            target_job_id=None,
            minimum_fit_score=minimum_fit_score,
            eligible_only=False,
            profile=profile,
            use_preferences=use_preferences,
            include_queued=True,
        )
        if auto_enabled
        else []
    )

    seen = {int(row["id"]) for row in active}
    ordered: list[tuple[dict[str, Any], str]] = [(row, "active") for row in active]
    for row in manual:
        job_id = int(row["id"])
        if job_id not in seen:
            ordered.append((row, "manual"))
            seen.add(job_id)
    manual_companies = {_company_key(row["company"]) for row in manual}
    for row in automatic:
        job_id = int(row["id"])
        if job_id in seen or _company_key(row["company"]) in manual_companies:
            continue
        ordered.append((row, "auto"))
        seen.add(job_id)

    result: list[dict[str, Any]] = []
    for position, (row, queued_origin) in enumerate(ordered, start=1):
        pipeline_status = str(row.get("pipeline_status") or "queued")
        queue_state = (
            "active"
            if queued_origin == "active"
            else {
                "queued": "preparing",
                "ready": "ready",
                "failed": "retry",
            }.get(pipeline_status, "waiting")
        )
        origin = str(row.get("apply_origin") or queued_origin)
        result.append(
            {
                "position": position,
                "id": int(row["id"]),
                "company": row["company"],
                "role": row["role"],
                "location": row.get("location") or "",
                "fit_score": row.get("fit_score"),
                "pipeline_status": pipeline_status,
                "queue_state": queue_state,
                "worker_status": row.get("worker_status"),
                "origin": origin,
                "first_seen_at": row.get("first_seen_at"),
                "queued_at": (
                    row.get("last_attempted_at")
                    if queue_state == "active"
                    else row.get("manual_requested_at") or row.get("first_seen_at")
                ),
                "detail": row.get("apply_error") if queue_state == "retry" else None,
            }
        )
    return result


def mark_apply_result(
    connection: sqlite3.Connection,
    job_id: int,
    result: str,
    detail: str | None = None,
    *,
    reason_code: str | None = None,
    retain_worker: bool = False,
    manual_handoff: bool = True,
) -> None:
    now = utc_now()
    status_map = {
        "applied": "applied",
        "review_ready": "manual_review",
        "expired": "expired",
        "needs_review": "manual_review",
        "captcha": "manual_review",
        "failed": "failed",
    }
    status = status_map.get(result, "failed")
    lowered_detail = (detail or "").casefold()
    access_blocked = reason_code == "access_blocked" or any(
        marker in lowered_detail
        for marker in ("http 403", "403 forbidden", "access denied", "access blocked")
    )
    current = connection.execute(
        "SELECT availability_status FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    availability_status = (
        str(current["availability_status"]) if current is not None else "unknown"
    )
    availability_detail: str | None = None
    availability_checked_at: str | None = None
    if result == "expired":
        availability_status = "closed"
        availability_detail = detail or "Employer application is no longer available"
        availability_checked_at = now
    elif access_blocked:
        status = "manual_review" if manual_handoff else "failed"
        availability_status = "manual_only"
        availability_detail = detail or "Employer blocks the automated browser"
        availability_checked_at = now
    elif result in {"applied", "review_ready"}:
        availability_status = "open"
        availability_detail = None
        availability_checked_at = now
    applied_at = now if result == "applied" else None
    connection.execute(
        """
        UPDATE jobs SET pipeline_status = ?,
                        availability_status = ?,
                        availability_detail = CASE
                            WHEN ? IS NOT NULL OR ? IN ('open', 'unknown')
                            THEN ?
                            ELSE availability_detail
                        END,
                        availability_checked_at = COALESCE(?, availability_checked_at),
                        applied_at = COALESCE(?, applied_at),
                        submitted_resume_id = CASE WHEN ? = 'applied'
                            THEN COALESCE(submitted_resume_id, base_resume_id)
                            ELSE submitted_resume_id END,
                        submitted_resume_path = CASE WHEN ? = 'applied'
                            THEN COALESCE(submitted_resume_path, resume_path)
                            ELSE submitted_resume_path END,
                        apply_error = ?, apply_reason_code = ?,
                        manual_requested = 0, submission_requested = 0,
                        worker_id = CASE WHEN ? THEN worker_id ELSE NULL END,
                        updated_at = ?
        WHERE id = ?
        """,
        (
            status,
            availability_status,
            availability_detail,
            availability_status,
            availability_detail,
            availability_checked_at,
            applied_at,
            result,
            result,
            None if result == "applied" else detail,
            None if result == "applied" else reason_code,
            int(retain_worker),
            now,
            job_id,
        ),
    )
    add_event(connection, job_id, result, detail)
    connection.commit()


def request_final_submission(
    connection: sqlite3.Connection,
    job_id: int,
) -> dict[str, Any] | None:
    """Authorize the still-live review session to click its final Submit button."""

    job = get_job(connection, job_id)
    if job is None:
        return None
    worker_id = str(job.get("worker_id") or "")
    worker = connection.execute(
        "SELECT status, job_id FROM worker_state WHERE worker_id = ?",
        (worker_id,),
    ).fetchone()
    if (
        job.get("pipeline_status") != "manual_review"
        or job.get("availability_status") == "manual_only"
        or not worker_id
        or worker is None
        or worker["status"] != "review_ready"
        or int(worker["job_id"] or 0) != job_id
        or list_agent_inputs(connection, job_id, pending_only=True)
    ):
        raise ValueError(
            "The completed form is no longer open. Start the agent again from Latest jobs."
        )
    now = utc_now()
    connection.execute(
        """
        UPDATE jobs SET submission_requested = 1, updated_at = ?
        WHERE id = ? AND pipeline_status = 'manual_review' AND worker_id = ?
        """,
        (now, job_id, worker_id),
    )
    add_event(connection, job_id, "submission_confirmed", "Confirmed in the dashboard")
    connection.commit()
    return get_job(connection, job_id)


def final_submission_requested(
    connection: sqlite3.Connection,
    job_id: int,
    worker_id: str,
) -> bool:
    row = connection.execute(
        """
        SELECT submission_requested FROM jobs
        WHERE id = ? AND pipeline_status = 'manual_review' AND worker_id = ?
        """,
        (job_id, worker_id),
    ).fetchone()
    return bool(row and row["submission_requested"])


def resume_application_for_submission(
    connection: sqlite3.Connection,
    job_id: int,
    worker_id: str,
) -> bool:
    """Move a confirmed final-review checkpoint back to its live worker."""

    now = utc_now()
    cursor = connection.execute(
        """
        UPDATE jobs SET pipeline_status = 'applying', submission_requested = 0,
                        apply_error = NULL, apply_reason_code = NULL, updated_at = ?
        WHERE id = ? AND pipeline_status = 'manual_review' AND worker_id = ?
          AND submission_requested = 1
          AND NOT EXISTS (
              SELECT 1 FROM agent_inputs
              WHERE job_id = ? AND status = 'pending'
          )
        """,
        (now, job_id, worker_id, job_id),
    )
    if cursor.rowcount:
        add_event(
            connection,
            job_id,
            "submission_started",
            f"Final Submit authorized on {worker_id}",
        )
    connection.commit()
    return bool(cursor.rowcount)


def close_live_checkpoint(
    connection: sqlite3.Connection,
    job_id: int,
    worker_id: str,
) -> None:
    """Clear a live-session claim after its local wait window ends."""

    cursor = connection.execute(
        """
        UPDATE jobs SET worker_id = NULL, submission_requested = 0, updated_at = ?
        WHERE id = ? AND worker_id = ? AND pipeline_status = 'manual_review'
        """,
        (utc_now(), job_id, worker_id),
    )
    if cursor.rowcount:
        add_event(
            connection,
            job_id,
            "review_session_closed",
            "The local browser wait window ended",
        )
    connection.commit()


def release_claim(connection: sqlite3.Connection, job_id: int, detail: str = "released") -> None:
    connection.execute(
        """
        UPDATE jobs SET pipeline_status = 'ready', worker_id = NULL, updated_at = ?
        WHERE id = ? AND pipeline_status = 'applying'
        """,
        (utc_now(), job_id),
    )
    add_event(connection, job_id, "claim_released", detail)
    connection.commit()


def applications_today(connection: sqlite3.Connection) -> int:
    return int(
        connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE applied_at IS NOT NULL AND date(applied_at) = date('now')"
        ).fetchone()[0]
    )


def source_status(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM sources WHERE enabled = 1 ORDER BY source_key, path"
        ).fetchall()
    ]
