"""Command-line interface for TI-AAA."""

from __future__ import annotations

import logging
import shutil
import threading
import webbrowser
from pathlib import Path
from typing import Annotated

import typer
import uvicorn
from rich.console import Console
from rich.table import Table

from tiaaa import __version__
from tiaaa.config import (
    SOURCE_DOCUMENTS,
    AppPaths,
    ensure_dirs,
    get_chrome_path,
    get_paths,
    initialize_user_files,
    load_environment,
    load_profile,
    load_settings,
)
from tiaaa.dashboard.app import create_app
from tiaaa.database import get_connection, get_stats, init_db, list_jobs, source_status, update_tracker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
console = Console()
app = typer.Typer(
    name="tiaaa",
    help="Tech Internship Autonomous Application Agent — GitHub-only discovery, preparation, and tracking.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold]tiaaa[/bold] {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show the installed version.",
    ),
) -> None:
    """Poll community internship repos, prepare applications, and track outcomes."""


def _bootstrap() -> tuple[AppPaths, dict, dict]:
    paths = ensure_dirs(get_paths())
    load_environment(paths)
    init_db(paths.database)
    return paths, load_profile(paths), load_settings(paths)


def _copy_if_requested(source: Path | None, destination: Path, force: bool) -> bool:
    if source is None:
        return False
    source = source.expanduser().resolve()
    if not source.is_file():
        raise typer.BadParameter(f"File does not exist: {source}")
    if destination.exists() and not force:
        raise typer.BadParameter(f"Destination exists: {destination}. Pass --force to replace it.")
    shutil.copyfile(source, destination)
    return True


@app.command("init")
def init_command(
    profile: Annotated[Path | None, typer.Option(help="Existing TI-AAA profile JSON to import.")] = None,
    settings_file: Annotated[Path | None, typer.Option("--settings", help="Settings YAML to import.")] = None,
    resume_txt: Annotated[Path | None, typer.Option(help="Plain-text resume fact source.")] = None,
    resume_pdf: Annotated[Path | None, typer.Option(help="Recruiter-ready resume PDF.")] = None,
    force: Annotated[bool, typer.Option(help="Replace existing configuration files.")] = False,
) -> None:
    """Create ~/.tiaaa and install editable profile/settings templates."""

    paths = ensure_dirs(get_paths())
    existed = {
        paths.profile: paths.profile.exists(),
        paths.settings: paths.settings.exists(),
        paths.resume_text: paths.resume_text.exists(),
        paths.resume_pdf: paths.resume_pdf.exists(),
    }
    created = initialize_user_files(paths, force=force)
    imported: list[Path] = []
    if _copy_if_requested(profile, paths.profile, force or not existed[paths.profile]):
        imported.append(paths.profile)
    if _copy_if_requested(settings_file, paths.settings, force or not existed[paths.settings]):
        imported.append(paths.settings)
    if _copy_if_requested(resume_txt, paths.resume_text, force or not existed[paths.resume_text]):
        imported.append(paths.resume_text)
    if _copy_if_requested(resume_pdf, paths.resume_pdf, force or not existed[paths.resume_pdf]):
        imported.append(paths.resume_pdf)
    init_db(paths.database)

    console.print("\n[bold green]TI-AAA workspace is ready.[/bold green]")
    console.print(f"  Data directory: [cyan]{paths.root}[/cyan]")
    for item in dict.fromkeys(created + imported):
        console.print(f"  Wrote: {item}")
    console.print("\n[bold]Before the first sync:[/bold]")
    console.print(f"  1. Edit [cyan]{paths.profile}[/cyan]")
    console.print(f"  2. Edit [cyan]{paths.settings}[/cyan]")
    console.print(f"  3. Put resume.txt and resume.pdf in [cyan]{paths.root}[/cyan]")
    console.print("  4. Run [bold]tiaaa doctor[/bold], then [bold]tiaaa sync[/bold]")
    console.print("\n[dim]The first sync establishes a baseline and does not queue old listings.[/dim]")


def _run_sync(
    *,
    paths: AppPaths,
    profile: dict,
    settings: dict,
    include_existing: bool,
    force: bool,
    source: str | None,
) -> list:
    from tiaaa.discovery.github import sync_repositories

    include_existing = include_existing or settings.get("initial_sync") == "include_existing"
    results = sync_repositories(
        profile=profile,
        settings=settings,
        include_existing=include_existing,
        force=force,
        source_key=source,
        db_path=str(paths.database),
    )
    table = Table(title="GitHub internship sync", header_style="bold green")
    table.add_column("Source")
    table.add_column("State")
    table.add_column("Parsed", justify="right")
    table.add_column("New", justify="right")
    table.add_column("Queued", justify="right")
    table.add_column("Note")
    for result in results:
        if result.status == "error":
            state = "[red]error[/red]"
            note = result.error or "unknown error"
        elif result.status == "unchanged":
            state = "[dim]unchanged[/dim]"
            note = "conditional request/cache hit"
        else:
            state = "[green]synced[/green]"
            note = "baseline only" if result.baseline else f"{result.expired} expired"
        table.add_row(
            result.label,
            state,
            str(result.parsed),
            str(result.new),
            str(result.queued),
            note,
        )
    console.print(table)
    return results


@app.command()
def sync(
    include_existing: Annotated[
        bool,
        typer.Option(help="Queue eligible listings already present during the first sync."),
    ] = False,
    force: Annotated[bool, typer.Option(help="Ignore ETags and reparse every source document.")] = False,
    source: Annotated[
        str | None,
        typer.Option(help="Limit to one source key; use `tiaaa sources` to list keys."),
    ] = None,
) -> None:
    """Poll only the configured GitHub repositories and reconcile their listings."""

    paths, profile, settings = _bootstrap()
    valid_keys = {document.key for document in SOURCE_DOCUMENTS}
    if source and source not in valid_keys:
        raise typer.BadParameter(f"Unknown source {source!r}. Choose from: {', '.join(sorted(valid_keys))}")
    results = _run_sync(
        paths=paths,
        profile=profile,
        settings=settings,
        include_existing=include_existing,
        force=force,
        source=source,
    )
    if results and all(result.status == "error" for result in results):
        raise typer.Exit(code=1)


@app.command()
def score(
    llm: Annotated[
        bool,
        typer.Option("--llm/--heuristic", help="Refine queued fit scores with an LLM."),
    ] = False,
    limit: Annotated[int, typer.Option(min=0, help="Maximum queued jobs to score; 0 means all.")] = 0,
) -> None:
    """Inspect transparent heuristic scores or refine them with the configured LLM."""

    paths, _, _ = _bootstrap()
    connection = get_connection(paths.database)
    if llm:
        from tiaaa.preparation import score_jobs_with_llm

        result = score_jobs_with_llm(paths=paths, limit=limit, db_path=paths.database)
        console.print(
            f"[green]Scored {result['scored']} queued internship(s).[/green] "
            f"Errors: {result['errors']}"
        )
        return
    rows = connection.execute(
        """
        SELECT company, role, fit_score, score_reasoning FROM jobs
        WHERE pipeline_status = 'queued' ORDER BY fit_score DESC, first_seen_at DESC
        """
    ).fetchall()
    if limit > 0:
        rows = rows[:limit]
    table = Table(title="Queued internship fit scores", header_style="bold cyan")
    table.add_column("Score", justify="right")
    table.add_column("Company")
    table.add_column("Role")
    table.add_column("Reason")
    for row in rows:
        table.add_row(str(row["fit_score"] or "—"), row["company"], row["role"], row["score_reasoning"] or "")
    console.print(table)


@app.command()
def prepare(
    limit: Annotated[int, typer.Option(min=0, help="Maximum application packets; 0 means all.")] = 0,
) -> None:
    """Attach the resume and optionally generate factual cover letters."""

    paths, profile, settings = _bootstrap()
    from tiaaa.preparation import prepare_jobs

    result = prepare_jobs(
        paths=paths,
        profile=profile,
        settings=settings,
        limit=limit,
        db_path=paths.database,
    )
    console.print(f"[green]Prepared {result['prepared']} application(s).[/green] Errors: {result['errors']}")


def _run_apply(
    *,
    paths: AppPaths,
    profile: dict,
    settings: dict,
    limit: int | None,
    workers: int,
    submit: bool,
    job_id: int | None,
) -> dict[str, int]:
    from tiaaa.apply import run_applications

    mode = "SUBMIT" if submit else "REVIEW ONLY"
    console.print(f"\n[bold]Launching browser workers[/bold] · {mode} · {workers} worker(s)")
    result = run_applications(
        profile=profile,
        settings=settings,
        paths=paths,
        limit=limit,
        workers=workers,
        submit=submit,
        target_job_id=job_id,
        db_path=paths.database,
    )
    console.print(
        f"[bold]Batch complete:[/bold] {result['applied']} applied, {result['review']} review, "
        f"{result['expired']} expired, {result['failed']} failed"
    )
    return result


@app.command()
def apply(
    limit: Annotated[
        int | None,
        typer.Option("--limit", "-l", min=1, help="Maximum jobs in this batch."),
    ] = None,
    workers: Annotated[int, typer.Option("--workers", "-w", min=1, max=8)] = 1,
    submit: Annotated[
        bool,
        typer.Option(help="Allow the browser agent to click the final Submit button."),
    ] = False,
    job_id: Annotated[int | None, typer.Option(help="Target one prepared job ID.")] = None,
) -> None:
    """Fill prepared applications; final submission requires explicit two-key opt-in."""

    paths, profile, settings = _bootstrap()
    _run_apply(
        paths=paths,
        profile=profile,
        settings=settings,
        limit=limit,
        workers=workers,
        submit=submit,
        job_id=job_id,
    )


@app.command()
def run(
    include_existing: Annotated[bool, typer.Option(help="Queue the initial baseline too.")] = False,
    llm_score: Annotated[bool, typer.Option(help="Refine new jobs with the configured LLM.")] = False,
    apply_now: Annotated[
        bool,
        typer.Option("--apply", help="Start browser workers after preparation."),
    ] = False,
    submit: Annotated[
        bool,
        typer.Option(help="Permit final submission (also requires settings opt-in)."),
    ] = False,
    workers: Annotated[int, typer.Option("--workers", "-w", min=1, max=8)] = 1,
) -> None:
    """Run one full sync → score → prepare → optional apply cycle."""

    if submit and not apply_now:
        raise typer.BadParameter("--submit requires --apply")
    paths, profile, settings = _bootstrap()
    _run_sync(
        paths=paths,
        profile=profile,
        settings=settings,
        include_existing=include_existing,
        force=False,
        source=None,
    )
    if llm_score or settings.get("preparation", {}).get("use_llm"):
        from tiaaa.preparation import score_jobs_with_llm

        score_jobs_with_llm(paths=paths, db_path=paths.database)
    from tiaaa.preparation import prepare_jobs

    prepared = prepare_jobs(paths=paths, profile=profile, settings=settings, db_path=paths.database)
    console.print(f"[green]Prepared {prepared['prepared']} new application(s).[/green]")
    if apply_now:
        _run_apply(
            paths=paths,
            profile=profile,
            settings=settings,
            limit=None,
            workers=workers,
            submit=submit,
            job_id=None,
        )


@app.command()
def watch(
    apply_new: Annotated[
        bool,
        typer.Option("--apply", help="Run browser workers for each new batch."),
    ] = False,
    submit: Annotated[
        bool,
        typer.Option(help="Permit final submission (also requires settings opt-in)."),
    ] = False,
    once: Annotated[bool, typer.Option(help="Run one cycle and exit; useful for schedulers.")] = False,
    interval: Annotated[
        int | None,
        typer.Option(min=30, help="Override polling interval in seconds."),
    ] = None,
    workers: Annotated[int, typer.Option("--workers", "-w", min=1, max=8)] = 1,
) -> None:
    """Continuously poll for newly added internships and process only the new queue."""

    if submit and not apply_new:
        raise typer.BadParameter("--submit requires --apply")
    paths, profile, settings = _bootstrap()
    poll_seconds = max(30, interval or int(settings.get("poll_interval_seconds", 300)))
    console.print(
        f"[bold green]Watching three GitHub repositories[/bold green] every {poll_seconds}s. "
        "Press Ctrl+C to stop."
    )
    stop = threading.Event()
    try:
        while not stop.is_set():
            _run_sync(
                paths=paths,
                profile=profile,
                settings=settings,
                include_existing=False,
                force=False,
                source=None,
            )
            if settings.get("preparation", {}).get("use_llm"):
                from tiaaa.preparation import score_jobs_with_llm

                score_jobs_with_llm(paths=paths, db_path=paths.database)
            from tiaaa.preparation import prepare_jobs

            result = prepare_jobs(paths=paths, profile=profile, settings=settings, db_path=paths.database)
            if result["prepared"]:
                console.print(f"[green]{result['prepared']} newly listed internship(s) prepared.[/green]")
            if apply_new:
                _run_apply(
                    paths=paths,
                    profile=profile,
                    settings=settings,
                    limit=None,
                    workers=workers,
                    submit=submit,
                    job_id=None,
                )
            if once:
                break
            stop.wait(poll_seconds)
    except KeyboardInterrupt:
        console.print("\n[yellow]Watcher stopped.[/yellow]")


@app.command()
def status() -> None:
    """Show application counts and OA/interview conversion rates."""

    paths, _, _ = _bootstrap()
    stats = get_stats(get_connection(paths.database))
    table = Table(title="TI-AAA status", header_style="bold green")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Internships discovered", str(stats["total_discovered"]))
    table.add_row("Active listings", str(stats["active"]))
    table.add_row("Eligible", str(stats["eligible"]))
    table.add_row("Queued", str(stats["queued"]))
    table.add_row("Ready to apply", str(stats["ready"]))
    table.add_row("Applications", str(stats["applications"]))
    table.add_row("Online assessments", f"{stats['oas']} ({stats['oa_rate']}%)")
    table.add_row("Interviews", f"{stats['interviews']} ({stats['interview_rate']}%)")
    table.add_row("Offers", str(stats["offers"]))
    console.print(table)


@app.command("jobs")
def jobs_command(
    status_filter: Annotated[str | None, typer.Option("--status", help="Pipeline or outcome status.")] = None,
    search: Annotated[str | None, typer.Option(help="Search company, role, or location.")] = None,
    limit: Annotated[int, typer.Option(min=1, max=500)] = 50,
) -> None:
    """List tracker rows in the terminal."""

    paths, _, _ = _bootstrap()
    rows = list_jobs(
        get_connection(paths.database), status=status_filter, search=search, limit=limit
    )
    table = Table(title="Internship tracker", header_style="bold cyan")
    table.add_column("ID", justify="right")
    table.add_column("Company")
    table.add_column("Role")
    table.add_column("Location")
    table.add_column("Pipeline")
    table.add_column("Outcome")
    for row in rows:
        table.add_row(
            str(row["id"]),
            row["company"],
            row["role"],
            row["location"],
            row["pipeline_status"],
            row["outcome_status"],
        )
    console.print(table)


@app.command()
def mark(
    job_id: Annotated[int, typer.Argument(help="Tracker job ID.")],
    pipeline: Annotated[str | None, typer.Option(help="New pipeline status.")] = None,
    outcome: Annotated[
        str | None,
        typer.Option(help="New outcome: oa/interview/offer/rejected/withdrawn."),
    ] = None,
    note: Annotated[str | None, typer.Option(help="Replace tracker notes.")] = None,
) -> None:
    """Update a tracker row from the CLI."""

    paths, _, _ = _bootstrap()
    try:
        row = update_tracker(
            get_connection(paths.database),
            job_id,
            pipeline_status=pipeline,
            outcome_status=outcome,
            notes=note,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if row is None:
        console.print(f"[red]Job ID {job_id} was not found.[/red]")
        raise typer.Exit(code=1)
    console.print(f"[green]Updated[/green] {row['company']} · {row['role']}")


@app.command()
def sources() -> None:
    """List the fixed GitHub discovery sources and their health."""

    paths = ensure_dirs(get_paths())
    init_db(paths.database)
    states = {row["document_key"]: row for row in source_status(get_connection(paths.database))}
    table = Table(title="Configured GitHub-only sources", header_style="bold magenta")
    table.add_column("Key")
    table.add_column("Document")
    table.add_column("Repository")
    table.add_column("Last success")
    for document in SOURCE_DOCUMENTS:
        state = states.get(document.document_key, {})
        table.add_row(
            document.key,
            document.path,
            document.repo_url,
            state.get("last_success_at") or "never",
        )
    console.print(table)


@app.command()
def dashboard(
    host: Annotated[str | None, typer.Option(help="Bind host; defaults to settings.yaml.")] = None,
    port: Annotated[int | None, typer.Option(min=1, max=65535, help="Bind port.")] = None,
    open_browser: Annotated[
        bool,
        typer.Option("--open/--no-open", help="Open the dashboard in a browser."),
    ] = True,
) -> None:
    """Serve the local web application tracker and analytics dashboard."""

    paths, _, settings = _bootstrap()
    dashboard_settings = settings.get("dashboard", {})
    bind_host = host or str(dashboard_settings.get("host", "127.0.0.1"))
    bind_port = port or int(dashboard_settings.get("port", 8787))
    if bind_host not in {"127.0.0.1", "localhost", "::1"}:
        console.print("[yellow]Warning:[/yellow] the dashboard has no authentication; use a firewall.")
    url_host = "127.0.0.1" if bind_host in {"0.0.0.0", "::"} else bind_host
    url = f"http://{url_host}:{bind_port}"
    console.print(f"[green]Dashboard:[/green] {url}")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(paths.database), host=bind_host, port=bind_port, log_level="info")


@app.command()
def doctor() -> None:
    """Check profile, resumes, discovery inputs, and optional automation tools."""

    paths = ensure_dirs(get_paths())
    load_environment(paths)
    init_db(paths.database)
    checks: list[tuple[str, bool, str]] = []
    try:
        profile = load_profile(paths)
        placeholder = str(profile.get("personal", {}).get("full_name", "")).startswith("YOUR ")
        checks.append(
            (
                "profile.json",
                not placeholder,
                "replace template values" if placeholder else str(paths.profile),
            )
        )
    except Exception as exc:
        checks.append(("profile.json", False, str(exc)))
    checks.append(("resume.txt", paths.resume_text.is_file(), str(paths.resume_text)))
    checks.append(("resume.pdf", paths.resume_pdf.is_file(), str(paths.resume_pdf)))
    checks.append(("settings.yaml", paths.settings.is_file(), str(paths.settings)))
    checks.append(
        (
            "GitHub sources",
            len({doc.repo_url for doc in SOURCE_DOCUMENTS}) == 3,
            "3 repositories / 5 active documents",
        )
    )
    claude = shutil.which("claude")
    checks.append(("Claude Code", claude is not None, claude or "optional: needed for auto-apply"))
    npx = shutil.which("npx")
    checks.append(("Node.js / npx", npx is not None, npx or "optional: needed for Playwright MCP"))
    try:
        chrome = get_chrome_path()
        checks.append(("Chrome", True, chrome))
    except FileNotFoundError as exc:
        checks.append(("Chrome", False, f"optional: {exc}"))

    table = Table(title="TI-AAA doctor", header_style="bold cyan")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail")
    for label, ok, detail in checks:
        table.add_row(label, "[green]OK[/green]" if ok else "[yellow]MISSING[/yellow]", detail)
    console.print(table)
    required_ok = all(ok for _, ok, _ in checks[:5])
    if not required_ok:
        raise typer.Exit(code=1)
