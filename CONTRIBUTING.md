# Contributing to TI-AAA

Thank you for helping students find and track technology internships more effectively.

## Project boundary

TI-AAA discovery is intentionally limited to the three repositories documented in the README. Contributions must not add LinkedIn, Indeed, Glassdoor, ZipRecruiter, Google Jobs, general Workday catalog, or other job-board scrapers.

A new file path within one of the configured repositories is in scope. Adding a different repository should begin with an issue explaining its maintenance quality, schema, geographic/role scope, and duplicate behavior.

## Development setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
ruff check .
pytest --cov=tiaaa --cov-report=term-missing --cov-fail-under=65
pip-audit
python -m build
python -m twine check dist/*
```

The test suite must run without network access. Live-source checks may be performed manually, but CI fixtures should be small representative excerpts rather than copies of upstream lists.

## Pull requests

1. Open an issue first for behavior changes or new source documents.
2. Keep changes focused and include tests.
3. Preserve first-import safety, explicit manual-apply intent, and direct-URL deduplication.
4. Never weaken the fact-only application rules or silently enable final submission.
5. Do not commit profiles, resumes, API keys, browser data, databases, or agent logs.
6. Run `ruff check src tests`, `pytest`, and `python -m build`.
7. Update the README and CHANGELOG when user-facing behavior changes.

## Source parser changes

Parser contributions should cover:

- header aliases
- direct Apply-link selection
- inherited-company rows (`↳`)
- closed, sponsorship, and citizenship markers
- dates/ages
- tracking-parameter removal without deleting job identifiers
- duplicate rows across sources

If a document fetch fails, its previous rows must remain active. Rows may expire only after a successful reconciliation proves they disappeared from every active source document.

## Application automation changes

The browser agent must:

- use only profile/resume facts
- stop on unknown required answers
- stop agent actions on CAPTCHA and retain the exact browser for candidate control; never automate, solve, or bypass the challenge
- create a required ordinary employer email/password account with TI-AAA's generated per-portal credential; never use social SSO, a shared password, or a user-supplied password
- stop on social SSO, non-code MFA, or verification that cannot be completed with a candidate-supplied one-time code
- keep the live browser session open while requesting a one-time code and clear the code after use
- avoid sensitive financial, identity, biometric, and device-permission flows
- complete and audit every form page before a separate final-submission turn; never use the final Submit control to trigger validation or discover fields
- verify visible confirmation before recording an application
- require an explicit manual Apply action followed by dashboard confirmation or enabled `automation.manual_auto_submit`, or an enabled `automation.auto_apply_new` unattended mode
- keep terminal final submission behind both `automation.allow_submission` and `--submit`
- preserve agent-discovered hard qualification conflicts so Auto mode cannot reclaim the job

## Licensing

By contributing, you agree that your contribution is licensed under AGPL-3.0-only. Do not submit code whose license is incompatible with the AGPL.
