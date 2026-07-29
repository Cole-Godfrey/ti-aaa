from __future__ import annotations

import io

from reportlab.pdfgen import canvas

from tiaaa.api import TIAAA


def test_public_python_facade_initializes_and_configures_workspace(
    tmp_path, profile, settings
) -> None:
    agent = TIAAA(tmp_path)
    agent.configure(profile=profile, settings=settings)
    buffer = io.BytesIO()
    document = canvas.Canvas(buffer)
    document.drawString(72, 760, "Avery Student")
    document.drawString(72, 735, "Built a Python backend service for a university project")
    document.save()
    resume_path = tmp_path / "resume.pdf"
    resume_path.write_bytes(buffer.getvalue())
    resume = agent.add_resume(resume_path, name="Backend", tags=["python"])

    assert agent.profile["personal"]["full_name"] == "Avery Student"
    assert agent.settings["poll_interval_seconds"] == 300
    assert resume["name"] == "Backend"
    assert agent.resumes()[0]["tags"] == ["python"]
    assert agent.stats()["applications"] == 0
    assert agent.analytics()["summary"]["applications"] == 0
    assert agent.jobs() == []
