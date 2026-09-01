"""Secure resume storage, deterministic selection, and byte-preserved application copies."""

from __future__ import annotations

import io
import re
import shutil
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import uuid4

from pypdf import PdfReader

from tiaaa.config import AppPaths
from tiaaa.database import add_resume_record, get_connection, list_resumes

MAX_RESUME_BYTES = 12 * 1024 * 1024
_WORD = re.compile(r"[a-z][a-z0-9+#.-]{1,}")
_STOP_WORDS = {
    "and",
    "for",
    "from",
    "intern",
    "internship",
    "the",
    "with",
    "engineer",
    "engineering",
    "summer",
    "tech",
}


def _tokens(value: str) -> set[str]:
    return {word for word in _WORD.findall(value.casefold()) if word not in _STOP_WORDS}


def extract_pdf_text(content: bytes) -> str:
    if len(content) > MAX_RESUME_BYTES:
        raise ValueError("Resume PDF is larger than 12 MiB")
    if not content.startswith(b"%PDF"):
        raise ValueError("Resume must be a PDF file")
    try:
        reader = PdfReader(io.BytesIO(content))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as exc:
        raise ValueError("The PDF could not be read") from exc
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()


def store_resume(
    *,
    paths: AppPaths,
    name: str,
    original_filename: str,
    content: bytes,
    text_override: str = "",
    tags: list[str] | None = None,
    notes: str = "",
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Validate an uploaded PDF, extract its facts, and register it locally."""

    paths.resumes.mkdir(parents=True, exist_ok=True)
    clean_name = re.sub(r"\s+", " ", name).strip()[:100]
    if not clean_name:
        raise ValueError("Resume name is required")
    extracted = extract_pdf_text(content)
    resume_text = text_override.strip() or extracted
    if len(resume_text) < 40:
        raise ValueError(
            "Not enough selectable text was found. Paste the resume text in the upload form."
        )
    identifier = uuid4().hex
    pdf_path = paths.resumes / f"{identifier}.pdf"
    text_path = paths.resumes / f"{identifier}.txt"
    pdf_path.write_bytes(content)
    text_path.write_text(resume_text + "\n", encoding="utf-8")
    for item in (pdf_path, text_path):
        with suppress(OSError):
            item.chmod(0o600)
    try:
        return add_resume_record(
            get_connection(db_path),
            name=clean_name,
            original_filename=Path(original_filename).name[:180] or "resume.pdf",
            pdf_path=str(pdf_path.resolve()),
            text_path=str(text_path.resolve()),
            tags=[tag.strip()[:40] for tag in (tags or []) if tag.strip()],
            notes=notes[:1000],
        )
    except Exception:
        pdf_path.unlink(missing_ok=True)
        text_path.unlink(missing_ok=True)
        raise


def import_legacy_resume(
    *, paths: AppPaths, db_path: str | Path | None = None
) -> dict[str, Any] | None:
    """Register the original resume.pdf/resume.txt pair for CLI backwards compatibility."""

    connection = get_connection(db_path)
    if list_resumes(connection):
        return None
    if not paths.resume_pdf.is_file() or not paths.resume_text.is_file():
        return None
    paths.resumes.mkdir(parents=True, exist_ok=True)
    identifier = uuid4().hex
    pdf_path = paths.resumes / f"{identifier}.pdf"
    text_path = paths.resumes / f"{identifier}.txt"
    shutil.copyfile(paths.resume_pdf, pdf_path)
    shutil.copyfile(paths.resume_text, text_path)
    for item in (pdf_path, text_path):
        with suppress(OSError):
            item.chmod(0o600)
    return add_resume_record(
        connection,
        name="Default resume",
        original_filename=paths.resume_pdf.name,
        pdf_path=str(pdf_path.resolve()),
        text_path=str(text_path.resolve()),
        tags=["general"],
        notes="Imported from the original TI-AAA CLI setup.",
    )


def rank_resumes(
    job: dict[str, Any], resumes: list[dict[str, Any]], profile: dict[str, Any]
) -> list[tuple[int, dict[str, Any], str]]:
    role_text = " ".join(
        str(job.get(key) or "") for key in ("role", "category")
    )
    target = _tokens(role_text)
    skills = profile.get("skills", {})
    profile_terms = _tokens(
        " ".join(
            str(item)
            for values in skills.values()
            if isinstance(values, list)
            for item in values
        )
    )
    ranked: list[tuple[int, dict[str, Any], str]] = []
    for resume in resumes:
        try:
            text = Path(str(resume["text_path"])).read_text(encoding="utf-8")
        except OSError:
            continue
        resume_terms = _tokens(text)
        tags = _tokens(" ".join(str(tag) for tag in resume.get("tags", [])))
        role_matches = sorted(target & resume_terms)
        tag_matches = sorted(target & tags)
        skill_matches = sorted(target & profile_terms & resume_terms)
        score = len(role_matches) * 3 + len(tag_matches) * 5 + len(skill_matches) * 2
        reason_parts = []
        if tag_matches:
            reason_parts.append(f"tags: {', '.join(tag_matches[:4])}")
        if role_matches:
            reason_parts.append(f"matching facts: {', '.join(role_matches[:6])}")
        reason = "; ".join(reason_parts) or "general resume fallback"
        ranked.append((score, resume, reason))
    return sorted(ranked, key=lambda item: (item[0], item[1]["created_at"]), reverse=True)


def candidate_resume_filename(profile: dict[str, Any]) -> str:
    """Build the stable applicant-facing PDF filename used for every application."""

    full_name = str(profile.get("personal", {}).get("full_name") or "Candidate")
    parts = re.findall(r"[A-Za-z0-9]+", full_name)
    stem = "_".join(parts) or "Candidate"
    return f"{stem}_Resume.pdf"


def choose_resume(
    *,
    job: dict[str, Any],
    paths: AppPaths,
    profile: dict[str, Any],
    db_path: str | Path | None = None,
) -> tuple[dict[str, Any], Path, str, str]:
    """Choose the best existing resume without changing its PDF.

    The apply/skip reviewer reads every resume against the employer's own posting,
    so its choice wins when it made one. Keyword ranking is the fallback for
    listings that were never reviewed.
    """

    connection = get_connection(db_path)
    import_legacy_resume(paths=paths, db_path=db_path)
    active = list_resumes(connection)
    if not active:
        raise FileNotFoundError("No active resumes. Upload at least one resume in the web app.")
    reviewed_id = job.get("apply_resume_id")
    if reviewed_id is not None:
        chosen = next((item for item in active if int(item["id"]) == int(reviewed_id)), None)
        if chosen is not None:
            return (
                chosen,
                Path(str(chosen["pdf_path"])),
                "chosen by the posting review",
                Path(str(chosen["text_path"])).read_text(encoding="utf-8"),
            )
    ranked = rank_resumes(job, active, profile)
    if not ranked:
        raise FileNotFoundError("No active resumes. Upload at least one resume in the web app.")
    _, selected, reason = ranked[0]
    source_text = Path(str(selected["text_path"])).read_text(encoding="utf-8")
    return selected, Path(str(selected["pdf_path"])), reason, source_text


def prepare_resume_copy(
    *,
    job: dict[str, Any],
    paths: AppPaths,
    profile: dict[str, Any],
    db_path: str | Path | None = None,
) -> tuple[dict[str, Any], Path, str, str]:
    """Copy the selected original PDF byte-for-byte under the candidate filename."""

    selected, source_path, reason, source_text = choose_resume(
        job=job,
        paths=paths,
        profile=profile,
        db_path=db_path,
    )
    packet_dir = paths.packets / f"{job['id']}-resume"
    output_path = packet_dir / candidate_resume_filename(profile)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_path, output_path)
    with suppress(OSError):
        output_path.chmod(0o600)
    selection = (
        f"Selected {selected['name']} ({reason}); preserved the original PDF as "
        f"{output_path.name}"
    )
    return selected, output_path, selection, source_text
