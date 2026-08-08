# Releasing TI-AAA

## One-time setup

1. Confirm that the `ti-aaa` package name is still available on PyPI.
2. Create a PyPI project or pending trusted-publisher mapping for:
   - GitHub owner `Cole-Godfrey` and repository `ti-aaa`
   - workflow filename `publish.yml` (stored in `.github/workflows/`)
   - environment `pypi`
3. Create the `pypi` GitHub environment. Add a required reviewer if release approval is desired.
4. Enable GitHub private vulnerability reporting so `SECURITY.md`'s reporting path is available.
5. Protect `main` and require the CI matrix before merging.

## Release checklist

```bash
ruff check src tests
pytest --cov=tiaaa --cov-report=term-missing --cov-fail-under=65
pip-audit
python -m build
python -m twine check dist/*
```

Then:

1. Update `CHANGELOG.md`.
2. Set the same version in `pyproject.toml` and `src/tiaaa/__init__.py`.
3. Commit the release.
4. Create and push an annotated tag matching the release, such as `vX.Y.Z`.
5. Verify the Publish workflow and install the artifact in a clean environment.

The publish workflow repeats linting, tests, dependency auditing, tag/version validation, building,
and package validation before it uses PyPI trusted publishing. It stores no long-lived PyPI token.
