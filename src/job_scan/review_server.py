from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from contextlib import contextmanager
from datetime import UTC, date, datetime
from inspect import Parameter, signature
from pathlib import Path
from threading import Lock, Thread
from typing import Annotated, Literal, Self, cast
from urllib.parse import quote, urlsplit

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from job_scan.ai_config import (
    AiConfigError,
    AiProviderDraft,
    AiProviderNotFound,
    AiProviderStore,
    AiProviderView,
    StoredAiProvider,
)
from job_scan.ai_runtime import AiRuntimeInvoker
from job_scan.ai_selection import (
    AiRuntimeSelection,
    AiSelectionError,
    AiSelectionStore,
    ClaudeRuntimeSelection,
    CodexRuntimeSelection,
    ai_selection_from_config,
    apply_ai_selection_to_claude,
    apply_ai_selection_to_config,
    claude_runtime_selection_from_settings,
    resolve_ai_selection,
)
from job_scan.anthropic_api import (
    AiModelDiscovery,
    AiModelOption,
    AnthropicApiError,
)
from job_scan.ats_history import AtsHistoryStore
from job_scan.ats_models import AtsCheckBundle, AtsRunState
from job_scan.ats_workflow import (
    AtsInputError,
    AtsInvalidJobSelection,
    AtsResumeInput,
    AtsWorkflow,
    AtsWorkflowBusy,
    AtsWorkflowInput,
)
from job_scan.claude_process import ClaudeProcessError
from job_scan.codex_login import CodexLoginSnapshot, CodexLoginWorkflow
from job_scan.codex_process import (
    CodexModelOption,
    CodexNotAuthenticated,
    CodexProcess,
    CodexProcessError,
)
from job_scan.company_size import (
    AiCompanySizeLookup,
    CompanySizeEvidence,
    CompanySizeLookupError,
    CompanySizeService,
    CompanySizeStore,
    CompanySizeStoreError,
)
from job_scan.config import AppConfig, load_config, load_config_bytes, save_config
from job_scan.dashboard.render import render_console
from job_scan.domain import (
    JobRecord,
    MachineStatus,
    SalaryPeriod,
    SalaryValue,
    Snapshot,
    SourceOccurrence,
    StoreMeta,
    UserStatus,
)
from job_scan.global_jobs import GlobalJobStore, filter_untracked_jobs
from job_scan.http_client import InvalidResponse
from job_scan.job_snapshot import JobSnapshotStore
from job_scan.locking import FileRWLock, LockUnavailable
from job_scan.manual_job_import import (
    AiJobExtractor,
    ManualJobImportError,
    ManualJobImportService,
    OpenCliPageReader,
    require_public_job_url,
)
from job_scan.manual_job_import_workflow import (
    ManualImportBusy,
    ManualImportResult,
    ManualImportState,
    ManualJobImportWorkflow,
)
from job_scan.paths import AppPaths
from job_scan.repository import JsonlRepository
from job_scan.resume import ResumeError, UnsupportedResumeFormat
from job_scan.resume_suggestions import (
    ResumeSuggestionError,
    ResumeSuggestions,
    ResumeSuggestionService,
    ResumeSuggestionSettings,
)
from job_scan.reviewer import ClaudeReviewer
from job_scan.scan_service import read_scan_run_state
from job_scan.scheduler import SchedulerError
from job_scan.search_history import SearchHistoryStore
from job_scan.setup_service import (
    SetupAnswers,
    SetupError,
    SetupPreparation,
    SetupService,
)
from job_scan.sources.base import BrowserSourceError
from job_scan.sources.job_snapshot_capture import capture_source_job_snapshot_html
from job_scan.web_workflow import (
    WebRunState,
    WebScheduleState,
    WebWorkflow,
    WebWorkflowBusy,
    read_resume_upload,
    read_stored_resume,
    store_uploaded_resume,
)

_SESSION_COOKIE = "job_scan_session"


class _StatusMutation(BaseModel):
    status: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9-]*$")

    @field_validator("status")
    @classmethod
    def reject_new_status(cls, value: str) -> str:
        """Keep New as an automatic state, never a user-selected state."""
        if value == UserStatus.NEW.value:
            raise ValueError("New is not a selectable user status")
        return value


class _TrackerGroupMutation(BaseModel):
    name: str = Field(min_length=1, max_length=80)

    @field_validator("name")
    @classmethod
    def trim_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("group name cannot be blank")
        return value


class _TrackerGroupDeletion(BaseModel):
    confirmation_name: str | None = Field(default=None, max_length=80)


class _BatchStatusMutation(_StatusMutation):
    keys: list[str] = Field(min_length=1, max_length=1000)

    @field_validator("keys")
    @classmethod
    def require_unique_job_keys(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(not key.strip() for key in value):
            raise ValueError("job keys must be non-blank and unique")
        return value


class _BatchDeletionMutation(BaseModel):
    keys: list[str] = Field(min_length=1, max_length=1000)
    confirmation_text: str = Field(max_length=40)

    @field_validator("keys")
    @classmethod
    def require_unique_job_keys(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(not key.strip() for key in value):
            raise ValueError("job keys must be non-blank and unique")
        return value


class _LifecycleDateMutation(BaseModel):
    changed_on: date


class _ReevaluationAcknowledgement(BaseModel):
    finished_at: datetime

    @field_validator("finished_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("finished_at must be timezone-aware")
        return value.astimezone(UTC)


class _ManualFactMutation(BaseModel):
    posted_at: date | None = None
    company_size: int | None = Field(default=None, ge=1)
    company_industry: str | None = Field(default=None, max_length=300)

    @field_validator("company_industry")
    @classmethod
    def trim_company_industry(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("Company industry cannot be empty")
        return trimmed

    @model_validator(mode="after")
    def require_one_fact(self) -> Self:
        values = (self.posted_at, self.company_size, self.company_industry)
        if sum(value is not None for value in values) != 1:
            raise ValueError("Submit exactly one manual fact")
        return self


class _SalaryMutation(BaseModel):
    expected_salary: str = Field(default="", max_length=100)
    expected_salary_period: SalaryPeriod = SalaryPeriod.YEAR
    offer_salary: str = Field(default="", max_length=100)
    offer_salary_period: SalaryPeriod = SalaryPeriod.YEAR

    @field_validator("expected_salary", "offer_salary")
    @classmethod
    def trim_salary(cls, value: str) -> str:
        return value.strip()


class _JobNoteMutation(BaseModel):
    content: str = Field(min_length=1, max_length=2000)

    @field_validator("content")
    @classmethod
    def trim_content(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Note cannot be empty")
        return value


class _UserTagMutation(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    color: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")

    @field_validator("name")
    @classmethod
    def trim_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Tag name cannot be empty")
        return value


class _UserTagDeletion(BaseModel):
    name: str = Field(min_length=1, max_length=40)

    @field_validator("name")
    @classmethod
    def trim_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Tag name cannot be empty")
        return value


class _AiModelDiscoveryRequest(BaseModel):
    provider_id: str | None = None
    base_url: str
    api_key: str | None = None


class _AtsStartRequest(BaseModel):
    job_keys: list[str] = Field(min_length=1)

    @field_validator("job_keys")
    @classmethod
    def require_unique_job_keys(cls, values: list[str]) -> list[str]:
        """Require distinct non-blank canonical job keys."""
        if any(not value.strip() for value in values) or len(values) != len(set(values)):
            raise ValueError("job keys must be non-empty and unique")
        return values


class _AiConfigurationState(BaseModel):
    """Expose the current global AI selection without provider secrets."""

    ai_runtime: str
    claude: ClaudeRuntimeSelection
    codex: CodexRuntimeSelection
    locked: bool


class _CodexAuthState(BaseModel):
    """Expose isolated Codex account state plus safe device-login fields."""

    authenticated: bool
    login: CodexLoginSnapshot


class _BackgroundTaskState(BaseModel):
    """Expose one active task in the sticky console task list."""

    task_id: str
    kind: Literal["scan", "add-job", "re-evaluate", "ats-run"]
    label: str
    status: Literal["waiting", "running", "failed"]
    message: str
    progress_percent: float = Field(ge=0, le=100)
    subject_key: str | None = None


class _BackgroundTaskCollection(BaseModel):
    tasks: list[_BackgroundTaskState]


def _workflow_lock_is_externally_held(paths: AppPaths) -> bool:
    """Return whether any process currently owns the whole-workflow lock."""
    try:
        with FileRWLock(paths.workflow_lock_file).shared(blocking=False):
            pass
    except LockUnavailable:
        return True
    return False


class _JobDisappeared(RuntimeError):
    """Report a job removed after the request's initial existence check."""


def create_review_app(
    repository: JsonlRepository,
    token: str,
    allowed_origins: frozenset[str],
    *,
    workflow: WebWorkflow | None = None,
    ai_store: AiProviderStore | None = None,
    ai_model_discovery: AiModelDiscovery | None = None,
    codex_model_discovery: Callable[[], list[CodexModelOption]] | None = None,
    history_store: SearchHistoryStore | None = None,
    resume_suggestion_service: ResumeSuggestionService | None = None,
    ats_workflow: AtsWorkflow | None = None,
    ats_history_store: AtsHistoryStore | None = None,
    global_job_store: GlobalJobStore | None = None,
    manual_job_importer: Callable[..., JobRecord] | None = None,
    manual_resume_preparer: Callable[[Path, SetupAnswers], SetupPreparation]
    | None = None,
    company_size_service: CompanySizeService | None = None,
    manual_import_workflow: ManualJobImportWorkflow | None = None,
    current_lan_origin: Callable[[], str | None] | None = None,
) -> FastAPI:
    """Create the local review HTTP application."""
    app = FastAPI()
    allowed_hosts = frozenset(urlsplit(origin).netloc for origin in allowed_origins)
    history = history_store or SearchHistoryStore(repository.paths)
    global_jobs = global_job_store or GlobalJobStore(repository.paths)
    resume_suggestions = resume_suggestion_service or ResumeSuggestionService(
        AiRuntimeInvoker(repository.paths)
    )
    company_size_invoker = AiRuntimeInvoker(repository.paths)
    job_snapshots = JobSnapshotStore(repository.paths.job_snapshots_dir)
    ai_selections = AiSelectionStore(repository.paths.ai_selection_toml)
    provider_store = ai_store or AiProviderStore(repository.paths.ai_config_toml)

    def migrate_job_tracker_config() -> None:
        """Seed the independent Job Tracker config once from existing review data."""
        target = repository.paths.job_tracker_config_toml
        if target.exists():
            return
        candidates: list[bytes] = []
        try:
            candidates.append(repository.paths.config_toml.read_bytes())
        except OSError:
            pass
        try:
            entries = history.list()
        except (OSError, ValueError):
            entries = []
        for entry in entries:
            try:
                candidates.append(history.read_review_input(entry.run_id).config_bytes)
            except (KeyError, OSError, ValueError):
                continue
        for contents in candidates:
            try:
                config = load_config_bytes(contents)
            except (UnicodeError, ValueError, ValidationError):
                continue
            save_config(target, config)
            return

    def load_job_tracker_config() -> AppConfig:
        """Load only the Job Tracker-owned configuration."""
        migrate_job_tracker_config()
        return load_config(repository.paths.job_tracker_config_toml)

    migrate_job_tracker_config()
    if manual_job_importer is None:
        manual_job_importer = ManualJobImportService(
            OpenCliPageReader(diagnostics_dir=repository.paths.logs_dir),
            AiJobExtractor(company_size_invoker),
            ClaudeReviewer(company_size_invoker),
            job_snapshots,
        ).import_url

    def _supports_callback(
        importer: Callable[..., JobRecord],
        callback_name: str,
    ) -> bool:
        """Return true when importer can receive one optional callback without break."""
        try:
            parameters = signature(importer).parameters
        except (TypeError, ValueError):
            return True
        has_var_keyword = any(
            parameter.kind == Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        if has_var_keyword:
            return True
        return callback_name in parameters

    def _import_job_with_progress(
        source_url: str,
        config_obj: AppConfig,
        profile_text: str,
        imported_at_obj: datetime,
        progress: Callable[[str, str], None],
        job_extracted: Callable[[JobRecord], None],
    ) -> JobRecord:
        """Run one manual importer with only the callbacks it supports."""
        callbacks: dict[str, object] = {}
        if _supports_callback(manual_job_importer, "on_progress"):
            callbacks["on_progress"] = progress
        if _supports_callback(manual_job_importer, "on_job_extracted"):
            callbacks["on_job_extracted"] = job_extracted
        importer = cast(Callable[..., JobRecord], manual_job_importer)
        return importer(
            source_url,
            config_obj,
            profile_text,
            imported_at_obj,
            **callbacks,
        )

    if manual_resume_preparer is None:
        setup_service = SetupService(repository.paths)

        def prepare_job_tracker_resume(
            resume_path: Path,
            answers: SetupAnswers,
        ) -> SetupPreparation:
            return setup_service.prepare(
                resume_path,
                answers,
                reuse_current_profile=True,
            )

        manual_resume_preparer = prepare_job_tracker_resume
    if manual_import_workflow is None:
        manual_import_workflow = ManualJobImportWorkflow()

    def resume_context(
        run_id: str | None = None,
    ) -> tuple[AppConfig, str, bytes] | None:
        """Return one review's config, original resume name, and resume bytes."""
        try:
            if run_id is not None:
                config = load_config_bytes(history.read_review_input(run_id).config_bytes)
                filename, resume_bytes = history.read_resume(run_id)
            else:
                config = load_config(repository.paths.config_toml)
                archived_resume: tuple[str, bytes] | None = None
                for entry in history.list():
                    try:
                        archived = load_config_bytes(
                            history.read_review_input(entry.run_id).config_bytes
                        )
                    except (KeyError, OSError, UnicodeError, ValueError, ValidationError):
                        continue
                    if archived.resume_sha256 == config.resume_sha256:
                        archived_resume = history.read_resume(entry.run_id)
                        break
                if archived_resume is None:
                    filename = config.resume_path.name
                    resume_bytes = config.resume_path.read_bytes()
                else:
                    filename, resume_bytes = archived_resume
        except (KeyError, OSError, UnicodeError, ValueError, ValidationError):
            return None
        actual_resume_id = "sha256:" + hashlib.sha256(resume_bytes).hexdigest()
        if actual_resume_id != config.resume_sha256:
            return None
        return config, filename, resume_bytes

    def save_review_status(
        job: JobRecord,
        selected_status: str,
        run_id: str | None = None,
    ) -> None:
        """Copy one Review decision and its default resume into Job Tracker."""
        group_ids = {
            group.id for group in global_jobs.load_read_only().meta.tracker_groups
        }
        if selected_status not in group_ids:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "The selected Job Tracker group does not exist.",
            )
        context = resume_context(run_id)
        if context is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "The resume for this review is unavailable.",
            )
        config, filename, resume_bytes = context
        resume_path, _created = store_uploaded_resume(
            repository.paths,
            filename,
            resume_bytes,
        )
        resume_id = f"sha256:{resume_path.stem}"
        global_jobs.set_status(
            job,
            selected_status,
            resume_id=resume_id,
            profile_hash=config.profile_sha256,
            application_resume_filename=filename,
        )

    def selected_ats_jobs(
        keys: list[str],
    ) -> tuple[JobRecord, ...]:
        """Resolve ATS jobs only from the independent Job Tracker store."""
        if not keys or len(keys) != len(set(keys)) or any(not key.strip() for key in keys):
            raise AtsInvalidJobSelection("Select one or more unique jobs.")
        try:
            return global_jobs.selected_jobs(keys)
        except ValueError:
            raise AtsInvalidJobSelection(
                "One or more selected jobs are unavailable."
            ) from None

    def selected_ats_config() -> AppConfig:
        """Build ATS configuration only from Job Tracker-owned settings."""
        try:
            base = load_job_tracker_config()
            return apply_ai_selection_to_config(
                base,
                current_ai_selection(base),
                provider_store,
            )
        except (AiConfigError, AiSelectionError, KeyError, OSError, ValueError):
            raise AtsInputError("The current AI configuration is unavailable.") from None

    def selected_ats_resumes(
        jobs: tuple[JobRecord, ...],
    ) -> tuple[AtsResumeInput, ...]:
        """Group selected jobs by their saved application resume."""
        grouped: dict[str, list[JobRecord]] = {}
        for job in jobs:
            if (
                job.application_resume_id is None
                or job.application_resume_filename is None
            ):
                raise AtsInputError(f"{job.title} has no saved application resume.")
            grouped.setdefault(job.application_resume_id, []).append(job)

        resume_inputs: list[AtsResumeInput] = []
        for resume_id, resume_jobs in grouped.items():
            filename = resume_jobs[0].application_resume_filename
            if filename is None:
                raise AtsInputError(
                    f"The saved resume for {resume_jobs[0].title} is unavailable."
                )
            try:
                resume_bytes = read_stored_resume(
                    repository.paths,
                    resume_id,
                    filename,
                )
            except (ResumeError, OSError, ValueError):
                raise AtsInputError(
                    f"The saved resume for {resume_jobs[0].title} is unavailable."
                ) from None
            resume_inputs.append(
                AtsResumeInput(
                    resume_id=resume_id,
                    candidate_name=Path(filename).stem,
                    resume_filename=filename,
                    resume_bytes=resume_bytes,
                    jobs=tuple(resume_jobs),
                )
            )
        return tuple(resume_inputs)

    def company_sizes(run_id: str | None = None) -> CompanySizeService:
        """Return the injected service or one using the selected run's cache."""
        if company_size_service is not None:
            return company_size_service
        cache_dir = (
            repository.paths.run_cache_dir(run_id)
            if run_id is not None
            else repository.paths.cache_dir
        )
        return CompanySizeService(
            CompanySizeStore(cache_dir / "company-sizes.json"),
            AiCompanySizeLookup(company_size_invoker),
        )

    def company_size_config(
        run_id: str | None = None,
        *,
        job_tracker: bool = False,
    ) -> AppConfig:
        """Load Review or Job Tracker policy from its owning data."""
        try:
            if job_tracker:
                base = load_job_tracker_config()
            elif run_id is None:
                base = load_config(repository.paths.config_toml)
            else:
                base = load_config_bytes(history.read_ats_input(run_id).config_bytes)
            return apply_ai_selection_to_config(
                base,
                current_ai_selection(base if job_tracker else None),
                provider_store,
            )
        except (AiConfigError, AiSelectionError, KeyError, OSError, ValueError):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "The configuration for this review is unavailable.",
            ) from None

    @app.exception_handler(RequestValidationError)
    async def hide_invalid_request_values(
        _request: Request,
        _error: RequestValidationError,
    ) -> JSONResponse:
        """Return one opaque validation error so secret request fields never echo."""
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"detail": "Invalid request."},
        )

    def require_mutation_request(request: Request) -> None:
        """Reject mutations without the server session and exact local headers."""
        cookie = request.cookies.get(_SESSION_COOKIE)
        cookie_matches = cookie is not None and secrets.compare_digest(
            cookie.encode(), token.encode()
        )
        if not cookie_matches:
            raise HTTPException(status.HTTP_403_FORBIDDEN)
        dynamic_origin = current_lan_origin() if current_lan_origin is not None else None
        dynamic_host = urlsplit(dynamic_origin).netloc if dynamic_origin is not None else None
        request_host = request.headers.get("host")
        request_origin = request.headers.get("origin")
        if request_host not in allowed_hosts and not (
            dynamic_host is not None and request_host == dynamic_host
        ):
            raise HTTPException(status.HTTP_403_FORBIDDEN)
        if request_origin not in allowed_origins and not (
            dynamic_origin is not None and request_origin == dynamic_origin
        ):
            raise HTTPException(status.HTTP_403_FORBIDDEN)

    def ai_work_is_running() -> bool:
        """Return whether a running workflow owns AI configuration."""
        for owner in (workflow, manual_import_workflow, ats_workflow):
            if owner is None:
                continue
            is_busy = getattr(owner, "is_busy", None)
            if callable(is_busy):
                if is_busy():
                    return True
            elif bool(getattr(owner, "busy", False)):
                return True
        return False

    @contextmanager
    def write_ai_configuration() -> Iterator[None]:
        """Reject AI configuration writes while current or external AI work runs."""
        detail = "AI is in use; retry the configuration change after it completes."
        if ai_work_is_running():
            raise HTTPException(status.HTTP_409_CONFLICT, detail)
        try:
            with FileRWLock(repository.paths.workflow_lock_file).exclusive(
                blocking=False
            ), FileRWLock(repository.paths.ai_usage_lock_file).exclusive(
                blocking=False
            ):
                if ai_work_is_running():
                    raise HTTPException(status.HTTP_409_CONFLICT, detail)
                yield
        except LockUnavailable:
            raise HTTPException(status.HTTP_409_CONFLICT, detail) from None

    def read_ai_configuration() -> _AiConfigurationState:
        """Return the saved selection and whether editing is currently locked."""
        selection = current_ai_selection()
        locked = ai_work_is_running()
        if not locked:
            try:
                with FileRWLock(repository.paths.workflow_lock_file).exclusive(
                    blocking=False
                ), FileRWLock(repository.paths.ai_usage_lock_file).exclusive(
                    blocking=False
                ):
                    pass
            except LockUnavailable:
                locked = True
        return _AiConfigurationState(
            ai_runtime=selection.ai_runtime,
            claude=selection.claude,
            codex=selection.codex,
            locked=locked,
        )

    def current_ai_selection(
        fallback_config: AppConfig | None = None,
    ) -> AiRuntimeSelection:
        """Return one validated global selection for a new AI operation."""
        try:
            if fallback_config is not None:
                fallback = ai_selection_from_config(fallback_config, provider_store)
            else:
                try:
                    current = load_config(repository.paths.config_toml)
                except (OSError, ValueError):
                    fallback = AiRuntimeSelection()
                else:
                    fallback = ai_selection_from_config(current, provider_store)
            return resolve_ai_selection(ai_selections.load(fallback), provider_store)
        except (AiConfigError, AiSelectionError):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "The current AI configuration is unavailable.",
            ) from None

    def apply_selection_to_setup(
        answers: SetupAnswers,
        fallback_config: AppConfig | None = None,
    ) -> SetupAnswers:
        """Replace browser-supplied model fields with the saved global selection."""
        selection = current_ai_selection(fallback_config)
        return answers.model_copy(
            update={
                "ai_runtime": selection.ai_runtime,
                "claude": apply_ai_selection_to_claude(
                    answers.claude,
                    selection,
                ),
            },
            deep=True,
        )

    def apply_selection_to_suggestions(
        settings: ResumeSuggestionSettings,
    ) -> ResumeSuggestionSettings:
        """Replace temporary browser model fields with the saved global selection."""
        selection = current_ai_selection()
        return settings.model_copy(
            update={
                "ai_runtime": selection.ai_runtime,
                "claude": apply_ai_selection_to_claude(
                    settings.claude,
                    selection,
                ),
            },
            deep=True,
        )

    def uploaded_resume_answers(config: AppConfig, filename: str) -> SetupAnswers:
        """Reuse current search preferences while naming the uploaded resume."""
        answers = SetupAnswers.model_validate(
            config.model_dump(
                mode="json",
                include=set(SetupAnswers.model_fields),
                warnings=False,
            )
        )
        return apply_selection_to_setup(
            answers.model_copy(
                update={
                    "candidate_name": Path(filename).stem.strip() or "Candidate",
                }
            ),
            config,
        )

    @app.post(
        "/api/global-jobs/import",
        dependencies=[Depends(require_mutation_request)],
    )
    def reject_legacy_global_job_import() -> None:
        raise HTTPException(
            status.HTTP_410_GONE,
            "Add one job requires a new resume upload.",
        )

    @app.get("/api/manual-job-imports/{import_id}")
    def read_manual_job_import(
        import_id: str,
    ) -> ManualImportState:
        state = manual_import_workflow.read_run(import_id)
        if state is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        return state

    @app.get("/api/background-tasks")
    def read_background_tasks() -> _BackgroundTaskCollection:
        tasks: list[_BackgroundTaskState] = []
        scan_reader = getattr(workflow, "read_current_run", None)
        scan_state = scan_reader() if callable(scan_reader) else None
        if scan_state is not None and scan_state.status == "running":
            tasks.append(
                _BackgroundTaskState(
                    task_id=f"scan:{scan_state.run_id}",
                    kind="scan",
                    label="Run scan",
                    status="running",
                    message=scan_state.message,
                    progress_percent=scan_state.progress_percent,
                )
            )
        for state in manual_import_workflow.read_active_runs():
            tasks.append(
                _BackgroundTaskState(
                    task_id=f"manual:{state.import_id}",
                    kind=state.task_kind,
                    label=state.task_label,
                    status="waiting" if state.step == "queued" else "running",
                    message=state.message,
                    progress_percent=state.progress_percent,
                    subject_key=state.subject_key,
                )
            )
        ats_reader = getattr(ats_workflow, "read_current_run", None)
        ats_state = ats_reader() if callable(ats_reader) else None
        if ats_state is not None and ats_state.status == "running":
            job_count = sum(
                getattr(task, "kind", None) == "job" for task in ats_state.tasks
            )
            tasks.append(
                _BackgroundTaskState(
                    task_id=f"ats:{ats_state.run_id}",
                    kind="ats-run",
                    label=f"ATS Run · {job_count} jobs",
                    status="running",
                    message=ats_state.message,
                    progress_percent=ats_state.progress_percent,
                )
            )
        tasks.extend(_external_scan_tasks(workflow, repository.paths))
        return _BackgroundTaskCollection(tasks=tasks)

    def _external_scan_tasks(
        workflow: WebWorkflow | None,
        paths: AppPaths,
    ) -> list[_BackgroundTaskState]:
        """Surface the persisted command-line scan state for the task list."""
        state = read_scan_run_state(paths)
        if state is None or state.status == "complete":
            return []
        if state.status == "running":
            busy_reader = getattr(workflow, "is_busy", None)
            busy = busy_reader() if callable(busy_reader) else False
            if busy or not _workflow_lock_is_externally_held(paths):
                # Either this server runs its own scan, or the persisted state
                # is a leftover from a killed scan process.
                return []
        return [
            _BackgroundTaskState(
                task_id=f"cli-scan:{state.run_id}",
                kind="scan",
                label="Command-line scan",
                status=state.status,
                message=state.message,
                progress_percent=state.progress_percent,
            )
        ]

    @app.post(
        "/api/global-jobs/import-with-resume",
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_mutation_request)],
    )
    def import_global_job_with_resume(
        url: Annotated[str, Form(min_length=1, max_length=2083)],
        resume: Annotated[UploadFile, File()],
    ) -> ManualImportState:
        try:
            job_url = require_public_job_url(url)
        except ManualJobImportError as error:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                str(error),
            ) from None

        try:
            filename = Path(resume.filename or "").name
            resume_bytes = read_resume_upload(resume.file)
            suffix = Path(filename).suffix.lower()
            if suffix not in {".pdf", ".docx"}:
                raise UnsupportedResumeFormat(
                    f"Unsupported resume format {suffix or '(none)'}; "
                    "use a .pdf or .docx file."
                )

            def run_import(progress: Callable[[str, str], None]) -> ManualImportResult:
                try:
                    resume_path, _created = store_uploaded_resume(
                        repository.paths,
                        filename,
                        resume_bytes,
                    )
                    resume_id = f"sha256:{resume_path.stem}"
                    with FileRWLock(repository.paths.ai_usage_lock_file).shared():
                        progress("validate", "Preparing the uploaded resume for review.")
                        base_config = load_job_tracker_config()
                        prepared = manual_resume_preparer(
                            resume_path,
                            uploaded_resume_answers(base_config, filename),
                        )
                        if prepared.config.resume_sha256 != resume_id:
                            raise ValueError("prepared resume hash does not match upload")
                        config = apply_ai_selection_to_config(
                            prepared.config,
                            current_ai_selection(base_config),
                            provider_store,
                        )
                        profile = prepared.profile_bytes.decode("utf-8")
                        imported_at = datetime.now(UTC)
                        manual_company_sizes = company_sizes(
                            f"manual-{resume_id.removeprefix('sha256:')}"
                        )
                        company_size_future: Future[
                            CompanySizeEvidence | None
                        ] = Future()
                        company_size_start_lock = Lock()
                        company_size_started = False
                        existing_job_at_extraction: JobRecord | None = None

                        def start_company_size_lookup(
                            extracted_job: JobRecord,
                        ) -> None:
                            """Start one company lookup at extraction time, at most once."""
                            nonlocal company_size_started, existing_job_at_extraction
                            with company_size_start_lock:
                                if company_size_started:
                                    return
                                company_size_started = True
                                existing_job_at_extraction = global_jobs.find(
                                    extracted_job.canonical_job_key
                                )

                            def lookup_company_size() -> None:
                                try:
                                    lookup_job = extracted_job.model_copy(
                                        update={"machine_status": MachineStatus.ELIGIBLE},
                                        deep=True,
                                    )
                                    lookup_snapshot = Snapshot(
                                        meta=StoreMeta(data_revision=0),
                                        jobs=[lookup_job],
                                    )
                                    manual_company_sizes.apply(
                                        lookup_snapshot,
                                        config,
                                        imported_at,
                                    )
                                    company_size_future.set_result(
                                        lookup_snapshot.jobs[0].company_size
                                    )
                                except Exception as error:  # noqa: BLE001 - lookup failure must not hide the saved job
                                    company_size_future.set_exception(error)

                            thread = Thread(
                                target=lookup_company_size,
                                name="job-scan-manual-company-size",
                                daemon=True,
                            )
                            try:
                                thread.start()
                            except RuntimeError as error:
                                company_size_future.set_exception(error)

                        job = _import_job_with_progress(
                            job_url,
                            config,
                            profile,
                            imported_at,
                            progress,
                            start_company_size_lookup,
                        )
                        start_company_size_lookup(job)
                        saved_group_name = next(
                            group.name
                            for group in global_jobs.load_read_only().meta.tracker_groups
                            if group.id == UserStatus.SAVED.value
                        )
                        progress("save", f"Saving this job to {saved_group_name}.")
                        saved_job = global_jobs.upsert_with_default_status(
                            job,
                            UserStatus.SAVED,
                            resume_id=resume_id,
                            profile_hash=config.profile_sha256,
                            application_resume_filename=filename,
                            expected_job=existing_job_at_extraction,
                        )

                        def update_company_size() -> None:
                            try:
                                result = company_size_future.result()
                                if result is None:
                                    return
                                _mutate_global_or_conflict(
                                    global_jobs,
                                    _company_size_mutator(
                                        manual_company_sizes,
                                        saved_job.canonical_job_key,
                                        result,
                                        config,
                                    ),
                                )
                            except Exception:  # noqa: BLE001 - background enrichment is best-effort
                                return

                        updater = Thread(
                            target=update_company_size,
                            name="job-scan-manual-company-size-save",
                            daemon=True,
                        )
                        try:
                            updater.start()
                        except RuntimeError:
                            pass
                        return ManualImportResult(
                            job_key=saved_job.canonical_job_key,
                            result_status=saved_job.user_status,
                            resume_id=resume_id,
                        )
                except BaseException:  # noqa: TRY203 - preserve task failure for workflow state
                    raise

            state = manual_import_workflow.start(
                run_import,
                task_kind="add-job",
                task_label=job_url,
                task_key="add-job",
            )
            return state
        except ManualJobImportError as error:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                str(error),
            ) from None
        except ManualImportBusy as error:
            raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from None
        except (ResumeError, SetupError, OSError, UnicodeError, ValueError) as error:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                str(error) or "Could not prepare the uploaded resume.",
            ) from None

    @app.post(
        "/api/global-jobs/{key}/re-evaluate",
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_mutation_request)],
    )
    def reevaluate_global_job(key: str, force: bool = False) -> ManualImportState:
        job = global_jobs.find(key)
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        if (
            job.application_resume_id is None
            or job.application_resume_filename is None
        ):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Attach a resume before re-evaluating this job.",
            )
        if (
            not force
            and job.application_resume_id == job.last_evaluated_resume_id
        ):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "The resume has not changed since the last evaluation.",
                headers={"X-Job-Scan-Conflict": "resume-unchanged"},
            )
        resume_id = job.application_resume_id
        filename = job.application_resume_filename

        def execute_reevaluation(
            progress: Callable[[str, str], None],
        ) -> ManualImportResult:
            with FileRWLock(repository.paths.ai_usage_lock_file).shared():
                current_job = global_jobs.find(key)
                if current_job is None:
                    raise ManualJobImportError(
                        "This job was removed before re-evaluation started."
                    )
                if (
                    current_job.application_resume_id != resume_id
                    or current_job.application_resume_filename != filename
                ):
                    raise ManualJobImportError(
                        "The attached resume changed before re-evaluation started."
                    )
                progress("validate", "Preparing the attached resume for review.")
                resume_bytes = read_stored_resume(
                    repository.paths,
                    resume_id,
                    filename,
                )
                resume_path, _created = store_uploaded_resume(
                    repository.paths,
                    filename,
                    resume_bytes,
                )
                base_config = load_job_tracker_config()
                prepared = manual_resume_preparer(
                    resume_path,
                    uploaded_resume_answers(base_config, filename),
                )
                if prepared.config.resume_sha256 != resume_id:
                    raise ValueError("prepared resume hash does not match stored resume")
                config = apply_ai_selection_to_config(
                    prepared.config,
                    current_ai_selection(base_config),
                    provider_store,
                )
                profile = prepared.profile_bytes.decode("utf-8")
                imported_at = datetime.now(UTC)
                evaluated = _import_job_with_progress(
                    str(current_job.url),
                    config,
                    profile,
                    imported_at,
                    progress,
                    lambda _job: None,
                )
                if evaluated.last_error is not None:
                    raise ManualJobImportError(evaluated.last_error)
                progress("save", "Saving this job's new evaluation.")
                saved_job = global_jobs.save_reevaluation(
                    key,
                    evaluated,
                    resume_id=resume_id,
                    expected_job=job,
                )
                return ManualImportResult(
                    job_key=saved_job.canonical_job_key,
                    result_status=saved_job.user_status,
                    resume_id=resume_id,
                )

        def run_reevaluation(
            progress: Callable[[str, str], None],
        ) -> ManualImportResult:
            try:
                return execute_reevaluation(progress)
            except BaseException:
                try:
                    global_jobs.record_reevaluation_result(key, "failed")
                except (KeyError, OSError, ValueError):
                    pass
                raise

        try:
            return manual_import_workflow.start(
                run_reevaluation,
                task_kind="re-evaluate",
                task_label=f"{job.title} at {job.company}",
                task_key=f"re-evaluate:{job.canonical_job_key}",
                subject_key=job.canonical_job_key,
            )
        except ManualImportBusy as error:
            raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from None

    @app.post(
        "/api/global-jobs/{key}/re-evaluation-result/acknowledge",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(require_mutation_request)],
    )
    def acknowledge_global_job_reevaluation_result(
        key: str,
        acknowledgement: _ReevaluationAcknowledgement,
    ) -> Response:
        try:
            saved_job = global_jobs.acknowledge_reevaluation_result(
                key,
                acknowledgement.finished_at,
            )
        except KeyError:
            raise HTTPException(status.HTTP_404_NOT_FOUND) from None
        if saved_job.reevaluation_notice is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "A newer re-evaluation result is available.",
                headers={"X-Job-Scan-Conflict": "re-evaluation-result-changed"},
            )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post(
        "/api/ats-runs",
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_mutation_request)],
    )
    def start_ats_run(
        job_keys: Annotated[str, Form()],
    ) -> AtsRunState:
        if ats_workflow is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        try:
            payload = _AtsStartRequest.model_validate(
                {
                    "job_keys": json.loads(job_keys),
                }
            )
            jobs = selected_ats_jobs(payload.job_keys)
            resumes = selected_ats_resumes(jobs)
            with FileRWLock(repository.paths.ai_usage_lock_file).shared():
                config = selected_ats_config()
                return ats_workflow.start(
                    AtsWorkflowInput(
                        search_run_id="global",
                        resumes=resumes,
                        config=config,
                    )
                )
        except (json.JSONDecodeError, ValidationError, TypeError):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "Invalid ATS selection.",
            ) from None
        except AtsInputError as error:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                str(error),
            ) from None
        except AtsInvalidJobSelection as error:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                str(error),
            ) from None
        except AtsWorkflowBusy as error:
            raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from None
        except (KeyError, OSError, ValueError):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "The selected resume or AI configuration is unavailable.",
            ) from None

    @app.get(
        "/api/ats-runs/current",
        response_model=AtsRunState,
        responses={status.HTTP_204_NO_CONTENT: {"description": "No active ATS run"}},
    )
    def read_current_ats_run() -> AtsRunState | Response:
        if ats_workflow is None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        current = ats_workflow.read_current_run()
        if current is None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        return current

    @app.get("/api/ats-runs/{run_id}")
    def read_ats_run(run_id: str) -> AtsRunState:
        if ats_workflow is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        current = ats_workflow.read_run(run_id)
        if current is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        return current

    @app.get("/api/ats-history/{run_id}")
    def load_ats_history(run_id: str) -> AtsCheckBundle:
        if ats_history_store is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        try:
            return ats_history_store.load(run_id)
        except (KeyError, ValueError):
            raise HTTPException(status.HTTP_404_NOT_FOUND) from None

    @app.delete(
        "/api/ats-history/{run_id}",
        dependencies=[Depends(require_mutation_request)],
    )
    def delete_ats_history(run_id: str) -> dict[str, bool]:
        if ats_history_store is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        try:
            if ats_workflow is None:
                ats_history_store.delete(run_id)
            else:
                ats_workflow.delete_history(run_id)
        except AtsWorkflowBusy as error:
            raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from None
        except (KeyError, ValueError):
            raise HTTPException(status.HTTP_404_NOT_FOUND) from None
        return {"deleted": True}

    def require_known_history_job(run_id: str, key: str) -> None:
        try:
            snapshot = history.load(run_id)
        except (KeyError, ValueError):
            raise HTTPException(status.HTTP_404_NOT_FOUND) from None
        if _find_job(snapshot, key) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)

    def sync_live_history(previous: Snapshot, updated: Snapshot) -> None:
        latest = history.latest()
        if latest is None:
            return
        try:
            archived = history.load(latest.run_id)
        except KeyError:
            return
        if archived.jobs == previous.jobs:
            history.replace_snapshot(latest.run_id, updated)

    def capture_missing_snapshot(
        snapshot: Snapshot,
        key: str,
        *,
        force: bool = False,
    ) -> SourceOccurrence | None:
        """Capture one source snapshot unless this job already has one."""
        job = _find_job(snapshot, key)
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        if not force and any(
            occurrence.job_snapshot is not None
            for occurrence in job.source_occurrences
        ):
            return None
        primary = next(
            (
                occurrence
                for occurrence in job.source_occurrences
                if occurrence.source_occurrence_key
                == job.primary_source_occurrence_key
            ),
            None,
        )
        occurrence = primary or next(
            (
                item
                for item in job.source_occurrences
            ),
            None,
        )
        if occurrence is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        try:
            html = capture_source_job_snapshot_html(occurrence)
        except (BrowserSourceError, InvalidResponse):
            html = None
        if html is None:
            occurrence.job_snapshot = None
            occurrence.job_snapshot_error_code = "snapshot_capture_failed"
            return occurrence
        try:
            occurrence.job_snapshot = job_snapshots.save(
                source_job_key=occurrence.source_job_key,
                captured_at=datetime.now(UTC),
                html=html,
            )
            occurrence.job_snapshot_error_code = None
        except (OSError, RuntimeError, ValueError):
            occurrence.job_snapshot = None
            occurrence.job_snapshot_error_code = "snapshot_save_failed"
        return occurrence

    if ai_store is not None:
        discovery = ai_model_discovery or AiModelDiscovery()
        codex_process = CodexProcess(home=repository.paths.codex_home)
        codex_login = CodexLoginWorkflow(repository.paths.codex_home)
        app.router.add_event_handler("shutdown", codex_login.close)
        discover_codex_models = (
            codex_model_discovery
            or codex_process.models
        )

        @app.get("/api/ai/config", response_model=_AiConfigurationState)
        def get_ai_configuration() -> _AiConfigurationState:
            return read_ai_configuration()

        @app.put(
            "/api/ai/config",
            response_model=_AiConfigurationState,
            dependencies=[Depends(require_mutation_request)],
        )
        def update_ai_configuration(
            selection: AiRuntimeSelection,
        ) -> _AiConfigurationState:
            with write_ai_configuration():
                if selection.ai_runtime.startswith("api:"):
                    try:
                        ai_store.require(selection.ai_runtime.removeprefix("api:"))
                    except AiProviderNotFound as error:
                        raise HTTPException(
                            status.HTTP_422_UNPROCESSABLE_CONTENT,
                            str(error),
                        ) from None
                try:
                    saved = ai_selections.save(selection)
                except AiSelectionError as error:
                    raise HTTPException(
                        status.HTTP_500_INTERNAL_SERVER_ERROR,
                        str(error),
                    ) from None
                return _AiConfigurationState(
                    ai_runtime=saved.ai_runtime,
                    claude=saved.claude,
                    codex=saved.codex,
                    locked=False,
                )

        @app.get("/api/ai/codex-models")
        def list_codex_models() -> list[CodexModelOption]:
            try:
                return discover_codex_models()
            except CodexProcessError as error:
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    str(error),
                ) from None

        @app.get("/api/ai/codex-auth", response_model=_CodexAuthState)
        def get_codex_auth() -> _CodexAuthState:
            login = CodexLoginSnapshot.model_validate(codex_login.snapshot())
            if login.state in {"starting", "pending"}:
                return _CodexAuthState(authenticated=False, login=login)
            try:
                codex_process.auth_status()
            except CodexNotAuthenticated:
                return _CodexAuthState(authenticated=False, login=login)
            except CodexProcessError as error:
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    str(error),
                ) from None
            return _CodexAuthState(authenticated=True, login=login)

        @app.post(
            "/api/ai/codex-login",
            response_model=CodexLoginSnapshot,
            dependencies=[Depends(require_mutation_request)],
        )
        def start_codex_login() -> CodexLoginSnapshot:
            return CodexLoginSnapshot.model_validate(codex_login.start())

        @app.delete(
            "/api/ai/codex-login",
            response_model=CodexLoginSnapshot,
            dependencies=[Depends(require_mutation_request)],
        )
        def cancel_codex_login() -> CodexLoginSnapshot:
            return CodexLoginSnapshot.model_validate(codex_login.cancel())

        @app.get("/api/ai/providers")
        def list_ai_providers() -> list[AiProviderView]:
            try:
                return ai_store.list()
            except AiConfigError as error:
                raise HTTPException(
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                    str(error),
                ) from None

        @app.post(
            "/api/ai/providers",
            status_code=status.HTTP_201_CREATED,
            dependencies=[Depends(require_mutation_request)],
        )
        def create_ai_provider(draft: AiProviderDraft) -> AiProviderView:
            try:
                with write_ai_configuration():
                    return ai_store.create(draft)
            except AiConfigError as error:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    str(error),
                ) from None

        @app.put(
            "/api/ai/providers/{provider_id}",
            dependencies=[Depends(require_mutation_request)],
        )
        def update_ai_provider(
            provider_id: str,
            draft: AiProviderDraft,
        ) -> AiProviderView:
            try:
                with write_ai_configuration():
                    return ai_store.update(provider_id, draft)
            except AiProviderNotFound as error:
                raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from None
            except AiConfigError as error:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    str(error),
                ) from None

        @app.delete(
            "/api/ai/providers/{provider_id}",
            dependencies=[Depends(require_mutation_request)],
        )
        def delete_ai_provider(provider_id: str) -> dict[str, bool]:
            try:
                with write_ai_configuration():
                    selected = current_ai_selection()
                    if selected.ai_runtime == f"api:{provider_id}":
                        raise HTTPException(
                            status.HTTP_409_CONFLICT,
                            "Switch AI runtime before deleting its selected configuration.",
                        )
                    ai_store.delete(provider_id)
            except AiProviderNotFound as error:
                raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from None
            except AiConfigError as error:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    str(error),
                ) from None
            return {"deleted": True}

        @app.post(
            "/api/ai/models/discover",
            dependencies=[Depends(require_mutation_request)],
        )
        def discover_ai_models(
            request: _AiModelDiscoveryRequest,
        ) -> list[AiModelOption]:
            try:
                saved = (
                    ai_store.require(request.provider_id)
                    if request.provider_id is not None
                    else None
                )
                api_key = request.api_key
                if api_key is None and saved is not None:
                    if request.base_url.rstrip("/") != saved.base_url.rstrip("/"):
                        raise AiConfigError(
                            "API key is required after changing the provider URL."
                        )
                    api_key = saved.api_key
                if api_key is None:
                    raise AiConfigError("API key is required to fetch models.")
                provider = StoredAiProvider(
                    id=saved.id if saved is not None else "discovery",
                    display_name=saved.display_name if saved is not None else "Discovery",
                    base_url=request.base_url,
                    api_key=api_key,
                    model=saved.model if saved is not None else "discovery",
                    reasoning_effort=(
                        saved.reasoning_effort if saved is not None else "low"
                    ),
                )
                return discovery.discover(provider)
            except ValidationError:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    "Invalid AI provider settings.",
                ) from None
            except AiProviderNotFound as error:
                raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from None
            except (AiConfigError, AnthropicApiError) as error:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    str(error),
                ) from None

    @app.get("/api/job-snapshots/{snapshot_id}")
    def job_snapshot(snapshot_id: str) -> Response:
        """Serve one inert local HTML snapshot without contacting its source."""
        try:
            contents = job_snapshots.read(snapshot_id)
        except (OSError, ValueError):
            raise HTTPException(status.HTTP_404_NOT_FOUND) from None
        return Response(
            content=contents,
            media_type="text/html",
            headers={
                "Cache-Control": "private, max-age=31536000, immutable",
                "Content-Security-Policy": (
                    "sandbox; default-src 'none'; img-src data:; "
                    "style-src 'unsafe-inline'; font-src data:; "
                    "form-action 'none'; base-uri 'none'"
                ),
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post(
        "/api/jobs/{key}/snapshot",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(require_mutation_request)],
    )
    def generate_job_snapshot(key: str, force: bool = False) -> Response:
        try:
            with FileRWLock(repository.paths.workflow_lock_file).exclusive(
                blocking=False
            ), FileRWLock(repository.paths.scan_lock_file).exclusive(
                blocking=False
            ):
                previous = repository.load()
                working = previous.model_copy(deep=True)
                occurrence = capture_missing_snapshot(working, key, force=force)
                if occurrence is not None:
                    updated = _mutate_or_conflict(
                        repository,
                        _job_snapshot_mutator(key, occurrence, replace_existing=force),
                    )
                    sync_live_history(previous, updated)
        except LockUnavailable:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "A scan is running; retry the snapshot after it completes.",
            ) from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post(
        "/api/scan-history/{run_id}/jobs/{key}/snapshot",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(require_mutation_request)],
    )
    def generate_history_job_snapshot(
        run_id: str,
        key: str,
        force: bool = False,
    ) -> Response:
        try:
            with FileRWLock(repository.paths.workflow_lock_file).exclusive(
                blocking=False
            ), FileRWLock(repository.paths.scan_lock_file).exclusive(
                blocking=False
            ):
                try:
                    working = history.load(run_id)
                except (KeyError, ValueError):
                    raise HTTPException(status.HTTP_404_NOT_FOUND) from None
                occurrence = capture_missing_snapshot(working, key, force=force)
                if occurrence is not None:
                    _mutate_history_or_conflict(
                        history,
                        run_id,
                        _job_snapshot_mutator(
                            key,
                            occurrence,
                            replace_existing=force,
                        ),
                    )
        except LockUnavailable:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "A scan is running; retry the snapshot after it completes.",
            ) from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post(
        "/api/global-jobs/{key}/snapshot",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(require_mutation_request)],
    )
    def generate_global_job_snapshot(key: str, force: bool = False) -> Response:
        working = global_jobs.load()
        occurrence = capture_missing_snapshot(working, key, force=force)
        if occurrence is not None:
            _mutate_global_or_conflict(
                global_jobs,
                _job_snapshot_mutator(
                    key,
                    occurrence,
                    replace_existing=force,
                ),
            )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    if workflow is not None:

        @app.get("/setup", response_class=HTMLResponse)
        def setup_console(
            run_id: str | None = None,
            ats_run_id: str | None = None,
        ) -> HTMLResponse:
            try:
                providers = ai_store.list() if ai_store is not None else []
            except AiConfigError as error:
                raise HTTPException(
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                    str(error),
                ) from None
            try:
                raw_snapshot = (
                    history.load(run_id) if run_id is not None else repository.load()
                )
                entries = history.list()
                try:
                    global_snapshot = global_jobs.load_for_tracker()
                except (OSError, UnicodeError, ValueError):
                    global_snapshot = Snapshot(meta=StoreMeta(data_revision=0))
                    snapshot = raw_snapshot
                else:
                    snapshot = filter_untracked_jobs(
                        raw_snapshot,
                        global_snapshot,
                    )
                ats_entries = ats_history_store.list() if ats_history_store is not None else []
                if ats_run_id is not None:
                    if ats_history_store is None:
                        raise KeyError(ats_run_id)
                    selected_ats = ats_history_store.load(ats_run_id)
                elif ats_history_store is not None and ats_entries:
                    selected_ats = ats_history_store.load(ats_entries[0].run_id)
                else:
                    selected_ats = None
            except (KeyError, ValueError):
                raise HTTPException(status.HTTP_404_NOT_FOUND) from None
            setup_answers = workflow.load_setup_answers()
            selection = current_ai_selection()
            if (
                setup_answers is not None
                and not repository.paths.ai_selection_toml.exists()
                and not repository.paths.config_toml.exists()
            ):
                selection_values: dict[str, object] = {
                    "ai_runtime": setup_answers.ai_runtime
                }
                if setup_answers.ai_runtime == "codex-cli":
                    selection_values["codex"] = CodexRuntimeSelection(
                        model=setup_answers.claude.model,
                        effort=setup_answers.claude.effort,
                    )
                else:
                    selection_values["claude"] = (
                        claude_runtime_selection_from_settings(setup_answers.claude)
                    )
                selection = AiRuntimeSelection.model_validate(selection_values)
            response = HTMLResponse(
                render_console(
                    snapshot=snapshot,
                    global_snapshot=global_snapshot,
                    setup_answers=setup_answers,
                    ai_selection=selection,
                    ai_providers=providers,
                    scan_history=entries,
                    selected_run_id=run_id,
                    ats_history=ats_entries,
                    selected_ats=selected_ats,
                )
            )
            response.set_cookie(
                _SESSION_COOKIE,
                token,
                httponly=True,
                samesite="strict",
                path="/",
            )
            return response

        @app.post(
            "/api/resume-suggestions",
            dependencies=[Depends(require_mutation_request)],
        )
        def suggest_resume_search_inputs(
            settings: Annotated[str, Form()],
            resume: Annotated[UploadFile, File()],
        ) -> ResumeSuggestions:
            try:
                parsed = ResumeSuggestionSettings.model_validate_json(settings)
            except (ValidationError, ValueError):
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    "Invalid AI suggestion settings.",
                ) from None
            try:
                with FileRWLock(repository.paths.ai_usage_lock_file).shared():
                    return resume_suggestions.suggest(
                        resume.filename or "",
                        resume.file.read(),
                        apply_selection_to_suggestions(parsed),
                    )
            except ResumeSuggestionError as error:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    str(error),
                ) from None

        @app.post(
            "/api/setup-and-scan",
            status_code=status.HTTP_202_ACCEPTED,
            dependencies=[Depends(require_mutation_request)],
        )
        def setup_and_scan(
            settings: Annotated[str, Form()],
            resume: Annotated[UploadFile, File()],
        ) -> WebRunState:
            try:
                answers = SetupAnswers.model_validate_json(settings)
            except (ValidationError, ValueError):
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    "Invalid setup settings.",
                ) from None
            try:
                with FileRWLock(repository.paths.ai_usage_lock_file).shared():
                    return workflow.start(
                        resume.filename or "",
                        resume.file.read(),
                        apply_selection_to_setup(answers),
                    )
            except WebWorkflowBusy as error:
                raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from None
            except OSError as error:
                raise HTTPException(
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                    str(error) or "Setup or scan failed.",
                ) from None

        @app.get(
            "/api/setup-and-scan/current",
            response_model=WebRunState,
            responses={status.HTTP_204_NO_CONTENT: {"description": "No active run"}},
        )
        def read_current_setup_and_scan() -> WebRunState | Response:
            current = workflow.read_current_run()
            if current is None:
                return Response(status_code=status.HTTP_204_NO_CONTENT)
            return current

        @app.get("/api/setup-and-scan/{run_id}")
        def read_setup_and_scan(run_id: str) -> WebRunState:
            current = workflow.read_run(run_id)
            if current is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND)
            return current

        @app.get("/api/schedule")
        def read_schedule_state() -> WebScheduleState:
            try:
                state = workflow.schedule_status()
            except (OSError, ValueError, SchedulerError) as error:
                raise HTTPException(
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                    str(error) or "Could not read schedule.",
                ) from None
            return WebScheduleState(
                installed=state.installed,
                local_time=state.local_time,
            )

        @app.post(
            "/api/schedule",
            dependencies=[Depends(require_mutation_request)],
        )
        def save_schedule(
            settings: Annotated[str, Form()],
            resume: Annotated[UploadFile, File()],
        ) -> WebScheduleState:
            try:
                answers = SetupAnswers.model_validate_json(settings)
            except (ValidationError, ValueError):
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    "Invalid setup settings.",
                ) from None
            if answers.scheduler.local_time is None:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    "Daily scan time is required.",
                )
            try:
                with FileRWLock(repository.paths.ai_usage_lock_file).shared():
                    state = workflow.save_schedule(
                        resume.filename or "",
                        read_resume_upload(resume.file),
                        apply_selection_to_setup(answers),
                    )
            except WebWorkflowBusy as error:
                raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from None
            except (SetupError, ResumeError, ClaudeProcessError, SchedulerError) as error:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    str(error),
                ) from None
            except OSError as error:
                raise HTTPException(
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                    str(error) or "Could not save daily scan.",
                ) from None
            return WebScheduleState(
                installed=state.installed,
                local_time=state.local_time,
            )

        @app.delete(
            "/api/schedule",
            dependencies=[Depends(require_mutation_request)],
        )
        def delete_schedule() -> WebScheduleState:
            try:
                state = workflow.remove_schedule()
            except WebWorkflowBusy as error:
                raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from None
            except (OSError, ValueError, SchedulerError) as error:
                raise HTTPException(
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                    str(error) or "Could not delete schedule.",
                ) from None
            return WebScheduleState(
                installed=state.installed,
                local_time=state.local_time,
            )

    @app.get("/api/scan-history/{run_id}/resume")
    def download_history_resume(run_id: str) -> Response:
        try:
            filename, contents = history.read_resume(run_id)
        except (KeyError, ValueError):
            raise HTTPException(status.HTTP_404_NOT_FOUND) from None
        encoded = quote(filename, safe="")
        return Response(
            contents,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{encoded}",
            },
        )

    @app.delete(
        "/api/scan-history/{run_id}",
        dependencies=[Depends(require_mutation_request)],
    )
    def delete_search_history(run_id: str) -> dict[str, bool]:
        if workflow is not None:
            is_busy = getattr(workflow, "is_busy", None)
            busy = (
                is_busy()
                if callable(is_busy)
                else bool(getattr(workflow, "busy", False))
            )
            if busy:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "A setup or scan is running; retry deletion after it completes.",
                )
        try:
            with FileRWLock(repository.paths.workflow_lock_file).exclusive(
                blocking=False
            ), FileRWLock(repository.paths.scan_lock_file).exclusive(
                blocking=False
            ), history.delete_transaction(run_id) as deleted_latest:
                if deleted_latest:
                    live_paths = [
                        repository.paths.jobs_jsonl,
                        repository.paths.dashboard_html,
                    ]
                    with (
                        repository.lock.exclusive(),
                        _quarantine_files(live_paths),
                    ):
                        pass
        except LockUnavailable:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "A scan is running; retry deletion after it completes.",
            ) from None
        except (KeyError, ValueError):
            raise HTTPException(status.HTTP_404_NOT_FOUND) from None
        return {"deleted_latest": deleted_latest}

    @app.post(
        "/api/jobs/{key}/company-size",
        response_model=CompanySizeEvidence,
        dependencies=[Depends(require_mutation_request)],
    )
    def refresh_job_company_size(key: str) -> CompanySizeEvidence:
        service = company_sizes()
        try:
            with FileRWLock(repository.paths.workflow_lock_file).exclusive(
                blocking=False
            ), FileRWLock(repository.paths.scan_lock_file).exclusive(blocking=False):
                previous = repository.load()
                job = _find_job(previous, key)
                if job is None:
                    raise HTTPException(status.HTTP_404_NOT_FOUND)
                config = company_size_config()
                result = service.lookup_for_job(job, config, datetime.now(UTC))
                updated = _mutate_or_conflict(
                    repository,
                    _company_size_mutator(service, key, result, config),
                )
                sync_live_history(previous, updated)
        except LockUnavailable:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "A scan is running; retry the company-size search after it completes.",
            ) from None
        except CompanySizeLookupError as error:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                str(error),
            ) from None
        except CompanySizeStoreError:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "Could not save the company-size result.",
            ) from None
        return result

    @app.post(
        "/api/scan-history/{run_id}/jobs/{key}/company-size",
        response_model=CompanySizeEvidence,
        dependencies=[Depends(require_mutation_request)],
    )
    def refresh_history_job_company_size(
        run_id: str,
        key: str,
    ) -> CompanySizeEvidence:
        service = company_sizes(run_id)
        try:
            with FileRWLock(repository.paths.workflow_lock_file).exclusive(
                blocking=False
            ), FileRWLock(repository.paths.scan_lock_file).exclusive(blocking=False):
                try:
                    snapshot = history.load(run_id)
                except (KeyError, ValueError):
                    raise HTTPException(status.HTTP_404_NOT_FOUND) from None
                job = _find_job(snapshot, key)
                if job is None:
                    raise HTTPException(status.HTTP_404_NOT_FOUND)
                config = company_size_config(run_id)
                result = service.lookup_for_job(job, config, datetime.now(UTC))
                _mutate_history_or_conflict(
                    history,
                    run_id,
                    _company_size_mutator(service, key, result, config),
                )
        except LockUnavailable:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "A scan is running; retry the company-size search after it completes.",
            ) from None
        except CompanySizeLookupError as error:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                str(error),
            ) from None
        except CompanySizeStoreError:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "Could not save the company-size result.",
            ) from None
        return result

    @app.post(
        "/api/global-jobs/{key}/company-size",
        response_model=CompanySizeEvidence,
        dependencies=[Depends(require_mutation_request)],
    )
    def refresh_global_job_company_size(key: str) -> CompanySizeEvidence:
        service = company_sizes()
        try:
            snapshot = global_jobs.load()
            job = _find_job(snapshot, key)
            if job is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND)
            config = company_size_config(job_tracker=True)
            result = service.lookup_for_job(job, config, datetime.now(UTC))
            _mutate_global_or_conflict(
                global_jobs,
                _company_size_mutator(service, key, result, config),
            )
        except CompanySizeLookupError as error:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                str(error),
            ) from None
        except CompanySizeStoreError:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "Could not save the company-size result.",
            ) from None
        return result

    @app.get("/api/global-jobs/{key}/resume")
    def download_global_job_resume(key: str) -> Response:
        job = global_jobs.find(key)
        if (
            job is None
            or job.application_resume_id is None
            or job.application_resume_filename is None
        ):
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        try:
            resume_bytes = read_stored_resume(
                repository.paths,
                job.application_resume_id,
                job.application_resume_filename,
            )
        except (ResumeError, OSError, ValueError):
            raise HTTPException(status.HTTP_404_NOT_FOUND) from None
        encoded = quote(job.application_resume_filename, safe="")
        return Response(
            resume_bytes,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{encoded}",
            },
        )

    @app.post(
        "/api/global-jobs/{key}/resume",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(require_mutation_request)],
    )
    def replace_global_job_resume(
        key: str,
        resume: Annotated[UploadFile, File()],
    ) -> Response:
        resume_path: Path | None = None
        resume_id: str | None = None
        try:
            filename = Path(resume.filename or "").name
            resume_bytes = read_resume_upload(resume.file)
            job = global_jobs.find(key)
            if job is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND)
            resume_path, _created = store_uploaded_resume(
                repository.paths,
                filename,
                resume_bytes,
            )
            resume_id = f"sha256:{resume_path.stem}"
            global_jobs.set_application_resume(job, resume_id, filename)
        except HTTPException:
            raise
        except (ResumeError, OSError, ValueError) as error:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                str(error) or "Could not save the uploaded resume.",
            ) from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.delete(
        "/api/global-jobs/{key}",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(require_mutation_request)],
    )
    def delete_global_job(key: str) -> Response:
        try:
            global_jobs.delete(key)
        except KeyError:
            raise HTTPException(status.HTTP_404_NOT_FOUND) from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post(
        "/api/tracker-groups",
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(require_mutation_request)],
    )
    def create_tracker_group(mutation: _TrackerGroupMutation) -> dict[str, str]:
        try:
            group = global_jobs.create_group(mutation.name)
        except ValueError as error:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                str(error),
            ) from None
        return {"id": group.id, "name": group.name}

    @app.put(
        "/api/tracker-groups/{group_id}",
        dependencies=[Depends(require_mutation_request)],
    )
    def rename_tracker_group(
        group_id: str,
        mutation: _TrackerGroupMutation,
    ) -> dict[str, str]:
        try:
            group = global_jobs.rename_group(group_id, mutation.name)
        except KeyError:
            raise HTTPException(status.HTTP_404_NOT_FOUND) from None
        except ValueError as error:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                str(error),
            ) from None
        return {"id": group.id, "name": group.name}

    @app.delete(
        "/api/tracker-groups/{group_id}",
        dependencies=[Depends(require_mutation_request)],
    )
    def delete_tracker_group(
        group_id: str,
        mutation: _TrackerGroupDeletion,
    ) -> dict[str, int]:
        try:
            deleted_jobs = global_jobs.delete_group(
                group_id,
                confirmation_name=mutation.confirmation_name,
            )
        except KeyError:
            raise HTTPException(status.HTTP_404_NOT_FOUND) from None
        except ValueError as error:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                str(error),
            ) from None
        return {"deleted_jobs": deleted_jobs}

    @app.post(
        "/api/job-tracker/jobs/batch-status",
        dependencies=[Depends(require_mutation_request)],
    )
    def set_batch_job_status(mutation: _BatchStatusMutation) -> dict[str, int]:
        try:
            global_jobs.set_status_many(mutation.keys, mutation.status)
        except KeyError:
            raise HTTPException(status.HTTP_404_NOT_FOUND) from None
        except ValueError as error:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                str(error),
            ) from None
        return {"updated_jobs": len(mutation.keys)}

    @app.delete(
        "/api/job-tracker/jobs/batch",
        dependencies=[Depends(require_mutation_request)],
    )
    def delete_batch_jobs(mutation: _BatchDeletionMutation) -> dict[str, int]:
        try:
            deleted_jobs = global_jobs.delete_many(
                mutation.keys,
                confirmation_text=mutation.confirmation_text,
            )
        except KeyError:
            raise HTTPException(status.HTTP_404_NOT_FOUND) from None
        except ValueError as error:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                str(error),
            ) from None
        return {"deleted_jobs": deleted_jobs}

    @app.post(
        "/api/jobs/{key}/status",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(require_mutation_request)],
    )
    def set_job_status(key: str, mutation: _StatusMutation) -> Response:
        try:
            with FileRWLock(repository.paths.workflow_lock_file).exclusive(
                blocking=False
            ), FileRWLock(repository.paths.scan_lock_file).exclusive(
                blocking=False
            ):
                job = _find_job(repository.load(), key)
                if job is None:
                    raise HTTPException(status.HTTP_404_NOT_FOUND)
                save_review_status(job, mutation.status)
        except LockUnavailable:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "A scan is running; retry the status change after it completes.",
            ) from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post(
        "/api/jobs/{key}/restore",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(require_mutation_request)],
    )
    def restore_job(key: str) -> Response:
        try:
            with FileRWLock(repository.paths.workflow_lock_file).exclusive(
                blocking=False
            ), FileRWLock(repository.paths.scan_lock_file).exclusive(
                blocking=False
            ):
                previous = repository.load()
                if _find_job(previous, key) is None:
                    raise HTTPException(status.HTTP_404_NOT_FOUND)
                updated = _mutate_or_conflict(repository, _restore_mutator(key))
                sync_live_history(previous, updated)
        except LockUnavailable:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "A scan is running; retry restore after it completes.",
            ) from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post(
        "/api/scan-history/{run_id}/jobs/{key}/status",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(require_mutation_request)],
    )
    def set_history_job_status(
        run_id: str,
        key: str,
        mutation: _StatusMutation,
    ) -> Response:
        try:
            with FileRWLock(repository.paths.workflow_lock_file).exclusive(
                blocking=False
            ), FileRWLock(repository.paths.scan_lock_file).exclusive(
                blocking=False
            ):
                try:
                    snapshot = history.load(run_id)
                except (KeyError, ValueError):
                    raise HTTPException(status.HTTP_404_NOT_FOUND) from None
                job = _find_job(snapshot, key)
                if job is None:
                    raise HTTPException(status.HTTP_404_NOT_FOUND)
                save_review_status(job, mutation.status, run_id)
        except LockUnavailable:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "A scan is running; retry the status change after it completes.",
            ) from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post(
        "/api/global-jobs/{key}/status",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(require_mutation_request)],
    )
    def set_global_job_status(key: str, mutation: _StatusMutation) -> Response:
        try:
            job = global_jobs.find(key)
            if job is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND)
            global_jobs.set_status(job, mutation.status)
        except ValueError as error:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                str(error),
            ) from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post(
        "/api/global-jobs/{key}/facts",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(require_mutation_request)],
    )
    def set_global_job_fact(key: str, mutation: _ManualFactMutation) -> Response:
        field_name, value = next(
            (name, value)
            for name, value in (
                ("posted_at", mutation.posted_at),
                ("company_size", mutation.company_size),
                ("company_industry", mutation.company_industry),
            )
            if value is not None
        )
        job = global_jobs.find(key)
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        try:
            global_jobs.set_manual_fact(job, field_name, value)
        except KeyError:
            raise HTTPException(status.HTTP_404_NOT_FOUND) from None
        except ValueError as error:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                str(error),
            ) from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post(
        "/api/global-jobs/{key}/lifecycle/{event_index}/date",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(require_mutation_request)],
    )
    def set_global_job_lifecycle_date(
        key: str,
        event_index: int,
        mutation: _LifecycleDateMutation,
    ) -> Response:
        job = global_jobs.find(key)
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        try:
            global_jobs.set_status_date(
                job,
                event_index,
                mutation.changed_on,
            )
        except (KeyError, IndexError):
            raise HTTPException(status.HTTP_404_NOT_FOUND) from None
        except ValueError as error:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                str(error),
            ) from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.delete(
        "/api/global-jobs/{key}/lifecycle/{event_index}",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(require_mutation_request)],
    )
    def delete_global_job_lifecycle_event(
        key: str,
        event_index: int,
    ) -> Response:
        job = global_jobs.find(key)
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        try:
            global_jobs.delete_status_event(job, event_index)
        except (KeyError, IndexError):
            raise HTTPException(status.HTTP_404_NOT_FOUND) from None
        except ValueError as error:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                str(error),
            ) from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post(
        "/api/global-jobs/{key}/salary",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(require_mutation_request)],
    )
    def set_global_job_salary(key: str, mutation: _SalaryMutation) -> Response:
        job = global_jobs.find(key)
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        global_jobs.set_salaries(
            job,
            expected_salary=(
                SalaryValue(
                    amount=mutation.expected_salary,
                    period=mutation.expected_salary_period,
                )
                if mutation.expected_salary
                else None
            ),
            offer_salary=(
                SalaryValue(
                    amount=mutation.offer_salary,
                    period=mutation.offer_salary_period,
                )
                if mutation.offer_salary
                else None
            ),
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post(
        "/api/global-jobs/{key}/notes",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(require_mutation_request)],
    )
    def add_global_job_note(key: str, mutation: _JobNoteMutation) -> Response:
        job = global_jobs.find(key)
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        try:
            global_jobs.add_note(job, mutation.content)
        except KeyError:
            raise HTTPException(status.HTTP_404_NOT_FOUND) from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.put(
        "/api/global-jobs/{key}/notes/{note_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(require_mutation_request)],
    )
    def edit_global_job_note(
        key: str,
        note_id: uuid.UUID,
        mutation: _JobNoteMutation,
    ) -> Response:
        job = global_jobs.find(key)
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        try:
            global_jobs.edit_note(job, note_id, mutation.content)
        except KeyError:
            raise HTTPException(status.HTTP_404_NOT_FOUND) from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.delete(
        "/api/global-jobs/{key}/notes/{note_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(require_mutation_request)],
    )
    def delete_global_job_note(key: str, note_id: uuid.UUID) -> Response:
        job = global_jobs.find(key)
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        try:
            global_jobs.delete_note(job, note_id)
        except KeyError:
            raise HTTPException(status.HTTP_404_NOT_FOUND) from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post(
        "/api/global-jobs/{key}/tags",
        dependencies=[Depends(require_mutation_request)],
    )
    def add_global_job_user_tag(
        key: str,
        mutation: _UserTagMutation,
    ) -> dict[str, str]:
        job = global_jobs.find(key)
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        try:
            tag = global_jobs.add_user_tag(job, mutation.name, mutation.color)
        except KeyError:
            raise HTTPException(status.HTTP_404_NOT_FOUND) from None
        return {"name": tag.name, "color": tag.color}

    @app.delete(
        "/api/global-jobs/{key}/tags",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(require_mutation_request)],
    )
    def delete_global_job_user_tag(
        key: str,
        mutation: _UserTagDeletion,
    ) -> Response:
        job = global_jobs.find(key)
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        try:
            global_jobs.delete_user_tag(job, mutation.name)
        except KeyError:
            raise HTTPException(status.HTTP_404_NOT_FOUND) from None
        except ValueError as error:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                str(error),
            ) from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post(
        "/api/scan-history/{run_id}/jobs/{key}/restore",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(require_mutation_request)],
    )
    def restore_history_job(run_id: str, key: str) -> Response:
        try:
            with FileRWLock(repository.paths.workflow_lock_file).exclusive(
                blocking=False
            ), FileRWLock(repository.paths.scan_lock_file).exclusive(
                blocking=False
            ):
                require_known_history_job(run_id, key)
                _mutate_history_or_conflict(
                    history,
                    run_id,
                    _restore_mutator(key),
                )
        except LockUnavailable:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "A scan is running; retry restore after it completes.",
            ) from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    _migrate_legacy_job_resumes(repository.paths, global_jobs)
    return app


def _migrate_legacy_job_resumes(paths: AppPaths, global_jobs: GlobalJobStore) -> None:
    """Move referenced legacy resume files into job attachments, then delete the catalog."""
    legacy_root = paths.root / "global-resumes"
    if not legacy_root.is_dir():
        return
    for job in global_jobs.load().jobs:
        if (
            job.application_resume_id is None
            or job.application_resume_filename is not None
        ):
            continue
        digest = job.application_resume_id.removeprefix("sha256:")
        legacy_entry = legacy_root / digest
        try:
            manifest = json.loads(
                (legacy_entry / "manifest.json").read_text(encoding="utf-8")
            )
            filename = manifest["filename"]
            if not isinstance(filename, str) or Path(filename).name != filename:
                raise ValueError("invalid legacy resume filename")
            resume_bytes = (legacy_entry / "resume").read_bytes()
            resume_path, _created = store_uploaded_resume(paths, filename, resume_bytes)
            if f"sha256:{resume_path.stem}" != job.application_resume_id:
                raise ValueError("legacy resume hash does not match its job")
        except (KeyError, OSError, TypeError, ValueError, ResumeError):
            global_jobs.set_application_resume(job, None)
            continue
        global_jobs.set_application_resume(
            job,
            job.application_resume_id,
            filename,
        )
    shutil.rmtree(legacy_root)
    (paths.root / ".global-resumes.lock").unlink(missing_ok=True)


@contextmanager
def _quarantine_files(paths: list[Path]) -> Iterator[None]:
    """Hide related live files together and restore them on any failure."""
    moved: list[tuple[Path, Path]] = []
    directories: set[Path] = set()
    try:
        for path in dict.fromkeys(paths):
            if not path.exists():
                continue
            tombstone = path.parent / f".deleted.{path.name}.{uuid.uuid4().hex}"
            os.replace(path, tombstone)
            moved.append((path, tombstone))
            directories.add(path.parent)
        for directory in directories:
            _fsync_directory(directory)
        yield
    except BaseException:
        for path, tombstone in reversed(moved):
            if tombstone.exists():
                os.replace(tombstone, path)
        for directory in directories:
            _fsync_directory(directory)
        raise
    else:
        for _path, tombstone in moved:
            try:
                tombstone.unlink(missing_ok=True)
            except OSError:
                pass
        for directory in directories:
            try:
                _fsync_directory(directory)
            except OSError:
                # Visible state is already committed. A failed durability flush
                # must not roll history back into a half-deleted user-visible state.
                pass


def _fsync_directory(path: Path) -> None:
    """Persist a directory's file-name changes."""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _find_job(snapshot: Snapshot, key: str) -> JobRecord | None:
    """Find one job by canonical key in a loaded snapshot."""
    return next(
        (job for job in snapshot.jobs if job.canonical_job_key == key),
        None,
    )


def _mutate_or_conflict(
    repository: JsonlRepository,
    mutator: Callable[[Snapshot], Snapshot],
) -> Snapshot:
    """Run one locked repository mutation and map disappearance to HTTP 409."""
    try:
        return repository.mutate(mutator)
    except _JobDisappeared:
        raise HTTPException(status.HTTP_409_CONFLICT) from None


def _mutate_history_or_conflict(
    history: SearchHistoryStore,
    run_id: str,
    mutator: Callable[[Snapshot], Snapshot],
) -> Snapshot:
    try:
        return history.mutate(run_id, mutator)
    except KeyError:
        raise HTTPException(status.HTTP_409_CONFLICT) from None
    except _JobDisappeared:
        raise HTTPException(status.HTTP_409_CONFLICT) from None


def _mutate_global_or_conflict(
    global_jobs: GlobalJobStore,
    mutator: Callable[[Snapshot], Snapshot],
) -> Snapshot:
    """Run one locked global mutation and map disappearance to HTTP 409."""
    try:
        return global_jobs.mutate_details(mutator)
    except _JobDisappeared:
        raise HTTPException(status.HTTP_409_CONFLICT) from None


def _status_mutator(
    key: str,
    user_status: UserStatus,
) -> Callable[[Snapshot], Snapshot]:
    def mutate(snapshot: Snapshot) -> Snapshot:
        job = _find_job(snapshot, key)
        if job is None:
            raise _JobDisappeared
        job.user_status = user_status
        job.user_status_updated_at = datetime.now(UTC)
        return snapshot

    return mutate


def _restore_mutator(key: str) -> Callable[[Snapshot], Snapshot]:
    def mutate(snapshot: Snapshot) -> Snapshot:
        job = _find_job(snapshot, key)
        if job is None:
            raise _JobDisappeared
        if job.machine_status is not MachineStatus.EXCLUDED:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT)
        if job.last_successful_review_profile_hash is None:
            raise HTTPException(status.HTTP_409_CONFLICT)
        job.manual_override = "show"
        job.manual_override_content_hash = job.content_hash
        job.manual_override_profile_hash = job.last_successful_review_profile_hash
        return snapshot

    return mutate


def _job_snapshot_mutator(
    key: str,
    captured: SourceOccurrence,
    *,
    replace_existing: bool = False,
) -> Callable[[Snapshot], Snapshot]:
    """Write one captured occurrence state, replacing when requested."""
    def mutate(snapshot: Snapshot) -> Snapshot:
        job = _find_job(snapshot, key)
        if job is None:
            raise _JobDisappeared
        if not replace_existing and any(
            occurrence.job_snapshot is not None
            for occurrence in job.source_occurrences
        ):
            return snapshot
        occurrence = next(
            (
                item
                for item in job.source_occurrences
                if item.source_occurrence_key == captured.source_occurrence_key
            ),
            None,
        )
        if occurrence is None:
            raise _JobDisappeared
        occurrence.job_snapshot = (
            captured.job_snapshot.model_copy(deep=True)
            if captured.job_snapshot is not None
            else None
        )
        occurrence.job_snapshot_error_code = captured.job_snapshot_error_code
        return snapshot

    return mutate


def _company_size_mutator(
    service: CompanySizeService,
    key: str,
    result: CompanySizeEvidence,
    config: AppConfig,
) -> Callable[[Snapshot], Snapshot]:
    def mutate(snapshot: Snapshot) -> Snapshot:
        try:
            service.apply_refreshed(snapshot, key, result, config)
        except KeyError:
            raise _JobDisappeared from None
        return snapshot

    return mutate
