from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from httpx import Response
from pydantic import HttpUrl

from job_scan.ai_config import AiProviderDraft, AiProviderStore
from job_scan.ai_runtime import AiRuntimeInvoker
from job_scan.ai_selection import (
    AiRuntimeSelection,
    AiSelectionStore,
    ClaudeRuntimeSelection,
)
from job_scan.ats_history import AtsHistoryStore
from job_scan.ats_models import (
    AtsCheckBundle,
    AtsResumeAssessment,
    AtsResumeFinding,
    AtsRunState,
)
from job_scan.ats_workflow import AtsWorkflowInput
from job_scan.claude_process import ClaudeInvocation, ClaudeRequest
from job_scan.company_size import (
    CompanySizeEvidence,
    CompanySizeLookupError,
    CompanySizeService,
    CompanySizeStore,
)
from job_scan.config import (
    AppConfig,
    ClaudeSettings,
    SchedulerSettings,
    load_config,
    save_config,
    serialize_config,
)
from job_scan.dashboard.render import render_dashboard
from job_scan.domain import (
    AvailabilityStatus,
    JobRecord,
    MachineStatus,
    SalaryPeriod,
    SalaryValue,
    Snapshot,
    SourceKind,
    SourceOccurrence,
    StoreMeta,
    UserStatus,
)
from job_scan.global_jobs import GlobalJobStore
from job_scan.locking import FileRWLock, LockUnavailable
from job_scan.manual_job_import_workflow import (
    ManualImportResult,
    ManualJobImportWorkflow,
)
from job_scan.paths import AppPaths
from job_scan.repository import JsonlRepository
from job_scan.review_server import create_review_app
from job_scan.search_history import SearchHistoryStore
from job_scan.setup_service import SetupAnswers, SetupPreparation

NOW = datetime(2026, 8, 19, 10, 0, tzinfo=UTC)
ORIGIN = "http://127.0.0.1:8765"
TOKEN = "test-token"
HEADERS = {"Origin": ORIGIN, "Host": "127.0.0.1:8765"}
SAMPLE_RESUME = Path(__file__).parent / "fixtures" / "resume" / "sample.docx"


class ReliableCompanySizeLookup:
    def lookup(
        self,
        company: str,
        _config: AppConfig,
        checked_at: datetime,
        *,
        location: str | None = None,
    ) -> CompanySizeEvidence:
        del location
        return CompanySizeEvidence(
            company_name=company,
            band="1000-9999",
            employee_count=4200,
            source_url="https://acme.example/company",
            source_title="Acme company facts",
            checked_at=checked_at,
            confidence="high",
        )


class UnavailableCompanySizeLookup:
    def lookup(
        self,
        _company: str,
        _config: AppConfig,
        _checked_at: datetime,
        *,
        location: str | None = None,
    ) -> CompanySizeEvidence:
        del location
        raise CompanySizeLookupError("No reliable employee-count source was found.")


class BlockingCompanySizeLookup(ReliableCompanySizeLookup):
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()

    def lookup(
        self,
        company: str,
        config: AppConfig,
        checked_at: datetime,
        *,
        location: str | None = None,
    ) -> CompanySizeEvidence:
        self.started.set()
        if not self.release.wait(timeout=5):
            raise AssertionError("test did not release company-size lookup")
        return super().lookup(
            company,
            config,
            checked_at,
            location=location,
        )


def _wait_manual_import_completion(
    client: TestClient,
    import_id: str,
) -> dict[str, object]:
    for _ in range(80):
        response = client.get(f"/api/manual-job-imports/{import_id}")
        assert response.status_code == 200
        state = response.json()
        if state.get("status") == "running":
            time.sleep(0.01)
            continue
        assert state.get("status") in {"complete", "failed"}
        return state
    raise AssertionError(f"Manual import did not finish: {import_id}")


def _prepare_uploaded_resume(paths: AppPaths) -> Callable[
    [Path, SetupAnswers],
    SetupPreparation,
]:
    def prepare(resume_path: Path, answers: SetupAnswers) -> SetupPreparation:
        resume_id = "sha256:" + hashlib.sha256(resume_path.read_bytes()).hexdigest()
        profile_bytes = b"# Uploaded resume profile"
        profile_hash = "sha256:" + hashlib.sha256(profile_bytes).hexdigest()
        config = load_config(paths.job_tracker_config_toml).model_copy(
            update={
                "candidate_name": answers.candidate_name,
                "resume_path": resume_path,
                "resume_sha256": resume_id,
                "profile_sha256": profile_hash,
            }
        )
        return SetupPreparation(
            config=config,
            profile_bytes=profile_bytes,
            config_bytes=serialize_config(config).encode("utf-8"),
            profile_hash=profile_hash,
        )

    return prepare


def _post_job_with_resume(
    client: TestClient,
    url: str = "https://careers.example/jobs/manual",
    **data: str,
) -> Response:
    return client.post(
        "/api/global-jobs/import-with-resume",
        data={"url": url, **data},
        files={"resume": ("backend.docx", SAMPLE_RESUME.read_bytes())},
        headers=HEADERS,
    )


def _attach_resume(
    paths: AppPaths,
    store: GlobalJobStore,
    job_key: str,
    filename: str,
    contents: bytes,
) -> str:
    digest = hashlib.sha256(contents).hexdigest()
    resume_id = f"sha256:{digest}"
    resume_dir = paths.root / "resumes"
    resume_dir.mkdir(parents=True, exist_ok=True)
    (resume_dir / f"{digest}{Path(filename).suffix.lower()}").write_bytes(contents)
    job = store.find(job_key)
    assert job is not None
    store.set_application_resume(job, resume_id, filename)
    return resume_id


def _job(
    key: str,
    *,
    external_id: str,
    user_status: UserStatus = UserStatus.NEW,
) -> JobRecord:
    occurrence = SourceOccurrence(
        source=SourceKind.LINKEDIN,
        source_instance="acme/jobs",
        external_id=external_id,
        source_generation=1,
        url=HttpUrl(f"https://acme.example/jobs/{external_id}"),
        company="Acme",
        title=f"Backend Engineer {key}",
        location="Berlin",
        description="Build complete backend systems.",
        posted_at=date(2026, 8, 19),
        content_hash=f"sha256:{key}",
        availability_status=AvailabilityStatus.ACTIVE,
        detail_complete=True,
    )
    return JobRecord(
        canonical_job_key=key,
        source_occurrences=[occurrence],
        primary_source_occurrence_key=occurrence.source_occurrence_key,
        company="Acme",
        title=f"Backend Engineer {key}",
        location="Berlin",
        url=occurrence.url,
        description=occurrence.description,
        posted_at=occurrence.posted_at,
        content_hash=occurrence.content_hash,
        first_seen=NOW,
        last_seen=NOW,
        availability_status=AvailabilityStatus.ACTIVE,
        machine_status=MachineStatus.ELIGIBLE,
        user_status=user_status,
        user_status_updated_at=NOW,
        score=80,
    )


def _snapshot(*jobs: JobRecord) -> Snapshot:
    return Snapshot(meta=StoreMeta(data_revision=1, generated_at=NOW), jobs=list(jobs))


def _repository(paths: AppPaths, *jobs: JobRecord) -> JsonlRepository:
    repository = JsonlRepository(paths, FileRWLock(paths.lock_file), render_dashboard)
    repository.mutate(lambda _old: _snapshot(*jobs))
    return repository


def _save_config(paths: AppPaths) -> None:
    paths.ensure_directories()
    resume_bytes = b"DEFAULT REVIEW RESUME"
    resume_path = paths.root / "default.pdf"
    resume_path.write_bytes(resume_bytes)
    save_config(
        paths.config_toml,
        AppConfig(
            candidate_name="Ada",
            ai_runtime="api:deepseek",
            ai_model="current-model",
            resume_path=resume_path,
            resume_sha256="sha256:" + hashlib.sha256(resume_bytes).hexdigest(),
            profile_sha256="sha256:" + "b" * 64,
            search_terms=["backend"],
            locations=["Berlin"],
            german_level="B1",
            claude=ClaudeSettings(model="sonnet", effort="medium"),
            scheduler=SchedulerSettings(),
        ),
    )


def _archive(
    history: SearchHistoryStore,
    tmp_path: Path,
    run_id: str,
    snapshot: Snapshot,
    resume_bytes: bytes = b"ARCHIVED RESUME",
    profile_bytes: bytes = b"profile",
    config_bytes: bytes | None = None,
) -> None:
    resume = tmp_path / f"{run_id}.pdf"
    resume.write_bytes(resume_bytes)
    if config_bytes is None:
        config_bytes = serialize_config(
            AppConfig(
                candidate_name="Ada",
                resume_path=resume,
                resume_sha256=(
                    "sha256:" + hashlib.sha256(resume_bytes).hexdigest()
                ),
                profile_sha256=(
                    "sha256:" + hashlib.sha256(profile_bytes).hexdigest()
                ),
                search_terms=["backend"],
                locations=["Berlin"],
                german_level="B1",
                claude=ClaudeSettings(model="sonnet", effort="medium"),
                scheduler=SchedulerSettings(),
            )
        ).encode("utf-8")
    history.archive(
        run_id=run_id,
        candidate_name="Ada",
        resume_filename=f"{run_id}.pdf",
        resume_path=resume,
        snapshot=snapshot,
        finished_at=NOW,
        profile_bytes=profile_bytes,
        config_bytes=config_bytes,
    )


def _archive_with_time(
    history: SearchHistoryStore,
    tmp_path: Path,
    run_id: str,
    finished_at: datetime,
    config_bytes: bytes,
) -> None:
    resume = tmp_path / f"{run_id}.pdf"
    resume.write_bytes(b"ARCHIVED RESUME")
    history.archive(
        run_id=run_id,
        candidate_name="Ada",
        resume_filename=f"{run_id}.pdf",
        resume_path=resume,
        snapshot=_snapshot(),
        finished_at=finished_at,
        profile_bytes=b"# History candidate profile",
        config_bytes=config_bytes,
    )


def _ats_bundle(
    run_id: str,
    *,
    resume_filename: str,
    finished_at: datetime,
    resume_id: str = "sha256:" + "a" * 64,
) -> AtsCheckBundle:
    return AtsCheckBundle(
        run_id=run_id,
        search_run_id="search-1",
        resume_id=resume_id,
        candidate_name="Ada",
        resume_filename=resume_filename,
        started_at=finished_at,
        finished_at=finished_at,
        ai_runtime="claude-code",
        ai_model="sonnet",
        resume=AtsResumeAssessment(
            readiness_score=88,
            verdict="ready",
            title="Resume content is ATS ready",
            summary="Core resume content is clear.",
            findings=[
                AtsResumeFinding(
                    label="Text extraction",
                    status="pass",
                    detail="Selectable text was extracted.",
                )
            ],
        ),
        jobs=[],
    )


class RecordingAtsWorkflow:
    def __init__(self) -> None:
        self.inputs: list[AtsWorkflowInput] = []
        self.busy = False

    def start(self, inputs: AtsWorkflowInput) -> AtsRunState:
        self.inputs.append(inputs)
        return AtsRunState(
            run_id="ats-1",
            search_run_id=inputs.search_run_id,
            status="running",
            stage="resume",
            message="Checking resume...",
            progress_percent=0,
            tasks=[],
        )

    def read_run(self, _run_id: str) -> AtsRunState | None:
        return None

    def read_current_run(self) -> AtsRunState | None:
        return None

    def is_busy(self) -> bool:
        return self.busy


def _open_session(client: TestClient) -> None:
    client.cookies.set("job_scan_session", TOKEN)


@pytest.mark.parametrize(
    "selected_status",
    [
        "saved",
        "applied",
        "interviewing",
        "offer",
        "withdrawn",
        "rejected",
        "ignored",
    ],
)
def test_every_status_change_is_global_and_new_is_rejected(
    tmp_path: Path,
    selected_status: str,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    _save_config(paths)
    repository = _repository(paths, _job("current", external_id="shared"))
    global_jobs = GlobalJobStore(paths)
    app = create_review_app(
        repository,
        TOKEN,
        frozenset({ORIGIN}),
        global_job_store=global_jobs,
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        saved = client.post(
            "/api/jobs/current/status",
            json={"status": selected_status},
            headers=HEADERS,
        )
        reset = client.post(
            "/api/jobs/current/status",
            json={"status": "new"},
            headers=HEADERS,
        )

    assert saved.status_code == 204
    assert reset.status_code == 422
    assert repository.load().jobs[0].user_status is UserStatus.NEW
    assert global_jobs.find("current").user_status is UserStatus(selected_status)


def test_review_status_rejects_transfer_when_resume_is_unavailable(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    global_jobs = GlobalJobStore(paths)
    app = create_review_app(
        _repository(paths, _job("current", external_id="current")),
        TOKEN,
        frozenset({ORIGIN}),
        global_job_store=global_jobs,
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        response = client.post(
            "/api/jobs/current/status",
            json={"status": "saved"},
            headers=HEADERS,
        )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "The resume for this review is unavailable."
    }
    assert global_jobs.find("current") is None


def test_global_status_does_not_attach_a_resume(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    _save_config(paths)
    paths.profile_md.write_text("# Current profile", encoding="utf-8")
    config = load_config(paths.config_toml)
    global_jobs = GlobalJobStore(paths)
    job = _job("tracked", external_id="tracked")
    global_jobs.set_status(
        job,
        UserStatus.SAVED,
        NOW,
        resume_id=config.resume_sha256,
        profile_hash=config.profile_sha256,
    )
    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        global_job_store=global_jobs,
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        response = client.post(
            "/api/global-jobs/tracked/status",
            json={"status": "applied"},
            headers=HEADERS,
        )

    saved = global_jobs.find("tracked")
    assert response.status_code == 204
    assert saved is not None
    assert saved.application_resume_id is None


def test_live_review_status_copies_its_resume_into_job_tracker(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    _save_config(paths)
    resume_bytes = b"CURRENT REVIEW RESUME"
    resume_id = "sha256:" + hashlib.sha256(resume_bytes).hexdigest()
    resume_path = paths.root / "current.pdf"
    resume_path.write_bytes(resume_bytes)
    save_config(
        paths.config_toml,
        load_config(paths.config_toml).model_copy(
            update={"resume_path": resume_path, "resume_sha256": resume_id}
        ),
    )
    global_jobs = GlobalJobStore(paths)
    app = create_review_app(
        _repository(paths, _job("current", external_id="current")),
        TOKEN,
        frozenset({ORIGIN}),
        global_job_store=global_jobs,
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        changed = client.post(
            "/api/jobs/current/status",
            json={"status": "saved"},
            headers=HEADERS,
        )
        paths.config_toml.unlink()
        downloaded = client.get("/api/global-jobs/current/resume")

    saved = global_jobs.find("current")
    assert changed.status_code == 204
    assert saved is not None
    assert saved.application_resume_id == resume_id
    assert saved.application_resume_filename == "current.pdf"
    assert downloaded.status_code == 200
    assert downloaded.content == resume_bytes


def test_review_transfer_keeps_resume_when_tracker_commit_reaches_disk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    _save_config(paths)
    global_jobs = GlobalJobStore(paths)
    app = create_review_app(
        _repository(paths, _job("current", external_id="current")),
        TOKEN,
        frozenset({ORIGIN}),
        global_job_store=global_jobs,
    )

    def fail_directory_fsync(_path: Path) -> None:
        raise OSError("injected directory fsync failure")

    monkeypatch.setattr(
        "job_scan.global_jobs._fsync_directory",
        fail_directory_fsync,
    )
    with TestClient(
        app,
        base_url=ORIGIN,
        raise_server_exceptions=False,
    ) as client:
        _open_session(client)
        response = client.post(
            "/api/jobs/current/status",
            json={"status": "saved"},
            headers=HEADERS,
        )

    saved = global_jobs.find("current")
    assert response.status_code == 500
    assert saved is not None
    assert saved.application_resume_id is not None
    assert saved.application_resume_filename == "default.pdf"
    digest = saved.application_resume_id.removeprefix("sha256:")
    assert (paths.root / "resumes" / f"{digest}.pdf").exists()


def test_global_lifecycle_date_can_be_changed(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    global_jobs = GlobalJobStore(paths)
    job = _job("tracked", external_id="tracked")
    global_jobs.set_status(job, UserStatus.SAVED, NOW)
    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        global_job_store=global_jobs,
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        response = client.post(
            "/api/global-jobs/tracked/lifecycle/0/date",
            json={"changed_on": "2026-08-05"},
            headers=HEADERS,
        )

    saved = global_jobs.find("tracked")
    assert response.status_code == 204
    assert saved is not None
    assert saved.user_status is UserStatus.SAVED
    assert saved.user_status_history[0].changed_at == datetime(
        2026,
        8,
        5,
        10,
        0,
        tzinfo=UTC,
    )


@pytest.mark.parametrize(
    ("payload", "field_name", "expected"),
    [
        ({"posted_at": "2026-08-12"}, "manual_posted_at", date(2026, 8, 12)),
        ({"company_size": 4200}, "manual_company_size", 4200),
        ({"company_industry": " Logistics "}, "manual_company_industry", "Logistics"),
    ],
)
def test_global_unknown_fact_can_be_saved(
    tmp_path: Path,
    payload: dict[str, object],
    field_name: str,
    expected: object,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    global_jobs = GlobalJobStore(paths)
    job = _job("tracked", external_id="tracked").model_copy(
        update={"posted_at": None}
    )
    global_jobs.set_status(job, UserStatus.SAVED, NOW)
    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        global_job_store=global_jobs,
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        response = client.post(
            "/api/global-jobs/tracked/facts",
            json=payload,
            headers=HEADERS,
        )

    saved = global_jobs.find("tracked")
    assert response.status_code == 204
    assert saved is not None
    assert getattr(saved, field_name) == expected


@pytest.mark.parametrize(
    "payload",
    [
        {"company_size": 0},
        {"company_industry": "   "},
        {"posted_at": "2026-08-12", "company_size": 4200},
    ],
)
def test_global_manual_fact_rejects_invalid_input(
    tmp_path: Path,
    payload: dict[str, object],
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    global_jobs = GlobalJobStore(paths)
    job = _job("tracked", external_id="tracked").model_copy(
        update={"posted_at": None}
    )
    global_jobs.set_status(job, UserStatus.SAVED, NOW)
    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        global_job_store=global_jobs,
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        response = client.post(
            "/api/global-jobs/tracked/facts",
            json=payload,
            headers=HEADERS,
        )

    assert response.status_code == 422


def test_global_manual_fact_rejects_a_known_value(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    global_jobs = GlobalJobStore(paths)
    job = _job("tracked", external_id="tracked")
    global_jobs.set_status(job, UserStatus.SAVED, NOW)
    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        global_job_store=global_jobs,
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        response = client.post(
            "/api/global-jobs/tracked/facts",
            json={"posted_at": "2026-08-12"},
            headers=HEADERS,
        )

    saved = global_jobs.find("tracked")
    assert response.status_code == 422
    assert saved is not None
    assert saved.manual_posted_at is None


def test_global_lifecycle_date_cannot_cross_the_previous_event(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    global_jobs = GlobalJobStore(paths)
    job = _job("tracked", external_id="tracked")
    global_jobs.set_status(job, UserStatus.SAVED, NOW)
    global_jobs.set_status(job, UserStatus.APPLIED, NOW.replace(day=21))
    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        global_job_store=global_jobs,
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        response = client.post(
            "/api/global-jobs/tracked/lifecycle/1/date",
            json={"changed_on": "2026-08-18"},
            headers=HEADERS,
        )

    saved = global_jobs.find("tracked")
    assert response.status_code == 422
    assert response.json()["detail"] == (
        "Lifecycle dates must stay between adjacent lifecycle events."
    )
    assert saved is not None
    assert len(saved.user_status_history) == 2
    assert saved.user_status_history[1].changed_at == NOW.replace(day=21)


def test_global_lifecycle_event_can_be_deleted(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    global_jobs = GlobalJobStore(paths)
    job = _job("tracked", external_id="tracked")
    global_jobs.set_status(job, UserStatus.SAVED, NOW)
    global_jobs.set_status(job, UserStatus.APPLIED, NOW.replace(day=21))
    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        global_job_store=global_jobs,
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        response = client.delete(
            "/api/global-jobs/tracked/lifecycle/1",
            headers=HEADERS,
        )

    saved = global_jobs.find("tracked")
    assert response.status_code == 204
    assert saved is not None
    assert [entry.status for entry in saved.user_status_history] == [
        UserStatus.SAVED
    ]
    assert saved.user_status is UserStatus.SAVED


def test_global_saved_lifecycle_event_cannot_be_deleted(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    global_jobs = GlobalJobStore(paths)
    job = _job("tracked", external_id="tracked")
    global_jobs.set_status(job, UserStatus.SAVED, NOW)
    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        global_job_store=global_jobs,
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        response = client.delete(
            "/api/global-jobs/tracked/lifecycle/0",
            headers=HEADERS,
        )

    saved = global_jobs.find("tracked")
    assert response.status_code == 422
    assert response.json()["detail"] == (
        "The Saved lifecycle event cannot be deleted."
    )
    assert saved is not None
    assert [entry.status for entry in saved.user_status_history] == [
        UserStatus.SAVED
    ]


def test_global_job_salaries_can_be_saved_and_cleared(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    _save_config(paths)
    global_jobs = GlobalJobStore(paths)
    job = _job("tracked", external_id="tracked")
    global_jobs.set_status(job, UserStatus.SAVED, NOW)
    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        global_job_store=global_jobs,
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        saved_response = client.post(
            "/api/global-jobs/tracked/salary",
            json={
                "expected_salary": " 5,500 EUR ",
                "expected_salary_period": "month",
                "offer_salary": "70,000 EUR",
                "offer_salary_period": "year",
            },
            headers=HEADERS,
        )
        cleared_response = client.post(
            "/api/global-jobs/tracked/salary",
            json={
                "expected_salary": "",
                "expected_salary_period": "year",
                "offer_salary": "72,000 EUR",
                "offer_salary_period": "year",
            },
            headers=HEADERS,
        )

    saved = global_jobs.find("tracked")
    assert saved_response.status_code == 204
    assert cleared_response.status_code == 204
    assert saved is not None
    assert saved.expected_salary is None
    assert saved.offer_salary is not None
    assert saved.offer_salary.amount == "72,000 EUR"
    assert saved.offer_salary.period.value == "year"


def test_global_job_notes_can_be_added_edited_and_deleted(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    _save_config(paths)
    global_jobs = GlobalJobStore(paths)
    job = _job("tracked", external_id="tracked")
    global_jobs.set_status(job, UserStatus.SAVED, NOW)
    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        global_job_store=global_jobs,
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        added_response = client.post(
            "/api/global-jobs/tracked/notes",
            json={"content": "  Follow up with recruiter.  "},
            headers=HEADERS,
        )
        added = global_jobs.find("tracked")
        assert added is not None
        note_id = added.notes[0].id
        created_at = added.notes[0].created_at
        edited_response = client.put(
            f"/api/global-jobs/tracked/notes/{note_id}",
            json={"content": "Follow up on Tuesday."},
            headers=HEADERS,
        )
        edited = global_jobs.find("tracked")
        invalid_response = client.post(
            "/api/global-jobs/tracked/notes",
            json={"content": "   "},
            headers=HEADERS,
        )
        missing_response = client.delete(
            "/api/global-jobs/tracked/notes/22222222-2222-4222-8222-222222222222",
            headers=HEADERS,
        )
        deleted_response = client.delete(
            f"/api/global-jobs/tracked/notes/{note_id}",
            headers=HEADERS,
        )

    deleted = global_jobs.find("tracked")
    assert added_response.status_code == 204
    assert edited_response.status_code == 204
    assert invalid_response.status_code == 422
    assert missing_response.status_code == 404
    assert deleted_response.status_code == 204
    assert created_at.tzinfo is not None
    assert edited is not None
    assert edited.notes[0].content == "Follow up on Tuesday."
    assert edited.notes[0].created_at == created_at
    assert deleted is not None
    assert deleted.notes == []


def test_history_status_hides_matching_review_without_changing_history(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    repository = _repository(paths)
    history = SearchHistoryStore(paths)
    _archive(history, tmp_path, "run-a", _snapshot(_job("a", external_id="shared")))
    _archive(history, tmp_path, "run-b", _snapshot(_job("b", external_id="shared")))
    global_jobs = GlobalJobStore(paths)
    workflow = SimpleNamespace(load_setup_answers=lambda: None)
    app = create_review_app(
        repository,
        TOKEN,
        frozenset({ORIGIN}),
        workflow=workflow,
        history_store=history,
        global_job_store=global_jobs,
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        response = client.post(
            "/api/scan-history/run-a/jobs/a/status",
            json={"status": "applied"},
            headers=HEADERS,
        )
        page_response = client.get("/setup?run_id=run-b#review")

    page = BeautifulSoup(page_response.text, "html.parser")
    assert response.status_code == 204
    assert page_response.status_code == 200
    assert page.select_one('[data-review-block="global"] #applied [data-job-key]')
    assert page.select_one(
        '[data-review-block="current"] [data-job-key="b"]'
    ) is None
    assert history.load("run-a").jobs[0].user_status is UserStatus.NEW
    assert history.load("run-b").jobs[0].user_status is UserStatus.NEW


def test_history_status_copies_its_resume_into_job_tracker(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    _save_config(paths)
    resume_bytes = b"HISTORY APPLICATION RESUME"
    resume_id = "sha256:" + hashlib.sha256(resume_bytes).hexdigest()
    history_config = load_config(paths.config_toml).model_copy(
        update={
            "resume_sha256": resume_id,
            "profile_sha256": "sha256:" + "d" * 64,
        }
    )
    history = SearchHistoryStore(paths)
    _archive(
        history,
        tmp_path,
        "run-a",
        _snapshot(_job("a", external_id="a")),
        resume_bytes=resume_bytes,
        config_bytes=serialize_config(history_config).encode("utf-8"),
    )
    global_jobs = GlobalJobStore(paths)
    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        history_store=history,
        global_job_store=global_jobs,
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        changed = client.post(
            "/api/scan-history/run-a/jobs/a/status",
            json={"status": "applied"},
            headers=HEADERS,
        )
        deleted = client.delete("/api/scan-history/run-a", headers=HEADERS)
        downloaded = client.get("/api/global-jobs/a/resume")

    saved = global_jobs.find("a")
    assert changed.status_code == 204
    assert deleted.status_code == 200
    assert saved is not None
    assert saved.application_resume_id == resume_id
    assert saved.application_resume_filename == "run-a.pdf"
    assert downloaded.status_code == 200
    assert downloaded.content == resume_bytes


def test_same_global_job_uses_the_latest_visible_review_in_tracker(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    _save_config(paths)
    paths.profile_md.write_text("# Current profile", encoding="utf-8")
    repository = _repository(paths)
    history = SearchHistoryStore(paths)
    resume_a_bytes = b"RESUME A"
    resume_b_bytes = b"RESUME B"
    resume_a_id = "sha256:" + hashlib.sha256(resume_a_bytes).hexdigest()
    resume_b_id = "sha256:" + hashlib.sha256(resume_b_bytes).hexdigest()
    profile_a = "sha256:" + "c" * 64
    profile_b = "sha256:" + "d" * 64
    base = load_config(paths.config_toml)
    config_a = base.model_copy(
        update={
            "candidate_name": "History A",
            "resume_sha256": resume_a_id,
            "profile_sha256": profile_a,
        }
    )
    config_b = base.model_copy(
        update={
            "candidate_name": "History B",
            "resume_sha256": resume_b_id,
            "profile_sha256": profile_b,
        }
    )
    job_a = _job("a", external_id="shared")
    job_a.score = 91
    job_a.reason = "Strong Java match"
    job_a.last_successful_review_profile_hash = profile_a
    job_b = _job("b", external_id="shared")
    job_b.score = 63
    job_b.reason = "Missing Kotlin experience"
    job_b.last_successful_review_profile_hash = profile_b
    _archive(
        history,
        tmp_path,
        "run-a",
        _snapshot(job_a),
        resume_bytes=resume_a_bytes,
        profile_bytes=b"# Profile A",
        config_bytes=serialize_config(config_a).encode("utf-8"),
    )
    _archive(
        history,
        tmp_path,
        "run-b",
        _snapshot(job_b),
        resume_bytes=resume_b_bytes,
        profile_bytes=b"# Profile B",
        config_bytes=serialize_config(config_b).encode("utf-8"),
    )
    global_jobs = GlobalJobStore(paths)
    app = create_review_app(
        repository,
        TOKEN,
        frozenset({ORIGIN}),
        workflow=SimpleNamespace(load_setup_answers=lambda: None),
        history_store=history,
        global_job_store=global_jobs,
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        assert client.post(
            "/api/scan-history/run-a/jobs/a/status",
            json={"status": "saved"},
            headers=HEADERS,
        ).status_code == 204
        assert client.post(
            "/api/scan-history/run-b/jobs/b/status",
            json={"status": "applied"},
            headers=HEADERS,
        ).status_code == 204
        page_a = BeautifulSoup(
            client.get("/setup#review").text,
            "html.parser",
        )
        page_b = BeautifulSoup(
            client.get("/setup#review").text,
            "html.parser",
        )

    card_a = page_a.select_one('[data-review-block="global"] [data-job-key]')
    card_b = page_b.select_one('[data-review-block="global"] [data-job-key]')
    assert len(global_jobs.load().jobs) == 1
    assert card_a is not None
    assert card_a.get("data-score") == "63"
    assert "Missing Kotlin experience" in card_a.get_text(" ", strip=True)
    assert card_b is not None
    assert card_b.get("data-score") == "63"
    assert "Missing Kotlin experience" in card_b.get_text(" ", strip=True)
    assert "applied" in card_a.get_text(" ", strip=True).lower()


def test_setup_filters_only_tracked_jobs_from_review_without_changing_stored_data(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    current = _job("current", external_id="shared")
    untracked = _job("untracked", external_id="untracked")
    repository = _repository(paths, current, untracked)
    tracked = _job("tracked", external_id="shared")
    global_jobs = GlobalJobStore(paths)
    global_jobs.set_status(tracked, UserStatus.SAVED, NOW)
    review_before = repository.load()
    tracker_before = global_jobs.load()
    app = create_review_app(
        repository,
        TOKEN,
        frozenset({ORIGIN}),
        workflow=SimpleNamespace(load_setup_answers=lambda: None),
        global_job_store=global_jobs,
    )

    with TestClient(app, base_url=ORIGIN) as client:
        page = BeautifulSoup(client.get("/setup#review").text, "html.parser")

    assert page.select_one(
        '[data-review-block="current"] [data-job-key="current"]'
    ) is None
    assert page.select_one(
        '[data-review-block="current"] [data-job-key="untracked"]'
    ) is not None
    assert page.select_one(
        '[data-review-block="global"] [data-job-key="tracked"]'
    ) is not None
    assert repository.load() == review_before
    assert global_jobs.load() == tracker_before


def test_setup_keeps_review_available_when_job_tracker_cannot_be_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    repository = _repository(paths, _job("current", external_id="current"))
    global_jobs = GlobalJobStore(paths)
    app = create_review_app(
        repository,
        TOKEN,
        frozenset({ORIGIN}),
        workflow=SimpleNamespace(load_setup_answers=lambda: None),
        global_job_store=global_jobs,
    )

    def fail_tracker_read() -> Snapshot:
        raise OSError("injected Job Tracker read failure")

    monkeypatch.setattr(global_jobs, "load_for_tracker", fail_tracker_read)

    with TestClient(
        app,
        base_url=ORIGIN,
        raise_server_exceptions=False,
    ) as client:
        response = client.get("/setup#review")

    page = BeautifulSoup(response.text, "html.parser")
    assert response.status_code == 200
    assert page.select_one(
        '[data-review-block="current"] [data-job-key="current"]'
    ) is not None
    assert page.select('[data-review-block="global"] [data-job-key]') == []


def test_job_tracker_aggregates_all_saved_jobs(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    global_jobs = GlobalJobStore(paths)
    global_jobs.set_status(
        _job("from-a", external_id="from-a"),
        UserStatus.SAVED,
        NOW,
    )
    global_jobs.set_status(
        _job("from-b", external_id="from-b"),
        UserStatus.SAVED,
        NOW,
    )
    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        workflow=SimpleNamespace(load_setup_answers=lambda: None),
        global_job_store=global_jobs,
    )

    with TestClient(app, base_url=ORIGIN) as client:
        page = BeautifulSoup(
            client.get("/setup#job-tracker").text,
            "html.parser",
        )

    assert {
        card.get("data-job-key")
        for card in page.select('[data-review-block="global"] [data-job-key]')
    } == {"from-a", "from-b"}


def test_opening_job_tracker_does_not_import_review_history(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    reviewed = _job("review-only", external_id="review-only").model_copy(
        update={"user_status": UserStatus.SAVED}
    )
    history = SearchHistoryStore(paths)
    _archive(history, tmp_path, "run-a", _snapshot(reviewed))
    global_jobs = GlobalJobStore(paths)
    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        workflow=SimpleNamespace(load_setup_answers=lambda: None),
        history_store=history,
        global_job_store=global_jobs,
    )

    with TestClient(app, base_url=ORIGIN) as client:
        response = client.get("/setup#job-tracker")

    assert response.status_code == 200
    assert global_jobs.find("review-only") is None


def test_job_tracker_resume_can_be_downloaded(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    resume_bytes = b"RESUME BYTES"
    resume_digest = hashlib.sha256(resume_bytes).hexdigest()
    resume_id = f"sha256:{resume_digest}"
    resume_dir = paths.root / "resumes"
    resume_dir.mkdir()
    (resume_dir / f"{resume_digest}.pdf").write_bytes(resume_bytes)
    global_jobs = GlobalJobStore(paths)
    tracked = global_jobs.set_status(
        _job("tracked", external_id="tracked"),
        UserStatus.SAVED,
        NOW,
    ).jobs[0]
    global_jobs.set_application_resume(tracked, resume_id, "backend cv.pdf")
    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        global_job_store=global_jobs,
    )

    with TestClient(app, base_url=ORIGIN) as client:
        response = client.get("/api/global-jobs/tracked/resume")

    assert response.status_code == 200
    assert response.content == resume_bytes
    assert "backend%20cv.pdf" in response.headers["content-disposition"]


def test_legacy_resume_catalog_is_migrated_to_job_attachment(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    resume_bytes = b"LEGACY RESUME"
    resume_digest = hashlib.sha256(resume_bytes).hexdigest()
    resume_id = f"sha256:{resume_digest}"
    legacy_dir = paths.root / "global-resumes" / resume_digest
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "manifest.json").write_text(
        json.dumps(
            {
                "resume_id": resume_id,
                "profile_hash": "sha256:" + "d" * 64,
                "profile_hashes": [],
                "candidate_name": "Legacy CV",
                "filename": "legacy cv.pdf",
                "created_at": NOW.isoformat(),
            }
        ),
        encoding="utf-8",
    )
    (legacy_dir / "resume").write_bytes(resume_bytes)
    global_jobs = GlobalJobStore(paths)
    tracked = global_jobs.set_status(
        _job("tracked", external_id="tracked"),
        UserStatus.SAVED,
        NOW,
    ).jobs[0]
    global_jobs.set_application_resume(tracked, resume_id)

    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        workflow=SimpleNamespace(load_setup_answers=lambda: None),
        global_job_store=global_jobs,
    )

    with TestClient(app, base_url=ORIGIN) as client:
        response = client.get("/setup")

    saved = global_jobs.find("tracked")
    assert response.status_code == 200
    assert saved is not None
    assert saved.application_resume_filename == "legacy cv.pdf"
    assert (paths.root / "resumes" / f"{resume_digest}.pdf").read_bytes() == (
        resume_bytes
    )
    assert not (paths.root / "global-resumes").exists()


def test_job_tracker_accepts_first_resume_without_setup_config(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    global_jobs = GlobalJobStore(paths)
    global_jobs.set_status(
        _job("tracked", external_id="tracked"),
        UserStatus.SAVED,
        NOW,
    )
    uploaded_bytes = SAMPLE_RESUME.read_bytes()
    uploaded_digest = hashlib.sha256(uploaded_bytes).hexdigest()
    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        global_job_store=global_jobs,
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        response = client.post(
            "/api/global-jobs/tracked/resume",
            files={"resume": ("first.docx", uploaded_bytes)},
            headers=HEADERS,
        )

    saved = global_jobs.find("tracked")
    assert response.status_code == 204
    assert saved is not None
    assert saved.application_resume_id == f"sha256:{uploaded_digest}"
    assert saved.application_resume_filename == "first.docx"
    assert (paths.root / "resumes" / f"{uploaded_digest}.docx").read_bytes() == (
        uploaded_bytes
    )


def test_job_tracker_resume_can_be_replaced_by_upload(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    _save_config(paths)
    paths.profile_md.write_text("# Current profile", encoding="utf-8")
    old_config = paths.config_toml.read_bytes()
    old_profile = paths.profile_md.read_bytes()
    global_jobs = GlobalJobStore(paths)
    global_jobs.set_status(
        _job("tracked", external_id="tracked"),
        UserStatus.SAVED,
        NOW,
    )
    old_resume_id = _attach_resume(
        paths,
        global_jobs,
        "tracked",
        "old.pdf",
        b"OLD RESUME",
    )
    tracked = global_jobs.find("tracked")
    assert tracked is not None
    global_jobs.set_status(
        tracked,
        UserStatus.SAVED,
        NOW,
        resume_id=old_resume_id,
        profile_hash="sha256:" + "a" * 64,
    )
    uploaded_bytes = SAMPLE_RESUME.read_bytes()
    uploaded_resume_id = "sha256:" + hashlib.sha256(uploaded_bytes).hexdigest()

    def fail_preparation(_resume_path: Path, _answers: SetupAnswers) -> SetupPreparation:
        raise AssertionError("Job attachment upload must not prepare a profile")

    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        global_job_store=global_jobs,
        manual_resume_preparer=fail_preparation,
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        response = client.post(
            "/api/global-jobs/tracked/resume",
            files={
                "resume": (
                    "updated.docx",
                    uploaded_bytes,
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
            headers=HEADERS,
        )

    saved = global_jobs.find("tracked")
    assert response.status_code == 204
    assert saved is not None
    assert saved.application_resume_id == uploaded_resume_id
    assert saved.application_resume_filename == "updated.docx"
    assert saved.last_evaluated_resume_id == old_resume_id
    assert old_resume_id != uploaded_resume_id
    digest = uploaded_resume_id.removeprefix("sha256:")
    assert (paths.root / "resumes" / f"{digest}.docx").read_bytes() == uploaded_bytes
    assert paths.config_toml.read_bytes() == old_config
    assert paths.profile_md.read_bytes() == old_profile


def test_invalid_job_tracker_resume_does_not_replace_the_attachment(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    global_jobs = GlobalJobStore(paths)
    global_jobs.set_status(
        _job("tracked", external_id="tracked"),
        UserStatus.SAVED,
        NOW,
    )
    old_resume_id = _attach_resume(
        paths,
        global_jobs,
        "tracked",
        "old.pdf",
        b"OLD RESUME",
    )

    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        global_job_store=global_jobs,
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        response = client.post(
            "/api/global-jobs/tracked/resume",
            files={"resume": ("invalid.txt", b"NOT A RESUME")},
            headers=HEADERS,
        )

    saved = global_jobs.find("tracked")
    assert response.status_code == 422
    assert saved is not None
    assert saved.application_resume_id == old_resume_id
    assert saved.application_resume_filename == "old.pdf"


def test_ats_groups_selected_jobs_by_their_application_resume(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    repository = _repository(paths)
    _save_config(paths)
    resume_a_bytes = b"RESUME A"
    resume_b_bytes = b"RESUME B"
    resume_a = "sha256:" + hashlib.sha256(resume_a_bytes).hexdigest()
    resume_b = "sha256:" + hashlib.sha256(resume_b_bytes).hexdigest()
    resume_dir = paths.root / "resumes"
    resume_dir.mkdir()
    for resume_id, filename, resume_bytes in (
        (resume_a, "resume-a.pdf", resume_a_bytes),
        (resume_b, "resume-b.pdf", resume_b_bytes),
    ):
        digest = resume_id.removeprefix("sha256:")
        (resume_dir / f"{digest}.pdf").write_bytes(resume_bytes)
    global_jobs = GlobalJobStore(paths)
    for key, resume_id, filename in (
        ("job-a", resume_a, "resume-a.pdf"),
        ("job-c", resume_a, "resume-a.pdf"),
        ("job-b", resume_b, "resume-b.pdf"),
    ):
        global_jobs.set_status(
            _job(key, external_id=key),
            UserStatus.SAVED,
            NOW,
        )
        tracked = global_jobs.find(key)
        assert tracked is not None
        global_jobs.set_application_resume(tracked, resume_id, filename)
    ats_workflow = RecordingAtsWorkflow()
    app = create_review_app(
        repository,
        TOKEN,
        frozenset({ORIGIN}),
        global_job_store=global_jobs,
        ats_workflow=ats_workflow,
        ats_history_store=AtsHistoryStore(paths),
    )
    assert paths.job_tracker_config_toml.exists()
    paths.config_toml.unlink()

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        response = client.post(
            "/api/ats-runs",
            data={
                "job_keys": json.dumps(["job-a", "job-c", "job-b"]),
            },
            headers=HEADERS,
        )

    assert response.status_code == 202, response.text
    inputs = ats_workflow.inputs[0]
    assert inputs.search_run_id == "global"
    assert [item.resume_id for item in inputs.resumes] == [resume_a, resume_b]
    assert [
        [job.canonical_job_key for job in item.jobs]
        for item in inputs.resumes
    ] == [["job-a", "job-c"], ["job-b"]]
    assert [item.resume_bytes for item in inputs.resumes] == [
        resume_a_bytes,
        resume_b_bytes,
    ]
    assert inputs.config.selected_model == "sonnet"


def test_ats_rejects_a_selected_job_without_an_application_resume(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    _save_config(paths)
    global_jobs = GlobalJobStore(paths)
    global_jobs.set_status(
        _job("job-a", external_id="job-a"),
        UserStatus.SAVED,
        NOW,
    )
    ats_workflow = RecordingAtsWorkflow()
    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        global_job_store=global_jobs,
        ats_workflow=ats_workflow,
        ats_history_store=AtsHistoryStore(paths),
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        response = client.post(
            "/api/ats-runs",
            data={"job_keys": json.dumps(["job-a"])},
            headers=HEADERS,
        )

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "Backend Engineer job-a has no saved application resume."
    )
    assert ats_workflow.inputs == []


def test_ats_rejects_a_selected_job_whose_saved_resume_is_unavailable(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    _save_config(paths)
    global_jobs = GlobalJobStore(paths)
    tracked = global_jobs.set_status(
        _job("job-a", external_id="job-a"),
        UserStatus.SAVED,
        NOW,
    ).jobs[0]
    global_jobs.set_application_resume(
        tracked,
        "sha256:" + "c" * 64,
        "missing.pdf",
    )
    ats_workflow = RecordingAtsWorkflow()
    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        global_job_store=global_jobs,
        ats_workflow=ats_workflow,
        ats_history_store=AtsHistoryStore(paths),
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        response = client.post(
            "/api/ats-runs",
            data={"job_keys": json.dumps(["job-a"])},
            headers=HEADERS,
        )

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "The saved resume for Backend Engineer job-a is unavailable."
    )
    assert ats_workflow.inputs == []


def test_ats_ignores_review_history_context_and_uses_the_job_resume(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    global_job = _job("global", external_id="global")
    repository = _repository(paths)
    _save_config(paths)
    history = SearchHistoryStore(paths)
    resume_bytes = b"DEFAULT RESUME"
    resume_id = "sha256:" + hashlib.sha256(resume_bytes).hexdigest()
    _archive(history, tmp_path, "run-a", _snapshot(global_job), resume_bytes)
    global_jobs = GlobalJobStore(paths)
    global_jobs.set_status(
        global_job,
        UserStatus.SAVED,
        NOW,
    )
    assert _attach_resume(
        paths,
        global_jobs,
        "global",
        "job-resume.pdf",
        resume_bytes,
    ) == resume_id
    ats_workflow = RecordingAtsWorkflow()
    app = create_review_app(
        repository,
        TOKEN,
        frozenset({ORIGIN}),
        history_store=history,
        global_job_store=global_jobs,
        ats_workflow=ats_workflow,
        ats_history_store=AtsHistoryStore(paths),
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        response = client.post(
            "/api/ats-runs",
            data={"search_run_id": "run-a", "job_keys": json.dumps(["global"])},
            headers=HEADERS,
        )

    assert response.status_code == 202
    inputs = ats_workflow.inputs[0]
    assert inputs.search_run_id == "global"
    assert inputs.resumes[0].resume_id == resume_id
    assert inputs.resumes[0].resume_filename == "job-resume.pdf"
    assert inputs.resumes[0].resume_bytes == resume_bytes


def test_ats_ignores_a_page_resume_override_and_uses_the_job_resume(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    global_job = _job("global", external_id="global")
    repository = _repository(paths)
    _save_config(paths)
    history = SearchHistoryStore(paths)
    _archive(history, tmp_path, "run-a", _snapshot(global_job), b"DEFAULT RESUME")
    resume_bytes = b"JOB RESUME"
    resume_id = "sha256:" + hashlib.sha256(resume_bytes).hexdigest()
    global_jobs = GlobalJobStore(paths)
    global_jobs.set_status(
        global_job,
        UserStatus.SAVED,
        NOW,
    )
    assert _attach_resume(
        paths,
        global_jobs,
        "global",
        "job.pdf",
        resume_bytes,
    ) == resume_id
    ats_workflow = RecordingAtsWorkflow()
    app = create_review_app(
        repository,
        TOKEN,
        frozenset({ORIGIN}),
        history_store=history,
        global_job_store=global_jobs,
        ats_workflow=ats_workflow,
        ats_history_store=AtsHistoryStore(paths),
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        response = client.post(
            "/api/ats-runs",
            data={
                "resume_id": "sha256:" + "f" * 64,
                "job_keys": json.dumps(["global"]),
            },
            headers=HEADERS,
        )

    assert response.status_code == 202
    inputs = ats_workflow.inputs[0]
    assert inputs.resumes[0].resume_id == resume_id
    assert inputs.resumes[0].resume_filename == "job.pdf"
    assert inputs.resumes[0].resume_bytes == b"JOB RESUME"


def test_ats_uses_job_tracker_config_instead_of_review_history_config(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    global_job = _job("global", external_id="global")
    repository = _repository(paths)
    _save_config(paths)
    resume_bytes = b"HISTORY JOB RESUME"
    resume_id = "sha256:" + hashlib.sha256(resume_bytes).hexdigest()
    history_config = AppConfig(
        candidate_name="History Candidate",
        ai_runtime="claude-code",
        resume_path=paths.root / "history.pdf",
        resume_sha256=resume_id,
        profile_sha256="sha256:" + "d" * 64,
        search_terms=["backend"],
        locations=["Berlin"],
        german_level="B1",
        claude=ClaudeSettings(model="history-opus", effort="high"),
        scheduler=SchedulerSettings(),
    )
    history = SearchHistoryStore(paths)
    _archive(
        history,
        tmp_path,
        "run-a",
        _snapshot(global_job),
        resume_bytes=resume_bytes,
        config_bytes=serialize_config(history_config).encode("utf-8"),
    )
    global_jobs = GlobalJobStore(paths)
    global_jobs.set_status(
        global_job,
        UserStatus.SAVED,
        NOW,
    )
    assert _attach_resume(
        paths,
        global_jobs,
        "global",
        "history-job.pdf",
        resume_bytes,
    ) == resume_id
    ats_workflow = RecordingAtsWorkflow()
    app = create_review_app(
        repository,
        TOKEN,
        frozenset({ORIGIN}),
        history_store=history,
        global_job_store=global_jobs,
        ats_workflow=ats_workflow,
        ats_history_store=AtsHistoryStore(paths),
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        response = client.post(
            "/api/ats-runs",
            data={
                "ai_choice": "history",
                "job_keys": json.dumps(["global"]),
            },
            headers=HEADERS,
        )

    assert response.status_code == 202
    inputs = ats_workflow.inputs[0]
    assert inputs.config.ai_runtime == "claude-code"
    assert inputs.config.selected_model == "sonnet"


def test_review_removes_the_ats_ai_picker_and_uses_the_global_modal(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    repository = _repository(paths)
    _save_config(paths)
    ai_store = AiProviderStore(paths.ai_config_toml)
    provider = ai_store.create(
        AiProviderDraft(
            display_name="ds",
            base_url="https://ai.example/v1",
            api_key="secret",
            model="deepseek-v4-pro",
            reasoning_effort="low",
        )
    )
    history_config = AppConfig(
        candidate_name="History Candidate",
        ai_runtime=f"api:{provider.id}",
        ai_model=provider.model,
        resume_path=paths.root / "history.pdf",
        resume_sha256="sha256:" + "c" * 64,
        profile_sha256="sha256:" + "d" * 64,
        search_terms=["backend"],
        locations=["Berlin"],
        german_level="B1",
        claude=ClaudeSettings(model="sonnet", effort="high"),
        scheduler=SchedulerSettings(),
    )
    history = SearchHistoryStore(paths)
    _archive(
        history,
        tmp_path,
        "run-a",
        _snapshot(_job("history", external_id="history")),
        config_bytes=serialize_config(history_config).encode("utf-8"),
    )
    workflow = SimpleNamespace(load_setup_answers=lambda: None)
    app = create_review_app(
        repository,
        TOKEN,
        frozenset({ORIGIN}),
        workflow=workflow,
        history_store=history,
        ai_store=ai_store,
    )

    with TestClient(app, base_url=ORIGIN) as client:
        page = BeautifulSoup(
            client.get("/setup?run_id=run-a#review").text,
            "html.parser",
        )

    assert page.select_one("#ats-ai-choice") is None
    assert page.select_one("#ai-config-modal #ai-runtime") is not None


def test_ats_can_override_history_ai_with_saved_provider(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    current = _job("current", external_id="current")
    repository = _repository(paths, current)
    _save_config(paths)
    paths.profile_md.write_text("# Ada", encoding="utf-8")
    global_jobs = GlobalJobStore(paths)
    global_jobs.set_status(
        current,
        UserStatus.SAVED,
        NOW,
    )
    _attach_resume(paths, global_jobs, "current", "current.pdf", b"RESUME")
    ai_store = AiProviderStore(paths.ai_config_toml)
    provider = ai_store.create(
        AiProviderDraft(
            display_name="Alternate",
            base_url="https://ai.example/v1",
            api_key="secret",
            model="alternate-model",
            reasoning_effort="low",
        )
    )
    AiSelectionStore(paths.ai_selection_toml).save(
        AiRuntimeSelection(
            ai_runtime=f"api:{provider.id}",
            claude=ClaudeRuntimeSelection(model="opus", effort="high"),
        )
    )
    ats_workflow = RecordingAtsWorkflow()
    app = create_review_app(
        repository,
        TOKEN,
        frozenset({ORIGIN}),
        ai_store=ai_store,
        global_job_store=global_jobs,
        ats_workflow=ats_workflow,
        ats_history_store=AtsHistoryStore(paths),
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        response = client.post(
            "/api/ats-runs",
            data={
                "ai_choice": f"runtime:api:{provider.id}",
                "job_keys": json.dumps(["current"]),
            },
            headers=HEADERS,
        )

    assert response.status_code == 202
    inputs = ats_workflow.inputs[0]
    assert inputs.config.ai_runtime == f"api:{provider.id}"
    assert inputs.config.selected_model == "alternate-model"


def test_ats_uses_global_ai_selection_instead_of_history_ai(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    global_job = _job("global", external_id="global")
    repository = _repository(paths, global_job)
    _save_config(paths)
    ai_store = AiProviderStore(paths.ai_config_toml)
    provider = ai_store.create(
        AiProviderDraft(
            display_name="Current",
            base_url="https://ai.example/v1",
            api_key="secret",
            model="current-model",
            reasoning_effort="high",
        )
    )
    AiSelectionStore(paths.ai_selection_toml).save(
        AiRuntimeSelection(
            ai_runtime=f"api:{provider.id}",
            claude=ClaudeRuntimeSelection(model="opus", effort="low"),
        )
    )
    history_config = load_config(paths.config_toml).model_copy(
        update={
            "candidate_name": "History Candidate",
            "ai_runtime": "claude-code",
            "ai_model": None,
            "claude": ClaudeSettings(model="history-opus", effort="high"),
        }
    )
    history = SearchHistoryStore(paths)
    _archive(
        history,
        tmp_path,
        "run-a",
        _snapshot(global_job),
        config_bytes=serialize_config(history_config).encode("utf-8"),
    )
    global_jobs = GlobalJobStore(paths)
    global_jobs.set_status(
        global_job,
        UserStatus.SAVED,
        NOW,
    )
    _attach_resume(paths, global_jobs, "global", "global.pdf", b"GLOBAL RESUME")
    ats_workflow = RecordingAtsWorkflow()
    app = create_review_app(
        repository,
        TOKEN,
        frozenset({ORIGIN}),
        history_store=history,
        global_job_store=global_jobs,
        ai_store=ai_store,
        ats_workflow=ats_workflow,
        ats_history_store=AtsHistoryStore(paths),
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        response = client.post(
            "/api/ats-runs",
            data={
                "ai_choice": "history",
                "job_keys": json.dumps(["global"]),
            },
            headers=HEADERS,
        )

    assert response.status_code == 202, response.text
    inputs = ats_workflow.inputs[0]
    assert inputs.config.ai_runtime == f"api:{provider.id}"
    assert inputs.config.selected_model == "current-model"
    assert inputs.config.claude.model == "opus"


def test_manual_job_import_rejects_non_public_url(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        response = _post_job_with_resume(
            client,
            "http://127.0.0.1:8765/setup",
        )

    assert response.status_code == 422
    assert response.json() == {
        "detail": "Use a public HTTPS job URL without credentials."
    }


def test_manual_job_import_persists_card_as_saved(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    imported = _job("manual", external_id="manual")
    imported.url = HttpUrl("https://careers.example/jobs/manual")
    imported.source_occurrences[0].url = imported.url
    global_jobs = GlobalJobStore(paths)
    _save_config(paths)
    AiSelectionStore(paths.ai_selection_toml).save(
        AiRuntimeSelection(
            ai_runtime="claude-code",
            claude=ClaudeRuntimeSelection(
                model="opus",
                effort="high",
                thinking_enabled=False,
            ),
        )
    )
    paths.profile_md.write_text("# Current candidate profile", encoding="utf-8")
    import_inputs: list[tuple[object, ...]] = []

    def import_job(*inputs: object) -> JobRecord:
        import_inputs.append(inputs)
        return imported

    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        global_job_store=global_jobs,
        manual_job_importer=import_job,
        manual_resume_preparer=_prepare_uploaded_resume(paths),
        company_size_service=CompanySizeService(
            CompanySizeStore(paths.cache_dir / "company-sizes.json"),
            UnavailableCompanySizeLookup(),
        ),
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        response = _post_job_with_resume(client)

    assert response.status_code == 202
    started = response.json()
    assert started["status"] == "running"
    assert isinstance(started["import_id"], str)
    assert started["progress_percent"] >= 0
    assert started["resume_id"] is None
    state = _wait_manual_import_completion(client, started["import_id"])
    assert state["status"] == "complete"
    assert state["job_key"] == "manual"
    assert state["result_status"] == "saved"
    assert len(import_inputs) == 1
    url, config, profile, imported_at = import_inputs[0]
    assert url == "https://careers.example/jobs/manual"
    assert isinstance(config, AppConfig)
    assert config.ai_runtime == "claude-code"
    assert config.claude.model == "opus"
    assert profile == "# Uploaded resume profile"
    assert isinstance(imported_at, datetime)
    assert imported_at.tzinfo is not None
    saved = global_jobs.find("manual")
    assert saved is not None
    assert saved.user_status is UserStatus.SAVED


def test_manual_job_import_reuses_profile_for_the_same_uploaded_resume(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    global_jobs = GlobalJobStore(paths)
    _save_config(paths)
    profile = """# Target roles
Backend Engineer

# Technical skills
Python

# Experience
Backend delivery

# Languages
English

# Work authorization and visa
Unknown

# Preferences
Berlin
"""
    profile_requests: list[ClaudeRequest] = []

    def invoke_profile(
        _self: AiRuntimeInvoker,
        request: ClaudeRequest,
    ) -> ClaudeInvocation:
        profile_requests.append(request)
        return ClaudeInvocation(
            argv=["fake-ai"],
            stdout=json.dumps(
                {"structured_output": {"profile_markdown": profile}}
            ).encode(),
            stderr=b"",
            exit_code=0,
            duration_seconds=0.01,
        )

    monkeypatch.setattr(AiRuntimeInvoker, "invoke", invoke_profile)
    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        global_job_store=global_jobs,
        manual_job_importer=lambda *_inputs: _job("manual", external_id="manual"),
        company_size_service=CompanySizeService(
            CompanySizeStore(paths.cache_dir / "company-sizes.json"),
            UnavailableCompanySizeLookup(),
        ),
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        for _ in range(2):
            response = _post_job_with_resume(client)
            assert response.status_code == 202
            state = _wait_manual_import_completion(
                client,
                response.json()["import_id"],
            )
            assert state["status"] == "complete"

    assert len(profile_requests) == 1


def test_manual_job_import_does_not_read_selected_history_profile_or_config(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    imported = _job("manual", external_id="manual")
    global_jobs = GlobalJobStore(paths)
    _save_config(paths)
    AiSelectionStore(paths.ai_selection_toml).save(
        AiRuntimeSelection(
            ai_runtime="claude-code",
            claude=ClaudeRuntimeSelection(model="opus", effort="high"),
        )
    )
    paths.profile_md.write_text("# Current candidate profile", encoding="utf-8")
    history_config = load_config(paths.config_toml).model_copy(
        update={
            "candidate_name": "History Candidate",
            "resume_sha256": "sha256:" + "c" * 64,
            "profile_sha256": "sha256:" + "d" * 64,
        }
    )
    history = SearchHistoryStore(paths)
    _archive(
        history,
        tmp_path,
        "run-a",
        _snapshot(),
        profile_bytes=b"# History candidate profile",
        config_bytes=serialize_config(history_config).encode("utf-8"),
    )
    import_inputs: list[tuple[object, ...]] = []

    def import_job(*inputs: object) -> JobRecord:
        import_inputs.append(inputs)
        return imported

    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        history_store=history,
        global_job_store=global_jobs,
        manual_job_importer=import_job,
        manual_resume_preparer=_prepare_uploaded_resume(paths),
        company_size_service=CompanySizeService(
            CompanySizeStore(paths.cache_dir / "company-sizes.json"),
            UnavailableCompanySizeLookup(),
        ),
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        response = _post_job_with_resume(client, run_id="run-a")

    assert response.status_code == 202
    started = response.json()
    assert started["status"] == "running"
    _wait_manual_import_completion(client, started["import_id"])
    assert len(import_inputs) == 1
    _url, config, profile, _imported_at = import_inputs[0]
    assert isinstance(config, AppConfig)
    assert config.candidate_name == "backend"
    assert config.resume_sha256 != "sha256:" + "c" * 64
    assert config.ai_runtime == "claude-code"
    assert config.claude.model == "opus"
    assert profile == "# Uploaded resume profile"


def test_ats_history_time_does_not_follow_review_history(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    resume_bytes = b"ARCHIVED RESUME"
    resume_id = "sha256:" + hashlib.sha256(resume_bytes).hexdigest()
    history_config = AppConfig(
        candidate_name="Ada",
        ai_runtime="claude-code",
        resume_path=paths.root / "history.pdf",
        resume_sha256=resume_id,
        profile_sha256="sha256:" + "d" * 64,
        search_terms=["backend"],
        locations=["Berlin"],
        german_level="B1",
        claude=ClaudeSettings(model="sonnet", effort="medium"),
        scheduler=SchedulerSettings(),
    )
    config_bytes = serialize_config(history_config).encode("utf-8")
    history = SearchHistoryStore(paths)
    _archive_with_time(
        history,
        tmp_path,
        "run-old",
        datetime(2026, 8, 18, 9, 0, tzinfo=UTC),
        config_bytes,
    )
    _archive_with_time(
        history,
        tmp_path,
        "run-new",
        datetime(2026, 8, 20, 15, 30, tzinfo=UTC),
        config_bytes,
    )
    ats_history = AtsHistoryStore(paths)
    ats_history.archive(
            _ats_bundle(
                "ats-1",
                resume_filename="history.pdf",
                finished_at=datetime(2026, 8, 19, 10, 0, tzinfo=UTC),
                resume_id=resume_id,
            ),
        resume_bytes,
    )
    ats_history.archive(
            _ats_bundle(
                "ats-2",
                resume_filename="other.pdf",
                finished_at=datetime(2026, 8, 19, 11, 0, tzinfo=UTC),
                resume_id=(
                    "sha256:" + hashlib.sha256(b"OTHER RESUME").hexdigest()
                ),
            ),
        b"OTHER RESUME",
    )
    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        workflow=SimpleNamespace(load_setup_answers=lambda: None),
        history_store=history,
        ats_history_store=ats_history,
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        page = BeautifulSoup(client.get("/setup").text, "html.parser")
        ats1_time = page.select_one(
            '[data-ats-history-id="ats-1"] time[data-local-datetime]'
        )
        ats2_time = page.select_one(
            '[data-ats-history-id="ats-2"] time[data-local-datetime]'
        )
        assert ats1_time is not None and ats2_time is not None
        assert ats1_time["datetime"] == "2026-08-19T10:00:00+00:00"
        assert ats2_time["datetime"] == "2026-08-19T11:00:00+00:00"

        assert (
            client.delete("/api/scan-history/run-new", headers=HEADERS).status_code
            == 200
        )
        page = BeautifulSoup(client.get("/setup").text, "html.parser")
        ats1_time = page.select_one(
            '[data-ats-history-id="ats-1"] time[data-local-datetime]'
        )
        assert ats1_time is not None
        assert ats1_time["datetime"] == "2026-08-19T10:00:00+00:00"


def test_manual_job_import_with_new_resume_adds_resume_and_associated_job(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    _save_config(paths)
    paths.profile_md.write_text("# Current candidate profile", encoding="utf-8")
    review_config = paths.config_toml.read_bytes()
    history = SearchHistoryStore(paths)
    _archive(
        history,
        tmp_path,
        "run-a",
        _snapshot(),
        config_bytes=review_config,
    )
    paths.config_toml.unlink()
    paths.profile_md.unlink()
    uploaded_bytes = SAMPLE_RESUME.read_bytes()
    resume_id = "sha256:" + hashlib.sha256(uploaded_bytes).hexdigest()
    profile_hash = "sha256:" + "e" * 64
    prepared_inputs: list[tuple[Path, SetupAnswers]] = []

    def prepare_resume(resume_path: Path, answers: SetupAnswers) -> SetupPreparation:
        prepared_inputs.append((resume_path, answers))
        config = load_config(paths.job_tracker_config_toml).model_copy(
            update={
                "candidate_name": answers.candidate_name,
                "resume_path": resume_path,
                "resume_sha256": resume_id,
                "profile_sha256": profile_hash,
            }
        )
        return SetupPreparation(
            config=config,
            profile_bytes=b"# Uploaded resume profile",
            config_bytes=serialize_config(config).encode("utf-8"),
            profile_hash=profile_hash,
        )

    global_jobs = GlobalJobStore(paths)
    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        history_store=history,
        global_job_store=global_jobs,
        manual_job_importer=lambda *_inputs: _job("manual", external_id="manual"),
        manual_resume_preparer=prepare_resume,
        company_size_service=CompanySizeService(
            CompanySizeStore(paths.cache_dir / "company-sizes.json"),
            UnavailableCompanySizeLookup(),
        ),
    )
    tracker_config = paths.job_tracker_config_toml.read_bytes()
    history.delete("run-a")

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        response = client.post(
            "/api/global-jobs/import-with-resume",
            data={"url": "https://careers.example/jobs/manual"},
            files={
                "resume": (
                    "backend.docx",
                    uploaded_bytes,
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
            headers=HEADERS,
        )

    assert response.status_code == 202
    started = response.json()
    assert started["status"] == "running"
    state = _wait_manual_import_completion(client, started["import_id"])
    assert state["result_status"] == "saved"
    assert state["resume_id"] == resume_id
    assert len(prepared_inputs) == 1
    assert prepared_inputs[0][1].candidate_name == "backend"
    assert paths.job_tracker_config_toml.read_bytes() == tracker_config
    assert not paths.config_toml.exists()
    assert not paths.profile_md.exists()
    saved = global_jobs.find("manual")
    assert saved is not None
    assert saved.application_resume_id == resume_id
    assert saved.application_resume_filename == "backend.docx"
    digest = resume_id.removeprefix("sha256:")
    assert (paths.root / "resumes" / f"{digest}.docx").read_bytes() == uploaded_bytes


def test_legacy_job_import_without_resume_is_gone(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    _save_config(paths)
    paths.profile_md.write_text("# Current candidate profile", encoding="utf-8")
    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        manual_job_importer=lambda *_inputs: _job("manual", external_id="manual"),
        company_size_service=CompanySizeService(
            CompanySizeStore(paths.cache_dir / "company-sizes.json"),
            UnavailableCompanySizeLookup(),
        ),
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        response = client.post(
            "/api/global-jobs/import",
            json={
                "url": "https://careers.example/jobs/manual",
                "run_id": "missing-run",
            },
            headers=HEADERS,
        )

    assert response.status_code == 410
    assert response.json() == {
        "detail": "Add one job requires a new resume upload."
    }


def test_manual_job_import_saves_before_company_size_lookup_finishes(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    imported = _job("manual", external_id="manual")
    global_jobs = GlobalJobStore(paths)
    _save_config(paths)
    paths.profile_md.write_text("# Current candidate profile", encoding="utf-8")
    lookup = BlockingCompanySizeLookup()
    company_sizes = CompanySizeService(
        CompanySizeStore(paths.cache_dir / "company-sizes.json"),
        lookup,
    )

    def import_job(
        _url: str,
        _config: AppConfig,
        _profile: str,
        _imported_at: datetime,
        on_progress: Callable[[str, str], None] | None = None,
        on_job_extracted: Callable[[JobRecord], None] | None = None,
    ) -> JobRecord:
        del on_progress
        assert on_job_extracted is not None
        on_job_extracted(imported)
        return imported

    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        global_job_store=global_jobs,
        manual_job_importer=import_job,
        manual_resume_preparer=_prepare_uploaded_resume(paths),
        company_size_service=company_sizes,
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        response = _post_job_with_resume(client)
        assert response.status_code == 202
        started = response.json()
        assert started["status"] == "running"
        try:
            state = _wait_manual_import_completion(client, started["import_id"])
            assert state["status"] == "complete"
            assert lookup.started.wait(timeout=1)
            with FileRWLock(paths.scan_lock_file).exclusive(blocking=False):
                pass
            saved = global_jobs.find("manual")
            assert saved is not None
            assert saved.company_size is None
            attached_resume = saved.application_resume_id
            global_jobs.set_status(saved, UserStatus.APPLIED)
            global_jobs.set_salaries(
                saved,
                expected_salary=SalaryValue(amount="75000", period=SalaryPeriod.YEAR),
                offer_salary=None,
            )
            global_jobs.add_note(saved, "Keep this note")
        finally:
            lookup.release.set()

        for _ in range(100):
            saved = global_jobs.find("manual")
            if saved is not None and saved.company_size is not None:
                break
            time.sleep(0.01)

    saved = global_jobs.find("manual")
    assert saved is not None
    assert saved.company_size is not None
    assert saved.company_size.employee_count == 4200
    assert saved.company_size.source_title == "Acme company facts"
    assert saved.user_status is UserStatus.APPLIED
    assert saved.expected_salary == SalaryValue(
        amount="75000",
        period=SalaryPeriod.YEAR,
    )
    assert [note.content for note in saved.notes] == ["Keep this note"]
    assert saved.application_resume_id == attached_resume


def test_manual_job_import_stays_saved_when_company_size_is_unknown(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    global_jobs = GlobalJobStore(paths)
    _save_config(paths)
    paths.profile_md.write_text("# Current candidate profile", encoding="utf-8")
    company_sizes = CompanySizeService(
        CompanySizeStore(paths.cache_dir / "company-sizes.json"),
        UnavailableCompanySizeLookup(),
    )
    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        global_job_store=global_jobs,
        manual_job_importer=lambda *_inputs: _job("manual", external_id="manual"),
        manual_resume_preparer=_prepare_uploaded_resume(paths),
        company_size_service=company_sizes,
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        response = _post_job_with_resume(client)

    assert response.status_code == 202
    started = response.json()
    assert started["status"] == "running"
    _wait_manual_import_completion(client, started["import_id"])
    saved = global_jobs.find("manual")
    assert saved is not None
    assert saved.user_status is UserStatus.SAVED
    assert saved.company_size is not None
    assert saved.company_size.band.value == "unknown"


def test_global_company_size_search_updates_the_global_job(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    global_jobs = GlobalJobStore(paths)
    global_jobs.upsert_with_default_status(
        _job("manual", external_id="manual"),
        UserStatus.SAVED,
        NOW,
    )
    _save_config(paths)
    company_sizes = CompanySizeService(
        CompanySizeStore(paths.cache_dir / "company-sizes.json"),
        ReliableCompanySizeLookup(),
    )
    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        global_job_store=global_jobs,
        company_size_service=company_sizes,
    )
    assert paths.job_tracker_config_toml.exists()
    paths.config_toml.unlink()

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        response = client.post(
            "/api/global-jobs/manual/company-size",
            headers=HEADERS,
        )

    assert response.status_code == 200
    saved = global_jobs.find("manual")
    assert saved is not None
    assert saved.company_size is not None
    assert saved.company_size.employee_count == 4200
    assert saved.company_size.source_title == "Acme company facts"


def test_global_company_size_search_reports_lookup_failure(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    global_jobs = GlobalJobStore(paths)
    global_jobs.upsert_with_default_status(
        _job("manual", external_id="manual"),
        UserStatus.SAVED,
        NOW,
    )
    _save_config(paths)
    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        global_job_store=global_jobs,
        company_size_service=CompanySizeService(
            CompanySizeStore(paths.cache_dir / "company-sizes.json"),
            UnavailableCompanySizeLookup(),
        ),
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        response = client.post(
            "/api/global-jobs/manual/company-size",
            headers=HEADERS,
        )

    assert response.status_code == 422
    assert response.json() == {
        "detail": "No reliable employee-count source was found."
    }
    saved = global_jobs.find("manual")
    assert saved is not None
    assert saved.user_status is UserStatus.SAVED


def test_delete_global_job_does_not_delete_the_current_search(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    current = _job("manual", external_id="manual")
    repository = _repository(paths, current)
    global_jobs = GlobalJobStore(paths)
    global_jobs.set_status(current, UserStatus.SAVED, NOW)
    before = repository.load()
    app = create_review_app(
        repository,
        TOKEN,
        frozenset({ORIGIN}),
        global_job_store=global_jobs,
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        response = client.delete(
            "/api/global-jobs/manual",
            headers=HEADERS,
        )

    assert response.status_code == 204
    assert global_jobs.find("manual") is None
    assert repository.load() == before


def test_delete_unknown_global_job_returns_404(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        response = client.delete(
            "/api/global-jobs/missing",
            headers=HEADERS,
        )

    assert response.status_code == 404


def test_manual_job_import_runs_while_scan_is_running(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    _save_config(paths)
    paths.profile_md.write_text("# Current candidate profile", encoding="utf-8")
    imported = _job("manual", external_id="manual")
    calls: list[tuple[object, ...]] = []

    def import_job(*inputs: object) -> JobRecord:
        calls.append(inputs)
        return imported

    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        manual_job_importer=import_job,
        manual_resume_preparer=_prepare_uploaded_resume(paths),
    )

    with FileRWLock(paths.scan_lock_file).exclusive(), TestClient(
        app,
        base_url=ORIGIN,
    ) as client:
        _open_session(client)
        response = _post_job_with_resume(client)
        if response.status_code == 202:
            state = _wait_manual_import_completion(
                client,
                response.json()["import_id"],
            )

    assert response.status_code == 202
    assert state["status"] == "complete"
    assert len(calls) == 1


def test_manual_job_import_does_not_claim_the_scan_lock(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    _save_config(paths)
    paths.profile_md.write_text("# Current candidate profile", encoding="utf-8")
    imported = _job("manual", external_id="manual")
    lock_available_during_import: list[bool] = []

    def import_job(*_inputs: object) -> JobRecord:
        try:
            with FileRWLock(paths.scan_lock_file).exclusive(blocking=False):
                lock_available_during_import.append(True)
        except LockUnavailable:
            lock_available_during_import.append(False)
        return imported

    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        manual_job_importer=import_job,
        manual_resume_preparer=_prepare_uploaded_resume(paths),
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        response = _post_job_with_resume(client)
        if response.status_code == 202:
            _wait_manual_import_completion(
                client,
                response.json()["import_id"],
            )

    assert response.status_code == 202
    assert lock_available_during_import == [True]


def test_manual_job_import_rejects_a_second_add_job_request(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    _save_config(paths)
    started = Event()
    release = Event()

    def import_job(*_inputs: object) -> JobRecord:
        started.set()
        if not release.wait(timeout=5):
            raise AssertionError("test did not release manual import")
        return _job("manual", external_id="manual")

    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        manual_job_importer=import_job,
        manual_resume_preparer=_prepare_uploaded_resume(paths),
        company_size_service=CompanySizeService(
            CompanySizeStore(paths.cache_dir / "company-sizes.json"),
            UnavailableCompanySizeLookup(),
        ),
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        first = _post_job_with_resume(client)
        try:
            assert first.status_code == 202
            assert started.wait(timeout=1)
            second_resume = b"different resume payload"
            second = client.post(
                "/api/global-jobs/import-with-resume",
                data={"url": "https://careers.example/jobs/second"},
                files={"resume": ("second.docx", second_resume)},
                headers=HEADERS,
            )
            assert second.status_code == 409
            second_digest = hashlib.sha256(second_resume).hexdigest()
            assert not (paths.root / "resumes" / f"{second_digest}.docx").exists()
        finally:
            release.set()

        _wait_manual_import_completion(client, first.json()["import_id"])


def test_manual_job_import_claims_task_before_preparing_profile(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    _save_config(paths)
    workflow = ManualJobImportWorkflow()
    preparation_started = Event()
    release_preparation = Event()
    prepare_resume = _prepare_uploaded_resume(paths)

    def prepare_current_resume(
        resume_path: Path,
        answers: SetupAnswers,
    ) -> SetupPreparation:
        preparation_started.set()
        if not release_preparation.wait(timeout=5):
            raise AssertionError("test did not release profile preparation")
        return prepare_resume(resume_path, answers)

    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        manual_job_importer=lambda *_inputs: _job("manual", external_id="manual"),
        manual_resume_preparer=prepare_current_resume,
        manual_import_workflow=workflow,
        company_size_service=CompanySizeService(
            CompanySizeStore(paths.cache_dir / "company-sizes.json"),
            UnavailableCompanySizeLookup(),
        ),
    )
    responses: list[Response] = []

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)

        request_thread = Thread(
            target=lambda: responses.append(_post_job_with_resume(client)),
        )
        request_thread.start()
        assert preparation_started.wait(timeout=1)
        request_thread.join(timeout=1)
        try:
            assert not request_thread.is_alive()
            assert len(responses) == 1
            assert responses[0].status_code == 202
            assert workflow.is_busy()
        finally:
            release_preparation.set()
            request_thread.join(timeout=5)

        state = _wait_manual_import_completion(
            client,
            responses[0].json()["import_id"],
        )

    assert state["status"] == "complete"


def test_background_tasks_endpoint_lists_every_active_task_type(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    release = Event()
    manual_workflow = ManualJobImportWorkflow()
    add_started = Event()
    reevaluation_started = Event()

    def block(
        job_key: str,
        started: Event,
    ) -> Callable[[object], ManualImportResult]:
        def run(_progress: object) -> ManualImportResult:
            started.set()
            if not release.wait(timeout=5):
                raise AssertionError(f"test did not release {job_key}")
            return ManualImportResult(job_key, UserStatus.SAVED)

        return run

    add_job = manual_workflow.start(
        block("added", add_started),
        task_kind="add-job",
        task_label="https://jobs.example/added",
        task_key="add-job",
    )
    reevaluation = manual_workflow.start(
        block("tracked", reevaluation_started),
        task_kind="re-evaluate",
        task_label="Backend Engineer at Acme",
        task_key="re-evaluate:tracked",
        subject_key="tracked",
    )
    scan_workflow = SimpleNamespace(
        load_setup_answers=lambda: None,
        read_current_run=lambda: SimpleNamespace(
            run_id="scan-1",
            status="running",
            message="Reviewing complete job descriptions...",
            progress_percent=60,
        ),
        is_busy=lambda: True,
    )
    ats_workflow = SimpleNamespace(
        read_current_run=lambda: SimpleNamespace(
            run_id="ats-1",
            status="running",
            message="Checking selected jobs...",
            progress_percent=25,
            tasks=[
                SimpleNamespace(kind="resume"),
                SimpleNamespace(kind="job"),
                SimpleNamespace(kind="job"),
            ],
        ),
        is_busy=lambda: True,
    )
    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        workflow=scan_workflow,
        manual_import_workflow=manual_workflow,
        ats_workflow=ats_workflow,
    )

    try:
        assert add_started.wait(timeout=1)
        assert reevaluation_started.wait(timeout=1)
        with TestClient(app, base_url=ORIGIN) as client:
            _open_session(client)
            response = client.get("/api/background-tasks")
    finally:
        release.set()

    assert response.status_code == 200
    assert response.json() == {
        "tasks": [
            {
                "task_id": "scan:scan-1",
                "kind": "scan",
                "label": "Save and run scan",
                "status": "running",
                "message": "Reviewing complete job descriptions...",
                "progress_percent": 60.0,
                "subject_key": None,
            },
            {
                "task_id": f"manual:{add_job.import_id}",
                "kind": "add-job",
                "label": "https://jobs.example/added",
                "status": "running",
                "message": "Preparing manual import...",
                "progress_percent": 5.0,
                "subject_key": None,
            },
            {
                "task_id": f"manual:{reevaluation.import_id}",
                "kind": "re-evaluate",
                "label": "Backend Engineer at Acme",
                "status": "running",
                "message": "Preparing manual import...",
                "progress_percent": 5.0,
                "subject_key": "tracked",
            },
            {
                "task_id": "ats:ats-1",
                "kind": "ats-run",
                "label": "ATS Run · 2 jobs",
                "status": "running",
                "message": "Checking selected jobs...",
                "progress_percent": 25.0,
                "subject_key": None,
            },
        ]
    }


def test_reimported_manual_job_preserves_existing_global_status(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    _save_config(paths)
    paths.profile_md.write_text("# Current candidate profile", encoding="utf-8")
    global_jobs = GlobalJobStore(paths)
    existing = _job("manual", external_id="manual")
    global_jobs.set_status(existing, UserStatus.APPLIED, NOW)
    refreshed = _job("manual", external_id="manual")

    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        global_job_store=global_jobs,
        manual_job_importer=lambda *_inputs: refreshed,
        manual_resume_preparer=_prepare_uploaded_resume(paths),
        company_size_service=CompanySizeService(
            CompanySizeStore(paths.cache_dir / "company-sizes.json"),
            UnavailableCompanySizeLookup(),
        ),
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        response = _post_job_with_resume(client)
        started = response.json()
        state = _wait_manual_import_completion(client, started["import_id"])

    assert response.status_code == 202
    assert started["status"] == "running"
    assert state["status"] == "complete"
    assert state["result_status"] == "applied"
    
    saved = global_jobs.find("manual")
    assert saved is not None
    assert saved.user_status is UserStatus.APPLIED


def test_manual_job_reevaluation_uses_attached_resume_and_updates_same_job(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    _save_config(paths)
    global_jobs = GlobalJobStore(paths)
    tracked = _job("tracked", external_id="original")
    company_size = ReliableCompanySizeLookup().lookup(
        tracked.company,
        load_config(paths.config_toml),
        NOW,
    )
    tracked.company_size = company_size
    global_jobs.set_status(tracked, UserStatus.APPLIED, NOW)
    resume_bytes = SAMPLE_RESUME.read_bytes()
    resume_id = _attach_resume(
        paths,
        global_jobs,
        "tracked",
        "updated.docx",
        resume_bytes,
    )
    saved = global_jobs.find("tracked")
    assert saved is not None
    global_jobs.set_salaries(
        saved,
        expected_salary=SalaryValue(amount="75000", period=SalaryPeriod.YEAR),
        offer_salary=None,
    )
    global_jobs.add_note(saved, "Keep this note", NOW)
    prepared_inputs: list[tuple[Path, SetupAnswers]] = []
    import_inputs: list[tuple[str, str]] = []
    prepare_resume = _prepare_uploaded_resume(paths)

    def prepare_current_resume(
        resume_path: Path,
        answers: SetupAnswers,
    ) -> SetupPreparation:
        prepared_inputs.append((resume_path, answers))
        return prepare_resume(resume_path, answers)

    def reevaluate_job(
        source_url: str,
        _config: AppConfig,
        profile: str,
        imported_at: datetime,
        **_callbacks: object,
    ) -> JobRecord:
        import_inputs.append((source_url, profile))
        refreshed = _job("unrelated-key", external_id="unrelated-source")
        refreshed.url = HttpUrl("https://acme.example/jobs/original")
        refreshed.source_occurrences[0].url = refreshed.url
        refreshed.source_occurrences[0].description = "Fresh page description."
        refreshed.source_occurrences[0].content_hash = "sha256:fresh-page"
        return refreshed.model_copy(
            update={
                "title": "Re-evaluated Backend Engineer",
                "description": "Fresh page description.",
                "content_hash": "sha256:fresh-page",
                "score": 97,
                "reason": "Updated resume is a stronger match.",
                "first_seen": imported_at,
                "last_seen": imported_at,
            },
            deep=True,
        )

    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        global_job_store=global_jobs,
        manual_job_importer=reevaluate_job,
        manual_resume_preparer=prepare_current_resume,
        company_size_service=CompanySizeService(
            CompanySizeStore(paths.cache_dir / "company-sizes.json"),
            UnavailableCompanySizeLookup(),
        ),
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        response = client.post(
            "/api/global-jobs/tracked/re-evaluate",
            headers=HEADERS,
        )
        assert response.status_code == 202
        state = _wait_manual_import_completion(
            client,
            response.json()["import_id"],
        )

    assert state["status"] == "complete"
    assert state["job_key"] == "tracked"
    assert state["result_status"] == "applied"
    assert state["resume_id"] == resume_id
    assert len(prepared_inputs) == 1
    assert prepared_inputs[0][0].read_bytes() == resume_bytes
    assert prepared_inputs[0][1].candidate_name == "updated"
    assert import_inputs == [
        (
            "https://acme.example/jobs/original",
            "# Uploaded resume profile",
        )
    ]
    assert len(global_jobs.load().jobs) == 1
    reevaluated = global_jobs.find("tracked")
    assert reevaluated is not None
    assert reevaluated.title == "Re-evaluated Backend Engineer"
    assert reevaluated.score == 97
    assert reevaluated.reason == "Updated resume is a stronger match."
    assert reevaluated.user_status is UserStatus.APPLIED
    assert [entry.status for entry in reevaluated.user_status_history] == [
        UserStatus.SAVED,
        UserStatus.APPLIED,
    ]
    assert reevaluated.application_resume_id == resume_id
    assert reevaluated.application_resume_filename == "updated.docx"
    assert reevaluated.last_evaluated_resume_id == resume_id
    assert reevaluated.reevaluation_notice is not None
    assert reevaluated.reevaluation_notice.status == "succeeded"
    assert reevaluated.expected_salary == SalaryValue(
        amount="75000",
        period=SalaryPeriod.YEAR,
    )
    assert [note.content for note in reevaluated.notes] == ["Keep this note"]
    assert reevaluated.company_size == company_size
    assert [
        occurrence.source_job_key for occurrence in reevaluated.source_occurrences
    ] == ["linkedin:acme/jobs:original"]
    assert reevaluated.source_occurrences[0].description == "Fresh page description."
    assert reevaluated.source_occurrences[0].content_hash == "sha256:fresh-page"


def test_add_job_cannot_overwrite_a_concurrent_reevaluation_of_the_same_job(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    _save_config(paths)
    global_jobs = GlobalJobStore(paths)
    tracked = _job("tracked", external_id="original")
    global_jobs.set_status(tracked, UserStatus.SAVED, NOW)
    old_resume_id = _attach_resume(
        paths,
        global_jobs,
        "tracked",
        "old.pdf",
        b"old resume",
    )
    add_extracted = Event()
    release_add = Event()

    def import_same_job(
        _url: str,
        config: AppConfig,
        _profile: str,
        imported_at: datetime,
        on_job_extracted: Callable[[JobRecord], None] | None = None,
        **_callbacks: object,
    ) -> JobRecord:
        refreshed = _job("tracked", external_id="original")
        if on_job_extracted is not None:
            on_job_extracted(refreshed)
        if config.resume_sha256 != old_resume_id:
            add_extracted.set()
            if not release_add.wait(timeout=5):
                raise AssertionError("test did not release Add job")
            score = 72
        else:
            score = 97
        return refreshed.model_copy(
            update={
                "score": score,
                "reason": f"Evaluation score {score}",
                "last_review_attempt_at": imported_at,
                "last_successful_review_content_hash": refreshed.content_hash,
                "last_successful_review_profile_hash": config.profile_sha256,
            },
            deep=True,
        )

    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        global_job_store=global_jobs,
        manual_job_importer=import_same_job,
        manual_resume_preparer=_prepare_uploaded_resume(paths),
        company_size_service=CompanySizeService(
            CompanySizeStore(paths.cache_dir / "company-sizes.json"),
            UnavailableCompanySizeLookup(),
        ),
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        add_response = _post_job_with_resume(client)
        assert add_response.status_code == 202
        assert add_extracted.wait(timeout=1)
        reevaluation_response = client.post(
            "/api/global-jobs/tracked/re-evaluate",
            headers=HEADERS,
        )
        assert reevaluation_response.status_code == 202
        reevaluation_state = _wait_manual_import_completion(
            client,
            reevaluation_response.json()["import_id"],
        )
        release_add.set()
        add_state = _wait_manual_import_completion(
            client,
            add_response.json()["import_id"],
        )

    saved = global_jobs.find("tracked")
    assert reevaluation_state["status"] == "complete"
    assert add_state["status"] == "failed"
    assert "changed while this task was running" in str(add_state["error"])
    assert saved is not None
    assert saved.score == 97
    assert saved.application_resume_id == old_resume_id


def test_manual_job_reevaluation_runs_while_scan_is_running(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    _save_config(paths)
    global_jobs = GlobalJobStore(paths)
    global_jobs.set_status(
        _job("tracked", external_id="original"),
        UserStatus.SAVED,
        NOW,
    )
    _attach_resume(
        paths,
        global_jobs,
        "tracked",
        "updated.docx",
        SAMPLE_RESUME.read_bytes(),
    )
    calls: list[str] = []

    def reevaluate_job(
        source_url: str,
        _config: AppConfig,
        _profile: str,
        imported_at: datetime,
        **_callbacks: object,
    ) -> JobRecord:
        calls.append(source_url)
        return _job("refreshed", external_id="original").model_copy(
            update={"first_seen": imported_at, "last_seen": imported_at},
            deep=True,
        )

    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        global_job_store=global_jobs,
        manual_job_importer=reevaluate_job,
        manual_resume_preparer=_prepare_uploaded_resume(paths),
    )

    with FileRWLock(paths.scan_lock_file).exclusive(), TestClient(
        app,
        base_url=ORIGIN,
    ) as client:
        _open_session(client)
        response = client.post(
            "/api/global-jobs/tracked/re-evaluate",
            headers=HEADERS,
        )
        if response.status_code == 202:
            state = _wait_manual_import_completion(
                client,
                response.json()["import_id"],
            )

    assert response.status_code == 202
    assert state["status"] == "complete"
    assert calls == ["https://acme.example/jobs/original"]


def test_manual_job_reevaluation_requires_confirmation_for_unchanged_resume(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    _save_config(paths)
    global_jobs = GlobalJobStore(paths)
    tracked = _job("tracked", external_id="original")
    global_jobs.set_status(tracked, UserStatus.SAVED, NOW)
    resume_id = _attach_resume(
        paths,
        global_jobs,
        "tracked",
        "current.docx",
        SAMPLE_RESUME.read_bytes(),
    )
    tracked = global_jobs.find("tracked")
    assert tracked is not None
    global_jobs.set_status(
        tracked,
        UserStatus.SAVED,
        NOW,
        resume_id=resume_id,
        profile_hash="sha256:" + "a" * 64,
    )
    import_inputs: list[str] = []

    def import_job(
        source_url: str,
        _config: AppConfig,
        _profile: str,
        _imported_at: datetime,
        **_callbacks: object,
    ) -> JobRecord:
        import_inputs.append(source_url)
        return _job("replacement", external_id="original")

    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        global_job_store=global_jobs,
        manual_job_importer=import_job,
        manual_resume_preparer=_prepare_uploaded_resume(paths),
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        unforced_response = client.post(
            "/api/global-jobs/tracked/re-evaluate",
            headers=HEADERS,
        )
        forced_response = client.post(
            "/api/global-jobs/tracked/re-evaluate?force=1",
            headers=HEADERS,
        )
        if forced_response.status_code == 202:
            forced_state = _wait_manual_import_completion(
                client,
                forced_response.json()["import_id"],
            )

    assert unforced_response.status_code == 409
    assert unforced_response.json()["detail"] == (
        "The resume has not changed since the last evaluation."
    )
    assert unforced_response.headers["X-Job-Scan-Conflict"] == "resume-unchanged"
    assert forced_response.status_code == 202
    assert forced_state["status"] == "complete"
    assert import_inputs == ["https://acme.example/jobs/original"]


def test_failed_reevaluation_keeps_the_previous_result_and_resume_hash(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    _save_config(paths)
    global_jobs = GlobalJobStore(paths)
    tracked = _job("tracked", external_id="original")
    tracked.score = 72
    tracked.reason = "Previous evaluation"
    global_jobs.set_status(tracked, UserStatus.SAVED, NOW)
    previous_resume_id = _attach_resume(
        paths,
        global_jobs,
        "tracked",
        "previous.pdf",
        b"PREVIOUS RESUME",
    )
    tracked = global_jobs.find("tracked")
    assert tracked is not None
    global_jobs.set_status(
        tracked,
        UserStatus.SAVED,
        NOW,
        resume_id=previous_resume_id,
        profile_hash="sha256:" + "a" * 64,
    )
    current_resume_id = _attach_resume(
        paths,
        global_jobs,
        "tracked",
        "current.docx",
        SAMPLE_RESUME.read_bytes(),
    )

    def fail_import(
        _source_url: str,
        _config: AppConfig,
        _profile: str,
        imported_at: datetime,
        **_callbacks: object,
    ) -> JobRecord:
        return _job("failed", external_id="original").model_copy(
            update={
                "first_seen": imported_at,
                "last_seen": imported_at,
                "machine_status": MachineStatus.PENDING,
                "score": None,
                "reason": "",
                "last_error": "AI review failed",
            },
            deep=True,
        )

    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        global_job_store=global_jobs,
        manual_job_importer=fail_import,
        manual_resume_preparer=_prepare_uploaded_resume(paths),
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        response = client.post(
            "/api/global-jobs/tracked/re-evaluate",
            headers=HEADERS,
        )
        state = _wait_manual_import_completion(
            client,
            response.json()["import_id"],
        )

    saved = global_jobs.find("tracked")
    assert response.status_code == 202
    assert state["status"] == "failed"
    assert saved is not None
    assert saved.application_resume_id == current_resume_id
    assert saved.last_evaluated_resume_id == previous_resume_id
    assert saved.score == 72
    assert saved.reason == "Previous evaluation"
    assert saved.reevaluation_notice is not None
    assert saved.reevaluation_notice.status == "failed"


def test_acknowledge_reevaluation_result_clears_the_persisted_notice(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    global_jobs = GlobalJobStore(paths)
    global_jobs.set_status(
        _job("tracked", external_id="original"),
        UserStatus.SAVED,
        NOW,
    )
    global_jobs.record_reevaluation_result(
        "tracked",
        "failed",
        finished_at=NOW,
    )
    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        global_job_store=global_jobs,
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        response = client.post(
            "/api/global-jobs/tracked/re-evaluation-result/acknowledge",
            headers=HEADERS,
            json={"finished_at": NOW.isoformat()},
        )

    saved = GlobalJobStore(paths).find("tracked")
    assert response.status_code == 204
    assert saved is not None
    assert saved.reevaluation_notice is None


def test_acknowledge_reevaluation_result_rejects_a_stale_result(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    global_jobs = GlobalJobStore(paths)
    global_jobs.set_status(
        _job("tracked", external_id="original"),
        UserStatus.SAVED,
        NOW,
    )
    global_jobs.record_reevaluation_result(
        "tracked",
        "succeeded",
        finished_at=NOW,
    )
    newer_finished_at = NOW + timedelta(minutes=1)
    global_jobs.record_reevaluation_result(
        "tracked",
        "failed",
        finished_at=newer_finished_at,
    )
    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        global_job_store=global_jobs,
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        response = client.post(
            "/api/global-jobs/tracked/re-evaluation-result/acknowledge",
            headers=HEADERS,
            json={"finished_at": NOW.isoformat()},
        )

    saved = GlobalJobStore(paths).find("tracked")
    assert response.status_code == 409
    assert response.headers["X-Job-Scan-Conflict"] == "re-evaluation-result-changed"
    assert saved is not None
    assert saved.reevaluation_notice is not None
    assert saved.reevaluation_notice.status == "failed"
    assert saved.reevaluation_notice.finished_at == newer_finished_at


def test_manual_job_reevaluation_claims_workflow_before_preparing_profile(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    _save_config(paths)
    global_jobs = GlobalJobStore(paths)
    global_jobs.set_status(
        _job("tracked", external_id="original"),
        UserStatus.SAVED,
        NOW,
    )
    _attach_resume(
        paths,
        global_jobs,
        "tracked",
        "updated.docx",
        SAMPLE_RESUME.read_bytes(),
    )
    preparation_started = Event()
    release_preparation = Event()
    prepare_resume = _prepare_uploaded_resume(paths)
    prepare_calls: list[Path] = []

    def prepare_current_resume(
        resume_path: Path,
        answers: SetupAnswers,
    ) -> SetupPreparation:
        prepare_calls.append(resume_path)
        preparation_started.set()
        if not release_preparation.wait(timeout=5):
            raise AssertionError("test did not release profile preparation")
        return prepare_resume(resume_path, answers)

    def reevaluate_job(
        _source_url: str,
        _config: AppConfig,
        _profile: str,
        imported_at: datetime,
        **_callbacks: object,
    ) -> JobRecord:
        return _job("refreshed", external_id="refreshed").model_copy(
            update={"first_seen": imported_at, "last_seen": imported_at},
            deep=True,
        )

    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        global_job_store=global_jobs,
        manual_job_importer=reevaluate_job,
        manual_resume_preparer=prepare_current_resume,
        company_size_service=CompanySizeService(
            CompanySizeStore(paths.cache_dir / "company-sizes.json"),
            UnavailableCompanySizeLookup(),
        ),
    )
    first_responses: list[Response] = []
    first_errors: list[BaseException] = []

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)

        def post_first() -> None:
            try:
                first_responses.append(
                    client.post(
                        "/api/global-jobs/tracked/re-evaluate",
                        headers=HEADERS,
                    )
                )
            except BaseException as error:  # noqa: BLE001 - surface request-thread failure
                first_errors.append(error)

        request_thread = Thread(target=post_first)
        request_thread.start()
        assert preparation_started.wait(timeout=1)
        request_thread.join(timeout=1)
        try:
            assert not request_thread.is_alive()
            assert first_errors == []
            assert len(first_responses) == 1
            assert first_responses[0].status_code == 202
            second = client.post(
                "/api/global-jobs/tracked/re-evaluate",
                headers=HEADERS,
            )
            assert second.status_code == 409
            assert len(prepare_calls) == 1
        finally:
            release_preparation.set()
            request_thread.join(timeout=5)

        if first_responses:
            state = _wait_manual_import_completion(
                client,
                first_responses[0].json()["import_id"],
            )
            assert state["status"] == "complete"
