from __future__ import annotations

import json
import subprocess

from fastapi.testclient import TestClient

from tiaaa.claude_auth import ClaudeAuthManager
from tiaaa.config import AppPaths
from tiaaa.dashboard.app import create_app


def test_claude_auth_status_exposes_only_safe_fields(tmp_path, monkeypatch) -> None:
    manager = ClaudeAuthManager(AppPaths(tmp_path))
    monkeypatch.setattr("tiaaa.claude_auth.shutil.which", lambda _name: "/bin/claude")
    raw = {
        "loggedIn": True,
        "authMethod": "claude.ai",
        "apiProvider": "firstParty",
        "email": "private@example.com",
        "accessToken": "never-return-this",
    }
    monkeypatch.setattr(
        "tiaaa.claude_auth.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(raw)
        ),
    )

    assert manager.status() == {
        "installed": True,
        "logged_in": True,
        "auth_method": "claude.ai",
        "provider": "firstParty",
        "login_pending": False,
    }


def test_dashboard_claude_subscription_auth_flow(tmp_path) -> None:
    class FakeClaudeAuth:
        def status(self):
            return {"installed": True, "logged_in": False, "login_pending": False}

        def start_login(self):
            return {
                "installed": True,
                "logged_in": False,
                "login_pending": True,
                "login_url": "https://claude.com/cai/oauth/authorize?state=test",
            }

        def complete_login(self, code):
            assert code == "one-time-code"
            return {
                "installed": True,
                "logged_in": True,
                "auth_method": "claude.ai",
                "login_pending": False,
            }

        def logout(self):
            return {"installed": True, "logged_in": False, "login_pending": False}

    paths = AppPaths(tmp_path)
    app = create_app(paths.database, paths=paths)
    app.state.claude_auth = FakeClaudeAuth()
    client = TestClient(app)

    assert client.get("/api/claude-auth").json()["logged_in"] is False
    login = client.post("/api/claude-auth/login")
    assert login.status_code == 200
    assert login.json()["login_url"].startswith("https://claude.com/")
    complete = client.post(
        "/api/claude-auth/complete", json={"code": "one-time-code"}
    )
    assert complete.status_code == 200
    assert complete.json()["auth_method"] == "claude.ai"
    assert client.delete("/api/claude-auth").json()["logged_in"] is False
