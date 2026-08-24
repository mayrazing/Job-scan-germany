from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import HttpUrl
from typer.testing import CliRunner

from job_scan import cli as cli_module
from job_scan import review_server as review_server_module
from job_scan.ai_config import AiProviderStore
from job_scan.anthropic_api import AiModelDiscovery
from job_scan.ats_history import AtsHistoryStore
from job_scan.ats_workflow import AtsWorkflow
from job_scan.cli import app as cli_app
from job_scan.company_size import (
    CompanySizeEvidence,
    CompanySizeLookupError,
    CompanySizeService,
    CompanySizeStore,
)
from job_scan.config import AppConfig, ClaudeSettings, SchedulerSettings, save_config
from job_scan.dashboard.render import render_dashboard
from job_scan.domain import (
    AIReview,
    AvailabilityStatus,
    JobRecord,
    MachineStatus,
    ReviewHistoryEntry,
    Snapshot,
    SourceKind,
    SourceOccurrence,
    UserStatus,
)
from job_scan.global_jobs import GLOBAL_USER_STATUSES, GlobalJobStore
from job_scan.job_snapshot import JobSnapshotStore
from job_scan.locking import FileRWLock
from job_scan.paths import AppPaths
from job_scan.repository import JsonlRepository
from job_scan.review_server import create_review_app
from job_scan.search_history import SearchHistoryStore
from job_scan.sources.base import BrowserSourceError
from job_scan.web_workflow import WebWorkflow

NOW = datetime(2026, 8, 3, 12, tzinfo=UTC)
TOKEN = "test-session-token"
ORIGIN = "http://127.0.0.1:8765"
OTHER_ALLOWED_ORIGIN = "http://localhost:8765"
MUTABLE_FIELDS = frozenset(
    {
        "user_status",
        "user_status_updated_at",
        "manual_override",
        "manual_override_content_hash",
        "manual_override_profile_hash",
    }
)


class LocationAwareCompanySizeLookup:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str | None]] = []

    def lookup(
        self,
        company: str,
        _config: AppConfig,
        checked_at: datetime,
        *,
        location: str | None = None,
    ) -> CompanySizeEvidence:
        self.requests.append((company, location))
        return CompanySizeEvidence(
            company_name=company,
            band="1000-9999",
            employee_count=4200,
            source_url="https://acme.example/company",
            source_title="Acme company facts",
            checked_at=checked_at,
            confidence="high",
        )


class FailingCompanySizeLookup:
    def lookup(
        self,
        _company: str,
        _config: AppConfig,
        _checked_at: datetime,
        *,
        location: str | None = None,
    ) -> CompanySizeEvidence:
        del location
        raise CompanySizeLookupError("AI search timed out.")


def _save_review_config(paths: AppPaths) -> None:
    resume_bytes = b"CURRENT REVIEW RESUME"
    resume_path = paths.root / "resume.pdf"
    resume_path.parent.mkdir(parents=True, exist_ok=True)
    resume_path.write_bytes(resume_bytes)
    save_config(
        paths.config_toml,
        AppConfig(
            resume_path=resume_path,
            resume_sha256="sha256:" + hashlib.sha256(resume_bytes).hexdigest(),
            profile_sha256="sha256:" + "b" * 64,
            search_terms=["backend"],
            locations=["Berlin"],
            minimum_company_size=0,
            german_level="B1",
            claude=ClaudeSettings(model="sonnet", effort="medium"),
            scheduler=SchedulerSettings(),
        ),
    )


def _job(
    key: str = "canonical-42",
    *,
    machine_status: MachineStatus = MachineStatus.EXCLUDED,
    successful_profile_hash: str | None = "sha256:profile",
) -> JobRecord:
    occurrence = SourceOccurrence(
        source=SourceKind.LINKEDIN,
        source_instance="acme/jobs",
        external_id="REQ-42",
        source_generation=1,
        url=HttpUrl("https://acme.example/jobs/REQ-42"),
        company="Acme",
        title="Backend Engineer",
        location="Berlin",
        description="Build APIs",
        posted_at=date(2026, 8, 1),
        content_hash="sha256:job",
        availability_status=AvailabilityStatus.ACTIVE,
        detail_complete=True,
    )
    review = AIReview(
        job_key=key,
        german_requirement="required",
        visa_sponsorship="not_mentioned",
        existing_work_authorization="not_mentioned",
        citizenship_requirement="none",
        security_clearance="none",
        staffing_agency="no",
        eligibility_evidence=["German required"],
        company_industry=None,
        company_industry_confidence="low",
        company_industry_evidence=[],
        score=41,
        reason="Strong technical match",
        confidence="high",
    )
    return JobRecord(
        canonical_job_key=key,
        source_occurrences=[occurrence],
        primary_source_occurrence_key=occurrence.source_occurrence_key,
        company="Acme",
        title="Backend Engineer",
        location="Berlin",
        url=occurrence.url,
        description="Build APIs",
        posted_at=date(2026, 8, 1),
        content_hash="sha256:job",
        first_seen=NOW,
        last_seen=NOW,
        availability_status=AvailabilityStatus.ACTIVE,
        machine_status=machine_status,
        user_status_updated_at=NOW,
        ai_review=review,
        review_history=[
            ReviewHistoryEntry(
                attempted_at=NOW,
                content_hash="sha256:job",
                profile_hash="sha256:profile",
                model="claude-test",
                outcome="accepted",
                review=review,
            )
        ],
        score=41,
        reason="Strong technical match",
        review_model="claude-test",
        reviewed_at=NOW,
        last_successful_review_content_hash="sha256:job",
        last_successful_review_profile_hash=successful_profile_hash,
        exclusion_reasons=["German required"],
        labels=["backend"],
    )


@pytest.fixture
def repository(tmp_path: Path) -> JsonlRepository:
    paths = AppPaths.from_root(tmp_path / "home")
    _save_review_config(paths)
    value = JsonlRepository(paths, FileRWLock(paths.lock_file), render_dashboard)
    value.mutate(lambda snapshot: snapshot.with_job(_job()))
    return value


@pytest.fixture
def review_client(
    repository: JsonlRepository,
) -> Iterator[tuple[TestClient, JsonlRepository]]:
    review_app = create_review_app(
        repository,
        TOKEN,
        frozenset({ORIGIN, OTHER_ALLOWED_ORIGIN}),
    )
    with TestClient(review_app, base_url=ORIGIN) as client:
        yield client, repository


def _open_session(client: TestClient) -> str:
    cookie_name = "job_scan_session"
    client.cookies.set(cookie_name, TOKEN)
    return cookie_name


def _mutation_headers(*, origin: str | None = ORIGIN, host: str = "127.0.0.1:8765") -> dict[str, str]:
    headers = {"Host": host}
    if origin is not None:
        headers["Origin"] = origin
    return headers


def _field_bytes(job: JobRecord) -> dict[str, bytes]:
    dumped = job.model_dump(mode="json", round_trip=True, warnings=False)
    return {
        name: json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        for name, value in dumped.items()
    }


def _assert_only_job_fields_changed(
    before: JobRecord,
    after: JobRecord,
    expected_changed: set[str],
) -> None:
    before_fields = _field_bytes(before)
    after_fields = _field_bytes(after)
    changed = {
        name
        for name in before_fields
        if before_fields[name] != after_fields[name]
    }
    assert changed == expected_changed
    assert changed <= MUTABLE_FIELDS


def test_root_review_page_is_not_available(
    review_client: tuple[TestClient, JsonlRepository],
) -> None:
    client, _repository = review_client

    response = client.get("/")

    assert response.status_code == 404


def test_job_snapshot_route_serves_saved_html_without_source_access(
    review_client: tuple[TestClient, JsonlRepository],
) -> None:
    client, repository = review_client
    html = (
        '<!doctype html><html data-job-scan-snapshot="stepstone:de:13889830">'
        '<head><meta http-equiv="Content-Security-Policy" '
        'content="default-src \'none\'; style-src \'unsafe-inline\'">'
        "<style>body{color:#0c2577}</style></head>"
        "<body><main>Senior Software Engineer Java</main></body></html>"
    )
    reference = JobSnapshotStore(repository.paths.job_snapshots_dir).save(
        source_job_key="stepstone:de:13889830",
        captured_at=NOW,
        html=html,
    )

    def attach_snapshot(snapshot: Snapshot) -> Snapshot:
        snapshot.jobs[0].source_occurrences[0].job_snapshot = reference
        return snapshot

    repository.mutate(attach_snapshot)

    response = client.get(f"/api/job-snapshots/{reference.snapshot_id}")

    assert response.status_code == 200
    assert response.content == html.encode()
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert response.headers["referrer-policy"] == "no-referrer"


def test_job_snapshot_route_hides_invalid_or_missing_ids(
    review_client: tuple[TestClient, JsonlRepository],
) -> None:
    client, _repository = review_client

    assert client.get("/api/job-snapshots/not-a-snapshot").status_code == 404
    assert (
        client.get(f"/api/job-snapshots/sha256:{'f' * 64}").status_code == 404
    )


def test_generate_snapshot_captures_one_current_job_only_once(
    repository: JsonlRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[str] = []

    def capture(occurrence: SourceOccurrence) -> str:
        attempts.append(occurrence.source_occurrence_key)
        return (
            "<!doctype html>"
            f'<html data-job-scan-snapshot="{occurrence.source_job_key}">'
            "<head><style>body{color:#2557a7}</style></head>"
            "<body><main>Backend Engineer</main></body></html>"
        )

    monkeypatch.setattr(
        review_server_module,
        "capture_source_job_snapshot_html",
        capture,
        raising=False,
    )
    review_app = create_review_app(
        repository,
        TOKEN,
        frozenset({ORIGIN, OTHER_ALLOWED_ORIGIN}),
    )

    with TestClient(review_app, base_url=ORIGIN) as client:
        _open_session(client)
        first = client.post(
            "/api/jobs/canonical-42/snapshot",
            headers=_mutation_headers(),
        )
        second = client.post(
            "/api/jobs/canonical-42/snapshot",
            headers=_mutation_headers(),
        )

    assert first.status_code == 204
    assert second.status_code == 204
    assert attempts == ["linkedin:acme/jobs:REQ-42@1"]
    occurrence = repository.load().jobs[0].source_occurrences[0]
    assert occurrence.job_snapshot is not None
    assert occurrence.job_snapshot_error_code is None
    assert JobSnapshotStore(repository.paths.job_snapshots_dir).read(
        occurrence.job_snapshot.snapshot_id
    ).endswith(b"<body><main>Backend Engineer</main></body></html>")


def test_generate_snapshot_records_an_unavailable_automatic_source(
    repository: JsonlRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        review_server_module,
        "capture_source_job_snapshot_html",
        lambda _occurrence: None,
        raising=False,
    )
    review_app = create_review_app(
        repository,
        TOKEN,
        frozenset({ORIGIN, OTHER_ALLOWED_ORIGIN}),
    )

    with TestClient(review_app, base_url=ORIGIN) as client:
        _open_session(client)
        response = client.post(
            "/api/jobs/canonical-42/snapshot",
            headers=_mutation_headers(),
        )

    assert response.status_code == 204
    occurrence = repository.load().jobs[0].source_occurrences[0]
    assert occurrence.job_snapshot is None
    assert occurrence.job_snapshot_error_code == "snapshot_capture_failed"


def test_generate_snapshot_force_replaces_an_existing_snapshot(
    repository: JsonlRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def capture(occurrence: SourceOccurrence) -> str:
        calls.append(occurrence.source_occurrence_key)
        return (
            "<!doctype html>"
            f'<html data-job-scan-snapshot="{occurrence.source_job_key}">'
            "<head><style>body{color:#2557a7}</style></head>"
            f"<body><main>version {len(calls)}</main></body></html>"
        )

    monkeypatch.setattr(
        review_server_module,
        "capture_source_job_snapshot_html",
        capture,
        raising=False,
    )
    review_app = create_review_app(
        repository,
        TOKEN,
        frozenset({ORIGIN, OTHER_ALLOWED_ORIGIN}),
    )

    with TestClient(review_app, base_url=ORIGIN) as client:
        _open_session(client)
        first = client.post(
            "/api/jobs/canonical-42/snapshot",
            headers=_mutation_headers(),
        )
        second = client.post(
            "/api/jobs/canonical-42/snapshot?force=1",
            headers=_mutation_headers(),
        )

    assert first.status_code == 204
    assert second.status_code == 204
    assert calls == ["linkedin:acme/jobs:REQ-42@1", "linkedin:acme/jobs:REQ-42@1"]
    occurrence = repository.load().jobs[0].source_occurrences[0]
    assert occurrence.job_snapshot is not None
    assert occurrence.job_snapshot_error_code is None
    assert JobSnapshotStore(repository.paths.job_snapshots_dir).read(
        occurrence.job_snapshot.snapshot_id
    ).endswith(b"<body><main>version 2</main></body></html>")


def test_generate_snapshot_records_a_browser_capture_failure(
    repository: JsonlRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_capture(_occurrence: SourceOccurrence) -> str:
        raise BrowserSourceError("OpenCLI timed out.", error_code="opencli_timeout")

    monkeypatch.setattr(
        review_server_module,
        "capture_source_job_snapshot_html",
        fail_capture,
    )
    review_app = create_review_app(
        repository,
        TOKEN,
        frozenset({ORIGIN, OTHER_ALLOWED_ORIGIN}),
    )

    with TestClient(review_app, base_url=ORIGIN) as client:
        _open_session(client)
        response = client.post(
            "/api/jobs/canonical-42/snapshot",
            headers=_mutation_headers(),
        )

    assert response.status_code == 204
    occurrence = repository.load().jobs[0].source_occurrences[0]
    assert occurrence.job_snapshot is None
    assert occurrence.job_snapshot_error_code == "snapshot_capture_failed"


def test_generate_snapshot_captures_a_manual_only_job_page(
    repository: JsonlRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def make_manual(snapshot: Snapshot) -> Snapshot:
        occurrence = snapshot.jobs[0].source_occurrences[0]
        occurrence.source = SourceKind.MANUAL
        occurrence.source_instance = "careers.example"
        occurrence.external_id = "manual-1"
        snapshot.jobs[0].primary_source_occurrence_key = (
            occurrence.source_occurrence_key
        )
        return snapshot

    repository.mutate(make_manual)

    def capture(_occurrence: SourceOccurrence) -> str:
        return (
            "<!doctype html>"
            '<html data-job-scan-snapshot="manual:careers.example:manual-1">'
            "<head><style>body{color:#2557a7}</style></head>"
            "<body><main>Backend Engineer</main></body></html>"
        )

    monkeypatch.setattr(
        review_server_module,
        "capture_source_job_snapshot_html",
        capture,
        raising=False,
    )
    review_app = create_review_app(
        repository,
        TOKEN,
        frozenset({ORIGIN, OTHER_ALLOWED_ORIGIN}),
    )

    with TestClient(review_app, base_url=ORIGIN) as client:
        _open_session(client)
        response = client.post(
            "/api/jobs/canonical-42/snapshot",
            headers=_mutation_headers(),
        )

    assert response.status_code == 204
    occurrence = repository.load().jobs[0].source_occurrences[0]
    assert occurrence.job_snapshot is not None
    assert occurrence.job_snapshot_error_code is None


def test_generate_snapshot_updates_only_the_selected_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    repository = JsonlRepository(paths, FileRWLock(paths.lock_file), render_dashboard)
    repository.mutate(lambda snapshot: snapshot.with_job(_job()))
    _save_review_config(paths)
    paths.profile_md.write_text("profile", encoding="utf-8")
    resume = tmp_path / "candidate.pdf"
    resume.write_bytes(b"resume")
    history = SearchHistoryStore(paths)
    history.archive(
        run_id="search-1",
        candidate_name="Candidate",
        resume_filename=resume.name,
        resume_path=resume,
        snapshot=repository.load(),
        finished_at=NOW,
    )

    attempts: list[str] = []

    def capture(occurrence: SourceOccurrence) -> str:
        attempts.append(occurrence.source_occurrence_key)
        return (
            "<!doctype html>"
            f'<html data-job-scan-snapshot="{occurrence.source_job_key}">'
            "<head><style>body{color:#2557a7}</style></head>"
            "<body><main>History Backend Engineer</main></body></html>"
        )

    monkeypatch.setattr(
        review_server_module,
        "capture_source_job_snapshot_html",
        capture,
    )
    review_app = create_review_app(
        repository,
        TOKEN,
        frozenset({ORIGIN, OTHER_ALLOWED_ORIGIN}),
        history_store=history,
    )

    with TestClient(review_app, base_url=ORIGIN) as client:
        _open_session(client)
        response = client.post(
            "/api/scan-history/search-1/jobs/canonical-42/snapshot",
            headers=_mutation_headers(),
        )
        second = client.post(
            "/api/scan-history/search-1/jobs/canonical-42/snapshot",
            headers=_mutation_headers(),
        )

    assert response.status_code == 204
    assert second.status_code == 204
    assert attempts == ["linkedin:acme/jobs:REQ-42@1"]
    assert history.load("search-1").jobs[0].source_occurrences[0].job_snapshot is not None
    assert repository.load().jobs[0].source_occurrences[0].job_snapshot is None


def test_generate_snapshot_updates_only_the_global_job(
    repository: JsonlRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    global_jobs = GlobalJobStore(repository.paths)
    global_jobs.set_status(repository.load().jobs[0], UserStatus.SAVED, NOW)

    attempts: list[str] = []

    def capture(occurrence: SourceOccurrence) -> str:
        attempts.append(occurrence.source_occurrence_key)
        return (
            "<!doctype html>"
            f'<html data-job-scan-snapshot="{occurrence.source_job_key}">'
            "<head><style>body{color:#2557a7}</style></head>"
            "<body><main>Saved Backend Engineer</main></body></html>"
        )

    monkeypatch.setattr(
        review_server_module,
        "capture_source_job_snapshot_html",
        capture,
    )
    review_app = create_review_app(
        repository,
        TOKEN,
        frozenset({ORIGIN, OTHER_ALLOWED_ORIGIN}),
        global_job_store=global_jobs,
    )

    with TestClient(review_app, base_url=ORIGIN) as client:
        _open_session(client)
        response = client.post(
            "/api/global-jobs/canonical-42/snapshot",
            headers=_mutation_headers(),
        )
        second = client.post(
            "/api/global-jobs/canonical-42/snapshot",
            headers=_mutation_headers(),
        )

    assert response.status_code == 204
    assert second.status_code == 204
    assert attempts == ["linkedin:acme/jobs:REQ-42@1"]
    saved = global_jobs.find("canonical-42")
    assert saved is not None
    assert saved.source_occurrences[0].job_snapshot is not None
    assert repository.load().jobs[0].source_occurrences[0].job_snapshot is None


@pytest.mark.parametrize("lock_name", ["workflow_lock_file", "scan_lock_file"])
def test_generate_snapshot_returns_conflict_without_capture_when_lock_is_busy(
    repository: JsonlRepository,
    monkeypatch: pytest.MonkeyPatch,
    lock_name: str,
) -> None:
    attempts: list[str] = []

    def capture(occurrence: SourceOccurrence) -> str:
        attempts.append(occurrence.source_occurrence_key)
        return "<html data-job-scan-snapshot='unexpected'></html>"

    monkeypatch.setattr(
        review_server_module,
        "capture_source_job_snapshot_html",
        capture,
    )
    review_app = create_review_app(
        repository,
        TOKEN,
        frozenset({ORIGIN, OTHER_ALLOWED_ORIGIN}),
    )

    with TestClient(review_app, base_url=ORIGIN) as client:
        _open_session(client)
        lock_path = getattr(repository.paths, lock_name)
        with FileRWLock(lock_path).exclusive():
            response = client.post(
                "/api/jobs/canonical-42/snapshot",
                headers=_mutation_headers(),
            )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "A scan is running; retry the snapshot after it completes."
    }
    assert attempts == []


def test_company_size_search_updates_live_job_and_public_cache(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    repository = JsonlRepository(paths, FileRWLock(paths.lock_file), render_dashboard)
    repository.mutate(
        lambda snapshot: snapshot.with_job(
            _job(machine_status=MachineStatus.ELIGIBLE)
        )
    )
    _save_review_config(paths)
    lookup = LocationAwareCompanySizeLookup()
    store = CompanySizeStore(paths.cache_dir / "company-sizes.json")
    review_app = create_review_app(
        repository,
        TOKEN,
        frozenset({ORIGIN, OTHER_ALLOWED_ORIGIN}),
        company_size_service=CompanySizeService(store, lookup),
    )

    with TestClient(review_app, base_url=ORIGIN) as client:
        _open_session(client)
        response = client.post(
            "/api/jobs/canonical-42/company-size",
            headers=_mutation_headers(),
        )

    assert response.status_code == 200
    assert response.json()["band"] == "1000-9999"
    assert lookup.requests == [("Acme", "Berlin")]
    assert repository.load().jobs[0].company_size.band == "1000-9999"
    assert store.load()["acme"].band == "1000-9999"


def test_company_size_search_returns_the_specific_safe_lookup_error(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    repository = JsonlRepository(paths, FileRWLock(paths.lock_file), render_dashboard)
    repository.mutate(
        lambda snapshot: snapshot.with_job(
            _job(machine_status=MachineStatus.ELIGIBLE)
        )
    )
    _save_review_config(paths)
    service = CompanySizeService(
        CompanySizeStore(paths.cache_dir / "company-sizes.json"),
        FailingCompanySizeLookup(),
    )
    review_app = create_review_app(
        repository,
        TOKEN,
        frozenset({ORIGIN, OTHER_ALLOWED_ORIGIN}),
        company_size_service=service,
    )

    with TestClient(review_app, base_url=ORIGIN) as client:
        _open_session(client)
        response = client.post(
            "/api/jobs/canonical-42/company-size",
            headers=_mutation_headers(),
        )

    assert response.status_code == 422
    assert response.json() == {"detail": "AI search timed out."}


def test_company_size_search_updates_only_selected_history_snapshot(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    repository = JsonlRepository(paths, FileRWLock(paths.lock_file), render_dashboard)
    repository.mutate(
        lambda snapshot: snapshot.with_job(
            _job(machine_status=MachineStatus.ELIGIBLE)
        )
    )
    _save_review_config(paths)
    paths.profile_md.write_text("profile", encoding="utf-8")
    resume = tmp_path / "candidate.pdf"
    resume.write_bytes(b"resume")
    history = SearchHistoryStore(paths)
    history.archive(
        run_id="search-1",
        candidate_name="Candidate",
        resume_filename=resume.name,
        resume_path=resume,
        snapshot=repository.load(),
        finished_at=NOW,
    )
    lookup = LocationAwareCompanySizeLookup()
    store = CompanySizeStore(
        paths.run_cache_dir("search-1") / "company-sizes.json"
    )
    review_app = create_review_app(
        repository,
        TOKEN,
        frozenset({ORIGIN, OTHER_ALLOWED_ORIGIN}),
        history_store=history,
        company_size_service=CompanySizeService(store, lookup),
    )

    with TestClient(review_app, base_url=ORIGIN) as client:
        _open_session(client)
        response = client.post(
            "/api/scan-history/search-1/jobs/canonical-42/company-size",
            headers=_mutation_headers(),
        )

    assert response.status_code == 200
    assert history.load("search-1").jobs[0].company_size.band == "1000-9999"
    assert repository.load().jobs[0].company_size is None
    assert store.load()["acme"].band == "1000-9999"


@pytest.mark.parametrize("status", sorted(GLOBAL_USER_STATUSES, key=lambda value: value.value))
def test_status_accepts_each_global_user_status_without_changing_scan_snapshot(
    review_client: tuple[TestClient, JsonlRepository], status: UserStatus
) -> None:
    client, repository = review_client
    _open_session(client)
    before = repository.load().jobs[0]
    response = client.post(
        "/api/jobs/canonical-42/status",
        json={"status": status.value},
        headers=_mutation_headers(),
    )

    assert response.status_code == 204
    after = repository.load().jobs[0]
    assert after == before
    assert GlobalJobStore(repository.paths).find("canonical-42").user_status is status


def test_new_is_not_a_selectable_status(
    review_client: tuple[TestClient, JsonlRepository],
) -> None:
    client, repository = review_client
    _open_session(client)

    response = client.post(
        "/api/jobs/canonical-42/status",
        json={"status": "new"},
        headers=_mutation_headers(),
    )

    assert response.status_code == 422
    assert GlobalJobStore(repository.paths).find("canonical-42") is None


def test_restore_captures_current_hashes_without_erasing_machine_evidence(
    review_client: tuple[TestClient, JsonlRepository],
) -> None:
    client, repository = review_client
    _open_session(client)
    before = repository.load().jobs[0]

    response = client.post(
        "/api/jobs/canonical-42/restore",
        headers=_mutation_headers(),
    )

    assert response.status_code == 204
    after = repository.load().jobs[0]
    assert after.manual_override == "show"
    assert after.manual_override_content_hash == before.content_hash
    assert (
        after.manual_override_profile_hash
        == before.last_successful_review_profile_hash
    )
    assert after.machine_status is MachineStatus.EXCLUDED
    assert after.exclusion_reasons == before.exclusion_reasons
    assert after.ai_review == before.ai_review
    assert after.review_history == before.review_history
    _assert_only_job_fields_changed(
        before,
        after,
        {
            "manual_override",
            "manual_override_content_hash",
            "manual_override_profile_hash",
        },
    )


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/api/jobs/missing/status", {"status": "applied"}),
        ("/api/jobs/missing/restore", None),
    ],
)
def test_unknown_job_returns_404(
    review_client: tuple[TestClient, JsonlRepository],
    path: str,
    payload: dict[str, str] | None,
) -> None:
    client, _repository = review_client
    _open_session(client)

    response = client.post(path, json=payload, headers=_mutation_headers())

    assert response.status_code == 404


def test_invalid_status_returns_422(
    review_client: tuple[TestClient, JsonlRepository],
) -> None:
    client, _repository = review_client
    _open_session(client)

    response = client.post(
        "/api/jobs/canonical-42/status",
        json={"status": "later"},
        headers=_mutation_headers(),
    )

    assert response.status_code == 422


def test_removed_reviewed_status_returns_422(
    review_client: tuple[TestClient, JsonlRepository],
) -> None:
    client, _repository = review_client
    _open_session(client)

    response = client.post(
        "/api/jobs/canonical-42/status",
        json={"status": "reviewed"},
        headers=_mutation_headers(),
    )

    assert response.status_code == 422


def test_restore_of_non_excluded_job_returns_422(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    repository = JsonlRepository(paths, FileRWLock(paths.lock_file), render_dashboard)
    repository.mutate(
        lambda snapshot: snapshot.with_job(
            _job(machine_status=MachineStatus.ELIGIBLE)
        )
    )
    review_app = create_review_app(
        repository, TOKEN, frozenset({ORIGIN, OTHER_ALLOWED_ORIGIN})
    )

    with TestClient(review_app, base_url=ORIGIN) as client:
        _open_session(client)
        response = client.post(
            "/api/jobs/canonical-42/restore",
            headers=_mutation_headers(),
        )

    assert response.status_code == 422


def test_restore_without_successful_profile_hash_returns_409(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    repository = JsonlRepository(paths, FileRWLock(paths.lock_file), render_dashboard)
    repository.mutate(
        lambda snapshot: snapshot.with_job(_job(successful_profile_hash=None))
    )
    review_app = create_review_app(
        repository, TOKEN, frozenset({ORIGIN, OTHER_ALLOWED_ORIGIN})
    )

    with TestClient(review_app, base_url=ORIGIN) as client:
        _open_session(client)
        before = repository.paths.jobs_jsonl.read_bytes()
        response = client.post(
            "/api/jobs/canonical-42/restore",
            headers=_mutation_headers(),
        )

    assert response.status_code == 409
    assert repository.paths.jobs_jsonl.read_bytes() == before


@pytest.mark.parametrize(
    "failure",
    ["missing-cookie", "wrong-cookie", "missing-origin", "wrong-origin", "wrong-host"],
)
def test_mutation_rejects_invalid_session_origin_or_host(
    review_client: tuple[TestClient, JsonlRepository], failure: str
) -> None:
    client, repository = review_client
    cookie_name = _open_session(client)
    headers = _mutation_headers()
    if failure == "missing-cookie":
        client.cookies.clear()
    elif failure == "wrong-cookie":
        client.cookies.clear()
        headers["Cookie"] = f"{cookie_name}=wrong"
    elif failure == "missing-origin":
        headers = _mutation_headers(origin=None)
    elif failure == "wrong-origin":
        headers = _mutation_headers(origin="http://example.test:8765")
    else:
        headers = _mutation_headers(host="127.0.0.1:9000")
    before = repository.paths.jobs_jsonl.read_bytes()

    response = client.post(
        "/api/jobs/canonical-42/status",
        json={"status": "applied"},
        headers=headers,
    )

    assert response.status_code == 403
    assert repository.paths.jobs_jsonl.read_bytes() == before


def test_mutation_uses_current_lan_ip_and_rejects_previous_lan_ip(
    repository: JsonlRepository,
) -> None:
    current_origin = "http://192.168.3.28:8765"
    review_app = create_review_app(
        repository,
        TOKEN,
        frozenset({ORIGIN, "http://job-scan-germany.local:8765"}),
        current_lan_origin=lambda: current_origin,
    )

    with TestClient(review_app, base_url=ORIGIN) as client:
        _open_session(client)
        current_origin = "http://192.168.3.29:8765"
        current_response = client.post(
            "/api/jobs/canonical-42/status",
            json={"status": "applied"},
            headers=_mutation_headers(
                origin=current_origin,
                host="192.168.3.29:8765",
            ),
        )
        previous_response = client.post(
            "/api/jobs/canonical-42/status",
            json={"status": "saved"},
            headers=_mutation_headers(
                origin="http://192.168.3.28:8765",
                host="192.168.3.28:8765",
            ),
        )

    assert current_response.status_code == 204
    assert previous_response.status_code == 403


def test_status_change_does_not_mutate_the_scan_repository(
    review_client: tuple[TestClient, JsonlRepository],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, repository = review_client
    _open_session(client)
    def fail_if_called(_mutator: Callable[[Snapshot], Snapshot]) -> Snapshot:
        raise AssertionError("status changes belong to the global store")

    monkeypatch.setattr(repository, "mutate", fail_if_called)

    response = client.post(
        "/api/jobs/canonical-42/status",
        json={"status": "applied"},
        headers=_mutation_headers(),
    )

    assert response.status_code == 204


@pytest.mark.parametrize("port", [0, 65536])
def test_review_cli_rejects_port_outside_tcp_range(port: int) -> None:
    result = CliRunner().invoke(cli_app, ["review", "--port", str(port)])

    assert result.exit_code == 2


def test_review_cli_rebuilds_dashboard_then_binds_lan_and_stops_mdns_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "home"
    paths = AppPaths.from_root(root)
    old_repository = JsonlRepository(paths, FileRWLock(paths.lock_file))
    old_repository.mutate(lambda snapshot: snapshot.with_job(_job()))
    jsonl_before = paths.jobs_jsonl.read_bytes()
    captured: dict[str, Any] = {}

    class RecordingMdnsPublisher:
        current_ip = "192.168.3.28"

        def start(self) -> str:
            captured["mdns_started"] = True
            return "192.168.3.28"

        def stop(self) -> None:
            captured["mdns_stopped"] = True

    def capture_create_app(
        repository: JsonlRepository,
        token: str,
        allowed_origins: frozenset[str],
        *,
        workflow: WebWorkflow,
        ai_store: Any,
        ai_model_discovery: Any,
        history_store: SearchHistoryStore,
        ats_workflow: AtsWorkflow,
        ats_history_store: AtsHistoryStore,
        current_lan_origin: Callable[[], str | None] | None = None,
    ) -> FastAPI:
        captured["repository"] = repository
        captured["token"] = token
        captured["allowed_origins"] = allowed_origins
        captured["workflow"] = workflow
        captured["ai_store"] = ai_store
        captured["ai_model_discovery"] = ai_model_discovery
        captured["history_store"] = history_store
        captured["ats_workflow"] = ats_workflow
        captured["ats_history_store"] = ats_history_store
        captured["current_lan_origin"] = current_lan_origin
        return create_review_app(
            repository,
            token,
            allowed_origins,
            workflow=workflow,
            ai_store=ai_store,
            ai_model_discovery=ai_model_discovery,
            history_store=history_store,
            ats_workflow=ats_workflow,
            ats_history_store=ats_history_store,
            current_lan_origin=current_lan_origin,
        )

    def token_urlsafe(byte_count: int) -> str:
        captured["token_byte_count"] = byte_count
        return "generated-token"

    def interrupting_run(server_app: FastAPI, **kwargs: Any) -> None:
        captured["server_app"] = server_app
        captured["uvicorn_kwargs"] = kwargs
        raise KeyboardInterrupt

    monkeypatch.setenv("JOB_SCAN_HOME", str(root))
    monkeypatch.setattr(cli_module, "create_review_app", capture_create_app)
    monkeypatch.setattr(secrets, "token_urlsafe", token_urlsafe)
    monkeypatch.setattr(uvicorn, "run", interrupting_run)
    monkeypatch.setattr(
        cli_module,
        "_mdns_publisher_factory",
        RecordingMdnsPublisher,
    )

    result = CliRunner().invoke(cli_app, ["review", "--port", "9123"])

    assert result.exit_code == 0, result.output
    repository = captured["repository"]
    assert isinstance(repository, JsonlRepository)
    assert repository.html_builder is render_dashboard
    assert paths.jobs_jsonl.read_bytes() == jsonl_before
    assert b"Job scan review desk" in paths.dashboard_html.read_bytes()
    assert captured["token_byte_count"] == 32
    assert captured["token"] == "generated-token"
    assert captured["allowed_origins"] == frozenset(
        {
            "http://127.0.0.1:9123",
            "http://localhost:9123",
            "http://job-scan-germany.local:9123",
        }
    )
    assert captured["current_lan_origin"]() == "http://192.168.3.28:9123"
    assert isinstance(captured["workflow"], WebWorkflow)
    assert isinstance(captured["history_store"], SearchHistoryStore)
    assert isinstance(captured["ats_workflow"], AtsWorkflow)
    assert isinstance(captured["ats_history_store"], AtsHistoryStore)
    assert isinstance(captured["ai_store"], AiProviderStore)
    assert captured["ai_store"].list() == []
    assert isinstance(captured["ai_model_discovery"], AiModelDiscovery)
    assert captured["uvicorn_kwargs"] == {
        "host": "0.0.0.0",
        "port": 9123,
        "access_log": False,
        "reload": False,
    }
    assert captured["mdns_started"] is True
    assert captured["mdns_stopped"] is True
    assert result.stdout.splitlines() == [
        "Setup: http://job-scan-germany.local:9123/setup",
        "LAN fallback: http://192.168.3.28:9123",
    ]


def test_review_cli_stops_mdns_when_app_creation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped = False

    class RecordingMdnsPublisher:
        current_ip = "192.168.3.28"

        def start(self) -> str:
            return "192.168.3.28"

        def stop(self) -> None:
            nonlocal stopped
            stopped = True

    def fail_create_app(*_args: Any, **_kwargs: Any) -> FastAPI:
        raise RuntimeError("app creation failed")

    monkeypatch.setenv("JOB_SCAN_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(cli_module, "create_review_app", fail_create_app)
    monkeypatch.setattr(
        cli_module,
        "_mdns_publisher_factory",
        RecordingMdnsPublisher,
    )

    result = CliRunner().invoke(cli_app, ["review"])

    assert result.exit_code == 1
    assert stopped is True


def test_review_cli_keeps_loopback_service_when_mdns_is_not_supported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def capture_run(_server_app: FastAPI, **kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setenv("JOB_SCAN_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(cli_module, "_mdns_publisher_factory", lambda: None)
    monkeypatch.setattr(uvicorn, "run", capture_run)

    result = CliRunner().invoke(cli_app, ["review", "--port", "9123"])

    assert result.exit_code == 0, result.output
    assert captured["host"] == "127.0.0.1"
    assert result.stdout.splitlines() == [
        "Setup: http://127.0.0.1:9123/setup",
    ]
