from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from pydantic import HttpUrl

from job_scan.ai_config import AiProviderDraft, AiProviderStore
from job_scan.ai_selection import (
    AiRuntimeSelection,
    AiSelectionStore,
    ClaudeRuntimeSelection,
)
from job_scan.ats_history import AtsHistoryStore
from job_scan.ats_models import AtsRunState
from job_scan.ats_workflow import AtsWorkflowInput
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
    Snapshot,
    SourceKind,
    SourceOccurrence,
    StoreMeta,
    UserStatus,
)
from job_scan.global_jobs import GlobalJobStore
from job_scan.locking import FileRWLock, LockUnavailable
from job_scan.paths import AppPaths
from job_scan.repository import JsonlRepository
from job_scan.resume_catalog import ResumeCatalogStore
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
    save_config(
        paths.config_toml,
        AppConfig(
            candidate_name="Ada",
            ai_runtime="api:deepseek",
            ai_model="current-model",
            resume_path=paths.root / "default.pdf",
            resume_sha256="sha256:" + "a" * 64,
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
    config_bytes: bytes = b"config",
) -> None:
    resume = tmp_path / f"{run_id}.pdf"
    resume.write_bytes(resume_bytes)
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


class RecordingAtsWorkflow:
    def __init__(self) -> None:
        self.inputs: list[AtsWorkflowInput] = []

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


def _open_session(client: TestClient) -> None:
    assert client.get("/").status_code == 200


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


def test_global_status_records_the_selected_application_resume(
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
            json={
                "status": "applied",
                "resume_id": config.resume_sha256,
            },
            headers=HEADERS,
        )

    saved = global_jobs.find("tracked")
    assert response.status_code == 204
    assert saved is not None
    assert saved.application_resume_id == config.resume_sha256


def test_global_status_rejects_an_unknown_application_resume(
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
            json={
                "status": "applied",
                "resume_id": "sha256:" + "f" * 64,
            },
            headers=HEADERS,
        )

    saved = global_jobs.find("tracked")
    assert response.status_code == 404
    assert saved is not None
    assert saved.user_status is UserStatus.SAVED
    assert saved.application_resume_id is None


def test_application_resume_can_be_corrected_through_the_global_api(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    _save_config(paths)
    paths.profile_md.write_text("# Current profile", encoding="utf-8")
    config = load_config(paths.config_toml)
    resume_b = "sha256:" + "c" * 64
    ResumeCatalogStore(paths).register(
        resume_id=resume_b,
        profile_hash="sha256:" + "d" * 64,
        candidate_name="Platform CV",
        filename="platform.pdf",
        profile_bytes=b"# Platform profile",
        config_bytes=serialize_config(config).encode("utf-8"),
        resume_bytes=b"PLATFORM RESUME",
        created_at=NOW,
    )
    global_jobs = GlobalJobStore(paths)
    job = _job("tracked", external_id="tracked")
    global_jobs.set_status(
        job,
        UserStatus.APPLIED,
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
            "/api/global-jobs/tracked/application-resume",
            json={"resume_id": resume_b},
            headers=HEADERS,
        )

    saved = global_jobs.find("tracked")
    assert response.status_code == 204
    assert saved is not None
    assert saved.application_resume_id == resume_b
    assert saved.user_status is UserStatus.APPLIED
    assert [entry.status for entry in saved.user_status_history] == [
        UserStatus.SAVED,
        UserStatus.APPLIED,
    ]


def test_history_status_appears_in_global_block_of_another_history(tmp_path: Path) -> None:
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
    assert page.select_one('[data-review-block="current"] [data-job-key="b"]') is None
    assert history.load("run-a").jobs[0].user_status is UserStatus.NEW
    assert history.load("run-b").jobs[0].user_status is UserStatus.NEW


def test_same_global_job_shows_each_history_resumes_own_match(tmp_path: Path) -> None:
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
            client.get(f"/setup?resume_id={resume_a_id}#review").text,
            "html.parser",
        )
        page_b = BeautifulSoup(
            client.get(f"/setup?resume_id={resume_b_id}#review").text,
            "html.parser",
        )

    card_a = page_a.select_one('[data-review-block="global"] [data-job-key]')
    card_b = page_b.select_one('[data-review-block="global"] [data-job-key]')
    assert len(page_a.select("[data-global-resume-id]")) == 3
    assert len(global_jobs.load().jobs) == 1
    assert card_a is not None
    assert card_a.get("data-score") == "91"
    assert "Strong Java match" in card_a.get_text(" ", strip=True)
    assert card_b is not None
    assert card_b.get("data-score") == "63"
    assert "Missing Kotlin experience" in card_b.get_text(" ", strip=True)
    assert "applied" in card_a.get_text(" ", strip=True).lower()


def test_completed_history_appears_in_resume_list_without_server_restart(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    _save_config(paths)
    paths.profile_md.write_text("# Current profile", encoding="utf-8")
    history = SearchHistoryStore(paths)
    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        workflow=SimpleNamespace(load_setup_answers=lambda: None),
        history_store=history,
    )
    resume_bytes = b"LATER RESUME"
    resume_id = "sha256:" + hashlib.sha256(resume_bytes).hexdigest()
    history_config = load_config(paths.config_toml).model_copy(
        update={
            "candidate_name": "Later History",
            "resume_sha256": resume_id,
            "profile_sha256": "sha256:" + "c" * 64,
        }
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        _archive(
            history,
            tmp_path,
            "run-later",
            _snapshot(),
            resume_bytes=resume_bytes,
            profile_bytes=b"# Later profile",
            config_bytes=serialize_config(history_config).encode("utf-8"),
        )
        page = BeautifulSoup(
            client.get("/setup?run_id=run-later#review").text,
            "html.parser",
        )

    selected = page.select_one(
        f'option[data-global-resume-id="{resume_id}"][selected]'
    )
    assert selected is not None
    assert "run-later.pdf" in selected.get_text(" ", strip=True)


def test_same_resume_keeps_old_and_new_profile_hash_migrations(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    _save_config(paths)
    paths.profile_md.write_text("# Current profile", encoding="utf-8")
    current_config = load_config(paths.config_toml)
    old_profile_job = _job("old-profile", external_id="old-profile")
    old_profile_job.last_successful_review_profile_hash = current_config.profile_sha256
    new_profile_hash = "sha256:" + "c" * 64
    new_profile_job = _job("new-profile", external_id="new-profile")
    new_profile_job.last_successful_review_profile_hash = new_profile_hash
    global_jobs = GlobalJobStore(paths)
    global_jobs.set_status(old_profile_job, UserStatus.SAVED, NOW)
    global_jobs.set_status(new_profile_job, UserStatus.APPLIED, NOW)
    history = SearchHistoryStore(paths)
    history_config = current_config.model_copy(
        update={"profile_sha256": new_profile_hash}
    )
    _archive(
        history,
        tmp_path,
        "run-new-profile",
        _snapshot(new_profile_job),
        profile_bytes=b"# New profile",
        config_bytes=serialize_config(history_config).encode("utf-8"),
    )
    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        history_store=history,
        global_job_store=global_jobs,
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)

    associated = global_jobs.load_for_resume(current_config.resume_sha256)
    assert {job.canonical_job_key for job in associated.jobs} == {
        "old-profile",
        "new-profile",
    }


def test_ats_uses_uploaded_resume_with_current_and_global_jobs(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    current = _job("current", external_id="current")
    global_job = _job("global", external_id="global")
    repository = _repository(paths, current)
    _save_config(paths)
    global_jobs = GlobalJobStore(paths)
    global_jobs.set_status(global_job, UserStatus.SAVED, NOW)
    ats_workflow = RecordingAtsWorkflow()
    app = create_review_app(
        repository,
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
            data={"search_run_id": "", "job_keys": json.dumps(["current", "global"])},
            files={"resume": ("Other CV.pdf", b"CUSTOM RESUME", "application/pdf")},
            headers=HEADERS,
        )

    assert response.status_code == 202
    inputs = ats_workflow.inputs[0]
    assert inputs.search_run_id == "global"
    assert inputs.resume_filename == "Other CV.pdf"
    assert inputs.resume_bytes == b"CUSTOM RESUME"
    assert [job.canonical_job_key for job in inputs.jobs] == ["current", "global"]
    assert inputs.config.selected_model == "sonnet"


def test_ats_defaults_to_selected_history_resume_when_no_upload_is_given(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    global_job = _job("global", external_id="global")
    repository = _repository(paths)
    _save_config(paths)
    history = SearchHistoryStore(paths)
    _archive(history, tmp_path, "run-a", _snapshot(global_job), b"DEFAULT RESUME")
    global_jobs = GlobalJobStore(paths)
    global_jobs.set_status(global_job, UserStatus.SAVED, NOW)
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
    assert inputs.search_run_id == "run-a"
    assert inputs.resume_filename == "run-a.pdf"
    assert inputs.resume_bytes == b"DEFAULT RESUME"


def test_ats_keeps_history_context_but_uses_the_global_ai_config(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    global_job = _job("global", external_id="global")
    repository = _repository(paths)
    _save_config(paths)
    history_config = AppConfig(
        candidate_name="History Candidate",
        ai_runtime="claude-code",
        resume_path=paths.root / "history.pdf",
        resume_sha256="sha256:" + "c" * 64,
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
        config_bytes=serialize_config(history_config).encode("utf-8"),
    )
    global_jobs = GlobalJobStore(paths)
    global_jobs.set_status(global_job, UserStatus.SAVED, NOW)
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
                "search_run_id": "run-a",
                "ai_choice": "history",
                "job_keys": json.dumps(["global"]),
            },
            headers=HEADERS,
        )

    assert response.status_code == 202
    inputs = ats_workflow.inputs[0]
    assert inputs.config.ai_runtime == "claude-code"
    assert inputs.config.selected_model == "sonnet"
    assert inputs.candidate_name == "History Candidate"


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
            files={"resume": ("Current.pdf", b"RESUME", "application/pdf")},
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
    global_jobs.set_status(global_job, UserStatus.SAVED, NOW)
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
                "search_run_id": "run-a",
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
    assert inputs.candidate_name == "History Candidate"


def test_manual_job_import_rejects_non_public_url(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        response = client.post(
            "/api/global-jobs/import",
            json={"url": "http://127.0.0.1:8765/setup"},
            headers=HEADERS,
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
        company_size_service=CompanySizeService(
            CompanySizeStore(paths.cache_dir / "company-sizes.json"),
            UnavailableCompanySizeLookup(),
        ),
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        response = client.post(
            "/api/global-jobs/import",
            json={"url": "https://careers.example/jobs/manual"},
            headers=HEADERS,
        )

    assert response.status_code == 202
    started = response.json()
    assert started["status"] == "running"
    assert isinstance(started["import_id"], str)
    assert started["progress_percent"] >= 0
    assert started["resume_id"] == "sha256:" + "a" * 64
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
    assert profile == "# Current candidate profile"
    assert isinstance(imported_at, datetime)
    assert imported_at.tzinfo is not None
    saved = global_jobs.find("manual")
    assert saved is not None
    assert saved.user_status is UserStatus.SAVED


def test_manual_job_import_uses_selected_resume_profile_and_config(
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
                "resume_id": history_config.resume_sha256,
            },
            headers=HEADERS,
        )

    assert response.status_code == 202
    started = response.json()
    assert started["status"] == "running"
    _wait_manual_import_completion(client, started["import_id"])
    assert len(import_inputs) == 1
    _url, config, profile, _imported_at = import_inputs[0]
    assert isinstance(config, AppConfig)
    assert config.candidate_name == "History Candidate"
    assert config.resume_sha256 == "sha256:" + "c" * 64
    assert config.ai_runtime == "claude-code"
    assert config.claude.model == "opus"
    assert profile == "# History candidate profile"


def test_manual_job_import_with_new_resume_adds_resume_and_associated_job(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    _save_config(paths)
    paths.profile_md.write_text("# Current candidate profile", encoding="utf-8")
    old_config = paths.config_toml.read_bytes()
    old_profile = paths.profile_md.read_bytes()
    uploaded_bytes = SAMPLE_RESUME.read_bytes()
    resume_id = "sha256:" + hashlib.sha256(uploaded_bytes).hexdigest()
    profile_hash = "sha256:" + "e" * 64
    prepared_inputs: list[tuple[Path, SetupAnswers]] = []

    def prepare_resume(resume_path: Path, answers: SetupAnswers) -> SetupPreparation:
        prepared_inputs.append((resume_path, answers))
        config = load_config(paths.config_toml).model_copy(
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
        global_job_store=global_jobs,
        manual_job_importer=lambda *_inputs: _job("manual", external_id="manual"),
        manual_resume_preparer=prepare_resume,
        company_size_service=CompanySizeService(
            CompanySizeStore(paths.cache_dir / "company-sizes.json"),
            UnavailableCompanySizeLookup(),
        ),
    )

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
    assert paths.config_toml.read_bytes() == old_config
    assert paths.profile_md.read_bytes() == old_profile
    assert ResumeCatalogStore(paths).read(resume_id).profile_bytes == (
        b"# Uploaded resume profile"
    )
    associated = global_jobs.load_for_resume(resume_id)
    assert [job.canonical_job_key for job in associated.jobs] == ["manual"]


def test_manual_job_import_rejects_missing_selected_history(tmp_path: Path) -> None:
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

    assert response.status_code == 404
    assert response.json() == {
        "detail": "The selected search history is unavailable."
    }


def test_manual_job_import_persists_company_size_before_saving(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    imported = _job("manual", external_id="manual")
    global_jobs = GlobalJobStore(paths)
    _save_config(paths)
    paths.profile_md.write_text("# Current candidate profile", encoding="utf-8")
    company_sizes = CompanySizeService(
        CompanySizeStore(paths.cache_dir / "company-sizes.json"),
        ReliableCompanySizeLookup(),
    )
    app = create_review_app(
        _repository(paths),
        TOKEN,
        frozenset({ORIGIN}),
        global_job_store=global_jobs,
        manual_job_importer=lambda *_inputs: imported,
        company_size_service=company_sizes,
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        response = client.post(
            "/api/global-jobs/import",
            json={"url": "https://careers.example/jobs/manual"},
            headers=HEADERS,
        )

    assert response.status_code == 202
    started = response.json()
    assert started["status"] == "running"
    _wait_manual_import_completion(client, started["import_id"])
    saved = global_jobs.find("manual")
    assert saved is not None
    assert saved.company_size is not None
    assert saved.company_size.employee_count == 4200
    assert saved.company_size.source_title == "Acme company facts"


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
        company_size_service=company_sizes,
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        response = client.post(
            "/api/global-jobs/import",
            json={"url": "https://careers.example/jobs/manual"},
            headers=HEADERS,
        )

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


def test_manual_job_import_rejects_while_scan_is_running(tmp_path: Path) -> None:
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
    )

    with FileRWLock(paths.scan_lock_file).exclusive(), TestClient(
        app,
        base_url=ORIGIN,
    ) as client:
        _open_session(client)
        response = client.post(
            "/api/global-jobs/import",
            json={"url": "https://careers.example/jobs/manual"},
            headers=HEADERS,
        )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "A scan is running; retry the job import after it completes."
    }
    assert calls == []


def test_manual_job_import_holds_scan_lock_until_import_finishes(tmp_path: Path) -> None:
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
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        response = client.post(
            "/api/global-jobs/import",
            json={"url": "https://careers.example/jobs/manual"},
            headers=HEADERS,
        )

    assert response.status_code == 202
    assert lock_available_during_import == [False]


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
    )

    with TestClient(app, base_url=ORIGIN) as client:
        _open_session(client)
        response = client.post(
            "/api/global-jobs/import",
            json={"url": "https://careers.example/jobs/manual"},
            headers=HEADERS,
        )

    assert response.status_code == 202
    started = response.json()
    assert started["status"] == "running"
    state = _wait_manual_import_completion(client, started["import_id"])
    assert state["status"] == "complete"
    assert state["result_status"] == "applied"
    
    saved = global_jobs.find("manual")
    assert saved is not None
    assert saved.user_status is UserStatus.APPLIED
