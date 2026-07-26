# Releasing TI-AAA

## One-time setup

1. Create the public GitHub repository and add it as this repository's `origin`.
2. Review the package name. `ti-aaa` returned no PyPI project on 2026-07-18, but availability can change.
3. Add the final repository URLs to `[project.urls]` in `pyproject.toml`.
4. Create a PyPI project/trusted-publisher mapping for:
   - GitHub owner and repository
   - workflow `.github/workflows/publish.yml`
   - environment `pypi`
5. Protect the `pypi` GitHub environment if release approval is desired.

## Release checklist

```bash
ruff check src tests
pytest --cov=tiaaa --cov-report=term-missing
python -m build
python -m twine check dist/*
```

Then:

1. Update `CHANGELOG.md`.
2. Set the same version in `pyproject.toml` and `src/tiaaa/__init__.py`.
3. Commit the release.
4. Create and push an annotated tag matching the release, such as `v0.3.1`.
5. Verify the Publish workflow and install the artifact in a clean environment.

The publish workflow uses PyPI trusted publishing and stores no long-lived PyPI token.
