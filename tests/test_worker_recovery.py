from datetime import UTC, datetime

import pytest

from tiaaa.apply import runner
from tiaaa.config import SOURCE_DOCUMENTS, AppPaths, ensure_dirs, save_settings
from tiaaa.database import (
    get_job,
    ingest_listings,
    init_db,
    live_human_interaction_checkpoint,
    request_human_control_return,
)
from tiaaa.models import InternshipListing


@pytest.mark.parametrize("blocker", ["captcha", "access_blocked", "verification_required", "email"])
def test_auto_worker_keeps_checkpoints_and_resumes_same_session(
    tmp_path, profile, settings, monkeypatch, blocker
):
    paths = ensure_dirs(AppPaths(tmp_path))
    settings["automation"]["auto_apply_new"] = True
    save_settings(settings, paths)
    db = init_db(paths.database)
    source = SOURCE_DOCUMENTS[0]
    listing = InternshipListing(
        company="Acme",
        role="Software Intern",
        location="Remote",
        application_url="https://jobs.ashbyhq.com/test",
        source_key=source.key,
        source_label=source.label,
        source_repo_url=source.repo_url,
        source_path=source.path,
    )
    ingest_listings(db, source, [listing], profile=profile, settings=settings, include_existing=True)
    db.execute("UPDATE jobs SET pipeline_status='ready', apply_decision='apply', discovered_as_new=1")
    db.commit()
    closed = []
    resumed = []

    class Preview:
        def __init__(self, **kwargs):
            pass

        def start(self):
            pass

        def stop(self):
            pass

    class Session:
        submission_authorized = True
        last_turn_started = datetime.now(UTC)

        def __init__(self, **kwargs):
            pass

        def start(self):
            if blocker == "email":
                return runner.AgentResult(
                    "needs_review",
                    "Email verification code",
                    "verification_required",
                    [
                        {
                            "key": "email_verification_code",
                            "label": "Email verification code",
                            "input_type": "verification_code",
                            "options": [],
                            "required": True,
                        }
                    ],
                )
            return runner.AgentResult(
                "captcha" if blocker == "captcha" else "needs_review", "Blocked", blocker
            )

        def continue_after_human_control(self):
            resumed.append("human")
            return runner.AgentResult("applied")

        def continue_with(self, answers):
            resumed.append(answers)
            return runner.AgentResult("applied")

        def close(self):
            closed.append(True)

    def handoff(connection, job_id, worker_id, **kwargs):
        assert live_human_interaction_checkpoint(connection, job_id, worker_id)
        assert get_job(connection, job_id)["worker_id"] == "worker-0"
        request_human_control_return(connection, job_id)
        return True

    monkeypatch.setattr(runner, "_ApplicationAgentSession", Session)
    monkeypatch.setattr(runner, "PreviewCapture", Preview)
    monkeypatch.setattr(runner, "launch_chrome", lambda **kwargs: (None, 9330))
    monkeypatch.setattr(runner, "_launch_mcp_bridge", lambda **kwargs: None)
    monkeypatch.setattr(runner, "_wait_for_human_control_return", handoff)
    if blocker == "email":
        monkeypatch.setattr(runner.EmailVerification, "enabled", property(lambda self: True))
        monkeypatch.setattr(runner.EmailVerification, "poll", lambda self, **kwargs: "A1B2C3D4")
    totals = runner._worker(
        worker_id=0,
        quota=1,
        target_job_id=None,
        profile=profile,
        settings=settings,
        paths=paths,
        db_path=paths.database,
        submit=True,
        unattended=True,
        interactive_review=False,
    )
    assert totals["applied"] == 1
    assert len(resumed) == 1
    assert closed == [True]
    assert get_job(db, 1)["pipeline_status"] == "applied"
    assert not db.execute("SELECT * FROM agent_inputs").fetchall()
