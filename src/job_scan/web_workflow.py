from __future__ import annotations

import hashlib
import os
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path
from threading import Lock, Thread
from typing import BinaryIO, Literal

from pydantic import BaseModel, ConfigDict, Field

from job_scan.claude_process import ClaudeProcessError
from job_scan.company_size import CompanySizeProgress
from job_scan.config import AppConfig, SchedulerSettings, load_config, save_config
from job_scan.domain import Snapshot
from job_scan.locking import FileRWLock, LockUnavailable
from job_scan.paths import AppPaths
from job_scan.resume import ResumeError, ResumeReadError, UnsupportedResumeFormat
from job_scan.reviewer import ReviewBatchProgress
from job_scan.scan_service import (
    ScanError,
    ScanProgress,
    ScanService,
    ScanSummary,
    SourceProgress,
)
from job_scan.scheduler import SchedulerBackend, SchedulerError, SchedulerState
from job_scan.search_history import SearchHistoryStore
from job_scan.setup_service import SetupAnswers, SetupError, SetupResult, SetupService

MAX_RESUME_BYTES = 20 * 1024 * 1024


class WebWorkflowBusy(RuntimeError):
    """Report a second browser run while one workflow owns the setup data."""


class WebScheduleState(BaseModel):
    """Expose the scheduler fields needed by the local console."""

    model_config = ConfigDict(extra="forbid")

    installed: bool
    local_time: str | None


class WebRunResult(BaseModel):
    """Return the real scan summary and resulting schedule state."""

    model_config = ConfigDict(extra="forbid")

    summary: ScanSummary
    schedule: WebScheduleState


class WebRunState(BaseModel):
    """Expose one background browser workflow without private request data."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: Literal["running", "complete", "failed"]
    stage: Literal["profile", "sources", "review", "company_size", "publish"]
    message: str
    progress_percent: float = Field(ge=0, le=100)
    ai_runtime: str = Field(
        default="claude-code",
        pattern=r"^(?:claude-code|api:[a-z0-9]+(?:-[a-z0-9]+)*)$",
    )
    source_progress: SourceProgress | None = None
    review_progress: ReviewBatchProgress | None = None
    company_size_progress: CompanySizeProgress | None = None
    result: WebRunResult | None = None
    error: str | None = None


_STAGE_MESSAGES = {
    "sources": "Searching configured job sources...",
    "review": "Reviewing complete job descriptions...",
    "company_size": "Checking company sizes...",
    "publish": "Publishing review queue...",
}


class WebWorkflow:
    """Run one browser-submitted setup, schedule reconciliation, and scan."""

    def __init__(
        self,
        paths: AppPaths,
        *,
        setup_service: SetupService,
        scan_service: ScanService,
        scheduler: SchedulerBackend,
        executable: Path,
        history_store: SearchHistoryStore | None = None,
    ) -> None:
        self._paths = paths
        self._setup_service = setup_service
        self._scan_service = scan_service
        self._scheduler = scheduler
        self._executable = executable.resolve()
        self._history_store = history_store or SearchHistoryStore(paths)
        self._run_lock = Lock()
        self._state_lock = Lock()
        self._current_run: WebRunState | None = None
        self._run_thread: Thread | None = None

    def start(
        self,
        resume_filename: str,
        resume_bytes: bytes,
        answers: SetupAnswers,
    ) -> WebRunState:
        """Start one browser workflow and return its initial observable state."""
        if not self._run_lock.acquire(blocking=False):
            raise WebWorkflowBusy("A setup and scan is already running.")
        run_id = str(uuid.uuid4())
        initial = WebRunState(
            run_id=run_id,
            status="running",
            stage="profile",
            message="Building candidate profile...",
            progress_percent=10,
            ai_runtime=answers.ai_runtime,
        )
        with self._state_lock:
            self._current_run = initial
        thread = Thread(
            target=self._run_in_background,
            args=(run_id, resume_filename, resume_bytes, answers),
            name=f"job-scan-web-{run_id}",
            daemon=True,
        )
        self._run_thread = thread
        try:
            thread.start()
        except BaseException:
            self._run_lock.release()
            raise
        return initial.model_copy(deep=True)

    def read_run(self, run_id: str) -> WebRunState | None:
        """Return a snapshot of the requested in-memory browser workflow."""
        with self._state_lock:
            if self._current_run is None or self._current_run.run_id != run_id:
                return None
            return self._current_run.model_copy(deep=True)

    def read_current_run(self) -> WebRunState | None:
        """Return the active browser workflow, excluding finished runs."""
        with self._state_lock:
            if self._current_run is None or self._current_run.status != "running":
                return None
            return self._current_run.model_copy(deep=True)

    def is_busy(self) -> bool:
        """Return whether this process is currently running setup or scan work."""
        return self._run_lock.locked()

    def run(
        self,
        resume_filename: str,
        resume_bytes: bytes,
        answers: SetupAnswers,
    ) -> WebRunResult:
        """Persist one uploaded resume, publish setup, then run the configured scan."""
        if not self._run_lock.acquire(blocking=False):
            raise WebWorkflowBusy("A setup and scan is already running.")
        try:
            return self._run_once(resume_filename, resume_bytes, answers)
        finally:
            self._run_lock.release()

    def _run_once(
        self,
        resume_filename: str,
        resume_bytes: bytes,
        answers: SetupAnswers,
        progress: Callable[[ScanProgress], None] | None = None,
    ) -> WebRunResult:
        """Execute one workflow while the caller owns the browser-run lock."""
        try:
            with FileRWLock(self._paths.workflow_lock_file).exclusive(blocking=False):
                return self._run_once_locked(
                    resume_filename,
                    resume_bytes,
                    answers,
                    progress,
                )
        except LockUnavailable:
            raise WebWorkflowBusy("Another setup or scan is already running.") from None

    def _run_once_locked(
        self,
        resume_filename: str,
        resume_bytes: bytes,
        answers: SetupAnswers,
        progress: Callable[[ScanProgress], None] | None,
    ) -> WebRunResult:
        """Run setup through archive while the cross-process workflow lock is held."""
        answers = answers.model_copy(
            update={
                "candidate_name": Path(resume_filename).stem.strip() or "Candidate",
            }
        )
        previous_profile_bytes = _read_optional_bytes(self._paths.profile_md)
        previous_config_bytes = _read_optional_bytes(self._paths.config_toml)
        try:
            previous_config = load_config(self._paths.config_toml)
        except (OSError, ValueError):
            previous_config = None
        resume_path, created = store_uploaded_resume(
            self._paths,
            resume_filename,
            resume_bytes,
        )
        try:
            setup_result = self._setup_service.run(resume_path, answers)
        except BaseException:
            if created:
                resume_path.unlink(missing_ok=True)
            raise

        try:
            return self._complete_locked(
                resume_filename,
                resume_path,
                answers,
                setup_result,
                progress,
            )
        except BaseException:
            try:
                self._setup_service.restore_pair(
                    previous_profile_bytes,
                    previous_config_bytes,
                )
                self._restore_previous_schedule(previous_config)
            finally:
                if created:
                    resume_path.unlink(missing_ok=True)
            raise

    def _complete_locked(
        self,
        resume_filename: str,
        resume_path: Path,
        answers: SetupAnswers,
        setup_result: SetupResult,
        progress: Callable[[ScanProgress], None] | None,
    ) -> WebRunResult:
        """Reconcile schedule, scan, and archive under the workflow lock."""

        if setup_result.config.scheduler.local_time is None:
            schedule = self._scheduler.remove(self._paths)
        else:
            schedule = self._scheduler.install(
                setup_result.config,
                self._paths,
                self._executable,
            )
        profile_bytes = setup_result.profile_path.read_bytes()
        config_bytes = self._paths.config_toml.read_bytes()

        def archive(summary: ScanSummary, snapshot: Snapshot) -> None:
            self._history_store.archive(
                run_id=summary.run_id,
                candidate_name=setup_result.config.candidate_name or "Candidate",
                resume_filename=resume_filename,
                resume_path=resume_path,
                snapshot=snapshot,
                finished_at=summary.finished_at,
                profile_bytes=profile_bytes,
                config_bytes=config_bytes,
            )

        summary = self._scan_service.run(
            progress=progress,
            on_published=archive,
            workflow_lock_held=True,
        )
        return WebRunResult(
            summary=summary,
            schedule=WebScheduleState(
                installed=schedule.installed,
                local_time=schedule.local_time,
            ),
        )

    def _restore_previous_schedule(self, config: AppConfig | None) -> None:
        """Restore the scheduler state paired with the prior setup after failure."""
        if config is None or config.scheduler.local_time is None:
            self._scheduler.remove(self._paths)
            return
        self._scheduler.install(config, self._paths, self._executable)

    def _run_in_background(
        self,
        run_id: str,
        resume_filename: str,
        resume_bytes: bytes,
        answers: SetupAnswers,
    ) -> None:
        """Execute one started workflow and publish safe state transitions."""
        try:
            result = self._run_once(
                resume_filename,
                resume_bytes,
                answers,
                progress=lambda current: self._record_progress(run_id, current),
            )
            self._replace_run(
                run_id,
                status="complete",
                stage="publish",
                message="Review queue published.",
                progress_percent=100,
                review_progress=None,
                result=result,
                error=None,
            )
        except BaseException as error:  # noqa: BLE001
            safe_error = _safe_run_error(error)
            self._replace_run(
                run_id,
                status="failed",
                message=safe_error,
                result=None,
                error=safe_error,
            )
        finally:
            self._run_lock.release()

    def _record_progress(self, run_id: str, current: ScanProgress) -> None:
        """Publish the latest real scan or configured review-batch progress."""
        self._replace_run(
            run_id,
            status="running",
            stage=current.stage,
            message=_progress_message(current),
            progress_percent=_progress_percent(current),
            source_progress=current.source_progress,
            review_progress=current.review,
            company_size_progress=current.company_size,
            result=None,
            error=None,
        )

    def _replace_run(self, run_id: str, **updates: object) -> None:
        """Replace current run state only when the matching run still owns it."""
        with self._state_lock:
            if self._current_run is None or self._current_run.run_id != run_id:
                return
            self._current_run = self._current_run.model_copy(update=updates)

    def remove_schedule(self) -> SchedulerState:
        """Remove the owned native task and clear its saved local time."""
        if not self._run_lock.acquire(blocking=False):
            raise WebWorkflowBusy("A setup and scan is already running.")
        try:
            try:
                with FileRWLock(self._paths.workflow_lock_file).exclusive(blocking=False):
                    state = self._scheduler.remove(self._paths)
                    if self._paths.config_toml.is_file():
                        config = load_config(self._paths.config_toml)
                        config = config.model_copy(
                            update={"scheduler": SchedulerSettings()}
                        )
                        save_config(self._paths.config_toml, config)
                    return state
            except LockUnavailable:
                raise WebWorkflowBusy(
                    "Another setup or scan is already running."
                ) from None
        finally:
            self._run_lock.release()

    def schedule_status(self) -> SchedulerState:
        """Return the current owned native scheduler state without changing it."""
        return self._scheduler.status(self._paths)

    def load_setup_answers(self) -> SetupAnswers | None:
        """Return saved browser setup values when available."""
        try:
            config = load_config(self._paths.config_toml)
        except (OSError, ValueError):
            return None
        return SetupAnswers.model_validate(
            config.model_dump(
                mode="json",
                include=set(SetupAnswers.model_fields),
                warnings=False,
            )
        )


def store_uploaded_resume(
    paths: AppPaths,
    filename: str,
    payload: bytes,
) -> tuple[Path, bool]:
    """Store an immutable PDF or DOCX upload under its content hash."""
    suffix = Path(filename).suffix.lower()
    if suffix not in {".pdf", ".docx"}:
        raise UnsupportedResumeFormat(
            f"Unsupported resume format {suffix or '(none)'}; use a .pdf or .docx file."
        )
    if not payload:
        raise ResumeReadError("Uploaded resume is empty.")
    if len(payload) > MAX_RESUME_BYTES:
        raise ResumeReadError("Uploaded resume exceeds the 20 MB limit.")

    digest = hashlib.sha256(payload).hexdigest()
    resume_dir = paths.root / "resumes"
    target = resume_dir / f"{digest}{suffix}"
    try:
        resume_dir.mkdir(parents=True, exist_ok=True)
        if target.is_file():
            if target.read_bytes() != payload:
                raise ResumeReadError("Stored resume does not match its content hash.")
            return target.resolve(), False
        descriptor, temporary_name = tempfile.mkstemp(
            dir=resume_dir,
            prefix=".resume.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    except ResumeReadError:
        raise
    except OSError:
        raise ResumeReadError("Could not save uploaded resume.") from None
    return target.resolve(), True


def read_resume_upload(stream: BinaryIO) -> bytes:
    """Read at most one byte beyond the accepted upload limit."""
    payload = stream.read(MAX_RESUME_BYTES + 1)
    if len(payload) > MAX_RESUME_BYTES:
        raise ResumeReadError("Uploaded resume exceeds the 20 MB limit.")
    return payload


def _read_optional_bytes(path: Path) -> bytes | None:
    """Read one prior setup file while preserving its missing state."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _progress_percent(current: ScanProgress) -> float:
    """Map real scan progress into the existing browser progress-bar range."""
    if current.stage == "sources":
        source = current.source_progress
        if source is None or source.total_sources == 0:
            return 35
        return round(
            35 + (40 * source.completed_sources / source.total_sources),
            1,
        )
    if current.stage == "publish":
        return 99
    company_size = current.company_size
    if current.stage == "company_size":
        if company_size is None or company_size.total_companies == 0:
            return 95
        return round(
            95
            + (4 * company_size.completed_companies / company_size.total_companies),
            1,
        )
    review = current.review
    if review is None or review.total_batches == 0:
        return 75
    return round(
        75 + (20 * review.completed_batches / review.total_batches),
        1,
    )


def _progress_message(current: ScanProgress) -> str:
    """Describe the current stage with review counts when batches exist."""
    source = current.source_progress
    if current.stage == "sources" and source is not None:
        job_label = "job" if source.found_jobs == 1 else "jobs"
        warning_text = ""
        if source.warning_count:
            warning_label = "warning" if source.warning_count == 1 else "warnings"
            warning_text = f", {source.warning_count} {warning_label}"
        return (
            "Searching job sources: "
            f"{source.completed_sources}/{source.total_sources} sources, "
            f"{source.found_jobs} {job_label} found{warning_text}..."
        )
    company_size = current.company_size
    if (
        current.stage == "company_size"
        and company_size is not None
        and company_size.total_companies > 0
    ):
        return (
            "Checking company sizes: "
            f"{company_size.completed_companies}/{company_size.total_companies} "
            "companies..."
        )
    review = current.review
    if current.stage != "review" or review is None or review.total_batches == 0:
        return _STAGE_MESSAGES[current.stage]
    return (
        "Reviewing complete job descriptions: "
        f"{review.completed_batches}/{review.total_batches} batches, "
        f"{review.completed_jobs}/{review.total_jobs} jobs..."
    )


def _safe_run_error(error: BaseException) -> str:
    """Return one browser-safe failure message without private payload data."""
    if isinstance(error, UnsupportedResumeFormat):
        return str(error) or "Unsupported resume format."
    if isinstance(error, ResumeError):
        return "Could not read uploaded resume."
    if isinstance(
        error,
        (
            SetupError,
            WebWorkflowBusy,
            ClaudeProcessError,
            ScanError,
            SchedulerError,
        ),
    ):
        return str(error) or "Setup or scan failed."
    return "Setup or scan failed."
