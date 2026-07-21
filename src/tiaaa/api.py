"""Small public Python facade for embedding TI-AAA workflows."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tiaaa.config import (
    AppPaths,
    ensure_dirs,
    get_paths,
    initialize_user_files,
    load_environment,
    load_profile,
    load_settings,
    save_profile,
    save_settings,
    update_secrets,
)
from tiaaa.database import (
    get_connection,
    get_job,
    get_stats,
    init_db,
    list_jobs,
    list_resumes,
    request_manual_application,
)


class TIAAA:
    """Configure and run bounded TI-AAA operations from Python."""

    def __init__(self, home: str | Path | None = None) -> None:
        self.paths: AppPaths = ensure_dirs(get_paths(home))
        initialize_user_files(self.paths)
        load_environment(self.paths)
        init_db(self.paths.database)

    @property
    def profile(self) -> dict[str, Any]:
        return load_profile(self.paths)

    @property
    def settings(self) -> dict[str, Any]:
        return load_settings(self.paths)

    def configure(
        self,
        *,
        profile: dict[str, Any] | None = None,
        settings: dict[str, Any] | None = None,
        secrets: dict[str, str | None] | None = None,
    ) -> None:
        if profile is not None:
            save_profile(profile, self.paths)
        if settings is not None:
            save_settings(settings, self.paths)
        if secrets:
            update_secrets(secrets, paths=self.paths)

    def sync(self, *, force: bool = False) -> list[dict[str, Any]]:
        """Poll all fixed sources; first-import roles remain available for manual selection."""

        from tiaaa.discovery.github import sync_repositories

        results = sync_repositories(
            profile=self.profile,
            settings=self.settings,
            force=force,
            db_path=str(self.paths.database),
        )
        return [result.as_dict() for result in results]

    def prepare(self, *, limit: int = 0) -> dict[str, int]:
        from tiaaa.preparation import prepare_jobs

        return prepare_jobs(
            paths=self.paths,
            profile=self.profile,
            settings=self.settings,
            limit=limit,
            db_path=self.paths.database,
        )

    def request(self, job_id: int, *, prepare: bool = True) -> dict[str, Any]:
        """Queue one active catalog listing, optionally selecting its resume immediately."""

        connection = get_connection(self.paths.database)
        row = request_manual_application(connection, job_id)
        if row is None:
            raise LookupError(f"Job ID {job_id} was not found")
        if prepare:
            from tiaaa.preparation import prepare_jobs

            result = prepare_jobs(
                paths=self.paths,
                profile=self.profile,
                settings=self.settings,
                limit=1,
                target_job_id=job_id,
                db_path=self.paths.database,
            )
            if result["errors"]:
                raise RuntimeError(f"Could not prepare job ID {job_id}")
        return get_job(connection, job_id) or row

    def add_resume(
        self,
        pdf: str | Path,
        *,
        name: str,
        tags: list[str] | None = None,
        text: str = "",
        notes: str = "",
    ) -> dict[str, Any]:
        """Add a resume PDF to the same library used by the web app."""

        from tiaaa.resumes import store_resume

        pdf_path = Path(pdf).expanduser().resolve()
        if not pdf_path.is_file():
            raise FileNotFoundError(f"Resume PDF not found: {pdf_path}")
        return store_resume(
            paths=self.paths,
            name=name,
            original_filename=pdf_path.name,
            content=pdf_path.read_bytes(),
            text_override=text,
            tags=tags,
            notes=notes,
            db_path=self.paths.database,
        )

    def apply(
        self,
        *,
        limit: int | None = None,
        workers: int = 1,
        submit: bool = False,
        job_id: int | None = None,
    ) -> dict[str, int]:
        from tiaaa.apply import run_applications

        return run_applications(
            profile=self.profile,
            settings=self.settings,
            paths=self.paths,
            limit=limit,
            workers=workers,
            submit=submit,
            target_job_id=job_id,
            db_path=self.paths.database,
        )

    def stats(self) -> dict[str, Any]:
        return get_stats(get_connection(self.paths.database))

    def jobs(
        self,
        *,
        status: str | None = None,
        search: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return list_jobs(
            get_connection(self.paths.database),
            status=status,
            search=search,
            limit=limit,
        )

    def resumes(self) -> list[dict[str, Any]]:
        return list_resumes(get_connection(self.paths.database))
