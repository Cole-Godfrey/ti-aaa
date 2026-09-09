"""Claude access for the apply/skip reviewer.

Two paths, in preference order:

1. `ANTHROPIC_API_KEY` with the official SDK. Parallel, cacheable, and the fastest
   way to review a full repository inbox.
2. The Claude Code CLI the dashboard already logs in for. No API key required, so a
   Claude Pro/Max subscription is enough to run the reviewer.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Protocol

from tiaaa.codex import codex_command, codex_events, codex_final, codex_usage_limited, run_codex

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5"
_CLI_MODEL_ALIASES = {
    "claude-opus-5": "opus",
    "claude-opus-4-8": "opus",
    "claude-sonnet-5": "sonnet",
    "claude-haiku-4-5": "haiku",
}
_MAX_OUTPUT_TOKENS = 16000


class ReviewUnavailable(RuntimeError):
    """No configured way to reach Claude for reviewing."""


class ReviewClient(Protocol):
    """Return one JSON object matching `schema` for the given prompt."""

    name: str

    def decide(self, *, system: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]: ...

    def close(self) -> None: ...


def _json_object(value: str) -> dict[str, Any]:
    value = value.strip()
    value = re.sub(r"^```(?:json)?\s*", "", value)
    value = re.sub(r"\s*```$", "", value)
    start, end = value.find("{"), value.rfind("}")
    if start < 0 or end < start:
        raise ValueError("Claude did not return a JSON object")
    parsed = json.loads(value[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("Claude returned JSON that was not an object")
    return parsed


class AnthropicReviewClient:
    """Structured-output reviewing through the Anthropic API."""

    name = "anthropic-api"

    def __init__(self, model: str = DEFAULT_MODEL, *, effort: str = "high") -> None:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise ReviewUnavailable("The anthropic package is not installed") from exc
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise ReviewUnavailable("ANTHROPIC_API_KEY is not set")
        self.model = model
        self.effort = effort
        self._client = anthropic.Anthropic()

    def decide(self, *, system: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        # The system prompt is identical for every company in a run, so caching it
        # keeps a full-inbox review cheap.
        with self._client.messages.stream(
            model=self.model,
            max_tokens=_MAX_OUTPUT_TOKENS,
            thinking={"type": "adaptive"},
            output_config={
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": schema},
            },
            system=[
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            message = stream.get_final_message()
        if message.stop_reason == "refusal":
            raise RuntimeError("Claude declined to review this company")
        text = next(
            (block.text for block in message.content if getattr(block, "type", "") == "text"),
            "",
        )
        return _json_object(text)

    def close(self) -> None:
        self._client.close()


class ClaudeCodeReviewClient:
    """Structured-output reviewing through the logged-in Claude Code CLI."""

    name = "claude-code"

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        *,
        effort: str = "high",
        timeout: int = 600,
        cwd: str | None = None,
    ) -> None:
        executable = shutil.which("claude")
        if executable is None:
            raise ReviewUnavailable("Claude Code CLI is not installed")
        self.executable = executable
        self.model = _CLI_MODEL_ALIASES.get(model, model)
        self.effort = effort
        self.timeout = timeout
        self.cwd = cwd
        self.environment = os.environ.copy()
        self.environment.pop("TIAAA_EMAIL_APP_PASSWORD", None)
        self.environment.pop("CLAUDECODE", None)
        self.environment.pop("CLAUDE_CODE_ENTRYPOINT", None)

    def _command(self, system: str, schema: dict[str, Any]) -> list[str]:
        return [
            self.executable,
            "--model",
            self.model,
            "--effort",
            self.effort,
            "--system-prompt",
            system,
            "-p",
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--mcp-config",
            json.dumps({"mcpServers": {}}),
            "--allowedTools",
            "",
            "--permission-mode",
            "dontAsk",
            "--no-session-persistence",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema, separators=(",", ":")),
        ]

    def decide(self, *, system: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if platform.system() == "Windows":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            completed = subprocess.run(
                self._command(system, schema),
                input=prompt,
                cwd=self.cwd,
                env=self.environment,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=self.timeout,
                check=False,
                **kwargs,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Claude Code review timed out after {self.timeout}s") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()[-300:]
            raise RuntimeError(f"Claude Code exited with status {completed.returncode}: {detail}")
        envelope = _json_object(completed.stdout)
        if envelope.get("is_error") or str(envelope.get("api_error_status")) == "429":
            raise RuntimeError(str(envelope.get("result") or "Claude Code reported an error")[:300])
        if isinstance(envelope.get("structured_output"), dict):
            return dict(envelope["structured_output"])
        result = envelope.get("result")
        if isinstance(result, dict):
            return result
        if isinstance(result, str):
            return _json_object(result)
        # A bare schema-shaped object is also acceptable.
        if "decisions" in envelope:
            return envelope
        raise ValueError("Claude Code returned no structured result")

    def close(self) -> None:
        return None


class CodexReviewClient:
    """Codex first, with a lazy Claude fallback only for provider usage limits."""

    name = "codex"

    def __init__(
        self, *, model: str = "", claude_model: str = DEFAULT_MODEL, fallback: bool = True, timeout: int = 600
    ) -> None:
        if not shutil.which("codex"):
            raise ReviewUnavailable("Install Codex CLI and run codex login before reviewing")
        self.model = model
        self.claude_model = claude_model
        self.fallback = fallback
        self.timeout = timeout
        self.retry_at = 0.0
        self.backup: ReviewClient | None = None

    def decide(self, *, system: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        if time.monotonic() >= self.retry_at:
            with tempfile.TemporaryDirectory(prefix="tiaaa-review-") as directory:
                root = Path(directory)
                schema_path = root / "schema.json"
                schema_path.write_text(json.dumps(schema), encoding="utf-8")
                output, code = run_codex(
                    codex_command(schema_path=schema_path, model=self.model),
                    system + "\n\n" + prompt,
                    cwd=root,
                    timeout=self.timeout,
                )
            if codex_usage_limited(output) and self.fallback:
                self.retry_at = time.monotonic() + 900
            elif code or any(event.get("type") == "turn.failed" for event in codex_events(output)):
                raise RuntimeError("Codex review failed; check Codex login and usage")
            else:
                return _json_object(codex_final(output))
        if self.backup is None:
            self.backup = _get_claude_review_client(model=self.claude_model, timeout=self.timeout)
        return self.backup.decide(system=system, prompt=prompt, schema=schema)

    def close(self) -> None:
        if self.backup is not None:
            self.backup.close()


def get_review_client(
    *,
    model: str = DEFAULT_MODEL,
    effort: str = "high",
    timeout: int = 600,
    cwd: str | None = None,
    provider: str = "codex",
    codex_model: str = "",
    claude_fallback: bool = True,
) -> ReviewClient:
    if provider == "codex":
        return CodexReviewClient(
            model=codex_model, claude_model=model, fallback=claude_fallback, timeout=timeout
        )
    if provider != "claude":
        raise ReviewUnavailable("Review provider must be codex or claude")
    return _get_claude_review_client(model=model, effort=effort, timeout=timeout, cwd=cwd)


def _get_claude_review_client(
    *,
    model: str = DEFAULT_MODEL,
    effort: str = "high",
    timeout: int = 600,
    cwd: str | None = None,
) -> ReviewClient:
    """Prefer the API when a key exists, otherwise use the connected Claude account."""

    errors: list[str] = []
    for build in (
        lambda: AnthropicReviewClient(model, effort=effort),
        lambda: ClaudeCodeReviewClient(model, effort=effort, timeout=timeout, cwd=cwd),
    ):
        try:
            return build()
        except ReviewUnavailable as exc:
            errors.append(str(exc))
    raise ReviewUnavailable(
        "No way to reach Claude for reviewing. Connect a Claude account in Settings, "
        f"or set ANTHROPIC_API_KEY. ({'; '.join(errors)})"
    )
