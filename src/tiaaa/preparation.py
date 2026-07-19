"""Fit scoring and factual application-packet preparation."""

from __future__ import annotations

import json
import logging
import re
from contextlib import suppress
from pathlib import Path
from typing import Any

from tiaaa.config import AppPaths
from tiaaa.database import add_event, get_connection, mark_prepared, pending_preparation, utc_now
from tiaaa.llm import get_client

log = logging.getLogger(__name__)


def _json_object(value: str) -> dict[str, Any]:
    value = value.strip()
    value = re.sub(r"^```(?:json)?\s*", "", value)
    value = re.sub(r"\s*```$", "", value)
    start, end = value.find("{"), value.rfind("}")
    if start < 0 or end < start:
        raise ValueError("LLM response did not contain a JSON object")
    parsed = json.loads(value[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("LLM response was not an object")
    return parsed


def _metadata(job: dict[str, Any]) -> str:
    return (
        f"Company: {job['company']}\n"
        f"Role: {job['role']}\n"
        f"Location: {job.get('location') or 'Not listed'}\n"
        f"Category: {job.get('category') or 'Tech'}\n"
        f"Season/posting date: {job.get('posting_date') or 'Not listed'}"
    )


def score_jobs_with_llm(
    *,
    paths: AppPaths,
    limit: int = 0,
    db_path: str | Path | None = None,
) -> dict[str, int]:
    """Optionally refine heuristic scores using repository metadata only."""

    if not paths.resume_text.exists():
        raise FileNotFoundError(f"Resume text not found: {paths.resume_text}")
    resume = paths.resume_text.read_text(encoding="utf-8")
    connection = get_connection(db_path)
    query = """
        SELECT * FROM jobs WHERE eligibility = 'eligible' AND is_active = 1
          AND pipeline_status = 'queued'
        ORDER BY discovered_as_new DESC, first_seen_at DESC
    """
    parameters: list[Any] = []
    if limit > 0:
        query += " LIMIT ?"
        parameters.append(limit)
    jobs = [dict(row) for row in connection.execute(query, parameters).fetchall()]
    completed = errors = 0
    client = get_client()
    try:
        for job in jobs:
            prompt = f"""Evaluate this student's fit for a technology internship using ONLY the facts below.
The listing came from a curated GitHub repository; no full job description is available.
Do not infer credentials that are absent. Return JSON only:
{{"score": 1-10, "reasoning": "one concise sentence"}}

STUDENT RESUME
{resume[:10000]}

INTERNSHIP METADATA
{_metadata(job)}
"""
            try:
                result = _json_object(client.ask(prompt, max_tokens=300))
                score = max(1, min(10, int(result["score"])))
                reasoning = str(result.get("reasoning", "LLM metadata score"))
                now = utc_now()
                connection.execute(
                    """
                    UPDATE jobs SET fit_score = ?, score_reasoning = ?, scored_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (score, reasoning, now, now, job["id"]),
                )
                add_event(connection, int(job["id"]), "scored", f"{score}/10: {reasoning}")
                connection.commit()
                completed += 1
            except Exception as exc:
                errors += 1
                log.warning("Could not score %s at %s: %s", job["role"], job["company"], exc)
    finally:
        client.close()
    return {"scored": completed, "errors": errors}


def _safe_slug(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").casefold()
    return value[:60] or "internship"


def _generate_packet(
    *,
    job: dict[str, Any],
    resume: str,
    profile: dict[str, Any],
) -> tuple[str, str]:
    client = get_client()
    try:
        prompt = f"""Create a concise internship application packet using ONLY verifiable facts in the
student profile and resume. Never invent a skill, metric, course, employer, project, or motivation.
Because discovery intentionally does not scrape the application page, use only the metadata provided.

Return valid JSON only:
{{
  "cover_letter": "180-260 words, natural and specific, or an empty string if there are too few facts",
  "talking_points": ["three to five factual resume points relevant to the role"]
}}

PROFILE
{json.dumps(profile, ensure_ascii=False)[:8000]}

RESUME
{resume[:12000]}

INTERNSHIP
{_metadata(job)}
"""
        result = _json_object(client.ask(prompt, max_tokens=1200, temperature=0.2))
        cover_letter = str(result.get("cover_letter", "")).strip()
        points = result.get("talking_points", [])
        if not isinstance(points, list):
            points = []
        notes = "\n".join(f"- {str(point).strip()}" for point in points if str(point).strip())
        return cover_letter, notes
    finally:
        client.close()


def prepare_jobs(
    *,
    paths: AppPaths,
    profile: dict[str, Any],
    settings: dict[str, Any],
    limit: int = 0,
    db_path: str | Path | None = None,
) -> dict[str, int]:
    """Attach the base resume and optionally generate a factual cover letter."""

    if not paths.resume_pdf.exists():
        raise FileNotFoundError(
            f"Resume PDF not found: {paths.resume_pdf}. Copy a recruiter-ready PDF there first."
        )
    if not paths.resume_text.exists():
        raise FileNotFoundError(
            f"Resume text not found: {paths.resume_text}. It is required as the agent's fact source."
        )
    resume = paths.resume_text.read_text(encoding="utf-8")
    minimum_score = int(settings.get("minimum_fit_score", 5))
    connection = get_connection(db_path)
    jobs = pending_preparation(connection, minimum_score, limit)
    use_llm = bool(settings.get("preparation", {}).get("use_llm"))
    generate_cover = bool(settings.get("preparation", {}).get("generate_cover_letters", True))
    prepared = errors = 0

    for job in jobs:
        cover_path: Path | None = None
        notes = "Base resume selected; repository metadata retained for the browser agent."
        try:
            if use_llm and generate_cover:
                cover_letter, talking_points = _generate_packet(job=job, resume=resume, profile=profile)
                packet_name = f"{job['id']}-{_safe_slug(job['company'])}-{_safe_slug(job['role'])}"
                packet_dir = paths.packets / packet_name
                packet_dir.mkdir(parents=True, exist_ok=True)
                if cover_letter:
                    cover_path = packet_dir / "cover-letter.txt"
                    cover_path.write_text(cover_letter + "\n", encoding="utf-8")
                    with suppress(OSError):
                        cover_path.chmod(0o600)
                if talking_points:
                    notes_path = packet_dir / "talking-points.txt"
                    notes_path.write_text(talking_points + "\n", encoding="utf-8")
                    with suppress(OSError):
                        notes_path.chmod(0o600)
                    notes = talking_points
            mark_prepared(
                connection,
                int(job["id"]),
                resume_path=str(paths.resume_pdf.resolve()),
                cover_letter_path=str(cover_path.resolve()) if cover_path else None,
                notes=notes,
            )
            prepared += 1
        except Exception as exc:
            errors += 1
            log.warning("Could not prepare %s at %s: %s", job["role"], job["company"], exc)
    return {"prepared": prepared, "errors": errors}
