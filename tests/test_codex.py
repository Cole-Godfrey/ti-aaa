from __future__ import annotations

import json
import subprocess
import sys
import time

import pytest

from tiaaa.apply import runner
from tiaaa.codex import (
    CodexStopped,
    codex_command,
    codex_final,
    codex_usage_limited,
    run_codex,
)


def events(*items):
    return "\n".join(json.dumps(item) for item in items)


def test_command_exposes_only_browser_tools_and_preserves_sandbox(tmp_path):
    command = codex_command(
        schema_path=tmp_path / "schema.json", port=9430, browser_tools=("browser_snapshot", "browser_click")
    )
    assert "--ignore-user-config" in command
    assert "--ephemeral" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert "features.shell_tool=false" in command
    assert 'web_search="disabled"' in command
    assert 'mcp_servers.tiaaa_browser.enabled_tools=["browser_snapshot", "browser_click"]' in command
    assert command[-1] == "-"
    assert "--model" not in command


def test_usage_detection_ignores_page_and_assistant_text():
    assert not codex_usage_limited(
        events(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "The webpage said 429 usage limit"},
            }
        )
    )
    assert codex_usage_limited(
        events({"type": "turn.failed", "error": {"message": "You've hit your usage limit"}})
    )
    assert not codex_usage_limited(
        events({"type": "turn.failed", "error": {"message": "Could not connect to browser"}})
    )


def test_only_final_agent_message_can_supply_a_result():
    output = events(
        [],
        {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "server": "tiaaa_browser",
                "tool": "browser_snapshot",
                "result": "RESULT:APPLIED",
            },
        },
        {"type": "item.completed", "item": {"type": "agent_message", "text": '{"status":"REVIEW_READY"}'}},
    )
    assert codex_final(output) == '{"status":"REVIEW_READY"}'
    assert runner._extract_agent_text(output) == codex_final(output)
    assert runner._stream_summary(output, returncode=0)["browser_action_count"] == 1


def test_codex_process_drains_output_and_keeps_credentials_out_of_child(tmp_path, monkeypatch):
    monkeypatch.setenv("TIAAA_EMAIL_APP_PASSWORD", "secret-mailbox-password")
    script = 'import os,sys; p=sys.stdin.read(); print(len(p)); print(os.getenv("TIAAA_EMAIL_APP_PASSWORD"))'
    output, code = run_codex([sys.executable, "-c", script], "x" * 100000, cwd=tmp_path, timeout=5)
    assert code == 0
    assert output.splitlines() == ["100000", "None"]


def test_codex_timeout_and_stop_reap_the_process(tmp_path):
    command = [sys.executable, "-c", "import sys,time; sys.stdin.read(); time.sleep(60)"]
    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_codex(command, "test", cwd=tmp_path, timeout=0.1)
    with pytest.raises(CodexStopped):
        run_codex(command, "test", cwd=tmp_path, timeout=30, should_stop=lambda: True)
    assert time.monotonic() - start < 5


def test_ephemeral_continuations_preserve_context_without_retaining_codes(tmp_path, monkeypatch):
    prompts = []
    monkeypatch.setattr(runner, "run_codex", lambda _cmd, prompt, **_kw: (prompts.append(prompt) or "", 0))
    process = runner._CodexProcess(["codex"], cwd=tmp_path, environment={})
    process.turn("Original facts", timeout=1)
    process.turn("Enter one-time code 87654321", timeout=1)
    process.turn("Check receipt", timeout=1)
    assert "Original facts" in prompts[-1]
    assert "87654321" not in prompts[-1]
    assert "historical" in prompts[-1]
    process.close()
    assert not process.context and not process.alive


@pytest.mark.parametrize("submission_started", [False, True])
def test_usage_fallback_keeps_form_and_never_retries_uncertain_submission(
    tmp_path, monkeypatch, submission_started
):
    session = object.__new__(runner._ApplicationAgentSession)
    session.provider = "codex"
    session.claude_fallback = True
    session.prepare_browser = None
    session.submission_started = submission_started
    session.initial_prompt = "Original facts"
    session.submit = True
    session.turn_number = 0
    session.process = object()
    session.job = {"id": 1}
    session.paths = object()
    session.worker_id = 0
    session.timeout = 10
    session.should_stop = None
    prompts = []

    def turn(**kwargs):
        prompts.append(kwargs["prompt"])
        if len(prompts) == 1:
            return runner.AgentResult(
                "failed", "Model usage limit reached; application receipt was not confirmed"
            ), {}
        return runner.AgentResult("needs_review" if submission_started else "review_ready"), {}

    monkeypatch.setattr(runner, "_run_agent_turn", turn)
    monkeypatch.setattr(runner.shutil, "which", lambda _name: "/bin/claude")
    monkeypatch.setattr(runner._ApplicationAgentSession, "close", lambda _self: None)
    monkeypatch.setattr(runner._ApplicationAgentSession, "_start_process", lambda _self: None)
    monkeypatch.setattr(runner, "_CODEX_RETRY_AT", 0)
    session._turn("Current turn", submit=submission_started)
    assert session.provider == "claude"
    assert len(prompts) == 2
    assert "Do not navigate, reload" in prompts[1]
    if submission_started:
        assert "do not click Submit again" in prompts[1]


def test_error_envelope_cannot_claim_success(tmp_path):
    output = events(
        {
            "type": "result",
            "is_error": True,
            "api_error_status": 429,
            "structured_output": {"status": "APPLIED"},
        }
    )

    class FakeProcess:
        def turn(self, *args, **kwargs):
            return output, 0

    from tiaaa.config import AppPaths, ensure_dirs

    parsed, _ = runner._run_agent_turn(
        process=FakeProcess(),
        prompt="test",
        job={"id": 1},
        paths=ensure_dirs(AppPaths(tmp_path)),
        worker_id=0,
        timeout=1,
        submit=True,
        turn_number=1,
    )
    assert parsed.result == "failed"
    assert "usage limit" in parsed.detail
