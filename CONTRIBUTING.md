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
pytest
python -m build
```

The test suite must run without network access. Live-source checks may be performed manually, but CI fixtures should be small representative excerpts rather than copies of upstream lists.

## Pull requests

1. Open an issue first for behavior changes or new source documents.
2. Keep changes focused and include tests.
3. Preserve first-sync baseline protection and direct-URL deduplication.
4. Never weaken the fact-only application rules or silently enable final submission.
5. Do not commit profiles, resumes, API keys, browser data, databases, or agent logs.
6. Run `ruff check .`, `pytest`, and `python -m build`.
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
- stop on CAPTCHA, MFA, SSO, or email verification
- avoid sensitive financial, identity, biometric, and device-permission flows
- verify visible confirmation before recording an application
- require both the setting and CLI flag before final submission

## Licensing

By contributing, you agree that your contribution is licensed under AGPL-3.0-only. Do not submit code whose license is incompatible with the AGPL.
