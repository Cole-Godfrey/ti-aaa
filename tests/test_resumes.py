from __future__ import annotations

import io
from pathlib import Path

from reportlab.pdfgen import canvas

from tiaaa.config import SOURCE_DOCUMENTS, AppPaths, ensure_dirs
from tiaaa.database import get_job, ingest_listings, init_db, mark_apply_result
from tiaaa.models import InternshipListing
from tiaaa.preparation import prepare_jobs
from tiaaa.resumes import store_resume


def pdf_with_text(*lines: str) -> bytes:
    buffer = io.BytesIO()
    document = canvas.Canvas(buffer)
    y = 760
    for line in lines:
        document.drawString(72, y, line)
        y -= 20
    document.save()
    return buffer.getvalue()


def test_best_resume_is_selected_tailored_and_recorded_on_submission(
    tmp_path, profile, settings
) -> None:
    paths = ensure_dirs(AppPaths(tmp_path))
    connection = init_db(paths.database)
    store_resume(
        paths=paths,
        name="Frontend",
        original_filename="frontend.pdf",
        content=pdf_with_text(
            "Avery Student",
            "Built accessible React interfaces for a university project",
            "JavaScript and CSS",
        ),
        tags=["frontend", "react"],
        db_path=paths.database,
    )
    backend = store_resume(
        paths=paths,
        name="Backend",
        original_filename="backend.pdf",
        content=pdf_with_text(
            "Avery Student",
            "Built a Python API for a university project",
            "Used Docker for local development",
        ),
        tags=["backend", "python"],
        db_path=paths.database,
    )
    source = SOURCE_DOCUMENTS[0]
    listing = InternshipListing(
        company="Acme",
        role="Backend Python Intern",
        location="Remote",
        application_url="https://jobs.test/backend",
        source_key=source.key,
        source_label=source.label,
        source_repo_url=source.repo_url,
        source_path=source.path,
    )
    ingest_listings(
        connection,
        source,
        [listing],
        profile=profile,
        settings=settings,
        include_existing=True,
    )

    result = prepare_jobs(
        paths=paths,
        profile=profile,
        settings=settings,
        db_path=paths.database,
    )
    prepared = get_job(connection, 1)

    assert result == {"prepared": 1, "errors": 0}
    assert prepared is not None
    assert prepared["base_resume_id"] == backend["id"]
    assert prepared["base_resume_name"] == "Backend"
    assert Path(prepared["resume_path"]).is_file()
    assert prepared["resume_path"] != backend["pdf_path"]
    assert "verbatim lines" in prepared["tailoring_reason"]

    mark_apply_result(connection, 1, "applied")
    submitted = get_job(connection, 1)
    assert submitted is not None
    assert submitted["submitted_resume_id"] == backend["id"]
    assert submitted["submitted_resume_name"] == "Backend"
    assert submitted["submitted_resume_path"] == prepared["resume_path"]
