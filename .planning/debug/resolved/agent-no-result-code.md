---
status: resolved
trigger: "Tesla application failed with: agent returned no result code"
created: 2026-07-25
updated: 2026-07-25
---

## Symptoms

- expected: Clicking Apply starts the browser worker and produces an applied or review-ready result.
- actual: The job prepares, enters applying on worker-0, then fails.
- error: `agent returned no result code`
- timeline: First reported immediately after a manual application attempt.
- reproduction: Open Latest jobs, select the Tesla Factory Software internship, and click Apply.

## Current Focus

- hypothesis: Confirmed. Claude completed without the required textual result before navigating, and TI-AAA discarded the stream metadata needed to explain why.
- test: Schema-validated result output and safe stream-diagnostic parsing were exercised through unit tests and a synthetic browser session.
- expecting: Every normal completion maps to a typed status; CLI/API/MCP failures produce an actionable non-sensitive error instead of `no result code`.
- next_action: Complete. The user can explicitly retry the failed job from Latest jobs.
- reasoning_checkpoint: The original model-level reason cannot be recovered because raw transcripts were intentionally disabled, but the persisted screenshot and browser history prove the worker never navigated.
- tdd_checkpoint: Regression tests cover structured results, legacy results, missing results, stream errors, permission denials, schema command construction, and diagnostic redaction.

## Evidence

- timestamp: 2026-07-25T17:14:12-07:00
  observation: Job 223 was claimed and failed 4.07 seconds later with process-level fallback `agent returned no result code`.
- timestamp: 2026-07-25T17:14:12-07:00
  observation: The saved worker screenshot was blank and worker-0 Chrome history contained zero URLs, proving no navigation occurred.
- timestamp: 2026-07-25T17:18:00-07:00
  observation: Claude Code 2.1.215 subscription authentication is valid; a benign stream-json prompt returned the requested result.
- timestamp: 2026-07-25T17:19:00-07:00
  observation: The exact restricted Playwright MCP command connected successfully and used browser_snapshot on a disposable blank profile.
- timestamp: 2026-07-25T17:22:00-07:00
  observation: Full synthetic TI-AAA prompts navigated both localhost and example.com and returned typed failure reasons.
- timestamp: 2026-07-25T18:00:00-07:00
  observation: The rebuilt Docker service reported healthy and `/api/health` returned version 0.3.1.

## Eliminated

- hypothesis: Resume preparation failed.
  reason: The tailored resume exists and the job reached applying.
- hypothesis: Chrome or CDP failed to start.
  reason: CDP responded normally and preview capture ran.
- hypothesis: Claude authentication expired or requires an API key.
  reason: Subscription authentication is valid and diagnostic prompts completed.
- hypothesis: Playwright MCP is missing or denied globally.
  reason: The exact production command connected to MCP and executed an allowed browser tool.
- hypothesis: External HTTPS navigation is blocked by Docker.
  reason: The synthetic full prompt navigated example.com successfully.

## Resolution

- root_cause: The original Claude run ended without the required textual `RESULT:` line and before any browser navigation. The runner then reduced the outcome to the generic `agent returned no result code` message and retained no safe stream metadata, so the exact model-level reason was unrecoverable.
- fix: Require a schema-validated final status and detail, retain legacy textual-result compatibility, translate stream/process failures into actionable messages, write private sanitized diagnostics without prompts or candidate data, and require navigation as the browser agent's first action.
- verification: Ruff passed, all 52 tests passed, distribution packages built, a synthetic end-to-end browser run returned a typed result, and the Docker service is healthy on version 0.3.1.
- files_changed:
  - `src/tiaaa/apply/runner.py`
  - `src/tiaaa/apply/prompt.py`
  - `tests/test_apply.py`
  - `src/tiaaa/__init__.py`
  - `pyproject.toml`
  - `README.md`
  - `CHANGELOG.md`
  - `RELEASING.md`
