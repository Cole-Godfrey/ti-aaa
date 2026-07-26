---
status: resolved
trigger: "errorChrome exited before opening its debug port (21)"
created: 2026-07-25
updated: 2026-07-25
---

## Symptoms

- expected: Clicking Retry starts the isolated Chrome worker, opens the application URL, and connects the browser agent.
- actual: Chrome exits before the remote-debugging port becomes available.
- error: `Chrome exited before opening its debug port (21)`
- timeline: Reported immediately after deploying v0.3.1 and retrying the failed Tesla application.
- reproduction: Open Latest jobs, select the failed Tesla application, and click Retry.

## Current Focus

- hypothesis: Confirmed. Docker recreation changed the container hostname while the persistent Chrome profile retained singleton locks owned by the previous hostname.
- test: Add conservative stale-lock recovery, then launch Chrome against a synthetic persisted profile whose lock hostname and socket are stale.
- expecting: TI-AAA removes only the stale singleton artifacts, Chrome opens CDP normally, and active/current-container locks remain protected.
- next_action: Complete. Use the listing's direct link for manual review because Tesla independently returned an HTTP 403 automation block.
- reasoning_checkpoint: The lock points to process 84 on container `7f4f26b72cb2`; the current container is `852dae6bf146`, and the referenced singleton socket no longer exists.
- tdd_checkpoint: Red test failed on the missing recovery helper; six focused Chrome lifecycle tests and the full 58-test suite now pass.

## Evidence

- timestamp: 2026-07-25T17:53:03-07:00
  observation: The background worker failed inside `launch_chrome` before claiming the queued application; the service stayed healthy but the worker exception escaped the cycle.
- timestamp: 2026-07-25T17:54:00-07:00
  observation: The persistent worker profile contained `SingletonLock -> 7f4f26b72cb2-84` and a `SingletonSocket` whose `/tmp` target no longer exists.
- timestamp: 2026-07-25T17:54:00-07:00
  observation: The active container hostname is `852dae6bf146`, proving the lock owner is a prior container.
- timestamp: 2026-07-25T17:55:06-07:00
  observation: An isolated five-second launch reproduced exit 21; Chromium explicitly reported the profile locked by process 84 on computer `7f4f26b72cb2`.
- timestamp: 2026-07-25T18:05:00-07:00
  observation: Recovery tests verify stale Docker locks are removed while current-process, live-socket, and foreign native-host locks are preserved; all 58 project tests and Ruff pass.
- timestamp: 2026-07-25T18:12:00-07:00
  observation: The v0.3.2 container recovered the original foreign-container lock and opened CDP port 9330 using the real persisted worker profile.
- timestamp: 2026-07-25T18:12:30-07:00
  observation: A second paused launch recovered the dead lock left by the current container and opened CDP port 9330 again.
- timestamp: 2026-07-25T18:14:00-07:00
  observation: After restoring the service, the real worker claimed job 223 and reached Tesla; it stopped safely when Tesla returned HTTP 403 Access Denied, with submission disabled.
## Eliminated

- hypothesis: Chromium itself is broken in the image.
  reason: Chromium reached profile validation and returned its specific singleton-lock exit code.
- hypothesis: The job was partially claimed or submitted before Chrome failed.
  reason: Job 223 remains ready with zero attempts and no worker assignment; Chrome failed before `claim_next_job`.
- hypothesis: Automatic application could submit during verification.
  reason: `automation.allow_submission` is currently false, so the existing manual request can only reach review-ready state.

## Resolution

- root_cause: Docker recreated the TI-AAA container with a new hostname, but the persistent Chromium profile retained `SingletonLock`, `SingletonCookie`, and `SingletonSocket` artifacts owned by the old container. Chromium treated the unreachable old owner as an active remote profile and exited with code 21 before CDP startup.
- fix: Recover an app-owned singleton lock when its socket is gone and either its local process is dead or it belongs to a prior managed Docker container. Preserve live locks and foreign native-host locks. Capture concise Chromium stderr so future startup failures include their actual reason.
- verification: Six focused lifecycle tests and all 58 project tests pass; Ruff passes; the v0.3.2 image built successfully; the persisted profile opened CDP twice after recovering foreign- and current-container dead locks; and the real Tesla attempt progressed beyond Chrome startup to Tesla's separate HTTP 403 protection page without submitting.
- files_changed:
  - `src/tiaaa/apply/chrome.py`
  - `tests/test_chrome.py`
  - `Dockerfile`
  - `src/tiaaa/__init__.py`
  - `pyproject.toml`
  - `README.md`
  - `CHANGELOG.md`
  - `RELEASING.md`
