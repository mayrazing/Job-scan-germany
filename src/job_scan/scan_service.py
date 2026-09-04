from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import uuid
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from job_scan.ai_config import AiConfigError, AiProviderStore
from job_scan.ai_runtime import AiRuntimeInvoker
from job_scan.ai_selection import (
    AiSelectionError,
    AiSelectionStore,
    ai_selection_from_config,
    apply_ai_selection_to_config,
)
from job_scan.availability import update_availability
from job_scan.company_industry import (
    CompanyIndustryService,
    CompanyIndustryStore,
    SourceNativeCompanyIndustryLookup,
)
from job_scan.company_size import (
    AiCompanySizeLookup,
    CompanySizeProgress,
    CompanySizeService,
    CompanySizeStore,
    SourceNativeCompanySizeLookup,
)
from job_scan.config import AppConfig, load_config
from job_scan.dashboard.render import render_dashboard
from job_scan.dedup import requires_source_generation_rollover
from job_scan.domain import (
    AvailabilityStatus,
    JobRecord,
    MachineStatus,
    Snapshot,
    SourceOccurrence,
)
from job_scan.global_jobs import GlobalJobStore, filter_untracked_job_records
from job_scan.http_client import PublicHttpClient
from job_scan.job_snapshot import JobSnapshotStore
from job_scan.locking import FileRWLock, LockUnavailable
from job_scan.normalization import normalize_text
from job_scan.paths import AppPaths
from job_scan.policy import apply_review, apply_review_failure, refresh_review_decision
from job_scan.repository import JsonlRepository
from job_scan.review_queue import select_jobs_for_review
from job_scan.reviewer import ClaudeReviewer, ReviewBatchOutcome, ReviewBatchProgress
from job_scan.run_log import RunLogger
from job_scan.sources.base import (
    FetchedOccurrence,
    JobReference,
    SourceAdapter,
    SourceError,
    SourceRunResult,
    run_source,
)
from job_scan.sources.bosch import BoschAdapter
from job_scan.sources.dallmeier import DallmeierAdapter
from job_scan.sources.dhl import DhlAdapter
from job_scan.sources.glassdoor import GlassdoorDeAdapter
from job_scan.sources.indeed import IndeedDeAdapter
from job_scan.sources.jobsuche import JobsucheAdapter
from job_scan.sources.linkedin import LinkedinAdapter
from job_scan.sources.rohde_schwarz import RohdeSchwarzAdapter
from job_scan.sources.siemens import SiemensAdapter
from job_scan.sources.simplify import SimplifyDeAdapter
from job_scan.sources.stepstone import StepstoneDeAdapter
from job_scan.sources.telekom import TelekomAdapter
from job_scan.sources.thyssenkrupp import ThyssenkruppAdapter
from job_scan.sources.workday import (
    AdvantechAdapter,
    HaierAdapter,
    JohnsonElectricAdapter,
    NexperiaAdapter,
    VosslohAdapter,
)


class ScanError(RuntimeError):
    """Report one safe fatal scan error."""


class ScanAlreadyRunning(ScanError):
    """Report non-blocking contention on the whole-scan lock."""


class ScanSummary(BaseModel):
    """Store bounded operational facts for one published scan."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    started_at: datetime
    finished_at: datetime
    source_counts: dict[str, int]
    source_errors: list[SourceError]
    occurrence_count: int = Field(ge=0)
    new_count: int = Field(ge=0)
    changed_count: int = Field(ge=0)
    reviewed_count: int = Field(ge=0)
    eligible_count: int = Field(ge=0)
    excluded_count: int = Field(ge=0)
    uncertain_count: int = Field(ge=0)
    pending_count: int = Field(ge=0)
    source_error_count: int = Field(ge=0)
    claude_model: str
    claude_batch_count: int = Field(ge=0)
    claude_budget_usd: Decimal = Field(ge=0)
    claude_failure_count: int = Field(ge=0)
    claude_failure_counts: dict[str, int]
    jobs_jsonl: Path
    dashboard_html: Path


class Reviewer(Protocol):
    def review(
        self,
        jobs: Sequence[JobRecord],
        profile: str,
        config: AppConfig,
        *,
        progress: Callable[[ReviewBatchProgress], None] | None = None,
    ) -> ReviewBatchOutcome: ...


SourceFactory = Callable[[AppConfig], Sequence[SourceAdapter]]
ScanProgressStage = Literal["sources", "review", "company_size", "publish"]


@dataclass(frozen=True)
class SourceProgress:
    """Report completed sources and their cumulative scan results."""

    completed_sources: int
    total_sources: int
    found_jobs: int
    warning_count: int


@dataclass(frozen=True)
class ScanProgress:
    """Report one scan stage and its optional detailed progress."""

    stage: ScanProgressStage
    source_progress: SourceProgress | None = None
    review: ReviewBatchProgress | None = None
    company_size: CompanySizeProgress | None = None


ScanProgressCallback = Callable[[ScanProgress], None]
PublishedSnapshotCallback = Callable[[ScanSummary, Snapshot], None]

SCAN_STAGE_MESSAGES = {
    "sources": "Searching configured job sources...",
    "review": "Reviewing complete job descriptions...",
    "company_size": "Checking company sizes...",
    "publish": "Publishing review queue...",
}


class ScanRunState(BaseModel):
    """Persist one command-line scan's progress for other processes to read."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    status: Literal["running", "complete", "failed"]
    stage: ScanProgressStage | None
    message: str
    progress_percent: float = Field(ge=0, le=100)
    updated_at: datetime


def read_scan_run_state(paths: AppPaths) -> ScanRunState | None:
    """Load the persisted command-line scan state, ignoring missing or unusable files."""
    try:
        payload = json.loads(paths.scan_run_state.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    try:
        return ScanRunState.model_validate(payload)
    except ValidationError:
        return None


def write_scan_run_state(paths: AppPaths, state: ScanRunState) -> None:
    """Atomically replace the persisted command-line scan state file."""
    paths.scan_run_state.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=paths.scan_run_state.parent,
        prefix=f".{paths.scan_run_state.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
            temporary_file.write(state.model_dump_json())
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, paths.scan_run_state)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def scan_progress_percent(current: ScanProgress) -> float:
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


def scan_progress_message(current: ScanProgress) -> str:
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
        return SCAN_STAGE_MESSAGES[current.stage]
    return (
        "Reviewing complete job descriptions: "
        f"{review.completed_batches}/{review.total_batches} batches, "
        f"{review.completed_jobs}/{review.total_jobs} jobs..."
    )


class ScanService:
    """Run source acquisition, review policy, and one repository publication."""

    def __init__(
        self,
        paths: AppPaths,
        *,
        repository: JsonlRepository | None = None,
        reviewer: Reviewer | None = None,
        global_job_store: GlobalJobStore | None = None,
        company_size_service: CompanySizeService | None = None,
        company_industry_service: CompanyIndustryService | None = None,
        source_factory: SourceFactory | None = None,
        clock: Callable[[], datetime] | None = None,
        run_id_factory: Callable[[], str] | None = None,
        run_logger: RunLogger | None = None,
    ) -> None:
        self._paths = paths
        self._repository = repository or JsonlRepository(
            paths,
            FileRWLock(paths.lock_file),
            render_dashboard,
        )
        self._ai_runtime = AiRuntimeInvoker(paths)
        self._reviewer = reviewer or ClaudeReviewer(self._ai_runtime)
        self._global_jobs = global_job_store or GlobalJobStore(paths)
        self._company_size_service = company_size_service
        self._company_industry_service = company_industry_service
        self._source_factory = source_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        self._run_id_factory = run_id_factory or (lambda: str(uuid.uuid4()))
        self._run_logger = run_logger or RunLogger(paths.logs_dir)

    def run(
        self,
        force_review: bool = False,
        *,
        progress: ScanProgressCallback | None = None,
        on_published: PublishedSnapshotCallback | None = None,
        workflow_lock_held: bool = False,
    ) -> ScanSummary:
        """Publish one isolated scan or fail immediately when another scan owns the lock."""
        try:
            if workflow_lock_held:
                return self._run_with_scan_lock(
                    force_review,
                    progress,
                    on_published,
                )
            with FileRWLock(self._paths.workflow_lock_file).exclusive(blocking=False):
                return self._run_with_scan_lock(
                    force_review,
                    progress,
                    on_published,
                )
        except LockUnavailable:
            raise ScanAlreadyRunning("A setup or scan is already running.") from None

    def _run_with_scan_lock(
        self,
        force_review: bool,
        progress: ScanProgressCallback | None,
        on_published: PublishedSnapshotCallback | None,
    ) -> ScanSummary:
        with FileRWLock(self._paths.scan_lock_file).exclusive(blocking=False):
            return self._run_locked(force_review, progress, on_published)

    def _run_locked(
        self,
        force_review: bool,
        progress: ScanProgressCallback | None,
        on_published: PublishedSnapshotCallback | None,
    ) -> ScanSummary:
        started_at = _as_utc(self._clock())
        run_id = self._run_id_factory()
        run_cache_dir = self._paths.run_cache_dir(run_id)
        company_size_service = self._company_size_service or CompanySizeService(
            CompanySizeStore(self._paths.cache_dir / "company-sizes.json"),
            AiCompanySizeLookup(self._ai_runtime),
            native_lookup=SourceNativeCompanySizeLookup(
                PublicHttpClient(run_cache_dir)
            ),
        )
        company_industry_service = (
            self._company_industry_service
            or CompanyIndustryService(
                CompanyIndustryStore(
                    self._paths.cache_dir / "company-industries.json"
                ),
                SourceNativeCompanyIndustryLookup(),
            )
        )
        config, profile, profile_hash, stored = self._load_inputs()
        stored_occurrences: dict[str, SourceOccurrence] = {}
        for job in stored.jobs:
            for occurrence in job.source_occurrences:
                current = stored_occurrences.get(occurrence.source_job_key)
                if (
                    current is None
                    or occurrence.source_generation > current.source_generation
                ):
                    stored_occurrences[occurrence.source_job_key] = occurrence
        source_factory = self._source_factory or _default_source_factory(
            self._paths,
            cache_dir=run_cache_dir,
            existing_source_occurrences=stored_occurrences,
        )
        initial = Snapshot(meta=stored.meta)
        try:
            adapters = list(source_factory(config))
        except Exception as error:
            raise ScanError("Could not initialize scan sources.") from error

        total_sources = len(adapters)
        found_jobs = 0
        warning_count = 0
        if progress is not None:
            progress(
                ScanProgress(
                    stage="sources",
                    source_progress=SourceProgress(0, total_sources, 0, 0),
                )
            )

        posted_since = (
            started_at.date() - timedelta(days=config.posted_within_days)
            if config.posted_within_days is not None
            else None
        )
        source_results: list[SourceRunResult] = []
        job_snapshot_store = JobSnapshotStore(self._paths.job_snapshots_dir)
        scanned = update_availability(initial, source_results, started_at)
        for completed_sources, adapter in enumerate(adapters, start=1):
            def report_source_progress(
                source_found_jobs: int,
                source_warning_count: int,
                *,
                completed_before: int = completed_sources - 1,
                found_before: int = found_jobs,
                warnings_before: int = warning_count,
            ) -> None:
                if progress is None:
                    return
                progress(
                    ScanProgress(
                        stage="sources",
                        source_progress=SourceProgress(
                            completed_before,
                            total_sources,
                            found_before + source_found_jobs,
                            warnings_before + source_warning_count,
                        ),
                    )
                )

            result = run_source(
                adapter,
                posted_since=posted_since,
                progress=report_source_progress if progress is not None else None,
            )
            _carry_existing_job_snapshots(result.occurrences, stored_occurrences)
            _persist_job_snapshots(
                result.occurrences,
                job_snapshot_store,
                started_at,
            )
            source_snapshot = update_availability(initial, [result], started_at)
            company_size_service.collect_native(source_snapshot, config, started_at)
            source_results.append(result)
            found_jobs += len(result.occurrences)
            warning_count += len(result.errors)
            scanned = update_availability(initial, source_results, started_at)
            _carry_source_company_industries(source_snapshot, scanned)
            company_industry_service.apply(scanned, config, started_at)
            if progress is not None:
                progress(
                    ScanProgress(
                        stage="sources",
                        source_progress=SourceProgress(
                            completed_sources,
                            total_sources,
                            found_jobs,
                            warning_count,
                        ),
                    )
                )
        new_count, changed_count = _change_counts(initial, scanned)
        company_size_service.restore(scanned, config)
        for job in scanned.jobs:
            refresh_review_decision(job, config)
        review_jobs = select_jobs_for_review(
            scanned,
            profile_hash,
            started_at,
            force=force_review,
        )
        try:
            tracker_snapshot = self._global_jobs.load_read_only()
        except (OSError, UnicodeError, ValueError):
            pass
        else:
            review_jobs = filter_untracked_job_records(
                review_jobs,
                tracker_snapshot,
            )

        if progress is not None:
            total_jobs = len(review_jobs)
            batch_size = config.claude.batch_size
            progress(
                ScanProgress(
                    stage="review",
                    review=ReviewBatchProgress(
                        completed_batches=0,
                        total_batches=(total_jobs + batch_size - 1) // batch_size,
                        completed_jobs=0,
                        total_jobs=total_jobs,
                    ),
                )
            )

        if not review_jobs:
            outcome = ReviewBatchOutcome(accepted={}, failed={}, invocations=[])
        elif progress is None:
            outcome = self._reviewer.review(review_jobs, profile, config)
        else:
            outcome = self._reviewer.review(
                review_jobs,
                profile,
                config,
                progress=lambda current: progress(
                    ScanProgress(stage="review", review=current)
                ),
            )
        jobs_by_key = {job.canonical_job_key: job for job in scanned.jobs}
        for key, review in sorted(outcome.accepted.items()):
            apply_review(jobs_by_key[key], review, config, profile_hash, started_at)
        for key, failure in sorted(outcome.failed.items()):
            apply_review_failure(jobs_by_key[key], failure, profile_hash, started_at)
        if progress is None:
            company_size_service.apply(scanned, config, started_at)
        else:
            company_size_service.apply(
                scanned,
                config,
                started_at,
                progress=lambda current: progress(
                    ScanProgress(stage="company_size", company_size=current)
                ),
            )
        if progress is not None:
            progress(ScanProgress(stage="publish"))

        try:
            published = self._repository.mutate(
                lambda latest: Snapshot(
                    meta=latest.meta,
                    jobs=[job.model_copy(deep=True) for job in scanned.jobs],
                )
            )
        except Exception as error:
            raise ScanError("Could not publish scan results.") from error

        finished_at = _as_utc(self._clock())
        summary = _summary(
            run_id=run_id,
            started_at=started_at,
            finished_at=finished_at,
            config=config,
            results=source_results,
            outcome=outcome,
            published=published,
            new_count=new_count,
            changed_count=changed_count,
            paths=self._paths,
        )
        if on_published is not None:
            try:
                on_published(summary, published.model_copy(deep=True))
            except Exception as error:
                try:
                    self._repository.mutate(
                        lambda latest: Snapshot(
                            meta=latest.meta,
                            jobs=[job.model_copy(deep=True) for job in stored.jobs],
                        )
                    )
                except Exception as rollback_error:
                    raise ScanError(
                        "Could not finalize scan history or restore prior live results."
                    ) from rollback_error
                raise ScanError(
                    "Could not finalize scan history; prior live results were restored."
                ) from error
        try:
            self._run_logger.write(summary)
        except Exception:  # noqa: BLE001
            # Logging is secondary to the valid snapshot already published above.
            print("Warning: Could not write scan log.", file=sys.stderr)
        return summary

    def _load_inputs(self) -> tuple[AppConfig, str, str, Snapshot]:
        """Load validated configuration, current profile bytes, and initial snapshot."""
        try:
            config = load_config(self._paths.config_toml)
            providers = AiProviderStore(self._paths.ai_config_toml)
            config = apply_ai_selection_to_config(
                config,
                AiSelectionStore(self._paths.ai_selection_toml).load(
                    ai_selection_from_config(config, providers)
                ),
                providers,
            )
            profile_bytes = self._paths.profile_md.read_bytes()
            profile = profile_bytes.decode("utf-8")
            profile_hash = f"sha256:{hashlib.sha256(profile_bytes).hexdigest()}"
            initial = self._repository.load()
        except (
            AiConfigError,
            AiSelectionError,
            OSError,
            UnicodeError,
            ValueError,
            ValidationError,
        ) as error:
            raise ScanError("Could not load scan configuration or data.") from error
        return config, profile, profile_hash, initial


def _default_source_factory(
    paths: AppPaths,
    *,
    cache_dir: Path | None = None,
    existing_source_occurrences: Mapping[str, SourceOccurrence] | None = None,
) -> SourceFactory:
    """Return a factory for every enabled discovery site."""

    def build(config: AppConfig) -> Sequence[SourceAdapter]:
        http_client = PublicHttpClient(cache_dir or paths.cache_dir)
        adapters: list[SourceAdapter] = []

        def should_capture_snapshot(reference: JobReference) -> bool:
            source_job_key = (
                f"{reference.source.value}:{reference.source_instance}:"
                f"{reference.external_id}"
            )
            stored = (
                existing_source_occurrences.get(source_job_key)
                if existing_source_occurrences is not None
                else None
            )
            if stored is None:
                return True
            if not stored.detail_complete:
                return False
            baseline_title = stored.identity_baseline_title or stored.title
            if normalize_text(baseline_title) == normalize_text(
                reference.listing_title
            ):
                return False
            if stored.availability_status is AvailabilityStatus.CLOSED:
                return True
            return bool(
                stored.posted_at is not None
                and reference.listing_posted_at is not None
                and (reference.listing_posted_at - stored.posted_at).days >= 60
            )

        if config.indeed_de_enabled:
            adapters.append(
                IndeedDeAdapter(
                    config,
                    capture_snapshot=should_capture_snapshot,
                )
            )
        if config.linkedin_enabled:
            adapters.append(
                LinkedinAdapter(
                    config,
                    capture_snapshot=should_capture_snapshot,
                )
            )
        if config.stepstone_de_enabled:
            adapters.append(
                StepstoneDeAdapter(
                    config,
                    capture_snapshot=should_capture_snapshot,
                )
            )
        if config.glassdoor_de_enabled:
            adapters.append(
                GlassdoorDeAdapter(
                    config,
                    capture_snapshot=should_capture_snapshot,
                )
            )
        if config.simplify_de_enabled:
            adapters.append(
                SimplifyDeAdapter(
                    config,
                    http_client,
                    capture_snapshot=should_capture_snapshot,
                )
            )
        if config.arbeitsagentur_enabled:
            adapters.append(
                JobsucheAdapter(
                    config,
                    http_client,
                    capture_snapshot=should_capture_snapshot,
                )
            )
        if "bosch" in config.target_companies:
            adapters.append(
                BoschAdapter(
                    config,
                    http_client,
                    capture_snapshot=should_capture_snapshot,
                )
            )
        if "telekom" in config.target_companies:
            adapters.append(
                TelekomAdapter(
                    config,
                    http_client,
                    capture_snapshot=should_capture_snapshot,
                )
            )
        if "rohde-schwarz" in config.target_companies:
            adapters.append(
                RohdeSchwarzAdapter(
                    config,
                    http_client,
                    capture_snapshot=should_capture_snapshot,
                )
            )
        if "siemens" in config.target_companies:
            adapters.append(
                SiemensAdapter(
                    config,
                    http_client,
                    capture_snapshot=should_capture_snapshot,
                )
            )
        if "dhl" in config.target_companies:
            adapters.append(
                DhlAdapter(
                    config,
                    http_client,
                    capture_snapshot=should_capture_snapshot,
                )
            )
        if "thyssenkrupp" in config.target_companies:
            adapters.append(
                ThyssenkruppAdapter(
                    config,
                    http_client,
                    capture_snapshot=should_capture_snapshot,
                )
            )
        if "dallmeier" in config.target_companies:
            adapters.append(
                DallmeierAdapter(
                    config,
                    http_client,
                    capture_snapshot=should_capture_snapshot,
                )
            )
        if "haier" in config.target_companies:
            adapters.append(
                HaierAdapter(
                    config,
                    http_client,
                    capture_snapshot=should_capture_snapshot,
                )
            )
        if "nexperia" in config.target_companies:
            adapters.append(
                NexperiaAdapter(
                    config,
                    http_client,
                    capture_snapshot=should_capture_snapshot,
                )
            )
        if "vossloh" in config.target_companies:
            adapters.append(
                VosslohAdapter(
                    config,
                    http_client,
                    capture_snapshot=should_capture_snapshot,
                )
            )
        if "johnson-electric" in config.target_companies:
            adapters.append(
                JohnsonElectricAdapter(
                    config,
                    http_client,
                    capture_snapshot=should_capture_snapshot,
                )
            )
        if "advantech" in config.target_companies:
            adapters.append(
                AdvantechAdapter(
                    config,
                    http_client,
                    capture_snapshot=should_capture_snapshot,
                )
            )
        return adapters

    return build


def _persist_job_snapshots(
    occurrences: Sequence[FetchedOccurrence],
    store: JobSnapshotStore,
    captured_at: datetime,
) -> None:
    """Save optional capture output without making it a source failure."""
    for occurrence in occurrences:
        html = occurrence.job_snapshot_html
        if html is None:
            continue
        try:
            occurrence.job_snapshot = store.save(
                source_job_key=occurrence.source_job_key,
                captured_at=captured_at,
                html=html,
            )
            occurrence.job_snapshot_error_code = None
        except Exception:  # noqa: BLE001
            # A snapshot is optional evidence. Keep the fetched job when its
            # archive cannot be validated or written.
            occurrence.job_snapshot = None
            occurrence.job_snapshot_error_code = "snapshot_save_failed"
        finally:
            occurrence.job_snapshot_html = None


def _carry_existing_job_snapshots(
    occurrences: Sequence[FetchedOccurrence],
    stored_occurrences: Mapping[str, SourceOccurrence],
) -> None:
    """Keep snapshot state only when a source ID still means the same posting."""
    for occurrence in occurrences:
        stored = stored_occurrences.get(occurrence.source_job_key)
        if stored is None:
            continue
        if requires_source_generation_rollover(stored, occurrence):
            stored_snapshot_id = (
                stored.job_snapshot.snapshot_id
                if stored.job_snapshot is not None
                else None
            )
            if (
                stored_snapshot_id is not None
                and occurrence.job_snapshot is not None
                and occurrence.job_snapshot.snapshot_id == stored_snapshot_id
            ):
                occurrence.job_snapshot = None
            if occurrence.job_snapshot_html is not None and stored_snapshot_id == (
                "sha256:"
                + hashlib.sha256(
                    occurrence.job_snapshot_html.encode("utf-8")
                ).hexdigest()
            ):
                occurrence.job_snapshot_html = None
            if (
                occurrence.job_snapshot is None
                and occurrence.job_snapshot_error_code is None
                and occurrence.job_snapshot_html is None
            ):
                occurrence.job_snapshot_error_code = "snapshot_capture_failed"
            continue
        # A conservative pre-detail decision may have captured an existing job.
        # Discard that transient page rather than backfilling or replacing the
        # immutable snapshot for the current source generation.
        occurrence.job_snapshot_html = None
        occurrence.job_snapshot = (
            stored.job_snapshot.model_copy(deep=True)
            if stored.job_snapshot is not None
            else None
        )
        occurrence.job_snapshot_error_code = stored.job_snapshot_error_code


def _change_counts(initial: Snapshot, scanned: Snapshot) -> tuple[int, int]:
    """Return canonical jobs added and existing jobs whose primary content changed."""
    initial_by_key = {job.canonical_job_key: job for job in initial.jobs}
    new_count = sum(
        job.canonical_job_key not in initial_by_key for job in scanned.jobs
    )
    changed_count = sum(
        job.canonical_job_key in initial_by_key
        and initial_by_key[job.canonical_job_key].content_hash != job.content_hash
        for job in scanned.jobs
    )
    return new_count, changed_count


def _carry_source_company_industries(source: Snapshot, target: Snapshot) -> None:
    """Copy company-page industries captured during the current source run."""
    industries = {
        " ".join(job.company.casefold().split()): job.company_industry
        for job in source.jobs
        if job.company_industry is not None
    }
    for job in target.jobs:
        industry = industries.get(" ".join(job.company.casefold().split()))
        if industry is not None:
            job.company_industry = industry.model_copy(deep=True)


def _summary(
    *,
    run_id: str,
    started_at: datetime,
    finished_at: datetime,
    config: AppConfig,
    results: Sequence[SourceRunResult],
    outcome: ReviewBatchOutcome,
    published: Snapshot,
    new_count: int,
    changed_count: int,
    paths: AppPaths,
) -> ScanSummary:
    """Build one privacy-bounded summary from source, review, and publication facts."""
    source_counts: Counter[str] = Counter()
    source_errors: list[SourceError] = []
    for result in results:
        source_counts[f"{result.source.value}:{result.source_instance}"] += len(
            result.occurrences
        )
        source_errors.extend(result.errors)
    machine_counts = Counter(job.machine_status for job in published.jobs)
    failure_counts = Counter(
        failure.category for failure in outcome.failed.values()
    )
    budget = sum(
        (
            invocation.budget_usd
            for invocation in outcome.invocations
            if invocation.budget_usd is not None
        ),
        start=Decimal(0),
    )
    return ScanSummary(
        run_id=run_id,
        started_at=started_at,
        finished_at=finished_at,
        source_counts=dict(source_counts),
        source_errors=source_errors,
        occurrence_count=sum(source_counts.values()),
        new_count=new_count,
        changed_count=changed_count,
        reviewed_count=len(outcome.accepted),
        eligible_count=machine_counts[MachineStatus.ELIGIBLE],
        excluded_count=machine_counts[MachineStatus.EXCLUDED],
        uncertain_count=machine_counts[MachineStatus.UNCERTAIN],
        pending_count=(
            machine_counts[MachineStatus.PENDING]
            + machine_counts[MachineStatus.PENDING_SOURCE]
        ),
        source_error_count=len(source_errors),
        claude_model=config.selected_model,
        claude_batch_count=len(outcome.invocations),
        claude_budget_usd=budget,
        claude_failure_count=len(outcome.failed),
        claude_failure_counts={
            str(category): count for category, count in failure_counts.items()
        },
        jobs_jsonl=paths.jobs_jsonl,
        dashboard_html=paths.dashboard_html,
    )


def _as_utc(value: datetime) -> datetime:
    """Return one timezone-aware timestamp normalized to UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ScanError("Scan clock returned a timestamp without a timezone.")
    return value.astimezone(UTC)
