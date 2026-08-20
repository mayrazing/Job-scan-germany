from __future__ import annotations

import json
import os
import secrets
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
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
from pydantic import BaseModel, Field, ValidationError, field_validator

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
    ai_selection_from_config,
    apply_ai_selection_to_claude,
    apply_ai_selection_to_config,
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
    AtsWorkflow,
    AtsWorkflowBusy,
    AtsWorkflowInput,
)
from job_scan.company_size import (
    AiCompanySizeLookup,
    CompanySizeEvidence,
    CompanySizeLookupError,
    CompanySizeService,
    CompanySizeStore,
    CompanySizeStoreError,
)
from job_scan.config import AppConfig, load_config, load_config_bytes, serialize_config
from job_scan.dashboard.render import render_console, render_dashboard
from job_scan.domain import JobRecord, MachineStatus, Snapshot, StoreMeta, UserStatus
from job_scan.global_jobs import GLOBAL_USER_STATUSES, GlobalJobStore
from job_scan.locking import FileRWLock, LockUnavailable
from job_scan.manual_job_import import (
    AiJobExtractor,
    ManualJobImportError,
    ManualJobImportService,
    OpenCliPageReader,
    require_public_job_url,
)
from job_scan.repository import JsonlRepository
from job_scan.resume import ResumeError
from job_scan.resume_catalog import ResumeCatalogEntry, ResumeCatalogStore
from job_scan.resume_suggestions import (
    ResumeSuggestionError,
    ResumeSuggestions,
    ResumeSuggestionService,
    ResumeSuggestionSettings,
)
from job_scan.reviewer import ClaudeReviewer
from job_scan.scheduler import SchedulerError
from job_scan.search_history import SearchHistoryEntry, SearchHistoryStore
from job_scan.setup_service import (
    SetupAnswers,
    SetupError,
    SetupPreparation,
    SetupService,
)
from job_scan.web_workflow import (
    WebRunState,
    WebScheduleState,
    WebWorkflow,
    WebWorkflowBusy,
    read_resume_upload,
    store_uploaded_resume,
)

_SESSION_COOKIE = "job_scan_session"


class _StatusMutation(BaseModel):
    status: UserStatus

    @field_validator("status")
    @classmethod
    def reject_new_status(cls, value: UserStatus) -> UserStatus:
        """Keep New as an automatic state, never a user-selected state."""
        if value not in GLOBAL_USER_STATUSES:
            raise ValueError("New is not a selectable user status")
        return value


class _AiModelDiscoveryRequest(BaseModel):
    provider_id: str | None = None
    base_url: str
    api_key: str | None = None


class _AtsStartRequest(BaseModel):
    search_run_id: str | None = None
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
    locked: bool


class _ManualJobImportRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2083)
    run_id: str | None = Field(default=None, min_length=1, max_length=100)
    resume_id: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )


class _ManualJobImportResponse(BaseModel):
    job_key: str
    status: UserStatus


class _ManualJobImportWithResumeResponse(_ManualJobImportResponse):
    resume_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


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
    history_store: SearchHistoryStore | None = None,
    resume_suggestion_service: ResumeSuggestionService | None = None,
    ats_workflow: AtsWorkflow | None = None,
    ats_history_store: AtsHistoryStore | None = None,
    global_job_store: GlobalJobStore | None = None,
    resume_catalog_store: ResumeCatalogStore | None = None,
    manual_job_importer: Callable[
        [str, AppConfig, str, datetime], JobRecord
    ]
    | None = None,
    manual_resume_preparer: Callable[[Path, SetupAnswers], SetupPreparation]
    | None = None,
    company_size_service: CompanySizeService | None = None,
    current_lan_origin: Callable[[], str | None] | None = None,
) -> FastAPI:
    """Create the local review HTTP application."""
    app = FastAPI()
    allowed_hosts = frozenset(urlsplit(origin).netloc for origin in allowed_origins)
    history = history_store or SearchHistoryStore(repository.paths)
    global_jobs = global_job_store or GlobalJobStore(repository.paths)
    resume_catalog = resume_catalog_store or ResumeCatalogStore(repository.paths)
    resume_suggestions = resume_suggestion_service or ResumeSuggestionService(
        AiRuntimeInvoker(repository.paths)
    )
    company_size_invoker = AiRuntimeInvoker(repository.paths)
    ai_selections = AiSelectionStore(repository.paths.ai_selection_toml)
    provider_store = ai_store or AiProviderStore(repository.paths.ai_config_toml)
    if manual_job_importer is None:
        manual_job_importer = ManualJobImportService(
            OpenCliPageReader(),
            AiJobExtractor(company_size_invoker),
            ClaudeReviewer(company_size_invoker),
        ).import_url
    if manual_resume_preparer is None:
        manual_resume_preparer = SetupService(repository.paths).prepare

    def register_current_resume() -> None:
        """Copy the current setup profile into the global resume catalog."""
        try:
            config = load_config(repository.paths.config_toml)
            profile_bytes = repository.paths.profile_md.read_bytes()
            resume_bytes = (
                config.resume_path.read_bytes() if config.resume_path.is_file() else None
            )
            created_at = datetime.fromtimestamp(
                max(
                    repository.paths.config_toml.stat().st_mtime,
                    repository.paths.profile_md.stat().st_mtime,
                ),
                UTC,
            )
            resume_catalog.register(
                resume_id=config.resume_sha256,
                profile_hash=config.profile_sha256,
                candidate_name=config.candidate_name or config.resume_path.stem or "Candidate",
                filename=config.resume_path.name,
                profile_bytes=profile_bytes,
                config_bytes=repository.paths.config_toml.read_bytes(),
                resume_bytes=resume_bytes,
                created_at=created_at,
            )
        except (OSError, UnicodeError, ValueError, ValidationError):
            pass

    def register_history_resumes() -> None:
        """Copy completed-search resume bundles into the global resume catalog."""
        for entry in history.list():
            try:
                review_input = history.read_review_input(entry.run_id)
                filename, resume_bytes = history.read_resume(entry.run_id)
                config = load_config_bytes(review_input.config_bytes)
                resume_catalog.register(
                    resume_id=config.resume_sha256,
                    profile_hash=config.profile_sha256,
                    candidate_name=entry.candidate_name,
                    filename=filename,
                    profile_bytes=review_input.profile_bytes,
                    config_bytes=review_input.config_bytes,
                    resume_bytes=resume_bytes,
                    created_at=entry.finished_at,
                )
            except (KeyError, OSError, UnicodeError, ValueError, ValidationError):
                continue

    def associate_catalog_profiles() -> list[ResumeCatalogEntry]:
        """Migrate profile-hash-only Global jobs to explicit resume associations."""
        entries = resume_catalog.list()
        for entry in entries:
            for profile_hash in entry.all_profile_hashes:
                global_jobs.associate_profile(
                    resume_id=entry.resume_id,
                    profile_hash=profile_hash,
                )
        return entries

    def sync_resume_catalog() -> list[ResumeCatalogEntry]:
        """Import current and historical resume bundles, then migrate Global jobs."""
        register_current_resume()
        register_history_resumes()
        return associate_catalog_profiles()

    def resume_context(run_id: str | None = None) -> tuple[str, str] | None:
        """Return the resume and profile hashes for current or historical review data."""
        try:
            if run_id is None:
                config = load_config(repository.paths.config_toml)
            else:
                config = load_config_bytes(history.read_review_input(run_id).config_bytes)
        except (KeyError, OSError, UnicodeError, ValueError, ValidationError):
            return None
        return config.resume_sha256, config.profile_sha256

    def selected_resume(
        entries: list[ResumeCatalogEntry],
        requested_resume_id: str | None,
        run_id: str | None = None,
    ) -> str | None:
        """Choose an explicit resume, otherwise prefer the visible History resume."""
        known = {entry.resume_id for entry in entries}
        if requested_resume_id is not None:
            if requested_resume_id not in known:
                raise HTTPException(status.HTTP_404_NOT_FOUND)
            return requested_resume_id
        context = resume_context(run_id)
        if context is not None and context[0] in known:
            return context[0]
        return entries[0].resume_id if entries else None

    def refresh_global_jobs(
        current: Snapshot | None = None,
        entries: list[SearchHistoryEntry] | None = None,
    ) -> Snapshot:
        """Import current and archived decisions into the global job store."""
        current_snapshot = current or repository.load()
        history_entries = history.list() if entries is None else entries
        snapshots = [current_snapshot]
        for entry in history_entries:
            try:
                snapshots.append(history.load(entry.run_id))
            except (KeyError, ValueError):
                continue
        imported = global_jobs.import_snapshots(snapshots)
        sync_resume_catalog()
        return imported

    def selected_ats_jobs(
        keys: list[str],
        review_run_id: str | None,
    ) -> tuple[JobRecord, ...]:
        """Resolve selected jobs from global decisions or the visible review."""
        if not keys or len(keys) != len(set(keys)) or any(not key.strip() for key in keys):
            raise AtsInvalidJobSelection("Select one or more unique jobs.")
        global_snapshot = refresh_global_jobs()
        global_by_key = {job.canonical_job_key: job for job in global_snapshot.jobs}
        history_snapshot: Snapshot | None = None
        if review_run_id:
            try:
                history_snapshot = history.load(review_run_id)
            except (KeyError, ValueError):
                pass
        live_snapshot = repository.load()
        resolved: list[JobRecord] = []
        for key in keys:
            job = global_by_key.get(key)
            if job is None:
                job = (
                    _find_job(history_snapshot, key)
                    if history_snapshot is not None
                    else None
                )
            if job is None:
                job = _find_job(live_snapshot, key)
            if job is None:
                raise AtsInvalidJobSelection("One or more selected jobs are unavailable.")
            resolved.append(job.model_copy(deep=True))
        return tuple(resolved)

    def selected_ats_config(
        review_run_id: str | None,
    ) -> AppConfig:
        """Keep ATS history context while using the global current AI selection."""
        try:
            base: AppConfig | None = None
            if review_run_id is not None:
                try:
                    base = load_config_bytes(
                        history.read_ats_input(review_run_id).config_bytes
                    )
                except (KeyError, OSError, ValueError):
                    pass
            if base is None:
                base = load_config(repository.paths.config_toml)
            return apply_ai_selection_to_config(
                base,
                current_ai_selection(),
                provider_store,
            )
        except (AiConfigError, AiSelectionError, KeyError, OSError, ValueError):
            raise AtsInputError("The current AI configuration is unavailable.") from None

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

    def company_size_config(run_id: str | None = None) -> AppConfig:
        """Load current or archived policy with the global current AI selection."""
        try:
            base = (
                load_config(repository.paths.config_toml)
                if run_id is None
                else load_config_bytes(history.read_ats_input(run_id).config_bytes)
            )
            return apply_ai_selection_to_config(
                base,
                current_ai_selection(),
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
        """Return whether a scan or ATS workflow currently owns AI configuration."""
        for owner in (workflow, ats_workflow):
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
            locked=locked,
        )

    def current_ai_selection() -> AiRuntimeSelection:
        """Return one validated global selection for a new AI operation."""
        try:
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

    def apply_selection_to_setup(answers: SetupAnswers) -> SetupAnswers:
        """Replace browser-supplied model fields with the saved global selection."""
        selection = current_ai_selection()
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
            )
        )

    @app.post(
        "/api/global-jobs/import",
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(require_mutation_request)],
    )
    def import_global_job(payload: _ManualJobImportRequest) -> _ManualJobImportResponse:
        try:
            job_url = require_public_job_url(payload.url)
        except ManualJobImportError as error:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                str(error),
            ) from None
        try:
            with FileRWLock(repository.paths.workflow_lock_file).exclusive(
                blocking=False
            ), FileRWLock(repository.paths.scan_lock_file).exclusive(
                blocking=False
            ):
                if payload.resume_id is not None:
                    try:
                        bundle = resume_catalog.read(payload.resume_id)
                        base_config = load_config_bytes(bundle.config_bytes)
                        profile = bundle.profile_bytes.decode("utf-8")
                    except (KeyError, OSError, UnicodeError, ValueError):
                        raise HTTPException(
                            status.HTTP_404_NOT_FOUND,
                            "The selected resume is unavailable.",
                        ) from None
                elif payload.run_id is None:
                    base_config = load_config(repository.paths.config_toml)
                    profile = repository.paths.profile_md.read_text(encoding="utf-8")
                else:
                    try:
                        review_input = history.read_review_input(payload.run_id)
                        base_config = load_config_bytes(review_input.config_bytes)
                        profile = review_input.profile_bytes.decode("utf-8")
                    except (KeyError, OSError, UnicodeError, ValueError):
                        raise HTTPException(
                            status.HTTP_404_NOT_FOUND,
                            "The selected search history is unavailable.",
                        ) from None
                config = apply_ai_selection_to_config(
                    base_config,
                    current_ai_selection(),
                    provider_store,
                )
                imported_at = datetime.now(UTC)
                job = manual_job_importer(
                    job_url,
                    config,
                    profile,
                    imported_at,
                )
                imported = Snapshot(
                    meta=StoreMeta(data_revision=0),
                    jobs=[job],
                )
                company_sizes().apply(imported, config, imported_at)
                saved_job = global_jobs.upsert_with_default_status(
                    imported.jobs[0],
                    UserStatus.SAVED,
                    resume_id=base_config.resume_sha256,
                    profile_hash=base_config.profile_sha256,
                )
        except LockUnavailable:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "A scan is running; retry the job import after it completes.",
            ) from None
        except (OSError, UnicodeError, ValueError):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "The current AI configuration and candidate profile are unavailable.",
            ) from None
        except ManualJobImportError as error:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                str(error),
            ) from None
        return _ManualJobImportResponse(
            job_key=saved_job.canonical_job_key,
            status=saved_job.user_status,
        )

    @app.post(
        "/api/global-jobs/import-with-resume",
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(require_mutation_request)],
    )
    def import_global_job_with_resume(
        url: Annotated[str, Form(min_length=1, max_length=2083)],
        resume: Annotated[UploadFile, File()],
    ) -> _ManualJobImportWithResumeResponse:
        try:
            job_url = require_public_job_url(url)
        except ManualJobImportError as error:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                str(error),
            ) from None

        resume_path: Path | None = None
        stored_resume_created = False
        catalog_created = False
        completed = False
        try:
            with FileRWLock(repository.paths.workflow_lock_file).exclusive(
                blocking=False
            ), FileRWLock(repository.paths.scan_lock_file).exclusive(
                blocking=False
            ):
                filename = Path(resume.filename or "").name
                resume_bytes = read_resume_upload(resume.file)
                resume_path, stored_resume_created = store_uploaded_resume(
                    repository.paths,
                    filename,
                    resume_bytes,
                )
                resume_id = f"sha256:{resume_path.stem}"
                try:
                    bundle = resume_catalog.read(resume_id)
                except KeyError:
                    base_config = load_config(repository.paths.config_toml)
                    prepared = manual_resume_preparer(
                        resume_path,
                        uploaded_resume_answers(base_config, filename),
                    )
                    if prepared.config.resume_sha256 != resume_id:
                        raise ValueError("prepared resume hash does not match upload")
                    config = apply_ai_selection_to_config(
                        prepared.config,
                        current_ai_selection(),
                        provider_store,
                    )
                    profile_bytes = prepared.profile_bytes
                    config_bytes = serialize_config(config).encode("utf-8")
                    new_catalog_entry = True
                else:
                    config = apply_ai_selection_to_config(
                        load_config_bytes(bundle.config_bytes),
                        current_ai_selection(),
                        provider_store,
                    )
                    profile_bytes = bundle.profile_bytes
                    config_bytes = bundle.config_bytes
                    new_catalog_entry = False

                profile = profile_bytes.decode("utf-8")
                imported_at = datetime.now(UTC)
                job = manual_job_importer(job_url, config, profile, imported_at)
                imported = Snapshot(
                    meta=StoreMeta(data_revision=0),
                    jobs=[job],
                )
                company_sizes().apply(imported, config, imported_at)
                if new_catalog_entry:
                    resume_catalog.register(
                        resume_id=resume_id,
                        profile_hash=config.profile_sha256,
                        candidate_name=config.candidate_name or Path(filename).stem,
                        filename=filename,
                        profile_bytes=profile_bytes,
                        config_bytes=config_bytes,
                        resume_bytes=resume_bytes,
                        created_at=imported_at,
                    )
                    catalog_created = True
                saved_job = global_jobs.upsert_with_default_status(
                    imported.jobs[0],
                    UserStatus.SAVED,
                    resume_id=resume_id,
                    profile_hash=config.profile_sha256,
                )
                completed = True
        except LockUnavailable:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "A scan is running; retry the job import after it completes.",
            ) from None
        except ManualJobImportError as error:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                str(error),
            ) from None
        except (ResumeError, SetupError, OSError, UnicodeError, ValueError) as error:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                str(error) or "Could not prepare the uploaded resume.",
            ) from None
        finally:
            if not completed:
                if catalog_created:
                    try:
                        resume_catalog.delete(resume_id)
                    except (KeyError, OSError, ValueError):
                        pass
                if stored_resume_created and resume_path is not None:
                    resume_path.unlink(missing_ok=True)

        return _ManualJobImportWithResumeResponse(
            job_key=saved_job.canonical_job_key,
            status=saved_job.user_status,
            resume_id=resume_id,
        )

    @app.post(
        "/api/ats-runs",
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_mutation_request)],
    )
    def start_ats_run(
        job_keys: Annotated[str, Form()],
        search_run_id: Annotated[str | None, Form()] = None,
        resume: Annotated[UploadFile | None, File()] = None,
    ) -> AtsRunState:
        if ats_workflow is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        try:
            payload = _AtsStartRequest.model_validate(
                {
                    "search_run_id": search_run_id.strip() if search_run_id else None,
                    "job_keys": json.loads(job_keys),
                }
            )
            jobs = selected_ats_jobs(payload.job_keys, payload.search_run_id)
            if resume is not None and resume.filename:
                resume_filename = Path(resume.filename).name
                resume_bytes = resume.file.read()
            elif payload.search_run_id is not None:
                resume_filename, resume_bytes = history.read_resume(payload.search_run_id)
            else:
                raise AtsInputError("Select or upload a resume for ATS Check.")
            with FileRWLock(repository.paths.ai_usage_lock_file).shared():
                config = selected_ats_config(payload.search_run_id)
                return ats_workflow.start(
                    AtsWorkflowInput(
                        search_run_id=payload.search_run_id or "global",
                        candidate_name=config.candidate_name,
                        resume_filename=resume_filename,
                        resume_bytes=resume_bytes,
                        jobs=jobs,
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
            ats_history_store.delete(run_id)
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

    if ai_store is not None:
        discovery = ai_model_discovery or AiModelDiscovery()

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
                    locked=False,
                )

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

    @app.get("/", response_class=HTMLResponse)
    def dashboard(resume_id: str | None = None) -> HTMLResponse:
        current = repository.load()
        refresh_global_jobs(current)
        resumes = resume_catalog.list()
        selected_resume_id = selected_resume(resumes, resume_id)
        global_snapshot = (
            global_jobs.load_for_resume(selected_resume_id)
            if selected_resume_id is not None
            else global_jobs.load()
        )
        response = HTMLResponse(
            render_dashboard(
                global_jobs.overlay(current),
                global_snapshot,
                resume_catalog=resumes,
                selected_resume_id=selected_resume_id,
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

    if workflow is not None:

        @app.get("/setup", response_class=HTMLResponse)
        def setup_console(
            run_id: str | None = None,
            ats_run_id: str | None = None,
            resume_id: str | None = None,
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
                refresh_global_jobs(raw_snapshot, entries)
                resumes = resume_catalog.list()
                selected_resume_id = selected_resume(resumes, resume_id, run_id)
                global_snapshot = (
                    global_jobs.load_for_resume(selected_resume_id)
                    if selected_resume_id is not None
                    else global_jobs.load()
                )
                snapshot = global_jobs.overlay(raw_snapshot)
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
            ats_source_run_id = run_id or (entries[0].run_id if entries else None)
            ats_default_resume_filename = next(
                (
                    entry.resume_filename
                    for entry in entries
                    if entry.run_id == ats_source_run_id
                ),
                None,
            )
            setup_answers = workflow.load_setup_answers()
            selection = current_ai_selection()
            if (
                setup_answers is not None
                and not repository.paths.ai_selection_toml.exists()
                and not repository.paths.config_toml.exists()
            ):
                selection = AiRuntimeSelection(
                    ai_runtime=setup_answers.ai_runtime,
                    claude=ClaudeRuntimeSelection(
                        model=setup_answers.claude.model,
                        effort=setup_answers.claude.effort,
                        thinking_enabled=setup_answers.claude.thinking_enabled,
                    ),
                )
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
                    ats_source_run_id=ats_source_run_id,
                    ats_default_resume_filename=ats_default_resume_filename,
                    resume_catalog=resumes,
                    selected_resume_id=selected_resume_id,
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
            ):
                resume_path = None
                try:
                    resume_path = load_config(repository.paths.config_toml).resume_path
                except (OSError, ValueError):
                    pass
                with history.delete_transaction(run_id) as deleted_latest:
                    if deleted_latest:
                        live_paths = [
                            repository.paths.jobs_jsonl,
                            repository.paths.dashboard_html,
                            repository.paths.config_toml,
                            repository.paths.profile_md,
                        ]
                        if resume_path is not None:
                            try:
                                resolved = resume_path.resolve()
                                resumes = (repository.paths.root / "resumes").resolve()
                                if resolved.parent == resumes:
                                    live_paths.append(resolved)
                            except OSError:
                                pass
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
            with FileRWLock(repository.paths.workflow_lock_file).exclusive(
                blocking=False
            ), FileRWLock(repository.paths.scan_lock_file).exclusive(blocking=False):
                snapshot = global_jobs.load()
                job = _find_job(snapshot, key)
                if job is None:
                    raise HTTPException(status.HTTP_404_NOT_FOUND)
                config = company_size_config()
                result = service.lookup_for_job(job, config, datetime.now(UTC))
                _mutate_global_or_conflict(
                    global_jobs,
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

    @app.delete(
        "/api/global-jobs/{key}",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(require_mutation_request)],
    )
    def delete_global_job(key: str) -> Response:
        try:
            with FileRWLock(repository.paths.workflow_lock_file).exclusive(
                blocking=False
            ), FileRWLock(repository.paths.scan_lock_file).exclusive(blocking=False):
                global_jobs.delete(key)
        except LockUnavailable:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "A scan is running; retry deleting the global job after it completes.",
            ) from None
        except KeyError:
            raise HTTPException(status.HTTP_404_NOT_FOUND) from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)

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
                context = resume_context()
                if context is None:
                    global_jobs.set_status(job, mutation.status)
                else:
                    global_jobs.set_status(
                        job,
                        mutation.status,
                        resume_id=context[0],
                        profile_hash=context[1],
                    )
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
                context = resume_context(run_id)
                if context is None:
                    global_jobs.set_status(job, mutation.status)
                else:
                    global_jobs.set_status(
                        job,
                        mutation.status,
                        resume_id=context[0],
                        profile_hash=context[1],
                    )
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
            with FileRWLock(repository.paths.workflow_lock_file).exclusive(
                blocking=False
            ), FileRWLock(repository.paths.scan_lock_file).exclusive(
                blocking=False
            ):
                job = global_jobs.find(key)
                if job is None:
                    raise HTTPException(status.HTTP_404_NOT_FOUND)
                global_jobs.set_status(job, mutation.status)
        except LockUnavailable:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "A scan is running; retry the status change after it completes.",
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

    sync_resume_catalog()
    return app


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
