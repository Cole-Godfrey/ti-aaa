from __future__ import annotations

import json

from typer.testing import CliRunner

from tiaaa.cli import app


def test_init_can_import_files_into_a_fresh_workspace(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    source = tmp_path / "inputs"
    source.mkdir()
    profile = source / "profile.json"
    profile.write_text(
        json.dumps(
            {
                "personal": {"full_name": "Avery Student"},
                "education": {},
                "work_authorization": {},
                "preferences": {},
            }
        ),
        encoding="utf-8",
    )
    settings = source / "settings.yaml"
    settings.write_text("poll_interval_seconds: 600\n", encoding="utf-8")
    resume_txt = source / "resume.txt"
    resume_txt.write_text("resume facts", encoding="utf-8")
    resume_pdf = source / "resume.pdf"
    resume_pdf.write_bytes(b"%PDF-1.4")
    monkeypatch.setenv("TIAAA_HOME", str(home))

    result = CliRunner().invoke(
        app,
        [
            "init",
            "--profile",
            str(profile),
            "--settings",
            str(settings),
            "--resume-txt",
            str(resume_txt),
            "--resume-pdf",
            str(resume_pdf),
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads((home / "profile.json").read_text())["personal"]["full_name"] == "Avery Student"
    assert (home / "resume.pdf").read_bytes() == b"%PDF-1.4"
