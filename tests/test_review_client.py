from __future__ import annotations

import json
import subprocess

import pytest

from tiaaa.review import client as client_module
from tiaaa.review.client import (
    ClaudeCodeReviewClient,
    ReviewUnavailable,
    get_review_client,
)

PAYLOAD = {"company_summary": "ok", "decisions": []}


@pytest.fixture
def cli(monkeypatch) -> ClaudeCodeReviewClient:
    monkeypatch.setattr(client_module.shutil, "which", lambda _name: "/usr/bin/claude")
    return ClaudeCodeReviewClient("claude-opus-5")


def completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


def test_cli_requests_a_text_only_turn_with_the_schema(cli, monkeypatch) -> None:
    seen: dict = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["input"] = kwargs["input"]
        return completed(json.dumps({"structured_output": PAYLOAD}))

    monkeypatch.setattr(client_module.subprocess, "run", fake_run)

    result = cli.decide(system="be careful", prompt="decide this", schema={"type": "object"})

    assert result == PAYLOAD
    command = seen["command"]
    assert seen["input"] == "decide this"
    # `opus` is the CLI's alias for the API's claude-opus-5.
    assert command[command.index("--model") + 1] == "opus"
    assert command[command.index("--effort") + 1] == "high"
    assert command[command.index("--system-prompt") + 1] == "be careful"
    assert command[command.index("--json-schema") + 1] == '{"type":"object"}'
    # Reviewing reads text we already fetched; it must not get tools or a browser.
    assert command[command.index("--allowedTools") + 1] == ""
    assert json.loads(command[command.index("--mcp-config") + 1]) == {"mcpServers": {}}


@pytest.mark.parametrize(
    "stdout",
    [
        json.dumps({"structured_output": PAYLOAD}),
        json.dumps({"result": PAYLOAD}),
        json.dumps({"result": json.dumps(PAYLOAD)}),
        json.dumps({"result": f"```json\n{json.dumps(PAYLOAD)}\n```"}),
        json.dumps(PAYLOAD),
    ],
)
def test_cli_reads_every_shape_claude_code_returns(cli, monkeypatch, stdout) -> None:
    monkeypatch.setattr(
        client_module.subprocess, "run", lambda *_a, **_k: completed(stdout)
    )

    assert cli.decide(system="s", prompt="p", schema={}) == PAYLOAD


def test_cli_failure_is_raised_rather_than_returned_as_a_decision(cli, monkeypatch) -> None:
    monkeypatch.setattr(
        client_module.subprocess,
        "run",
        lambda *_a, **_k: completed("boom", returncode=1),
    )

    with pytest.raises(RuntimeError, match="status 1"):
        cli.decide(system="s", prompt="p", schema={})


def test_cli_reported_error_is_raised(cli, monkeypatch) -> None:
    monkeypatch.setattr(
        client_module.subprocess,
        "run",
        lambda *_a, **_k: completed(json.dumps({"is_error": True, "result": "rate limited"})),
    )

    with pytest.raises(RuntimeError, match="rate limited"):
        cli.decide(system="s", prompt="p", schema={})


def test_cli_timeout_is_reported_clearly(cli, monkeypatch) -> None:
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired([], 600)

    monkeypatch.setattr(client_module.subprocess, "run", timeout)

    with pytest.raises(RuntimeError, match="timed out"):
        cli.decide(system="s", prompt="p", schema={})


def test_the_api_is_preferred_when_a_key_is_configured(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(client_module.shutil, "which", lambda _name: "/usr/bin/claude")

    assert get_review_client().name == "anthropic-api"


def test_a_connected_claude_account_is_enough_without_an_api_key(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(client_module.shutil, "which", lambda _name: "/usr/bin/claude")

    assert get_review_client().name == "claude-code"


def test_no_credentials_explains_both_options(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(client_module.shutil, "which", lambda _name: None)

    with pytest.raises(ReviewUnavailable) as error:
        get_review_client()

    assert "Connect a Claude account" in str(error.value)
    assert "ANTHROPIC_API_KEY" in str(error.value)
