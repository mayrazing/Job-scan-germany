from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import sys
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Never

import typer
import uvicorn
from pydantic import ValidationError

from job_scan import __version__
from job_scan.ai_config import AiConfigError, AiProviderStore
from job_scan.ai_runtime import AiRuntimeInvoker
from job_scan.ai_selection import (
    AiRuntimeSelection,
    AiSelectionError,
    AiSelectionStore,
    ai_selection_from_config,
    apply_ai_selection_to_claude,
    claude_runtime_selection_from_settings,
    resolve_ai_selection,
)
from job_scan.anthropic_api import AiModelDiscovery
from job_scan.ats_history import AtsHistoryStore
from job_scan.ats_service import AtsCheckService
from job_scan.ats_workflow import AtsWorkflow
from job_scan.claude_process import ClaudeProcessError
from job_scan.config import (
    AppConfig,
    ClaudeSettings,
    SchedulerSettings,
    load_config,
    load_config_bytes,
    save_config,
)
from job_scan.dashboard.render import render_dashboard
from job_scan.doctor import run_doctor
from job_scan.domain import Snapshot
from job_scan.locking import FileRWLock, LockUnavailable
from job_scan.mdns import MDNS_HOSTNAME, MdnsError, MdnsPublisher
from job_scan.paths import AppPaths
from job_scan.repository import JsonlRepository
from job_scan.resume import ResumeError
from job_scan.review_server import create_review_app
from job_scan.scan_service import (
    ScanAlreadyRunning,
    ScanError,
    ScanProgress,
    ScanProgressCallback,
    ScanProgressStage,
    ScanRunState,
    ScanService,
    ScanSummary,
    scan_progress_message,
    scan_progress_percent,
    write_scan_run_state,
)
from job_scan.scheduler import (
    SchedulerBackend,
    SchedulerError,
    SchedulerState,
    scheduler_for_platform,
)
from job_scan.search_history import SearchHistoryStore
from job_scan.setup_service import SetupAnswers, SetupError, SetupService
from job_scan.web_workflow import WebWorkflow, WebWorkflowBusy

app = typer.Typer(no_args_is_help=True, callback=lambda: None)
scheduler_app = typer.Typer(no_args_is_help=True)
app.add_typer(scheduler_app, name="scheduler")


@app.command()
def version() -> None:
    typer.echo(f"job-scan {__version__}")


@app.command()
def doctor(
    log: Annotated[
        bool,
        typer.Option("--log", help="Append privacy-bounded results to doctor.jsonl."),
    ] = False,
) -> None:
    """Report local readiness without fetching jobs or exposing private inputs."""
    paths = AppPaths.from_environment(os.environ)
    report = run_doctor(paths, log=True) if log else run_doctor(paths)
    for check in report.checks:
        typer.echo(f"[{check.status}] {check.name}: {check.message}")
    if report.has_errors:
        raise typer.Exit(code=1)


def _default_setup_service_factory(paths: AppPaths) -> SetupService:
    return SetupService(paths)


_setup_service_factory: Callable[[AppPaths], SetupService] = (
    _default_setup_service_factory
)


def _default_scan_service_factory(paths: AppPaths) -> ScanService:
    return ScanService(paths)


_scan_service_factory: Callable[[AppPaths], ScanService] = _default_scan_service_factory


def _default_scheduler_backend_factory() -> SchedulerBackend:
    return scheduler_for_platform()


_scheduler_backend_factory: Callable[[], SchedulerBackend] = (
    _default_scheduler_backend_factory
)


def _default_scheduler_executable_factory() -> Path:
    discovered = shutil.which("job-scan")
    return Path(discovered if discovered is not None else sys.argv[0]).resolve()


_scheduler_executable_factory: Callable[[], Path] = (
    _default_scheduler_executable_factory
)


def _default_mdns_publisher_factory() -> MdnsPublisher | None:
    """Enable Avahi-backed LAN publishing only on Linux."""
    return MdnsPublisher() if sys.platform.startswith("linux") else None


_mdns_publisher_factory: Callable[[], MdnsPublisher | None] = (
    _default_mdns_publisher_factory
)


class _SetupInputError(ValueError):
    """Report one short setup input error safe for terminal display."""


@app.command()
def setup(
    resume: Annotated[
        Path,
        typer.Option("--resume", help="Path to a text-based PDF or DOCX resume."),
    ],
) -> None:
    """Create a factual profile and persist Germany-only scan settings."""
    try:
        search_terms = _required_csv(
            typer.prompt("Search terms (comma-separated)", default="", show_default=False),
            "search terms",
        )
        locations = _csv_values(
            typer.prompt(
                "Locations (comma-separated, blank for all Germany)",
                default="",
                show_default=False,
            )
        )
        linkedin_limit = typer.prompt(
            "LinkedIn jobs per search (0 disables, max 100)",
            default=10,
            type=int,
        )
        indeed_de_limit = typer.prompt(
            "Indeed Deutschland jobs per search (0 disables, max 100)",
            default=10,
            type=int,
        )
        stepstone_de_limit = typer.prompt(
            "StepStone jobs per search (0 disables, max 100)",
            default=10,
            type=int,
        )
        glassdoor_de_limit = typer.prompt(
            "Glassdoor DE jobs per search (0 disables, max 100)",
            default=10,
            type=int,
        )
        simplify_de_limit = typer.prompt(
            "Simplify DE jobs per search (0 disables, max 100)",
            default=10,
            type=int,
        )
        german_level = typer.prompt("German certificate or level")
        model = typer.prompt("Claude model")
        effort = typer.prompt("Claude effort (low/medium/high)")
        batch_size = typer.prompt("Claude batch size", type=int)
        local_time = typer.prompt(
            "Daily local scan time (HH:MM, blank for manual scans only)",
            default="",
            show_default=False,
        )
        answers = SetupAnswers(
            search_terms=search_terms,
            locations=locations,
            linkedin_limit=linkedin_limit,
            indeed_de_limit=indeed_de_limit,
            stepstone_de_limit=stepstone_de_limit,
            glassdoor_de_limit=glassdoor_de_limit,
            simplify_de_limit=simplify_de_limit,
            german_level=german_level.strip(),
            claude=ClaudeSettings(
                model=model.strip(),
                effort=effort.strip(),
                batch_size=batch_size,
            ),
            scheduler=SchedulerSettings(local_time=local_time.strip() or None),
        )
    except _SetupInputError as error:
        _exit_setup_error(str(error))
    except ValidationError as error:
        field = ".".join(str(item) for item in error.errors()[0]["loc"])
        _exit_setup_error(f"Invalid {field}; correct it and retry.")

    paths = AppPaths.from_environment(os.environ)
    try:
        with FileRWLock(paths.workflow_lock_file).exclusive(blocking=False):
            providers = AiProviderStore(paths.ai_config_toml)
            fallback = AiRuntimeSelection(
                ai_runtime=answers.ai_runtime,
                claude=claude_runtime_selection_from_settings(answers.claude),
            )
            try:
                fallback = ai_selection_from_config(
                    load_config(paths.config_toml),
                    providers,
                )
            except (OSError, ValueError):
                pass
            selection = resolve_ai_selection(
                AiSelectionStore(paths.ai_selection_toml).load(fallback),
                providers,
            )
            answers = answers.model_copy(
                update={
                    "ai_runtime": selection.ai_runtime,
                    "claude": apply_ai_selection_to_claude(
                        answers.claude,
                        selection,
                    ),
                },
                deep=True,
            )
            result = _setup_service_factory(paths).run(resume, answers)
    except LockUnavailable:
        _exit_setup_error("Another setup or scan is already running.")
    except (AiConfigError, AiSelectionError) as error:
        _exit_setup_error(str(error))
    except (SetupError, ResumeError, ClaudeProcessError) as error:
        _exit_setup_error(str(error))

    typer.echo(f"Profile: {result.profile_path}")
    typer.echo(f"Profile hash: {result.profile_hash}")
    typer.echo(f"Config: {paths.config_toml}")
    typer.echo(f"Resume hash: {result.config.resume_sha256}")


@app.command()
def scan(
    force_review: Annotated[
        bool,
        typer.Option("--force-review", help="Review all active jobs with complete JDs."),
    ] = False,
    scheduled: Annotated[
        bool,
        typer.Option(
            "--scheduled",
            help="Run the saved daily setup and archive its search history.",
        ),
    ] = False,
) -> None:
    """Fetch configured sources, review due jobs, and publish one snapshot."""
    paths = AppPaths.from_environment(os.environ)
    try:
        result = _run_tracked_scan(
            paths,
            lambda progress: (
                _run_scheduled_scan(paths, force_review=force_review, progress=progress)
                if scheduled
                else _scan_service_factory(paths).run(
                    force_review=force_review,
                    progress=progress,
                )
            ),
        )
    except ScanAlreadyRunning as error:
        _exit_scan_error(str(error), code=2)
    except ScanError as error:
        _exit_scan_error(str(error), code=1)

    typer.echo(f"Source occurrences: {result.occurrence_count}")
    typer.echo(f"New jobs: {result.new_count}")
    typer.echo(f"Changed jobs: {result.changed_count}")
    typer.echo(f"Reviewed jobs: {result.reviewed_count}")
    typer.echo(f"Eligible: {result.eligible_count}")
    typer.echo(f"Excluded: {result.excluded_count}")
    typer.echo(f"Uncertain: {result.uncertain_count}")
    typer.echo(f"Pending: {result.pending_count}")
    typer.echo(f"Source errors: {result.source_error_count}")
    typer.echo(f"Jobs JSONL: {result.jobs_jsonl}")
    typer.echo(f"Dashboard: {result.dashboard_html}")


def _run_tracked_scan(
    paths: AppPaths,
    run: Callable[[ScanProgressCallback], ScanSummary],
) -> ScanSummary:
    """Persist command-line scan progress so other processes can show it."""
    run_id = str(uuid.uuid4())
    last_stage: ScanProgressStage | None = None
    last_percent = 0.0

    def record(current: ScanProgress) -> None:
        nonlocal last_stage, last_percent
        last_stage = current.stage
        last_percent = scan_progress_percent(current)
        _write_scan_progress_state(
            paths,
            ScanRunState(
                run_id=run_id,
                status="running",
                stage=current.stage,
                message=scan_progress_message(current),
                progress_percent=last_percent,
                updated_at=datetime.now(UTC),
            ),
        )

    try:
        summary = run(record)
    except ScanAlreadyRunning:
        raise
    except BaseException as error:
        _write_scan_progress_state(
            paths,
            ScanRunState(
                run_id=run_id,
                status="failed",
                stage=last_stage,
                message=str(error) if isinstance(error, ScanError) else "Scan failed.",
                progress_percent=last_percent,
                updated_at=datetime.now(UTC),
            ),
        )
        raise
    _write_scan_progress_state(
        paths,
        ScanRunState(
            run_id=run_id,
            status="complete",
            stage="publish",
            message="Review queue published.",
            progress_percent=100.0,
            updated_at=datetime.now(UTC),
        ),
    )
    return summary


def _write_scan_progress_state(paths: AppPaths, state: ScanRunState) -> None:
    """Store one scan state snapshot while tolerating an unusable output directory."""
    try:
        write_scan_run_state(paths, state)
    except OSError:
        pass  # state reporting must never fail the scan itself


def _run_scheduled_scan(
    paths: AppPaths,
    *,
    force_review: bool,
    progress: ScanProgressCallback | None = None,
) -> ScanSummary:
    """Publish the saved daily setup, scan it, and archive the exact result."""
    try:
        with FileRWLock(paths.workflow_lock_file).exclusive(blocking=False):
            with FileRWLock(paths.schedule_lock_file).exclusive(blocking=False):
                config_bytes = paths.scheduled_config_toml.read_bytes()
                profile_bytes = paths.scheduled_profile_md.read_bytes()
                config = load_config_bytes(config_bytes)
                if config.scheduler.local_time is None:
                    raise ScanError("Daily scan time is not configured.")
                actual_profile_hash = (
                    "sha256:" + hashlib.sha256(profile_bytes).hexdigest()
                )
                if config.profile_sha256 != actual_profile_hash:
                    raise ScanError("Saved daily scan profile does not match its configuration.")

            setup_service = _setup_service_factory(paths)
            previous_profile = (
                paths.profile_md.read_bytes() if paths.profile_md.is_file() else None
            )
            previous_config = (
                paths.config_toml.read_bytes() if paths.config_toml.is_file() else None
            )
            setup_service.restore_pair(profile_bytes, config_bytes)
            resume_path = Path(config.resume_path)
            resume_filename = (
                f"{config.candidate_name.strip() or 'resume'}{resume_path.suffix.lower()}"
            )
            history = SearchHistoryStore(paths)

            def archive(summary: ScanSummary, snapshot: Snapshot) -> None:
                history.archive(
                    run_id=summary.run_id,
                    candidate_name=config.candidate_name or "Candidate",
                    resume_filename=resume_filename,
                    resume_path=resume_path,
                    snapshot=snapshot,
                    finished_at=summary.finished_at,
                    profile_bytes=profile_bytes,
                    config_bytes=config_bytes,
                )

            try:
                return _scan_service_factory(paths).run(
                    force_review=force_review,
                    progress=progress,
                    on_published=archive,
                    workflow_lock_held=True,
                )
            except BaseException:
                setup_service.restore_pair(previous_profile, previous_config)
                raise
    except LockUnavailable:
        raise ScanAlreadyRunning("Another setup or scan is already running.") from None
    except (OSError, UnicodeError, ValueError, SetupError) as error:
        raise ScanError("Could not load the saved daily scan setup.") from error


@app.command()
def review(
    port: Annotated[
        int,
        typer.Option("--port", min=1, max=65535, help="Review HTTP port."),
    ] = 8765,
) -> None:
    """Serve the local Setup workflow and its review mutations."""
    paths = AppPaths.from_environment(os.environ)
    repository = JsonlRepository(
        paths,
        FileRWLock(paths.lock_file),
        render_dashboard,
    )
    repository.rebuild_dashboard()
    token = secrets.token_urlsafe(32)
    mdns_publisher = _mdns_publisher_factory()
    lan_ip: str | None = None
    if mdns_publisher is not None:
        try:
            lan_ip = mdns_publisher.start()
        except MdnsError as error:
            typer.echo(f"mDNS startup failed: {error}", err=True)
            raise typer.Exit(code=1) from error
    try:
        allowed_origins = {
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
        }
        if mdns_publisher is not None:
            allowed_origins.add(f"http://{MDNS_HOSTNAME}:{port}")
        search_history = SearchHistoryStore(paths)
        ats_history = AtsHistoryStore(paths)
        ats_workflow = AtsWorkflow(
            AtsCheckService(AiRuntimeInvoker(paths)),
            ats_history,
        )
        workflow = WebWorkflow(
            paths,
            setup_service=_setup_service_factory(paths),
            scan_service=_scan_service_factory(paths),
            scheduler=_scheduler_backend_or_exit(),
            executable=_scheduler_executable_factory(),
            history_store=search_history,
        )
        try:
            workflow.reconcile_schedule()
        except (OSError, ValueError, SchedulerError, WebWorkflowBusy) as error:
            typer.echo(
                f"Scheduler reconciliation failed: {error}",
                err=True,
            )
        review_app = create_review_app(
            repository,
            token,
            frozenset(allowed_origins),
            workflow=workflow,
            ai_store=AiProviderStore(paths.ai_config_toml),
            ai_model_discovery=AiModelDiscovery(),
            history_store=search_history,
            ats_workflow=ats_workflow,
            ats_history_store=ats_history,
            current_lan_origin=(
                lambda: (
                    f"http://{mdns_publisher.current_ip}:{port}"
                    if mdns_publisher.current_ip is not None
                    else None
                )
            )
            if mdns_publisher is not None
            else None,
        )
        if mdns_publisher is not None:
            typer.echo(f"Setup: http://{MDNS_HOSTNAME}:{port}/setup")
            typer.echo(f"LAN fallback: http://{lan_ip}:{port}")
        else:
            typer.echo(f"Setup: http://127.0.0.1:{port}/setup")
        try:
            uvicorn.run(
                review_app,
                host="0.0.0.0" if mdns_publisher is not None else "127.0.0.1",
                port=port,
                access_log=False,
                reload=False,
            )
        except KeyboardInterrupt:
            pass
    finally:
        if mdns_publisher is not None:
            mdns_publisher.stop()


@scheduler_app.command("install")
def scheduler_install(
    local_time: Annotated[
        str | None,
        typer.Option("--time", help="Daily local scan time in HH:MM."),
    ] = None,
) -> None:
    """Install or reconcile the native daily scheduler entry."""
    paths = AppPaths.from_environment(os.environ)
    backend = _scheduler_backend_or_exit()
    scheduler_settings: SchedulerSettings | None = None
    if local_time is not None:
        try:
            scheduler_settings = SchedulerSettings(local_time=local_time)
        except ValidationError:
            _exit_scheduler_error("time must use HH:MM.")
    try:
        with FileRWLock(paths.workflow_lock_file).exclusive(blocking=False), FileRWLock(
            paths.schedule_lock_file
        ).exclusive(blocking=False):
            previous_profile = (
                paths.scheduled_profile_md.read_bytes()
                if paths.scheduled_profile_md.is_file()
                else None
            )
            previous_config = (
                paths.scheduled_config_toml.read_bytes()
                if paths.scheduled_config_toml.is_file()
                else None
            )
            setup_service = _setup_service_factory(paths)
            try:
                config = _load_or_seed_scheduled_setup(paths)
                if scheduler_settings is not None:
                    config = config.model_copy(update={"scheduler": scheduler_settings})
                    save_config(paths.scheduled_config_toml, config)
                if config.scheduler.local_time is None:
                    raise SchedulerError(
                        "Daily scan time is not configured; pass --time HH:MM."
                    )
                state = backend.install(
                    config,
                    paths,
                    _scheduler_executable_factory(),
                )
            except BaseException:
                setup_service.restore_pair(
                    previous_profile,
                    previous_config,
                    profile_path=paths.scheduled_profile_md,
                    config_path=paths.scheduled_config_toml,
                )
                raise
    except LockUnavailable:
        _exit_scheduler_error("Another setup or scan is already running.")
    except (OSError, ValueError, SchedulerError) as error:
        _exit_scheduler_error(str(error) or "Scheduler operation failed.")
    _print_scheduler_state(state, paths)


@scheduler_app.command("remove")
def scheduler_remove() -> None:
    """Remove only the scheduler entry owned by job-scan."""
    paths = AppPaths.from_environment(os.environ)
    backend = _scheduler_backend_or_exit()
    try:
        with FileRWLock(paths.workflow_lock_file).exclusive(blocking=False), FileRWLock(
            paths.schedule_lock_file
        ).exclusive(blocking=False):
            state = backend.remove(paths)
            if paths.scheduled_config_toml.is_file():
                config = load_config(paths.scheduled_config_toml)
                config = config.model_copy(update={"scheduler": SchedulerSettings()})
                save_config(paths.scheduled_config_toml, config)
    except LockUnavailable:
        _exit_scheduler_error("Another setup or scan is already running.")
    except (OSError, ValueError, SchedulerError) as error:
        _exit_scheduler_error(str(error) or "Scheduler operation failed.")
    _print_scheduler_state(state, paths)


@scheduler_app.command("status")
def scheduler_status() -> None:
    """Print the current native scheduler state."""
    paths = AppPaths.from_environment(os.environ)
    backend = _scheduler_backend_or_exit()
    try:
        state = backend.status(paths)
    except (OSError, SchedulerError) as error:
        _exit_scheduler_error(str(error) or "Scheduler operation failed.")
    _print_scheduler_state(state, paths)


def _scheduler_backend_or_exit() -> SchedulerBackend:
    """Select a platform backend or exit with one terminal-safe error."""
    try:
        return _scheduler_backend_factory()
    except SchedulerError as error:
        _exit_scheduler_error(str(error))


def _load_or_seed_scheduled_setup(paths: AppPaths) -> AppConfig:
    """Load the dedicated daily setup or seed it from the current CLI setup."""
    if paths.scheduled_config_toml.is_file() or paths.scheduled_profile_md.is_file():
        config = load_config(paths.scheduled_config_toml)
        paths.scheduled_profile_md.read_bytes()
        return config
    config_bytes = paths.config_toml.read_bytes()
    profile_bytes = paths.profile_md.read_bytes()
    config = load_config_bytes(config_bytes)
    _setup_service_factory(paths).restore_pair(
        profile_bytes,
        config_bytes,
        profile_path=paths.scheduled_profile_md,
        config_path=paths.scheduled_config_toml,
    )
    return config


def _print_scheduler_state(state: SchedulerState, paths: AppPaths) -> None:
    """Print stable scheduler fields shared by install, remove, and status."""
    typer.echo(f"Backend: {state.backend}")
    typer.echo(f"Installed: {'yes' if state.installed else 'no'}")
    typer.echo(f"Schedule: {state.local_time or 'not installed'}")
    typer.echo(f"Executable: {state.executable or 'not installed'}")
    typer.echo(f"Data root: {paths.root}")
    typer.echo(f"Log path: {paths.logs_dir / 'scheduler.log'}")


def _required_csv(raw: str, field: str) -> list[str]:
    """Parse one ordered comma-separated list and reject an empty result."""
    values = _csv_values(raw)
    if not values:
        raise _SetupInputError(f"{field.capitalize()} must contain at least one value.")
    return values


def _csv_values(raw: str) -> list[str]:
    """Return non-empty trimmed values from one comma-separated input."""
    return [item.strip() for item in raw.split(",") if item.strip()]


def _exit_setup_error(message: str) -> None:
    """Print one safe setup failure and exit without a traceback."""
    typer.echo(f"Setup failed: {message}", err=True)
    raise typer.Exit(code=1)


def _exit_scan_error(message: str, *, code: int) -> None:
    """Print one safe scan failure and exit without a traceback."""
    typer.echo(f"Scan failed: {message}", err=True)
    raise typer.Exit(code=code)


def _exit_scheduler_error(message: str) -> Never:
    """Print one safe scheduler failure and exit without a traceback."""
    typer.echo(f"Scheduler failed: {message}", err=True)
    raise typer.Exit(code=1)
