from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from job_scan import scan_service as scan_service_module
from job_scan.ai_config import AiProviderDraft, AiProviderStore
from job_scan.ai_selection import (
    AiRuntimeSelection,
    AiSelectionStore,
    ClaudeRuntimeSelection,
)
from job_scan.claude_process import ClaudeInvocation, ClaudeRequest
from job_scan.company_size import (
    CompanySizeEvidence,
    CompanySizeService,
    CompanySizeStore,
)
from job_scan.config import (
    AppConfig,
    ClaudeSettings,
    SchedulerSettings,
    save_config,
)
from job_scan.dedup import merge_occurrences
from job_scan.domain import (
    AIReview,
    AvailabilityStatus,
    CompanyIndustryEvidence,
    CompanyIndustrySource,
    CompanySizeSource,
    JobRecord,
    MachineStatus,
    Snapshot,
    SourceKind,
    SourceOccurrence,
    StoreMeta,
    UserStatus,
)
from job_scan.global_jobs import GlobalJobStore
from job_scan.http_client import BlockedResponse
from job_scan.job_snapshot import JobSnapshotReference, JobSnapshotStore
from job_scan.locking import FileRWLock
from job_scan.normalization import content_hash
from job_scan.paths import AppPaths
from job_scan.repository import JsonlRepository
from job_scan.reviewer import (
    ClaudeReviewer,
    ReviewBatchOutcome,
    ReviewBatchProgress,
    ReviewFailure,
)
from job_scan.scan_service import (
    ScanAlreadyRunning,
    ScanError,
    ScanService,
    ScanSummary,
    _carry_existing_job_snapshots,
    _default_source_factory,
    _persist_job_snapshots,
)
from job_scan.sources.base import FetchedOccurrence, JobReference, SourceAdapter
from job_scan.sources.bosch import BoschAdapter
from job_scan.sources.dallmeier import DallmeierAdapter

NOW = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)
PROFILE = "# Profile\nBackend engineer needing visa sponsorship.\n"
PROFILE_HASH = f"sha256:{hashlib.sha256(PROFILE.encode()).hexdigest()}"


def config(paths: AppPaths) -> AppConfig:
    resume = paths.root / "resume.pdf"
    resume.parent.mkdir(parents=True, exist_ok=True)
    resume.write_bytes(b"fixture")
    value = AppConfig(
        resume_path=resume,
        resume_sha256="sha256:" + "a" * 64,
        profile_sha256=PROFILE_HASH,
        search_terms=["backend"],
        locations=["Berlin"],
        german_level="B1",
        claude=ClaudeSettings(
            model="claude-sonnet-4-5",
            effort="high",
            batch_size=10,
        ),
        scheduler=SchedulerSettings(local_time="09:00"),
    )
    save_config(paths.config_toml, value)
    paths.profile_md.write_text(PROFILE, encoding="utf-8")
    return value


def test_scan_loads_current_api_model_as_one_run_snapshot(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    value = config(paths)
    store = AiProviderStore(paths.ai_config_toml)
    provider = store.create(
        AiProviderDraft(
            display_name="DeepSeek",
            base_url="https://api.example.com/anthropic",
            api_key="sk-test",
            model="provider-model-v2",
            reasoning_effort="low",
        )
    )
    save_config(
        paths.config_toml,
        value.model_copy(
            update={
                "ai_runtime": f"api:{provider.id}",
                "ai_model": "stale-model-v1",
            }
        ),
    )

    loaded, _profile, _profile_hash, _snapshot = ScanService(paths)._load_inputs()

    assert loaded.selected_model == "provider-model-v2"


def test_scan_overlays_the_global_ai_selection_on_saved_setup(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    config(paths)
    provider = AiProviderStore(paths.ai_config_toml).create(
        AiProviderDraft(
            display_name="DeepSeek",
            base_url="https://api.example.com/anthropic",
            api_key="sk-test",
            model="deepseek-v4",
            reasoning_effort="high",
        )
    )
    AiSelectionStore(paths.ai_selection_toml).save(
        AiRuntimeSelection(
            ai_runtime=f"api:{provider.id}",
            claude=ClaudeRuntimeSelection(
                model="opus",
                effort="low",
                thinking_enabled=False,
            ),
        )
    )

    loaded, _profile, _profile_hash, _snapshot = ScanService(paths)._load_inputs()

    assert loaded.ai_runtime == f"api:{provider.id}"
    assert loaded.selected_model == "deepseek-v4"
    assert loaded.claude.model == "opus"
    assert loaded.claude.effort == "low"
    assert loaded.claude.thinking_enabled is False


def test_scan_falls_back_to_global_claude_settings_when_selected_provider_is_missing(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    config(paths)
    AiSelectionStore(paths.ai_selection_toml).save(
        AiRuntimeSelection(
            ai_runtime="api:missing",
            claude=ClaudeRuntimeSelection(
                model="haiku",
                effort="low",
                thinking_enabled=False,
            ),
        )
    )

    loaded, _profile, _profile_hash, _snapshot = ScanService(paths)._load_inputs()

    assert loaded.ai_runtime == "claude-code"
    assert loaded.selected_model == "haiku"
    assert loaded.claude.effort == "low"
    assert loaded.claude.thinking_enabled is False


def fetched(
    external_id: str,
    title: str,
    description: str,
    *,
    source: SourceKind = SourceKind.LINKEDIN,
    source_instance: str = "acme/jobs",
) -> FetchedOccurrence:
    company = "Acme"
    location = "Berlin"
    return FetchedOccurrence(
        source=source,
        source_instance=source_instance,
        external_id=external_id,
        url=f"https://{source_instance.replace('/', '.')}.example/jobs/{external_id}",
        company=company,
        title=title,
        location=location,
        description=description,
        posted_at=date(2026, 8, 1),
        content_hash=content_hash(company, title, location, description),
        detail_complete=True,
    )


def stored_job(
    key: str,
    occurrence: FetchedOccurrence,
) -> JobRecord:
    stored = SourceOccurrence(
        **occurrence.model_dump(exclude={"fetch_error_code"}),
        source_generation=1,
        availability_status=AvailabilityStatus.ACTIVE,
        last_fetch_error_code=occurrence.fetch_error_code,
    )
    return JobRecord(
        canonical_job_key=key,
        source_occurrences=[stored],
        primary_source_occurrence_key=stored.source_occurrence_key,
        company=stored.company,
        title=stored.title,
        location=stored.location,
        url=stored.url,
        description=stored.description,
        posted_at=stored.posted_at,
        content_hash=stored.content_hash,
        first_seen=NOW - timedelta(days=5),
        last_seen=NOW - timedelta(days=1),
        availability_status=AvailabilityStatus.ACTIVE,
        machine_status=MachineStatus.ELIGIBLE,
        user_status_updated_at=NOW - timedelta(days=5),
        last_review_attempt_content_hash=stored.content_hash,
        last_review_attempt_profile_hash=PROFILE_HASH,
        last_successful_review_content_hash=stored.content_hash,
        last_successful_review_profile_hash=PROFILE_HASH,
    )


class FakeAdapter:
    def __init__(
        self,
        source: SourceKind,
        source_instance: str,
        occurrences: Sequence[FetchedOccurrence] = (),
        *,
        discovery_error: Exception | None = None,
    ) -> None:
        self.source = source
        self.source_instance = source_instance
        self._occurrences = {item.external_id: item for item in occurrences}
        self._discovery_error = discovery_error

    def discover(self) -> list[JobReference]:
        if self._discovery_error is not None:
            raise self._discovery_error
        return [
            JobReference(
                source=item.source,
                source_instance=item.source_instance,
                external_id=item.external_id,
                detail_url=item.url,
                listing_title=item.title,
                listing_company=item.company,
                listing_location=item.location,
                listing_posted_at=item.posted_at,
            )
            for item in self._occurrences.values()
        ]

    def fetch_detail(self, reference: JobReference) -> FetchedOccurrence:
        return self._occurrences[reference.external_id]


class RecordingReviewer:
    def __init__(self) -> None:
        self.reviewed_titles: list[str] = []

    def review(
        self,
        jobs: Sequence[JobRecord],
        _profile: str,
        _config: AppConfig,
        *,
        progress=None,
    ) -> ReviewBatchOutcome:
        del progress
        self.reviewed_titles.extend(job.title for job in jobs)
        return ReviewBatchOutcome(accepted={}, failed={}, invocations=[])


def test_scan_filters_tracked_job_before_forced_ai_review(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    config(paths)
    tracked = fetched("TRACKED", "Tracked Role", "Complete tracked role.")
    untracked = fetched("UNTRACKED", "Untracked Role", "Complete new role.")
    global_jobs = GlobalJobStore(paths)
    global_jobs.set_status(
        stored_job("tracker-job", tracked),
        UserStatus.SAVED,
        NOW,
    )
    tracker_before = global_jobs.load()
    reviewer = RecordingReviewer()
    repo = repository(paths)
    service = ScanService(
        paths,
        repository=repo,
        reviewer=reviewer,
        company_size_service=NoopCompanySizeService(),  # type: ignore[arg-type]
        source_factory=lambda _config: [
            FakeAdapter(SourceKind.LINKEDIN, "acme/jobs", [tracked, untracked])
        ],
        global_job_store=global_jobs,
        clock=lambda: NOW,
    )

    service.run(force_review=True)

    assert reviewer.reviewed_titles == ["Untracked Role"]
    assert {job.title for job in repo.load().jobs} == {
        "Tracked Role",
        "Untracked Role",
    }
    assert global_jobs.load() == tracker_before


def test_scan_continues_ai_review_when_job_tracker_cannot_be_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    config(paths)
    occurrence = fetched("CURRENT", "Current Role", "Complete current role.")
    global_jobs = GlobalJobStore(paths)
    reviewer = RecordingReviewer()
    service = ScanService(
        paths,
        reviewer=reviewer,
        company_size_service=NoopCompanySizeService(),  # type: ignore[arg-type]
        source_factory=lambda _config: [
            FakeAdapter(SourceKind.LINKEDIN, "acme/jobs", [occurrence])
        ],
        global_job_store=global_jobs,
        clock=lambda: NOW,
    )

    def fail_tracker_read() -> Snapshot:
        raise OSError("injected Job Tracker read failure")

    monkeypatch.setattr(global_jobs, "load_read_only", fail_tracker_read)

    service.run()

    assert reviewer.reviewed_titles == ["Current Role"]


def test_each_scan_replaces_old_search_instead_of_reviewing_it(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    config(paths)
    repo = repository(paths)
    old = stored_job(
        "old",
        fetched(
            "OLD",
            "Old Search Role",
            "Description from another candidate search.",
            source_instance="old/jobs",
        ),
    )
    old.last_review_attempt_profile_hash = "sha256:" + "0" * 64
    repo.mutate(lambda snapshot: Snapshot(meta=snapshot.meta, jobs=[old]))
    current = fetched(
        "CURRENT",
        "Current Search Role",
        "Description found for this candidate search.",
        source_instance="current/jobs",
    )
    reviewer = RecordingReviewer()
    service = ScanService(
        paths,
        repository=repo,
        reviewer=reviewer,
        company_size_service=NoopCompanySizeService(),  # type: ignore[arg-type]
        source_factory=lambda _config: [
            FakeAdapter(SourceKind.LINKEDIN, "current/jobs", [current])
        ],
        clock=lambda: NOW,
        run_id_factory=lambda: "run-independent",
    )

    summary = service.run()

    assert reviewer.reviewed_titles == ["Current Search Role"]
    assert [job.title for job in repo.load().jobs] == ["Current Search Role"]
    assert summary.new_count == 1
    assert summary.changed_count == 0


def test_new_search_does_not_inherit_user_status_from_previous_search(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    config(paths)
    repo = repository(paths)
    occurrence = fetched(
        "SAME",
        "Role Found Again",
        "The same source job found for a different candidate.",
    )
    reviewer = RecordingReviewer()
    service = ScanService(
        paths,
        repository=repo,
        reviewer=reviewer,
        company_size_service=NoopCompanySizeService(),  # type: ignore[arg-type]
        source_factory=lambda _config: [
            FakeAdapter(SourceKind.LINKEDIN, "acme/jobs", [occurrence])
        ],
        clock=lambda: NOW,
    )
    service.run()

    def mark_applied(snapshot: Snapshot) -> Snapshot:
        snapshot.jobs[0].user_status = UserStatus.APPLIED
        snapshot.jobs[0].user_status_updated_at = NOW + timedelta(minutes=1)
        return snapshot

    repo.mutate(mark_applied)

    service.run()

    assert reviewer.reviewed_titles == ["Role Found Again", "Role Found Again"]
    assert repo.load().jobs[0].user_status is UserStatus.NEW


def test_published_callback_receives_this_runs_exact_snapshot(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    config(paths)
    repo = repository(paths)
    current = fetched("CURRENT", "Current Published Role", "Complete backend role.")
    captured: list[tuple[str, list[str]]] = []
    service = ScanService(
        paths,
        repository=repo,
        reviewer=RecordingReviewer(),
        company_size_service=NoopCompanySizeService(),  # type: ignore[arg-type]
        source_factory=lambda _config: [
            FakeAdapter(SourceKind.LINKEDIN, "acme/jobs", [current])
        ],
        clock=lambda: NOW,
        run_id_factory=lambda: "run-exact",
    )

    def record_and_simulate_later_publication(
        summary: ScanSummary,
        snapshot: Snapshot,
    ) -> None:
        captured.append((summary.run_id, [job.title for job in snapshot.jobs]))
        repo.mutate(lambda latest: Snapshot(meta=latest.meta))

    service.run(on_published=record_and_simulate_later_publication)

    assert captured == [("run-exact", ["Current Published Role"])]
    assert repo.load().jobs == []


def test_published_callback_failure_restores_previous_live_jobs(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    config(paths)
    repo = repository(paths)
    old = stored_job("old", fetched("OLD", "Previous Live Role", "Old role."))
    repo.mutate(lambda snapshot: Snapshot(meta=snapshot.meta, jobs=[old]))
    current = fetched("CURRENT", "Unarchived Role", "Complete backend role.")
    service = ScanService(
        paths,
        repository=repo,
        reviewer=RecordingReviewer(),
        company_size_service=NoopCompanySizeService(),  # type: ignore[arg-type]
        source_factory=lambda _config: [
            FakeAdapter(SourceKind.LINKEDIN, "acme/jobs", [current])
        ],
        clock=lambda: NOW,
    )

    def fail_archive(_summary: ScanSummary, _snapshot: Snapshot) -> None:
        raise OSError("archive failed")

    with pytest.raises(ScanError, match="finalize"):
        service.run(on_published=fail_archive)

    assert [job.title for job in repo.load().jobs] == ["Previous Live Role"]


class PartialFakeClaude:
    def __init__(self, repo: JsonlRepository) -> None:
        self._repo = repo
        self.reviewed_keys: list[list[str]] = []
        self._concurrent_update_done = False

    def invoke(self, request: ClaudeRequest) -> ClaudeInvocation:
        marker = "Submitted jobs (JSON; complete_jd is the complete plain-text JD):\n"
        jobs = json.loads(request.prompt.split(marker, 1)[1])
        keys = [item["job_key"] for item in jobs]
        self.reviewed_keys.append(keys)
        if not self._concurrent_update_done:
            self._concurrent_update_done = True

            def save_job(latest: Snapshot) -> Snapshot:
                current = next(
                    item for item in latest.jobs if item.canonical_job_key == "good"
                )
                current.user_status = UserStatus.SAVED
                current.user_status_updated_at = NOW + timedelta(minutes=1)
                return latest

            self._repo.mutate(save_job)

        results: list[dict[str, object]] = []
        for item in jobs:
            if item["title"] != "Needs Retry":
                results.append(
                    {
                        "job_key": item["job_key"],
                        "german_requirement": "none",
                        "visa_sponsorship": "offered",
                        "existing_work_authorization": "not_required",
                        "citizenship_requirement": "none",
                        "security_clearance": "none",
                        "staffing_agency": "no",
                        "eligibility_evidence": [],
                        "company_industry": None,
                        "company_industry_confidence": "low",
                        "company_industry_evidence": [],
                        "score": 85,
                        "reason": "Strong backend match.",
                        "confidence": "high",
                    }
                )
        stdout = json.dumps({"structured_output": {"results": results}}).encode()
        return ClaudeInvocation(
            argv=["fake-claude"],
            stdout=stdout,
            stderr=b"",
            exit_code=0,
            duration_seconds=0.01,
            budget_usd=Decimal("0.25"),
        )


def repository(paths: AppPaths) -> JsonlRepository:
    return JsonlRepository(paths, FileRWLock(paths.lock_file))


class NoopCompanySizeService:
    """Keep scan-service tests isolated from external company-size lookups."""

    def restore(self, _snapshot: Snapshot, _config: AppConfig) -> None:
        return None

    def collect_native(
        self,
        _snapshot: Snapshot,
        _config: AppConfig,
        _checked_at: datetime,
    ) -> None:
        return None

    def apply(
        self,
        _snapshot: Snapshot,
        _config: AppConfig,
        _checked_at: datetime,
        *,
        progress=None,
    ) -> None:
        del progress


def test_scan_applies_source_industry_before_ai_review(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    config(paths)
    occurrence = fetched(
        "INDUSTRY",
        "Industry Role",
        "We manufacture industrial robots for automotive factories.",
    )

    class IndustryService:
        def apply(self, snapshot, _config, checked_at) -> None:
            snapshot.jobs[0].company_industry = CompanyIndustryEvidence(
                company_name="Acme",
                industry="Industrial Automation",
                source_url="https://example.com/company",
                source_title="Source company profile",
                checked_at=checked_at,
                confidence="high",
                lookup_method="native",
                source_name="linkedin",
                evidence=[],
            )

    class IndustryRecordingReviewer:
        def __init__(self) -> None:
            self.seen: list[str | None] = []

        def review(self, jobs, _profile, _config, *, progress=None):
            del progress
            self.seen.extend(
                job.company_industry.industry
                if job.company_industry is not None
                else None
                for job in jobs
            )
            return ReviewBatchOutcome(accepted={}, failed={}, invocations=[])

    reviewer = IndustryRecordingReviewer()
    ScanService(
        paths,
        reviewer=reviewer,  # type: ignore[arg-type]
        company_size_service=NoopCompanySizeService(),  # type: ignore[arg-type]
        company_industry_service=IndustryService(),  # type: ignore[arg-type]
        source_factory=lambda _config: [
            FakeAdapter(SourceKind.LINKEDIN, "acme/jobs", [occurrence])
        ],
        clock=lambda: NOW,
        run_id_factory=lambda: "run-industry",
    ).run()

    assert reviewer.seen == ["Industrial Automation"]


def test_source_company_enrichment_finishes_before_source_is_completed(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    config(paths)
    occurrence = fetched(
        "INDUSTRY-ORDER",
        "Industry Ordering Role",
        "We manufacture industrial robots for automotive factories.",
    )
    events: list[str] = []

    class IndustryService:
        def apply(self, _snapshot, _config, _checked_at) -> None:
            events.append("company-enrichment")

    def record(current) -> None:
        source = current.source_progress
        if (
            current.stage == "sources"
            and source is not None
            and source.completed_sources == source.total_sources == 1
        ):
            events.append("source-complete")

    ScanService(
        paths,
        reviewer=RecordingReviewer(),
        company_size_service=NoopCompanySizeService(),  # type: ignore[arg-type]
        company_industry_service=IndustryService(),  # type: ignore[arg-type]
        source_factory=lambda _config: [
            FakeAdapter(SourceKind.LINKEDIN, "acme/jobs", [occurrence])
        ],
        clock=lambda: NOW,
        run_id_factory=lambda: "run-company-enrichment-order",
    ).run(progress=record)

    assert events == ["company-enrichment", "source-complete"]


def test_source_native_company_size_finishes_before_source_is_completed(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    config(paths)
    occurrence = fetched(
        "SIZE-ORDER",
        "Size Ordering Role",
        "Complete backend role.",
    )
    events: list[str] = []

    class NativeLookup:
        def lookup(self, job, _config, checked_at):
            events.append("native-size")
            return CompanySizeEvidence(
                company_name=job.company,
                band="1000-9999",
                employee_count=1200,
                source_url="https://www.acme.example/about",
                source_title="Acme company profile",
                checked_at=checked_at,
                confidence="high",
                lookup_method="native",
                source_name="linkedin",
                reported_size="1000+",
                minimum_employees=1000,
            )

    class AiLookup:
        def lookup(self, *_args, **_kwargs):
            raise AssertionError("native company size must prevent AI fallback")

    company_sizes = CompanySizeService(
        CompanySizeStore(paths.cache_dir / "company-sizes.json"),
        AiLookup(),  # type: ignore[arg-type]
        native_lookup=NativeLookup(),  # type: ignore[arg-type]
    )

    def record(current) -> None:
        source = current.source_progress
        if (
            current.stage == "sources"
            and source is not None
            and source.completed_sources == source.total_sources == 1
        ):
            events.append("source-complete")

    ScanService(
        paths,
        reviewer=AcceptingReviewer(),
        company_size_service=company_sizes,
        source_factory=lambda _config: [
            FakeAdapter(SourceKind.LINKEDIN, "acme/jobs", [occurrence])
        ],
        clock=lambda: NOW,
        run_id_factory=lambda: "run-native-size-order",
    ).run(progress=record)

    assert events == ["native-size", "source-complete"]


def test_each_source_checks_only_companies_found_by_that_source(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    config(paths)
    acme = fetched("ACME", "Acme Role", "Complete backend role.")
    beta = fetched(
        "BETA",
        "Beta Role",
        "Complete backend role.",
        source=SourceKind.INDEED,
        source_instance="beta/jobs",
    )
    beta.company = "Beta"
    native_calls: list[str] = []

    class MissingNativeLookup:
        def lookup(self, current, _config, _checked_at):
            native_calls.append(current.company)

    class UnknownAiLookup:
        def lookup(self, company, _config, checked_at, *, location=None):
            del location
            return CompanySizeEvidence(
                company_name=company,
                band="unknown",
                checked_at=checked_at,
                confidence="low",
                lookup_method="unknown",
            )

    company_sizes = CompanySizeService(
        CompanySizeStore(paths.cache_dir / "company-sizes.json"),
        UnknownAiLookup(),  # type: ignore[arg-type]
        native_lookup=MissingNativeLookup(),  # type: ignore[arg-type]
    )

    ScanService(
        paths,
        reviewer=AcceptingReviewer(),
        company_size_service=company_sizes,
        source_factory=lambda _config: [
            FakeAdapter(SourceKind.LINKEDIN, "acme/jobs", [acme]),
            FakeAdapter(SourceKind.INDEED, "beta/jobs", [beta]),
        ],
        clock=lambda: NOW,
        run_id_factory=lambda: "run-source-company-scope",
    ).run()

    assert native_calls == ["Acme", "Beta"]


@pytest.mark.parametrize(
    ("source_kind", "source_name", "external_id", "profile_url"),
    [
        (
            SourceKind.LINKEDIN,
            "linkedin",
            "4423914728",
            "https://www.linkedin.com/company/acme/about/",
        ),
        (
            SourceKind.INDEED,
            "indeed",
            "abcdef0123456789",
            "https://de.indeed.com/cmp/Acme/about",
        ),
        (
            SourceKind.STEPSTONE,
            "stepstone",
            "1234567890",
            "https://www.stepstone.de/cmp/de/Acme-123/jobs",
        ),
        (
            SourceKind.GLASSDOOR,
            "glassdoor",
            "123456789012",
            "https://www.glassdoor.de/Overview/Working-at-Acme-EI_IE123.htm",
        ),
    ],
)
def test_source_company_profile_is_opened_once_for_size_and_industry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_kind: SourceKind,
    source_name: str,
    external_id: str,
    profile_url: str,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    config(paths)
    executable = tmp_path / "opencli"
    calls_path = tmp_path / "opencli-calls.jsonl"
    executable.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import json",
                "import pathlib",
                "import sys",
                f"calls = pathlib.Path({str(calls_path)!r})",
                "args = sys.argv[1:]",
                "with calls.open('a', encoding='utf-8') as output:",
                "    output.write(json.dumps(args) + '\\n')",
                "if args[:2] == ['linkedin', 'job-detail']:",
                "    print(json.dumps([{'company_url': 'https://www.linkedin.com/company/acme/life'}]))",
                "elif args[0] != 'browser':",
                "    raise SystemExit(78)",
                "elif args[2] == 'eval' and 'readSize' in args[3]:",
                "    print(json.dumps({'status': 'ok', 'reported_size': '1000+'}))",
                "elif args[2] == 'eval':",
                "    print(json.dumps({'status': 'ok', 'reported_industry': 'IT Services'}))",
                "else:",
                "    print(json.dumps({'status': 'ok'}))",
            ]
        ),
        encoding="utf-8",
    )
    executable.chmod(0o755)
    monkeypatch.setenv("JOB_SCAN_OPENCLI", str(executable))
    occurrence = fetched(
        external_id,
        "Company Facts Role",
        "Complete backend role.",
        source=source_kind,
        source_instance=source_name,
    )
    occurrence.company_size_source = CompanySizeSource(
        source_name=source_name,
        lookup_url=profile_url,
        public_url=profile_url,
        source_title=f"{source_name} company profile",
    )
    occurrence.company_industry_source = CompanyIndustrySource(
        source_name=source_name,
        lookup_url=profile_url,
        public_url=profile_url,
        source_title=f"{source_name} company profile",
    )
    repo = repository(paths)

    ScanService(
        paths,
        repository=repo,
        reviewer=AcceptingReviewer(),
        source_factory=lambda _config: [
            FakeAdapter(source_kind, source_name, [occurrence])
        ],
        clock=lambda: NOW,
        run_id_factory=lambda: "run-company-facts-once",
    ).run()

    result = repo.load().jobs[0]
    calls = [json.loads(line) for line in calls_path.read_text().splitlines()]
    open_calls = [call for call in calls if call[0] == "browser" and call[2] == "open"]
    assert len(open_calls) == 1
    assert result.company_size is not None
    assert result.company_size.reported_size == "1000+"
    assert result.company_industry is not None
    assert result.company_industry.industry == "IT Services"


def test_new_scan_reviews_a_job_even_when_an_old_search_already_reviewed_it(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    config(paths)
    occurrence = fetched("SAVED", "Saved Role", "Backend role with no visa details.")
    saved = stored_job("saved", occurrence)
    saved.machine_status = MachineStatus.UNCERTAIN
    saved.ai_review = AIReview(
        job_key=saved.canonical_job_key,
        german_requirement="none",
        visa_sponsorship="not_mentioned",
        existing_work_authorization="not_mentioned",
        citizenship_requirement="none",
        security_clearance="none",
        staffing_agency="no",
        company_industry=None,
        company_industry_confidence="low",
        company_industry_evidence=[],
        score=85,
        reason="Strong backend match.",
        confidence="high",
    )
    repo = repository(paths)
    repo.mutate(lambda old: Snapshot(meta=old.meta, jobs=[saved]))

    reviewer = RecordingReviewer()

    service = ScanService(
        paths,
        repository=repo,
        reviewer=reviewer,
        company_size_service=NoopCompanySizeService(),  # type: ignore[arg-type]
        source_factory=lambda _config: [
            FakeAdapter(SourceKind.LINKEDIN, "acme/jobs", [occurrence])
        ],
        clock=lambda: NOW,
        run_id_factory=lambda: "run-policy-refresh",
    )

    summary = service.run()
    result = repo.load().jobs[0]

    assert reviewer.reviewed_titles == ["Saved Role"]
    assert summary.new_count == 1
    assert result.machine_status is MachineStatus.PENDING


def test_default_source_factory_includes_opencli_sources_after_jobsuche(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()

    adapters = _default_source_factory(paths)(config(paths))

    assert [adapter.source for adapter in adapters] == [
        SourceKind.ARBEITSAGENTUR,
        SourceKind.LINKEDIN,
        SourceKind.INDEED,
        SourceKind.STEPSTONE,
        SourceKind.GLASSDOOR,
        SourceKind.SIMPLIFY,
    ]


def test_scan_keeps_a_saved_snapshot_reference_on_the_next_scan(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    config(paths)
    occurrence = fetched("SNAPSHOT", "Saved Snapshot Role", "Build backend services.")
    failed_occurrence = fetched(
        "SNAPSHOT-FAILED",
        "Failed Snapshot Role",
        "Operate backend services.",
    )
    saved = stored_job("saved-snapshot", occurrence)
    failed = stored_job("failed-snapshot", failed_occurrence)
    snapshot_reference = JobSnapshotReference(
        snapshot_id=f"sha256:{'a' * 64}",
        captured_at=NOW - timedelta(days=1),
    )
    latest = saved.source_occurrences[0]
    latest.source_generation = 2
    latest.job_snapshot = snapshot_reference
    saved.primary_source_occurrence_key = latest.source_occurrence_key
    older = latest.model_copy(
        deep=True,
        update={
            "source_generation": 1,
            "availability_status": AvailabilityStatus.CLOSED,
            "closed_at": NOW - timedelta(days=2),
            "job_snapshot": JobSnapshotReference(
                snapshot_id=f"sha256:{'b' * 64}",
                captured_at=NOW - timedelta(days=3),
            ),
        },
    )
    saved.source_occurrences.append(older)
    failed.source_occurrences[0].job_snapshot_error_code = "snapshot_capture_failed"
    repo = repository(paths)
    repo.mutate(lambda old: Snapshot(meta=old.meta, jobs=[saved, failed]))

    ScanService(
        paths,
        repository=repo,
        reviewer=RecordingReviewer(),
        company_size_service=NoopCompanySizeService(),  # type: ignore[arg-type]
        source_factory=lambda _config: [
            FakeAdapter(
                SourceKind.LINKEDIN,
                "acme/jobs",
                [occurrence, failed_occurrence],
            )
        ],
        clock=lambda: NOW,
        run_id_factory=lambda: "run-snapshot-retention",
    ).run()

    current = {
        item.external_id: item
        for job in repo.load().jobs
        for item in job.source_occurrences
    }
    assert current["SNAPSHOT"].job_snapshot == snapshot_reference
    assert current["SNAPSHOT"].job_snapshot_error_code is None
    assert current["SNAPSHOT-FAILED"].job_snapshot is None
    assert (
        current["SNAPSHOT-FAILED"].job_snapshot_error_code
        == "snapshot_capture_failed"
    )


@pytest.mark.parametrize(
    ("source", "source_instance", "external_id"),
    [
        (SourceKind.ARBEITSAGENTUR, "default", "10000-1234567890-S"),
        (SourceKind.LINKEDIN, "default", "4454519520"),
        (SourceKind.INDEED, "de", "8c683c2df48291d7"),
        (SourceKind.STEPSTONE, "de", "14358591"),
        (SourceKind.GLASSDOOR, "de", "1010232175081"),
        (SourceKind.SIMPLIFY, "de", "4189a132-d02f-4d3a-90ab-df09f5743198"),
    ],
)
def test_default_browser_source_only_captures_jobs_absent_at_scan_start(
    tmp_path: Path,
    source: SourceKind,
    source_instance: str,
    external_id: str,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    adapters = _default_source_factory(
        paths,
        existing_source_occurrences={
            f"{source.value}:{source_instance}:{external_id}": SourceOccurrence(
                source=source,
                source_instance=source_instance,
                external_id=external_id,
                source_generation=1,
                url=f"https://example.com/jobs/{external_id}",
                company="Example GmbH",
                title="Engineer",
                location="Berlin",
                description="Build software.",
                posted_at=date(2026, 8, 1),
                content_hash=content_hash(
                    "Example GmbH", "Engineer", "Berlin", "Build software."
                ),
                availability_status=AvailabilityStatus.ACTIVE,
                detail_complete=True,
            )
        },
    )(config(paths))
    adapter = next(item for item in adapters if item.source is source)
    existing = JobReference(
        source=source,
        source_instance=source_instance,
        external_id=external_id,
        detail_url=f"https://example.com/jobs/{external_id}",
        listing_title="Engineer",
        listing_company="Example GmbH",
        listing_location="Berlin",
    )
    new = existing.model_copy(update={"external_id": f"{external_id}2"})

    assert adapter._capture_snapshot is not None  # type: ignore[attr-defined]
    assert adapter._capture_snapshot(existing) is False  # type: ignore[attr-defined]
    assert adapter._capture_snapshot(new) is True  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("company", "source", "source_instance", "external_id"),
    [
        ("bosch", SourceKind.BOSCH, "bosch", "REF300001A"),
        ("telekom", SourceKind.TELEKOM, "telekom", "907522"),
        ("rohde-schwarz", SourceKind.SUCCESSFACTORS, "rohdeschwarz", "707"),
        ("siemens", SourceKind.SIEMENS, "siemens", "513387"),
        ("dhl", SourceKind.DHL, "dhl", "DPDHGLOBALAV361651ENAMEREXTERNAL"),
        ("thyssenkrupp", SourceKind.THYSSENKRUPP, "thyssenkrupp", "967315"),
        (
            "dallmeier",
            SourceKind.DALLMEIER,
            "dallmeier",
            "java-developer-w/m/d-backend",
        ),
    ],
)
def test_default_company_source_only_captures_jobs_absent_at_scan_start(
    tmp_path: Path,
    company: str,
    source: SourceKind,
    source_instance: str,
    external_id: str,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    existing_key = f"{source.value}:{source_instance}:{external_id}"
    value = config(paths).model_copy(update={"target_companies": [company]})
    adapters = _default_source_factory(
        paths,
        existing_source_occurrences={
            existing_key: SourceOccurrence(
                source=source,
                source_instance=source_instance,
                external_id=external_id,
                source_generation=1,
                url=f"https://example.com/jobs/{external_id}",
                company="Example GmbH",
                title="Engineer",
                location="Berlin",
                description="Build software.",
                posted_at=date(2026, 8, 1),
                content_hash=content_hash(
                    "Example GmbH", "Engineer", "Berlin", "Build software."
                ),
                availability_status=AvailabilityStatus.ACTIVE,
                detail_complete=True,
            )
        },
    )(value)
    adapter = next(
        item
        for item in adapters
        if item.source is source and item.source_instance == source_instance
    )
    existing = JobReference(
        source=source,
        source_instance=source_instance,
        external_id=external_id,
        detail_url=f"https://example.com/jobs/{external_id}",
        listing_title="Engineer",
        listing_company="Example GmbH",
        listing_location="Berlin",
    )
    new = existing.model_copy(update={"external_id": f"{external_id}-new"})

    assert adapter._capture_snapshot is not None  # type: ignore[attr-defined]
    assert adapter._capture_snapshot(existing) is False  # type: ignore[attr-defined]
    assert adapter._capture_snapshot(new) is True  # type: ignore[attr-defined]


def test_closed_reused_source_id_saves_a_new_generation_snapshot(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    value = config(paths).model_copy(
        update={
            "arbeitsagentur_enabled": False,
            "linkedin_enabled": False,
            "indeed_de_enabled": False,
            "stepstone_de_enabled": True,
            "glassdoor_de_enabled": False,
            "simplify_de_enabled": False,
        }
    )
    old = fetched(
        "REUSED",
        "Old Backend Role",
        "a" * 100,
        source=SourceKind.STEPSTONE,
        source_instance="de",
    )
    stored = stored_job("old-canonical", old)
    old_occurrence = stored.source_occurrences[0]
    old_occurrence.availability_status = AvailabilityStatus.CLOSED
    old_occurrence.job_snapshot = JobSnapshotReference(
        snapshot_id=f"sha256:{'a' * 64}",
        captured_at=NOW - timedelta(days=100),
    )
    stored.availability_status = AvailabilityStatus.CLOSED
    replacement = fetched(
        "REUSED",
        "New Security Role",
        "z" * 100,
        source=SourceKind.STEPSTONE,
        source_instance="de",
    )
    reference = JobReference(
        source=replacement.source,
        source_instance=replacement.source_instance,
        external_id=replacement.external_id,
        detail_url=replacement.url,
        listing_title=replacement.title,
        listing_company=replacement.company,
        listing_location=replacement.location,
        listing_posted_at=replacement.posted_at,
    )
    adapter = _default_source_factory(
        paths,
        existing_source_occurrences={old_occurrence.source_job_key: old_occurrence},
    )(value)[0]

    assert adapter._capture_snapshot is not None  # type: ignore[attr-defined]
    assert adapter._capture_snapshot(reference) is True  # type: ignore[attr-defined]
    replacement.job_snapshot_html = f"""<!doctype html>
<html data-job-scan-snapshot="{replacement.source_job_key}">
<head><style>body {{ color: #0c2577; }}</style></head>
<body><main>New Security Role</main></body>
</html>
"""
    _carry_existing_job_snapshots(
        [replacement],
        {old_occurrence.source_job_key: old_occurrence},
    )
    _persist_job_snapshots(
        [replacement],
        JobSnapshotStore(paths.job_snapshots_dir),
        NOW,
    )

    result = merge_occurrences(
        Snapshot(meta=StoreMeta(data_revision=1), jobs=[stored]),
        [replacement],
        NOW,
    )
    generations = sorted(
        (
            occurrence
            for job in result.jobs
            for occurrence in job.source_occurrences
            if occurrence.source_job_key == old_occurrence.source_job_key
        ),
        key=lambda occurrence: occurrence.source_generation,
    )
    assert [occurrence.source_generation for occurrence in generations] == [1, 2]
    assert generations[0].job_snapshot == old_occurrence.job_snapshot
    assert generations[1].job_snapshot is not None
    assert generations[1].job_snapshot.snapshot_id != old_occurrence.job_snapshot.snapshot_id


def test_active_reused_source_id_after_sixty_days_requests_a_new_snapshot(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    value = config(paths).model_copy(
        update={
            "arbeitsagentur_enabled": False,
            "linkedin_enabled": False,
            "indeed_de_enabled": False,
            "stepstone_de_enabled": True,
            "glassdoor_de_enabled": False,
            "simplify_de_enabled": False,
        }
    )
    old = fetched(
        "REUSED-ACTIVE",
        "Old Backend Role",
        "a" * 100,
        source=SourceKind.STEPSTONE,
        source_instance="de",
    )
    stored = stored_job("old-active-canonical", old).source_occurrences[0]
    stored.posted_at = date(2026, 1, 1)
    replacement = fetched(
        "REUSED-ACTIVE",
        "New Security Role",
        "z" * 100,
        source=SourceKind.STEPSTONE,
        source_instance="de",
    )
    reference = JobReference(
        source=replacement.source,
        source_instance=replacement.source_instance,
        external_id=replacement.external_id,
        detail_url=replacement.url,
        listing_title=replacement.title,
        listing_company=replacement.company,
        listing_location=replacement.location,
        listing_posted_at=replacement.posted_at,
    )
    adapter = _default_source_factory(
        paths,
        existing_source_occurrences={stored.source_job_key: stored},
    )(value)[0]

    assert adapter._capture_snapshot is not None  # type: ignore[attr-defined]
    assert adapter._capture_snapshot(reference) is True  # type: ignore[attr-defined]


def test_reused_source_id_cannot_copy_the_old_snapshot_into_new_generation() -> None:
    old = fetched("REUSED-DIRECT", "Old Backend Role", "a" * 100)
    stored = stored_job("old-direct-canonical", old).source_occurrences[0]
    stored.availability_status = AvailabilityStatus.CLOSED
    old_snapshot = JobSnapshotReference(
        snapshot_id=f"sha256:{'a' * 64}",
        captured_at=NOW - timedelta(days=100),
    )
    stored.job_snapshot = old_snapshot
    replacement = fetched("REUSED-DIRECT", "New Security Role", "z" * 100)
    replacement.job_snapshot = old_snapshot.model_copy(deep=True)

    _carry_existing_job_snapshots(
        [replacement],
        {stored.source_job_key: stored},
    )

    assert replacement.job_snapshot is None
    assert replacement.job_snapshot_error_code == "snapshot_capture_failed"


def test_reused_source_id_rejects_html_with_the_old_snapshot_sha() -> None:
    old = fetched("REUSED-HTML", "Old Backend Role", "a" * 100)
    stored = stored_job("old-html-canonical", old).source_occurrences[0]
    stored.availability_status = AvailabilityStatus.CLOSED
    old_html = f"""<!doctype html>
<html data-job-scan-snapshot="{stored.source_job_key}">
<head><style>body {{ color: #0c2577; }}</style></head>
<body><main>Old Backend Role</main></body>
</html>
"""
    stored.job_snapshot = JobSnapshotReference(
        snapshot_id=f"sha256:{hashlib.sha256(old_html.encode('utf-8')).hexdigest()}",
        captured_at=NOW - timedelta(days=100),
    )
    replacement = fetched("REUSED-HTML", "New Security Role", "z" * 100)
    replacement.job_snapshot_html = old_html

    _carry_existing_job_snapshots(
        [replacement],
        {stored.source_job_key: stored},
    )

    assert replacement.job_snapshot_html is None
    assert replacement.job_snapshot is None
    assert replacement.job_snapshot_error_code == "snapshot_capture_failed"


def test_default_source_factory_includes_selected_bosch_source(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    value = config(paths).model_copy(update={"target_companies": ["bosch"]})

    adapters = _default_source_factory(paths)(value)

    assert [adapter.source for adapter in adapters] == [
        SourceKind.ARBEITSAGENTUR,
        SourceKind.BOSCH,
        SourceKind.LINKEDIN,
        SourceKind.INDEED,
        SourceKind.STEPSTONE,
        SourceKind.GLASSDOOR,
        SourceKind.SIMPLIFY,
    ]
    assert isinstance(adapters[1], BoschAdapter)
    assert adapters[1].source_instance == "bosch"


def test_default_source_factory_includes_selected_target_companies_together(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    value = config(paths).model_copy(
        update={
            "target_companies": [
                "bosch",
                "telekom",
                "rohde-schwarz",
                "siemens",
                "dhl",
                "thyssenkrupp",
                "dallmeier",
            ]
        }
    )

    adapters = _default_source_factory(paths)(value)

    assert [adapter.source for adapter in adapters] == [
        SourceKind.ARBEITSAGENTUR,
        SourceKind.BOSCH,
        SourceKind.TELEKOM,
        SourceKind.SUCCESSFACTORS,
        SourceKind.SIEMENS,
        SourceKind.DHL,
        SourceKind.THYSSENKRUPP,
        SourceKind.DALLMEIER,
        SourceKind.LINKEDIN,
        SourceKind.INDEED,
        SourceKind.STEPSTONE,
        SourceKind.GLASSDOOR,
        SourceKind.SIMPLIFY,
    ]
    assert adapters[1].source_instance == "bosch"
    assert adapters[2].source_instance == "telekom"
    assert adapters[3].source_instance == "rohdeschwarz"
    assert adapters[4].source_instance == "siemens"
    assert adapters[5].source_instance == "dhl"
    assert adapters[6].source_instance == "thyssenkrupp"
    assert isinstance(adapters[7], DallmeierAdapter)
    assert adapters[7].source_instance == "dallmeier"


def test_default_http_cache_is_scoped_to_its_run_id(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    saved = config(paths)
    save_config(
        paths.config_toml,
        saved.model_copy(
            update={
                "arbeitsagentur_enabled": False,
                "linkedin_enabled": False,
                "indeed_de_enabled": False,
                "stepstone_de_enabled": False,
                "glassdoor_de_enabled": False,
                "simplify_de_enabled": False,
            }
        ),
    )
    service = ScanService(
        paths,
        clock=lambda: NOW,
        run_id_factory=lambda: "run-cache-isolated",
    )

    service.run()

    assert (paths.cache_dir / "runs" / "run-cache-isolated").is_dir()


def test_default_scan_reuses_shared_company_size_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    config(paths)
    cached = CompanySizeEvidence(
        company_name="Acme",
        band="250-999",
        employee_count=500,
        source_url="https://www.acme.example/about",
        source_title="Acme facts",
        checked_at=NOW,
        confidence="high",
    )
    CompanySizeStore(paths.cache_dir / "company-sizes.json").save({"acme": cached})

    def reject_external_lookup(_self: object, _request: ClaudeRequest) -> object:
        raise AssertionError("fresh shared cache must prevent another AI lookup")

    monkeypatch.setattr(
        scan_service_module.AiRuntimeInvoker,
        "invoke",
        reject_external_lookup,
    )
    occurrence = fetched(
        "CACHED-SIZE",
        "Cached Company Role",
        "Complete backend role.",
        source=SourceKind.BOSCH,
        source_instance="bosch",
    )
    repo = repository(paths)
    service = ScanService(
        paths,
        repository=repo,
        reviewer=AcceptingReviewer(),
        source_factory=lambda _config: [
            FakeAdapter(SourceKind.BOSCH, "bosch", [occurrence])
        ],
        clock=lambda: NOW,
        run_id_factory=lambda: "run-shared-company-size-cache",
    )

    service.run()

    assert repo.load().jobs[0].company_size == cached


def test_default_source_factory_skips_arbeitsagentur_when_disabled(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    value = config(paths).model_copy(update={"arbeitsagentur_enabled": False})

    adapters = _default_source_factory(paths)(value)

    assert [adapter.source for adapter in adapters] == [
        SourceKind.LINKEDIN,
        SourceKind.INDEED,
        SourceKind.STEPSTONE,
        SourceKind.GLASSDOOR,
        SourceKind.SIMPLIFY,
    ]


@pytest.mark.parametrize(
    (
        "disabled_field",
        "expected_sources",
    ),
    [
        (
            "linkedin_enabled",
            [
                SourceKind.ARBEITSAGENTUR,
                SourceKind.INDEED,
                SourceKind.STEPSTONE,
                SourceKind.GLASSDOOR,
                SourceKind.SIMPLIFY,
            ],
        ),
        (
            "indeed_de_enabled",
            [
                SourceKind.ARBEITSAGENTUR,
                SourceKind.LINKEDIN,
                SourceKind.STEPSTONE,
                SourceKind.GLASSDOOR,
                SourceKind.SIMPLIFY,
            ],
        ),
        (
            "stepstone_de_enabled",
            [
                SourceKind.ARBEITSAGENTUR,
                SourceKind.LINKEDIN,
                SourceKind.INDEED,
                SourceKind.GLASSDOOR,
                SourceKind.SIMPLIFY,
            ],
        ),
        (
            "glassdoor_de_enabled",
            [
                SourceKind.ARBEITSAGENTUR,
                SourceKind.LINKEDIN,
                SourceKind.INDEED,
                SourceKind.STEPSTONE,
                SourceKind.SIMPLIFY,
            ],
        ),
        (
            "simplify_de_enabled",
            [
                SourceKind.ARBEITSAGENTUR,
                SourceKind.LINKEDIN,
                SourceKind.INDEED,
                SourceKind.STEPSTONE,
                SourceKind.GLASSDOOR,
            ],
        ),
    ],
)
def test_default_source_factory_skips_opencli_sources_with_disabled_switch(
    tmp_path: Path,
    disabled_field: str,
    expected_sources: list[SourceKind],
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    value = config(paths).model_copy(update={disabled_field: False})

    adapters = _default_source_factory(paths)(value)

    assert [adapter.source for adapter in adapters] == expected_sources


def test_scan_reports_each_real_workflow_phase_before_completion(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    config(paths)
    stages: list[str] = []
    service = ScanService(
        paths,
        source_factory=lambda _config: [],
        clock=lambda: NOW,
    )

    service.run(progress=lambda current: stages.append(current.stage))

    assert stages == ["sources", "review", "company_size", "publish"]


def test_scan_reports_progress_after_each_fetched_job_and_completed_source(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    config(paths)
    source_progress: list[tuple[int, int, int, int] | None] = []
    occurrences = [
        fetched("JOB-1", "Role 1", "Complete backend role."),
        fetched("JOB-2", "Role 2", "Complete backend role."),
    ]

    class ProgressNoopCompanySizeService(NoopCompanySizeService):
        def apply(self, _snapshot, _config, _checked_at, *, progress=None) -> None:
            del progress

    def record(current) -> None:
        if current.stage != "sources":
            return
        source = getattr(current, "source_progress", None)
        source_progress.append(
            None
            if source is None
            else (
                source.completed_sources,
                source.total_sources,
                source.found_jobs,
                source.warning_count,
            )
        )

    ScanService(
        paths,
        company_size_service=ProgressNoopCompanySizeService(),  # type: ignore[arg-type]
        reviewer=RecordingReviewer(),
        source_factory=lambda _config: [
            FakeAdapter(SourceKind.LINKEDIN, "acme/jobs", occurrences),
            FakeAdapter(
                SourceKind.INDEED,
                "indeed/de",
                discovery_error=RuntimeError("source unavailable"),
            ),
        ],
        clock=lambda: NOW,
    ).run(progress=record)

    assert source_progress == [
        (0, 2, 0, 0),
        (0, 2, 1, 0),
        (0, 2, 2, 0),
        (1, 2, 2, 0),
        (2, 2, 2, 1),
    ]


def test_scan_reports_review_progress_for_each_completed_batch(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    saved = config(paths)
    save_config(
        paths.config_toml,
        saved.model_copy(
            update={"claude": saved.claude.model_copy(update={"batch_size": 2})}
        ),
    )
    occurrences = [
        fetched(f"JOB-{index}", f"Role {index}", "Complete backend role.")
        for index in range(3)
    ]

    class ProgressReviewer:
        def review(
            self,
            jobs: Sequence[JobRecord],
            profile: str,
            app_config: AppConfig,
            *,
            progress=None,
        ) -> ReviewBatchOutcome:
            del profile, app_config
            assert len(jobs) == 3
            assert progress is not None
            progress(ReviewBatchProgress(1, 2, 2, 3))
            progress(ReviewBatchProgress(2, 2, 3, 3))
            return ReviewBatchOutcome(accepted={}, failed={}, invocations=[])

    observed: list[tuple[str, tuple[int, int, int, int] | None]] = []

    def record(current) -> None:
        review = current.review
        observed.append(
            (
                current.stage,
                None
                if review is None
                else (
                    review.completed_batches,
                    review.total_batches,
                    review.completed_jobs,
                    review.total_jobs,
                ),
            )
        )

    ScanService(
        paths,
        reviewer=ProgressReviewer(),  # type: ignore[arg-type]
        source_factory=lambda _config: [
            FakeAdapter(SourceKind.LINKEDIN, "acme/jobs", occurrences)
        ],
        clock=lambda: NOW,
    ).run(progress=record)

    assert observed == [
        ("sources", None),
        ("sources", None),
        ("sources", None),
        ("sources", None),
        ("sources", None),
        ("review", (0, 2, 0, 3)),
        ("review", (1, 2, 2, 3)),
        ("review", (2, 2, 3, 3)),
        ("company_size", None),
        ("publish", None),
    ]


def test_scan_isolates_source_and_review_failures_without_old_search_state(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    config(paths)
    repo = repository(paths)
    old_good = stored_job(
        "good", fetched("GOOD", "Good Role", "Old backend description.")
    )
    old_jobsuche = stored_job(
        "jobsuche-old",
        fetched(
            "BA-OLD",
            "Jobsuche Role",
            "Existing Jobsuche description.",
            source=SourceKind.ARBEITSAGENTUR,
            source_instance="default",
        ),
    )
    old_blocked = stored_job(
        "blocked-old",
        fetched(
            "BLOCKED-OLD",
            "Blocked Company Role",
            "Existing blocked source description.",
            source=SourceKind.GLASSDOOR,
            source_instance="blocked.example",
        ),
    )
    repo.mutate(
        lambda old: Snapshot(
            meta=old.meta,
            jobs=[old_good, old_jobsuche, old_blocked],
        )
    )
    current_good = fetched("GOOD", "Good Role", "New backend description.")
    retry = fetched("RETRY", "Needs Retry", "Review this backend role.")
    adapters: list[SourceAdapter] = [
        FakeAdapter(
            SourceKind.ARBEITSAGENTUR,
            "default",
            discovery_error=TimeoutError("jobsuche unavailable"),
        ),
        FakeAdapter(SourceKind.LINKEDIN, "acme/jobs", [current_good, retry]),
        FakeAdapter(
            SourceKind.GLASSDOOR,
            "blocked.example",
            discovery_error=BlockedResponse("blocked"),
        ),
    ]
    fake_claude = PartialFakeClaude(repo)
    service = ScanService(
        paths,
        repository=repo,
        reviewer=ClaudeReviewer(fake_claude),
        company_size_service=NoopCompanySizeService(),  # type: ignore[arg-type]
        source_factory=lambda _config: adapters,
        clock=lambda: NOW,
        run_id_factory=lambda: "run-1",
    )

    first = service.run()
    persisted = repo.load()

    jobs_by_title = {job.title: job for job in persisted.jobs}
    good_job = jobs_by_title["Good Role"]
    assert good_job.machine_status is MachineStatus.ELIGIBLE
    assert good_job.user_status is UserStatus.NEW
    retry_job = next(job for job in persisted.jobs if job.title == "Needs Retry")
    assert retry_job.machine_status is MachineStatus.PENDING
    assert set(jobs_by_title) == {"Good Role", "Needs Retry"}
    assert first.occurrence_count == 2
    assert first.new_count == 2
    assert first.changed_count == 0
    assert first.reviewed_count == 1
    assert first.source_error_count == 2
    assert first.source_counts == {
        "arbeitsagentur:default": 0,
        "glassdoor:blocked.example": 0,
        "linkedin:acme/jobs": 2,
    }
    assert {error.category for error in first.source_errors} == {"blocked", "http"}
    assert first.eligible_count == 1
    assert first.pending_count == 1
    assert first.claude_failure_count == 1
    assert first.claude_failure_counts == {"missing": 1}
    assert first.claude_batch_count == 2
    assert first.claude_budget_usd == Decimal("0.50")
    assert first.jobs_jsonl == paths.jobs_jsonl
    assert first.dashboard_html == paths.dashboard_html
    assert paths.jobs_jsonl.exists()
    assert paths.dashboard_html.exists()

    second = service.run()

    assert len(fake_claude.reviewed_keys) == 4
    assert fake_claude.reviewed_keys[0] == fake_claude.reviewed_keys[2]
    assert set(fake_claude.reviewed_keys[0]) == {
        retry_job.canonical_job_key,
        good_job.canonical_job_key,
    }
    assert fake_claude.reviewed_keys[1] == fake_claude.reviewed_keys[3] == [
        retry_job.canonical_job_key
    ]
    assert second.new_count == 2
    assert second.changed_count == 0
    assert second.reviewed_count == 1
    assert len(repo.load().jobs) == 2


class ForcedFailureReviewer:
    def __init__(self, repo: JsonlRepository) -> None:
        self._repo = repo

    def review(
        self, jobs: Sequence[JobRecord], profile: str, app_config: AppConfig
    ) -> ReviewBatchOutcome:
        del profile
        assert len(jobs) == 1
        item = jobs[0]

        def add_matching_override(latest: Snapshot) -> Snapshot:
            current = latest.jobs[0]
            current.manual_override = "show"
            current.manual_override_content_hash = item.content_hash
            current.manual_override_profile_hash = PROFILE_HASH
            return latest

        self._repo.mutate(add_matching_override)
        failure = ReviewFailure(
            job_key=item.canonical_job_key,
            category="timeout",
            message="Claude review timed out.",
            model=app_config.claude.model,
        )
        return ReviewBatchOutcome(accepted={}, failed={item.canonical_job_key: failure}, invocations=[])


def test_force_failure_discards_old_concurrent_override(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    config(paths)
    repo = repository(paths)
    occurrence = fetched("FORCED", "Forced Role", "Backend role to force review.")
    repo.mutate(
        lambda old: Snapshot(
            meta=old.meta,
            jobs=[stored_job("forced", occurrence)],
        )
    )
    reviewer = ForcedFailureReviewer(repo)
    service = ScanService(
        paths,
        repository=repo,
        reviewer=reviewer,
        company_size_service=NoopCompanySizeService(),  # type: ignore[arg-type]
        source_factory=lambda _config: [
            FakeAdapter(SourceKind.LINKEDIN, "acme/jobs", [occurrence])
        ],
        clock=lambda: NOW,
        run_id_factory=lambda: "run-force",
    )

    summary = service.run(force_review=True)
    result = repo.load().jobs[0]

    assert len(result.review_history) == 1
    assert result.review_history[0].outcome == "failed"
    assert result.manual_override is None
    assert result.manual_override_content_hash is None
    assert result.manual_override_profile_hash is None
    assert summary.claude_failure_count == 1


class AcceptingReviewer:
    def review(
        self,
        jobs: Sequence[JobRecord],
        _profile: str,
        _config: AppConfig,
        *,
        progress=None,
    ) -> ReviewBatchOutcome:
        del progress
        return ReviewBatchOutcome(
            accepted={
                job.canonical_job_key: AIReview(
                    job_key=job.canonical_job_key,
                    german_requirement="none",
                    visa_sponsorship="offered",
                    existing_work_authorization="not_required",
                    citizenship_requirement="none",
                    security_clearance="none",
                    staffing_agency="no",
                    company_industry=None,
                    company_industry_confidence="low",
                    company_industry_evidence=[],
                    score=85,
                    reason="Strong match.",
                    confidence="high",
                )
                for job in jobs
            },
            failed={},
            invocations=[],
        )


class SmallCompanyLookup:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def lookup(
        self,
        company: str,
        _config: AppConfig,
        checked_at: datetime,
        *,
        location: str | None = None,
    ) -> CompanySizeEvidence:
        del location
        self.calls.append(company)
        return CompanySizeEvidence(
            company_name=company,
            band="50-249",
            employee_count=120,
            source_url="https://www.acme.example/about",
            source_title="Acme facts",
            checked_at=checked_at,
            confidence="high",
        )


def test_scan_reviews_then_filters_verified_small_companies(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    saved = config(paths)
    save_config(paths.config_toml, saved.model_copy(update={"minimum_company_size": 250}))
    occurrence = fetched("SMALL", "Small Company Role", "Complete backend role.")
    lookup = SmallCompanyLookup()
    company_sizes = CompanySizeService(
        CompanySizeStore(paths.cache_dir / "company-sizes.json"),
        lookup,
    )
    repo = repository(paths)
    service = ScanService(
        paths,
        repository=repo,
        reviewer=AcceptingReviewer(),
        company_size_service=company_sizes,
        source_factory=lambda _config: [
            FakeAdapter(SourceKind.LINKEDIN, "acme/jobs", [occurrence])
        ],
        clock=lambda: NOW,
        run_id_factory=lambda: "run-company-size",
    )

    summary = service.run()
    result = repo.load().jobs[0]

    assert lookup.calls == ["Acme"]
    assert result.ai_review is not None
    assert result.company_size is not None
    assert result.company_size.band == "50-249"
    assert result.machine_status is MachineStatus.EXCLUDED
    assert result.exclusion_reasons == ["company_too_small"]
    assert summary.reviewed_count == 1
    assert summary.excluded_count == 1


@respx.mock
def test_default_scan_uses_source_native_company_size_before_ai(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    saved = config(paths)
    save_config(paths.config_toml, saved.model_copy(update={"minimum_company_size": 1000}))
    occurrence = fetched(
        "10000-123456-S",
        "JetBrains Role",
        "Complete backend role.",
        source=SourceKind.ARBEITSAGENTUR,
        source_instance="default",
    )
    profile_url = (
        "https://rest.arbeitsagentur.de/vermittlung/"
        "ag-darstellung-service/pc/v1/arbeitgeberdarstellung/hash"
    )
    occurrence.company_size_source = CompanySizeSource(
        source_name="arbeitsagentur",
        lookup_url=profile_url,
        public_url=(
            "https://www.arbeitsagentur.de/jobsuche/jobdetail/10000-123456-S"
        ),
        source_title="Arbeitsagentur · Betriebsgröße",
    )
    route = respx.get(profile_url).mock(
        return_value=httpx.Response(200, json={"betriebsgroesse": "1000+"})
    )

    class FailingAiInvoker:
        def invoke(self, _request: ClaudeRequest) -> ClaudeInvocation:
            return ClaudeInvocation(
                argv=["fake"],
                stdout=b"",
                stderr=b"disabled",
                exit_code=1,
                duration_seconds=0.0,
            )

    monkeypatch.setattr(
        scan_service_module,
        "AiRuntimeInvoker",
        lambda _paths: FailingAiInvoker(),
    )
    repo = repository(paths)
    service = ScanService(
        paths,
        repository=repo,
        reviewer=AcceptingReviewer(),
        source_factory=lambda _config: [
            FakeAdapter(SourceKind.ARBEITSAGENTUR, "default", [occurrence])
        ],
        clock=lambda: NOW,
        run_id_factory=lambda: "run-native-company-size",
    )

    service.run()
    result = repo.load().jobs[0]

    assert route.call_count == 1
    assert result.company_size is not None
    assert result.company_size.reported_size == "1000+"
    assert result.company_size.source_name == "arbeitsagentur"
    assert result.machine_status is MachineStatus.ELIGIBLE


def test_scan_lock_is_separate_and_second_scan_fails_immediately(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    config(paths)
    source_factory_called = False

    def source_factory(_config: AppConfig) -> Sequence[SourceAdapter]:
        nonlocal source_factory_called
        source_factory_called = True
        return []

    service = ScanService(paths, source_factory=source_factory)

    assert paths.scan_lock_file != paths.lock_file
    with FileRWLock(paths.scan_lock_file).exclusive(), pytest.raises(
        ScanAlreadyRunning
    ):
        service.run()

    assert source_factory_called is False


def test_workflow_lock_blocks_standalone_scan_before_loading_setup(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    config(paths)
    source_factory_called = False

    def source_factory(_config: AppConfig) -> Sequence[SourceAdapter]:
        nonlocal source_factory_called
        source_factory_called = True
        return []

    service = ScanService(paths, source_factory=source_factory)

    with FileRWLock(paths.workflow_lock_file).exclusive(), pytest.raises(
        ScanAlreadyRunning
    ):
        service.run()

    assert source_factory_called is False
