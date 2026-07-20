"""Secure resume storage, deterministic selection, and fact-preserving tailoring."""

from __future__ import annotations

import io
import re
import shutil
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import uuid4
from xml.sax.saxutils import escape

from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

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
_HEADINGS = {
    "education",
    "experience",
    "leadership",
    "projects",
    "relevant coursework",
    "research",
    "skills",
    "technical skills",
    "work experience",
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


def _line_score(line: str, target: set[str]) -> int:
    terms = _tokens(line)
    metric_bonus = 2 if re.search(r"\b\d+(?:\.\d+)?%?\b", line) else 0
    return len(terms & target) * 4 + metric_bonus


def build_fact_safe_tailored_pdf(
    *,
    source_text: str,
    output_path: Path,
    job: dict[str, Any],
    profile: dict[str, Any],
) -> list[str]:
    """Reprioritize verbatim source lines; never generate or rewrite a factual claim."""

    lines = [re.sub(r"\s+", " ", line).strip() for line in source_text.splitlines()]
    lines = [line for line in lines if line]
    target = _tokens(f"{job.get('role', '')} {job.get('category', '')}")
    skills = profile.get("skills", {})
    target |= _tokens(
        " ".join(
            str(item)
            for values in skills.values()
            if isinstance(values, list)
            for item in values
        )
    )
    candidates = [
        (index, line, _line_score(line, target))
        for index, line in enumerate(lines)
        if len(line) >= 18 and line.casefold().strip(":") not in _HEADINGS
    ]
    highlights = sorted(candidates, key=lambda item: (item[2], -item[0]), reverse=True)[:6]
    selected_indexes = {index for index, _, score in highlights if score > 0}
    selected_lines = [line for index, line, score in highlights if score > 0]

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ResumeName",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=17,
        leading=20,
        textColor=colors.HexColor("#2f2132"),
        alignment=TA_LEFT,
        spaceAfter=5,
    )
    contact_style = ParagraphStyle(
        "Contact",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor("#5f5361"),
        spaceAfter=10,
    )
    heading_style = ParagraphStyle(
        "Heading",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=9,
        leading=11,
        textColor=colors.HexColor("#b44b37"),
        spaceBefore=8,
        spaceAfter=4,
    )
    body_style = ParagraphStyle(
        "ResumeBody",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=8.4,
        leading=10.8,
        textColor=colors.HexColor("#241e25"),
        bulletIndent=8,
        leftIndent=0,
        spaceAfter=2.5,
    )
    personal = profile.get("personal", {})
    full_name = str(personal.get("full_name") or (lines[0] if lines else "Candidate"))
    contact = "  ·  ".join(
        str(personal.get(key) or "").strip()
        for key in ("email", "phone", "city", "state", "github_url", "portfolio_url")
        if str(personal.get(key) or "").strip()
    )
    story: list[Any] = [Paragraph(escape(full_name), title_style)]
    if contact:
        story.append(Paragraph(escape(contact), contact_style))
    if selected_lines:
        story.append(Paragraph("RELEVANT HIGHLIGHTS", heading_style))
        for line in selected_lines:
            story.append(Paragraph(f"• {escape(line)}", body_style))
        story.append(Spacer(1, 0.04 * inch))
    story.append(Paragraph("RESUME DETAILS", heading_style))
    for index, line in enumerate(lines):
        if index in selected_indexes or line == full_name or (contact and line in contact):
            continue
        normalized = line.casefold().strip(":")
        if normalized in _HEADINGS or (line.isupper() and len(line) < 45):
            story.append(Paragraph(escape(line.upper()), heading_style))
        else:
            prefix = "• " if line.startswith(("-", "•", "▪")) else ""
            clean_line = line.lstrip("-•▪ ") if prefix else line
            story.append(Paragraph(f"{prefix}{escape(clean_line)}", body_style))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    document = SimpleDocTemplate(
        str(output_path),
        pagesize=LETTER,
        rightMargin=0.62 * inch,
        leftMargin=0.62 * inch,
        topMargin=0.52 * inch,
        bottomMargin=0.52 * inch,
        title=f"{full_name} — {job.get('role', 'Internship')}",
        author=full_name,
    )
    document.build(story)
    with suppress(OSError):
        output_path.chmod(0o600)
    return selected_lines


def choose_and_tailor_resume(
    *,
    job: dict[str, Any],
    paths: AppPaths,
    profile: dict[str, Any],
    tailor: bool,
    db_path: str | Path | None = None,
) -> tuple[dict[str, Any], Path, str, str]:
    connection = get_connection(db_path)
    import_legacy_resume(paths=paths, db_path=db_path)
    ranked = rank_resumes(job, list_resumes(connection), profile)
    if not ranked:
        raise FileNotFoundError("No active resumes. Upload at least one resume in the web app.")
    _, selected, reason = ranked[0]
    source_text = Path(str(selected["text_path"])).read_text(encoding="utf-8")
    if not tailor:
        return selected, Path(str(selected["pdf_path"])), reason, source_text
    packet_dir = paths.packets / f"{job['id']}-resume"
    output_path = packet_dir / "tailored-resume.pdf"
    highlights = build_fact_safe_tailored_pdf(
        source_text=source_text,
        output_path=output_path,
        job=job,
        profile=profile,
    )
    tailoring = f"Selected {selected['name']} ({reason}); reprioritized {len(highlights)} verbatim lines"
    return selected, output_path, tailoring, source_text
