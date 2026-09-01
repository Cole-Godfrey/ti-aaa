from __future__ import annotations

import json

from tiaaa.config import SOURCE_DOCUMENTS, AppPaths
from tiaaa.database import get_connection, ingest_listings, init_db, request_manual_application
from tiaaa.models import InternshipListing
from tiaaa.preparation import prepare_jobs


class FakeClient:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.closed = False

    def ask(self, *_args, **_kwargs) -> str:
        return json.dumps(self.response)

    def close(self) -> None:
        self.closed = True


def seed_job(path, profile, settings) -> None:
    connection = init_db(path)
    source = SOURCE_DOCUMENTS[0]
    job = InternshipListing(
        company="Acme",
        role="Software Engineer Intern",
        location="Remote",
        application_url="https://jobs.test/1",
        source_key=source.key,
        source_label=source.label,
        source_repo_url=source.repo_url,
        source_path=source.path,
    )
    ingest_listings(
        connection, source, [job], profile=profile, settings=settings, include_existing=True
    )


def make_paths(tmp_path) -> AppPaths:
    paths = AppPaths(tmp_path)
    paths.packets.mkdir()
    paths.resume_text.write_text("Built a Python API for a university project.", encoding="utf-8")
    paths.resume_pdf.write_bytes(b"%PDF-1.4")
    return paths


def test_prepare_without_llm_attaches_base_resume(tmp_path, profile, settings) -> None:
    paths = make_paths(tmp_path)
    seed_job(paths.database, profile, settings)
    result = prepare_jobs(
        paths=paths,
        profile=profile,
        settings=settings,
        db_path=paths.database,
    )

    row = get_connection(paths.database).execute("SELECT * FROM jobs").fetchone()
    assert result == {"prepared": 1, "errors": 0}
    assert row["pipeline_status"] == "ready"
    assert row["base_resume_id"] is not None
    assert row["resume_path"].endswith("Avery_Student_Resume.pdf")
    assert (tmp_path / row["resume_path"]).read_bytes() == paths.resume_pdf.read_bytes()
    assert "preserved the original PDF" in row["tailoring_reason"]
    assert row["cover_letter_path"] is None


def test_manual_request_prepares_a_first_sync_listing(tmp_path, profile, settings) -> None:
    paths = make_paths(tmp_path)
    connection = init_db(paths.database)
    source = SOURCE_DOCUMENTS[0]
    job = InternshipListing(
        company="Manual Co",
        role="Backend Intern",
        location="Remote",
        application_url="https://jobs.test/manual",
        source_key=source.key,
        source_label=source.label,
        source_repo_url=source.repo_url,
        source_path=source.path,
    )
    ingest_listings(connection, source, [job], profile=profile, settings=settings)
    request_manual_application(connection, 1)

    result = prepare_jobs(
        paths=paths, profile=profile, settings=settings, db_path=paths.database
    )

    row = get_connection(paths.database).execute("SELECT * FROM jobs").fetchone()
    assert result["prepared"] == 1
    assert row["pipeline_status"] == "ready"
    assert row["manual_requested"] == 1


def test_prepare_with_llm_writes_fact_packet(tmp_path, profile, settings, monkeypatch) -> None:
    paths = make_paths(tmp_path)
    seed_job(paths.database, profile, settings)
    settings["preparation"]["use_llm"] = True
    fake = FakeClient(
        {
            "cover_letter": "Dear Acme, my Python university project is relevant.",
            "talking_points": ["Built a Python API for a university project."],
        }
    )
    monkeypatch.setattr("tiaaa.preparation.get_client", lambda: fake)

    result = prepare_jobs(
        paths=paths,
        profile=profile,
        settings=settings,
        db_path=paths.database,
    )

    row = get_connection(paths.database).execute("SELECT * FROM jobs").fetchone()
    cover_path = tmp_path / row["cover_letter_path"]
    assert result["prepared"] == 1
    assert fake.closed
    assert "Dear Acme" in cover_path.read_text()
    assert "Built a Python API" in row["preparation_notes"]
