from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from threading import Event

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
from job_scan.anthropic_api import AiModelOption, OutboundAiUrlError
from job_scan.ats_history import AtsHistoryStore
from job_scan.ats_models import (
    AtsCheckBundle,
    AtsResumeAssessment,
    AtsResumeFinding,
    AtsRunState,
)
from job_scan.ats_workflow import (
    AtsInputError,
    AtsInvalidJobSelection,
    AtsWorkflowBusy,
    AtsWorkflowInput,
)
from job_scan.codex_process import CodexAuthStatus, CodexModelOption, CodexNotAuthenticated
from job_scan.config import AppConfig, ClaudeSettings, SchedulerSettings, save_config
from job_scan.dashboard.render import render_dashboard
from job_scan.domain import AvailabilityStatus, JobRecord, MachineStatus, UserStatus
from job_scan.global_jobs import GlobalJobStore
from job_scan.locking import FileRWLock
from job_scan.manual_job_import_workflow import (
    ManualImportResult,
    ManualJobImportWorkflow,
)
from job_scan.paths import AppPaths
from job_scan.repository import JsonlRepository
from job_scan.resume_suggestions import ResumeSuggestions, ResumeSuggestionSettings
from job_scan.review_server import create_review_app
from job_scan.scan_service import ScanSummary
from job_scan.scheduler import SchedulerState
from job_scan.search_history import SearchHistoryStore
from job_scan.setup_service import SetupAnswers
from job_scan.web_workflow import WebRunResult, WebScheduleState, WebWorkflowBusy

TOKEN = "test-console-token"
ORIGIN = "http://127.0.0.1:8765"
HEADERS = {"Host": "127.0.0.1:8765", "Origin": ORIGIN}


class RecordingWorkflow:
    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths
        self.runs: list[tuple[str, bytes, SetupAnswers]] = []
        self.schedules: list[tuple[str, bytes, SetupAnswers]] = []
        self.remove_count = 0
        self.status_count = 0
        self.busy = False
        self.saved_setup_answers: SetupAnswers | None = None
        self.run_state: dict[str, object] | None = None

    def load_setup_answers(self) -> SetupAnswers | None:
        return self.saved_setup_answers

    def start(
        self,
        filename: str,
        payload: bytes,
        answers: SetupAnswers,
    ) -> dict[str, object]:
        if self.busy:
            raise WebWorkflowBusy("A setup and scan is already running.")
        self.runs.append((filename, payload, answers))
        self.run_state = {
            "run_id": "web-run-1",
            "status": "running",
            "stage": "profile",
            "message": "Building candidate profile...",
            "progress_percent": 10,
            "ai_runtime": answers.ai_runtime,
            "review_progress": None,
            "result": None,
            "error": None,
        }
        return self.run_state

    def run(
        self,
        filename: str,
        payload: bytes,
        answers: SetupAnswers,
    ) -> WebRunResult:
        if self.busy:
            raise WebWorkflowBusy("A setup and scan is already running.")
        self.runs.append((filename, payload, answers))
        return self.completed_result()

    def save_schedule(
        self,
        filename: str,
        payload: bytes,
        answers: SetupAnswers,
    ) -> SchedulerState:
        if self.busy:
            raise WebWorkflowBusy("A setup and scan is already running.")
        self.schedules.append((filename, payload, answers))
        return SchedulerState(
            backend="cron",
            installed=True,
            local_time=answers.scheduler.local_time,
            executable=Path("/opt/job-scan/bin/job-scan"),
            managed_location="fixture:scheduler",
        )

    def read_run(self, run_id: str) -> dict[str, object] | None:
        if self.run_state is None or self.run_state["run_id"] != run_id:
            return None
        return self.run_state

    def read_current_run(self) -> dict[str, object] | None:
        if self.run_state is None or self.run_state["status"] != "running":
            return None
        return self.run_state

    def completed_result(self) -> WebRunResult:
        now = datetime(2026, 8, 4, 12, tzinfo=UTC)
        return WebRunResult(
            summary=ScanSummary(
                run_id="web-run-1",
                started_at=now,
                finished_at=now,
                source_counts={"linkedin:linkedin": 42},
                source_errors=[],
                occurrence_count=42,
                new_count=12,
                changed_count=3,
                reviewed_count=8,
                eligible_count=5,
                excluded_count=2,
                uncertain_count=1,
                pending_count=34,
                source_error_count=0,
                claude_model="sonnet",
                claude_batch_count=1,
                claude_budget_usd=Decimal("0.12"),
                claude_failure_count=0,
                claude_failure_counts={},
                jobs_jsonl=self.paths.jobs_jsonl,
                dashboard_html=self.paths.dashboard_html,
            ),
            schedule=WebScheduleState(installed=False, local_time=None),
        )

    def remove_schedule(self) -> SchedulerState:
        if self.busy:
            raise WebWorkflowBusy("A setup and scan is already running.")
        self.remove_count += 1
        return SchedulerState(
            backend="cron",
            installed=False,
            local_time=None,
            executable=None,
            managed_location="fixture:scheduler",
        )

    def schedule_status(self) -> SchedulerState:
        self.status_count += 1
        return SchedulerState(
            backend="cron",
            installed=True,
            local_time="08:30",
            executable=Path("/opt/job-scan/bin/job-scan"),
            managed_location="fixture:scheduler",
        )


class RecordingAtsWorkflow:
    def __init__(self, history: AtsHistoryStore) -> None:
        self.history = history
        self.started: list[AtsWorkflowInput] = []
        self.error: Exception | None = None
        self.busy = False
        self.state = AtsRunState(
            run_id="ats-1",
            search_run_id="search-1",
            status="running",
            stage="resume",
            message="Checking resume...",
            progress_percent=0,
            tasks=[],
        )

    def start(self, inputs: AtsWorkflowInput) -> AtsRunState:
        if self.error is not None:
            raise self.error
        self.started.append(inputs)
        return self.state

    def read_run(self, run_id: str) -> AtsRunState | None:
        return self.state if run_id == self.state.run_id else None

    def read_current_run(self) -> AtsRunState | None:
        return self.state if self.state.status == "running" else None

    def is_busy(self) -> bool:
        return self.busy

    def delete_history(self, run_id: str) -> None:
        if self.busy:
            raise AtsWorkflowBusy("An ATS check is already running.")
        self.history.delete(run_id)


@pytest.fixture
def console_client(
    tmp_path: Path,
) -> Iterator[tuple[TestClient, RecordingWorkflow]]:
    paths = AppPaths.from_root(tmp_path / "home")
    repository = JsonlRepository(paths, FileRWLock(paths.lock_file), render_dashboard)
    repository.mutate(lambda snapshot: snapshot)
    workflow = RecordingWorkflow(paths)
    app = create_review_app(
        repository,
        TOKEN,
        frozenset({ORIGIN}),
        workflow=workflow,  # type: ignore[arg-type]
        ai_store=AiProviderStore(paths.ai_config_toml),
    )
    with TestClient(app, base_url=ORIGIN) as client:
        yield client, workflow


@pytest.fixture
def ats_console_client(
    tmp_path: Path,
) -> Iterator[tuple[TestClient, RecordingWorkflow, RecordingAtsWorkflow]]:
    paths = AppPaths.from_root(tmp_path / "home")
    repository = JsonlRepository(paths, FileRWLock(paths.lock_file), render_dashboard)
    now = datetime(2026, 8, 9, 12, tzinfo=UTC)
    jobs = [
        JobRecord(
            canonical_job_key=key,
            primary_source_occurrence_key=f"test:{key}:1",
            company="Example GmbH",
            title="Backend Engineer",
            location="Berlin",
            url=HttpUrl(f"https://example.test/jobs/{key}"),
            description="Complete backend job description",
            posted_at=date(2026, 8, 9),
            content_hash=f"sha256:{key}",
            first_seen=now,
            last_seen=now,
            availability_status=AvailabilityStatus.ACTIVE,
            machine_status=MachineStatus.ELIGIBLE,
            score=90,
            user_status_updated_at=now,
        )
        for key in ("job-1", "job-2", "missing-or-pending")
    ]
    repository.mutate(
        lambda snapshot: snapshot.model_copy(update={"jobs": jobs}, deep=True)
    )
    resume_bytes = b"DEFAULT RESUME"
    resume_digest = hashlib.sha256(resume_bytes).hexdigest()
    resume_id = f"sha256:{resume_digest}"
    save_config(
        paths.config_toml,
        AppConfig(
            candidate_name="Ada",
            resume_path=paths.root / "default.pdf",
            resume_sha256=resume_id,
            profile_sha256="sha256:" + "b" * 64,
            search_terms=["backend"],
            locations=["Berlin"],
            german_level="B1",
            claude=ClaudeSettings(model="sonnet", effort="medium"),
            scheduler=SchedulerSettings(),
        ),
    )
    paths.profile_md.write_text("# Ada", encoding="utf-8")
    (paths.root / "default.pdf").write_bytes(resume_bytes)
    resume_dir = paths.root / "resumes"
    resume_dir.mkdir()
    (resume_dir / f"{resume_digest}.pdf").write_bytes(resume_bytes)
    global_jobs = GlobalJobStore(paths)
    for job in jobs:
        global_jobs.set_status(
            job,
            UserStatus.SAVED,
            now,
        )
        tracked = global_jobs.find(job.canonical_job_key)
        assert tracked is not None
        global_jobs.set_application_resume(tracked, resume_id, "default.pdf")
    workflow = RecordingWorkflow(paths)
    ats_history = AtsHistoryStore(paths)
    ats_workflow = RecordingAtsWorkflow(ats_history)
    app = create_review_app(
        repository,
        TOKEN,
        frozenset({ORIGIN}),
        workflow=workflow,  # type: ignore[arg-type]
        global_job_store=global_jobs,
        ats_workflow=ats_workflow,  # type: ignore[arg-type]
        ats_history_store=ats_history,
    )
    with TestClient(app, base_url=ORIGIN) as client:
        yield client, workflow, ats_workflow


def settings_json() -> str:
    return json.dumps(
        {
            "search_terms": ["Backend Engineer", "Platform Engineer"],
            "locations": ["Berlin", "Hamburg"],
            "posted_within_days": 14,
            "target_companies": ["bosch", "telekom", "thyssenkrupp"],
            "linkedin_limit": 50,
            "indeed_de_limit": 35,
            "stepstone_de_limit": 27,
            "glassdoor_de_limit": 38,
            "simplify_de_limit": 41,
            "german_level": "B1",
            "claude": {
                "model": "sonnet",
                "effort": "medium",
                "batch_size": 10,
                "thinking_enabled": False,
            },
            "scheduler": {"local_time": None},
        }
    )


def open_console(client: TestClient) -> None:
    response = client.get("/setup")
    assert response.status_code == 200


def test_ats_start_requires_session_and_returns_pollable_state(
    ats_console_client: tuple[TestClient, RecordingWorkflow, RecordingAtsWorkflow],
) -> None:
    client, _workflow, ats_workflow = ats_console_client
    open_console(client)

    response = client.post(
        "/api/ats-runs",
        data={"job_keys": '["job-2", "job-1"]'},
        headers=HEADERS,
    )

    assert response.status_code == 202
    assert response.json()["run_id"] == "ats-1"
    assert client.get("/api/ats-runs/ats-1").json()["stage"] == "resume"
    assert len(ats_workflow.started) == 1
    assert ats_workflow.started[0].search_run_id == "global"
    assert [
        job.canonical_job_key
        for job in ats_workflow.started[0].resumes[0].jobs
    ] == [
        "job-2",
        "job-1",
    ]
    assert ats_workflow.started[0].resumes[0].resume_bytes == b"DEFAULT RESUME"


def test_ats_current_returns_active_run_then_no_content_when_finished(
    ats_console_client: tuple[TestClient, RecordingWorkflow, RecordingAtsWorkflow],
) -> None:
    client, _workflow, ats_workflow = ats_console_client

    response = client.get("/api/ats-runs/current")

    assert response.status_code == 200
    assert response.json()["run_id"] == "ats-1"
    ats_workflow.state = ats_workflow.state.model_copy(
        update={"status": "complete", "stage": "archive", "progress_percent": 100}
    )
    assert client.get("/api/ats-runs/current").status_code == 204


def test_ats_start_rejects_missing_mutation_session(
    ats_console_client: tuple[TestClient, RecordingWorkflow, RecordingAtsWorkflow],
) -> None:
    client, _workflow, ats_workflow = ats_console_client

    response = client.post(
        "/api/ats-runs",
        data={"job_keys": '["job-1"]'},
        headers=HEADERS,
    )

    assert response.status_code == 403
    assert ats_workflow.started == []


@pytest.mark.parametrize("job_keys", [[], ["job-1", "job-1"], [" "]])
def test_ats_start_rejects_empty_duplicate_or_blank_job_keys_before_workflow(
    ats_console_client: tuple[TestClient, RecordingWorkflow, RecordingAtsWorkflow],
    job_keys: list[str],
) -> None:
    client, _workflow, ats_workflow = ats_console_client
    open_console(client)

    response = client.post(
        "/api/ats-runs",
        data={"job_keys": json.dumps(job_keys)},
        headers=HEADERS,
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid ATS selection."}
    assert ats_workflow.started == []


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (AtsInputError("Select or upload a resume for ATS Check."), 422),
        (
            AtsInvalidJobSelection(
                "Selected jobs must belong to this search's active review groups."
            ),
            422,
        ),
        (AtsWorkflowBusy("An ATS check is already running."), 409),
    ],
)
def test_ats_start_maps_workflow_errors_without_exposing_source_details(
    ats_console_client: tuple[TestClient, RecordingWorkflow, RecordingAtsWorkflow],
    error: Exception,
    expected_status: int,
) -> None:
    client, _workflow, ats_workflow = ats_console_client
    open_console(client)
    ats_workflow.error = error

    response = client.post(
        "/api/ats-runs",
        data={"job_keys": '["missing-or-pending"]'},
        headers=HEADERS,
    )

    assert response.status_code == expected_status


def test_ats_poll_returns_not_found_for_unknown_run(
    ats_console_client: tuple[TestClient, RecordingWorkflow, RecordingAtsWorkflow],
) -> None:
    client, _workflow, _ats_workflow = ats_console_client

    assert client.get("/api/ats-runs/missing").status_code == 404


def _archive_search_and_ats(paths: AppPaths) -> None:
    repository = JsonlRepository(paths, FileRWLock(paths.lock_file), render_dashboard)
    now = datetime(2026, 8, 9, 12, tzinfo=UTC)
    archived_job = JobRecord(
        canonical_job_key="job-1",
        primary_source_occurrence_key="test:job-1:1",
        company="Example GmbH",
        title="Backend Engineer",
        location="Berlin",
        url=HttpUrl("https://example.test/jobs/1"),
        description="Complete backend job description",
        posted_at=date(2026, 8, 9),
        content_hash="sha256:job-1",
        first_seen=now,
        last_seen=now,
        availability_status=AvailabilityStatus.ACTIVE,
        machine_status=MachineStatus.ELIGIBLE,
        score=90,
        user_status_updated_at=now,
    )
    repository.mutate(
        lambda snapshot: snapshot.model_copy(update={"jobs": [archived_job]}, deep=True)
    )
    resume = paths.root / "Ada.pdf"
    resume.write_bytes(b"PRIVATE RESUME BYTES")
    paths.profile_md.write_bytes(b"# Ada")
    paths.config_toml.write_bytes(b'candidate_name = "Ada"\n')
    SearchHistoryStore(paths).archive(
        run_id="search-1",
        candidate_name="Ada",
        resume_filename="Ada.pdf",
        resume_path=resume,
        snapshot=repository.load(),
        finished_at=now,
    )
    AtsHistoryStore(paths).archive(
        AtsCheckBundle(
            run_id="ats-1",
            search_run_id="search-1",
            resume_id="sha256:" + "a" * 64,
            candidate_name="Ada",
            resume_filename="Ada.pdf",
            started_at=now,
            finished_at=now,
            ai_runtime="api:deepseek",
            ai_model="deepseek-chat",
            resume=AtsResumeAssessment(
                readiness_score=88,
                verdict="ready",
                title="Resume content is ready",
                summary="Resume content can be checked against jobs.",
                findings=[
                    AtsResumeFinding(
                        label="Text extraction",
                        status="pass",
                        detail="Selectable resume text was extracted.",
                    )
                ],
            ),
            jobs=[],
        ),
        b"PRIVATE RESUME BYTES",
    )


def test_ats_history_can_be_loaded_and_deleted_without_touching_search_history(
    ats_console_client: tuple[TestClient, RecordingWorkflow, RecordingAtsWorkflow],
) -> None:
    client, workflow, _ats_workflow = ats_console_client
    paths = workflow.paths
    _archive_search_and_ats(paths)
    cache_marker = paths.cache_dir / "keep.json"
    cache_marker.write_bytes(b"cache")
    untouched = {
        path: path.read_bytes()
        for path in (
            paths.config_toml,
            paths.profile_md,
            paths.jobs_jsonl,
            cache_marker,
        )
    }
    open_console(client)

    loaded = client.get("/api/ats-history/ats-1")
    deleted = client.delete("/api/ats-history/ats-1", headers=HEADERS)

    assert loaded.status_code == 200
    assert loaded.json()["search_run_id"] == "search-1"
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True}
    assert SearchHistoryStore(paths).load("search-1").jobs
    assert {path: path.read_bytes() for path in untouched} == untouched
    with pytest.raises(KeyError):
        AtsHistoryStore(paths).load("ats-1")


def test_ats_history_delete_returns_conflict_while_an_ats_run_is_busy(
    ats_console_client: tuple[TestClient, RecordingWorkflow, RecordingAtsWorkflow],
) -> None:
    client, workflow, ats_workflow = ats_console_client
    _archive_search_and_ats(workflow.paths)
    open_console(client)
    ats_workflow.busy = True

    response = client.delete("/api/ats-history/ats-1", headers=HEADERS)

    assert response.status_code == 409
    assert response.json() == {"detail": "An ATS check is already running."}
    assert AtsHistoryStore(workflow.paths).load("ats-1").run_id == "ats-1"


@pytest.mark.parametrize("run_id", ["missing", "bad!"])
def test_ats_history_returns_not_found_for_unknown_or_invalid_ids(
    ats_console_client: tuple[TestClient, RecordingWorkflow, RecordingAtsWorkflow],
    run_id: str,
) -> None:
    client, _workflow, _ats_workflow = ats_console_client
    open_console(client)

    loaded = client.get(f"/api/ats-history/{run_id}")
    deleted = client.delete(f"/api/ats-history/{run_id}", headers=HEADERS)

    assert loaded.status_code == 404
    assert deleted.status_code == 404


def test_ats_history_delete_requires_mutation_session(
    ats_console_client: tuple[TestClient, RecordingWorkflow, RecordingAtsWorkflow],
) -> None:
    client, workflow, _ats_workflow = ats_console_client
    _archive_search_and_ats(workflow.paths)

    response = client.delete("/api/ats-history/ats-1", headers=HEADERS)

    assert response.status_code == 403
    assert AtsHistoryStore(workflow.paths).load("ats-1").run_id == "ats-1"


def test_setup_can_open_one_archived_search_result(
    console_client: tuple[TestClient, RecordingWorkflow],
) -> None:
    client, workflow = console_client
    paths = workflow.paths
    repository = JsonlRepository(paths, FileRWLock(paths.lock_file), render_dashboard)
    resume = paths.root / "candidate.pdf"
    resume.write_bytes(b"resume")
    paths.profile_md.write_text("profile", encoding="utf-8")
    paths.config_toml.write_text("config", encoding="utf-8")
    SearchHistoryStore(paths).archive(
        run_id="history-1",
        candidate_name="Ada Lovelace",
        resume_filename="Ada CV.pdf",
        resume_path=resume,
        snapshot=repository.load(),
        finished_at=datetime(2026, 8, 7, 12, tzinfo=UTC),
    )

    response = client.get("/setup?run_id=history-1")

    assert response.status_code == 200
    page = BeautifulSoup(response.text, "html.parser")
    assert page.body.get("data-review-run-id") == "history-1"
    assert page.select_one('[data-scan-history-id="history-1"]') is not None


def test_setup_returns_not_found_for_unknown_archived_search(
    console_client: tuple[TestClient, RecordingWorkflow],
) -> None:
    client, _workflow = console_client

    assert client.get("/setup?run_id=missing").status_code == 404


def test_setup_loads_selected_ats_result(
    ats_console_client: tuple[TestClient, RecordingWorkflow, RecordingAtsWorkflow],
) -> None:
    client, workflow, _ats_workflow = ats_console_client
    _archive_search_and_ats(workflow.paths)

    response = client.get("/setup?ats_run_id=ats-1")

    assert response.status_code == 200
    page = BeautifulSoup(response.text, "html.parser")
    assert page.body.get("data-ats-run-id") == "ats-1"


def test_setup_loads_newest_ats_result_by_default(
    ats_console_client: tuple[TestClient, RecordingWorkflow, RecordingAtsWorkflow],
) -> None:
    client, workflow, _ats_workflow = ats_console_client
    _archive_search_and_ats(workflow.paths)
    history = AtsHistoryStore(workflow.paths)
    older = history.load("ats-1")
    history.archive(
        older.model_copy(
            update={
                "run_id": "ats-2",
                "resume_id": "sha256:" + "b" * 64,
                "finished_at": datetime(2026, 8, 9, 13, tzinfo=UTC),
            }
        ),
        b"NEWER PRIVATE RESUME BYTES",
    )

    response = client.get("/setup")

    assert response.status_code == 200
    page = BeautifulSoup(response.text, "html.parser")
    assert page.body.get("data-ats-run-id") == "ats-2"


@pytest.mark.parametrize("ats_run_id", ["missing", "bad!"])
def test_setup_returns_not_found_for_unknown_or_invalid_ats_result(
    ats_console_client: tuple[TestClient, RecordingWorkflow, RecordingAtsWorkflow],
    ats_run_id: str,
) -> None:
    client, _workflow, _ats_workflow = ats_console_client

    assert client.get(f"/setup?ats_run_id={ats_run_id}").status_code == 404


def test_resume_suggestion_endpoint_uses_uploaded_resume_and_selected_runtime(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    repository = JsonlRepository(paths, FileRWLock(paths.lock_file), render_dashboard)
    repository.mutate(lambda snapshot: snapshot)
    workflow = RecordingWorkflow(paths)

    class RecordingSuggestions:
        def __init__(self) -> None:
            self.calls: list[tuple[str, bytes, ResumeSuggestionSettings]] = []

        def suggest(
            self,
            filename: str,
            payload: bytes,
            settings: ResumeSuggestionSettings,
        ) -> ResumeSuggestions:
            self.calls.append((filename, payload, settings))
            return ResumeSuggestions(
                search_terms=["Java Backend Engineer"],
            )

    suggestions = RecordingSuggestions()
    app = create_review_app(
        repository,
        TOKEN,
        frozenset({ORIGIN}),
        workflow=workflow,  # type: ignore[arg-type]
        ai_store=AiProviderStore(paths.ai_config_toml),
        resume_suggestion_service=suggestions,  # type: ignore[arg-type]
    )
    suggestion_settings = {
        "ai_runtime": "api:deepseek",
        "claude": {
            "model": "deepseek-v4",
            "effort": "high",
            "thinking_enabled": False,
            "batch_size": 10,
        },
    }

    with TestClient(app, base_url=ORIGIN) as client:
        open_console(client)
        selected = client.put(
            "/api/ai/config",
            json={
                "ai_runtime": "claude-code",
                "claude": {
                    "model": "opus",
                    "effort": "low",
                    "thinking_enabled": True,
                },
            },
            headers=HEADERS,
        )
        assert selected.status_code == 200, selected.text
        response = client.post(
            "/api/resume-suggestions",
            data={"settings": json.dumps(suggestion_settings)},
            files={"resume": ("candidate.pdf", b"resume bytes", "application/pdf")},
            headers=HEADERS,
        )

    assert response.status_code == 200
    assert response.json() == {
        "search_terms": ["Java Backend Engineer"],
    }
    filename, payload, parsed = suggestions.calls[0]
    assert filename == "candidate.pdf"
    assert payload == b"resume bytes"
    assert parsed.ai_runtime == "claude-code"
    assert parsed.claude.model == "opus"
    assert parsed.claude.effort == "low"
    assert parsed.claude.thinking_enabled is True


class RecordingDiscovery:
    def __init__(self) -> None:
        self.providers = []
        self.error: Exception | None = None

    def discover(self, provider):
        if self.error is not None:
            raise self.error
        self.providers.append(provider)
        return [
            AiModelOption(
                id="deepseek-chat",
                name="DeepSeek Chat",
                supported_reasoning_efforts=["low", "medium"],
            )
        ]


CODEX_MODELS = [
    CodexModelOption(
        id="gpt-5.6-sol",
        name="GPT-5.6-Sol",
        default_reasoning_effort="low",
        supported_reasoning_efforts=["low", "medium", "high", "ultra"],
    ),
    CodexModelOption(
        id="gpt-5.6-luna",
        name="GPT-5.6-Luna",
        default_reasoning_effort="medium",
        supported_reasoning_efforts=["low", "medium", "high", "xhigh", "max"],
    ),
]


@pytest.fixture
def ai_console_client(
    tmp_path: Path,
) -> Iterator[tuple[TestClient, AiProviderStore, RecordingDiscovery]]:
    paths = AppPaths.from_root(tmp_path / "home")
    repository = JsonlRepository(paths, FileRWLock(paths.lock_file), render_dashboard)
    repository.mutate(lambda snapshot: snapshot)
    workflow = RecordingWorkflow(paths)
    store = AiProviderStore(paths.ai_config_toml)
    discovery = RecordingDiscovery()
    app = create_review_app(
        repository,
        TOKEN,
        frozenset({ORIGIN}),
        workflow=workflow,  # type: ignore[arg-type]
        ai_store=store,
        ai_model_discovery=discovery,  # type: ignore[arg-type]
        codex_model_discovery=lambda: CODEX_MODELS,
    )
    with TestClient(app, base_url=ORIGIN) as client:
        yield client, store, discovery


def ai_provider_json(api_key: str | None = "sk-private") -> dict[str, object]:
    return {
        "display_name": "DeepSeek",
        "base_url": "https://api.example.com/anthropic",
        "api_key": api_key,
        "model": "deepseek-chat",
        "reasoning_effort": "low",
    }


def test_ai_provider_api_saves_key_but_only_returns_masked_metadata(
    ai_console_client: tuple[TestClient, AiProviderStore, RecordingDiscovery],
) -> None:
    client, store, _discovery = ai_console_client
    open_console(client)

    response = client.post("/api/ai/providers", json=ai_provider_json(), headers=HEADERS)

    assert response.status_code == 201, response.text
    assert response.json() == {
        "id": "deepseek",
        "display_name": "DeepSeek",
        "base_url": "https://api.example.com/anthropic",
        "model": "deepseek-chat",
        "reasoning_effort": "low",
        "api_key_configured": True,
    }
    assert "sk-private" not in response.text
    assert store.require("deepseek").api_key == "sk-private"


def test_ai_provider_api_requires_local_mutation_session(
    ai_console_client: tuple[TestClient, AiProviderStore, RecordingDiscovery],
) -> None:
    client, store, _discovery = ai_console_client

    response = client.post("/api/ai/providers", json=ai_provider_json(), headers=HEADERS)

    assert response.status_code == 403
    assert store.list() == []


def test_ai_configuration_api_persists_the_selected_runtime_and_cli_model(
    ai_console_client: tuple[TestClient, AiProviderStore, RecordingDiscovery],
) -> None:
    client, _store, _discovery = ai_console_client
    open_console(client)

    response = client.put(
        "/api/ai/config",
        json={
            "ai_runtime": "claude-code",
            "claude": {
                "model": "opus",
                "effort": "high",
                "thinking_enabled": False,
            },
        },
        headers=HEADERS,
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "ai_runtime": "claude-code",
        "claude": {
            "model": "opus",
            "effort": "high",
            "thinking_enabled": False,
        },
        "codex": {
            "model": "gpt-5.6-sol",
            "effort": "high",
        },
        "locked": False,
    }
    assert client.get("/api/ai/config").json() == response.json()


def test_ai_configuration_api_lists_codex_cli_models(
    ai_console_client: tuple[TestClient, AiProviderStore, RecordingDiscovery],
) -> None:
    client, _store, _discovery = ai_console_client

    response = client.get("/api/ai/codex-models")

    assert response.status_code == 200, response.text
    assert response.json() == [model.model_dump(mode="json") for model in CODEX_MODELS]


def test_ai_configuration_api_runs_isolated_codex_device_login(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from job_scan import review_server

    paths = AppPaths.from_root(tmp_path / "home")
    repository = JsonlRepository(paths, FileRWLock(paths.lock_file), render_dashboard)
    repository.mutate(lambda snapshot: snapshot)

    class LoggedOutCodex:
        def __init__(self, *, home: Path) -> None:
            assert home == paths.codex_home

        def auth_status(self) -> CodexAuthStatus:
            raise CodexNotAuthenticated("not logged in")

    class RecordingLogin:
        def __init__(self, home: Path) -> None:
            assert home == paths.codex_home
            self.state = "idle"
            self.closed = False

        def snapshot(self) -> dict[str, object | None]:
            return {
                "state": self.state,
                "verification_url": (
                    "https://auth.openai.com/codex/device"
                    if self.state == "pending"
                    else None
                ),
                "user_code": "TEST-9YWCE" if self.state == "pending" else None,
                "error": None,
            }

        def start(self) -> dict[str, object | None]:
            self.state = "pending"
            return self.snapshot()

        def cancel(self) -> dict[str, object | None]:
            self.state = "cancelled"
            return self.snapshot()

        def close(self) -> None:
            self.closed = True

    login = RecordingLogin(paths.codex_home)
    monkeypatch.setattr(review_server, "CodexProcess", LoggedOutCodex)
    monkeypatch.setattr(
        review_server,
        "CodexLoginWorkflow",
        lambda home: login,
        raising=False,
    )
    app = create_review_app(
        repository,
        TOKEN,
        frozenset({ORIGIN}),
        workflow=RecordingWorkflow(paths),  # type: ignore[arg-type]
        ai_store=AiProviderStore(paths.ai_config_toml),
        ai_model_discovery=RecordingDiscovery(),  # type: ignore[arg-type]
        codex_model_discovery=lambda: CODEX_MODELS,
    )

    with TestClient(app, base_url=ORIGIN) as client:
        open_console(client)
        status_response = client.get("/api/ai/codex-auth")
        start_response = client.post("/api/ai/codex-login", headers=HEADERS)
        cancel_response = client.delete("/api/ai/codex-login", headers=HEADERS)

    assert status_response.status_code == 200
    assert status_response.json() == {
        "authenticated": False,
        "login": {
            "state": "idle",
            "verification_url": None,
            "user_code": None,
            "error": None,
        },
    }
    assert start_response.status_code == 200
    assert start_response.json()["state"] == "pending"
    assert start_response.json()["user_code"] == "TEST-9YWCE"
    assert cancel_response.status_code == 200
    assert cancel_response.json()["state"] == "cancelled"
    assert login.closed is True


def test_ai_configuration_api_persists_codex_cli_selection(
    ai_console_client: tuple[TestClient, AiProviderStore, RecordingDiscovery],
) -> None:
    client, _store, _discovery = ai_console_client
    open_console(client)

    response = client.put(
        "/api/ai/config",
        json={
            "ai_runtime": "codex-cli",
            "claude": {
                "model": "opus",
                "effort": "medium",
                "thinking_enabled": True,
            },
            "codex": {
                "model": "gpt-5.6-sol",
                "effort": "high",
            },
        },
        headers=HEADERS,
    )

    assert response.status_code == 200, response.text
    assert response.json()["ai_runtime"] == "codex-cli"
    assert response.json()["codex"] == {
        "model": "gpt-5.6-sol",
        "effort": "high",
    }
    assert client.get("/api/ai/config").json() == response.json()


def test_ai_configuration_api_falls_back_when_selected_provider_is_missing(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    repository = JsonlRepository(paths, FileRWLock(paths.lock_file), render_dashboard)
    repository.mutate(lambda snapshot: snapshot)
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
    app = create_review_app(
        repository,
        TOKEN,
        frozenset({ORIGIN}),
        ai_store=AiProviderStore(paths.ai_config_toml),
    )

    with TestClient(app, base_url=ORIGIN) as client:
        response = client.get("/api/ai/config")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "ai_runtime": "claude-code",
        "claude": {
            "model": "haiku",
            "effort": "low",
            "thinking_enabled": False,
        },
        "codex": {
            "model": "gpt-5.6-sol",
            "effort": "high",
        },
        "locked": False,
    }


def test_ai_configuration_api_rejects_changes_while_a_scan_is_running(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    repository = JsonlRepository(paths, FileRWLock(paths.lock_file), render_dashboard)
    repository.mutate(lambda snapshot: snapshot)
    workflow = RecordingWorkflow(paths)
    workflow.busy = True
    app = create_review_app(
        repository,
        TOKEN,
        frozenset({ORIGIN}),
        workflow=workflow,  # type: ignore[arg-type]
        ai_store=AiProviderStore(paths.ai_config_toml),
    )

    with TestClient(app, base_url=ORIGIN) as client:
        client.cookies.set("job_scan_session", TOKEN)
        response = client.put(
            "/api/ai/config",
            json={
                "ai_runtime": "claude-code",
                "claude": {
                    "model": "opus",
                    "effort": "high",
                    "thinking_enabled": False,
                },
            },
            headers=HEADERS,
        )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "AI is in use; retry the configuration change after it completes."
    }


def test_ai_configuration_api_is_locked_while_a_manual_ai_task_is_running(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    repository = JsonlRepository(paths, FileRWLock(paths.lock_file), render_dashboard)
    repository.mutate(lambda snapshot: snapshot)
    manual_workflow = ManualJobImportWorkflow()
    started = Event()
    release = Event()

    def run_manual_task(_progress: object) -> ManualImportResult:
        started.set()
        if not release.wait(timeout=5):
            raise AssertionError("test did not release manual task")
        return ManualImportResult("tracked", UserStatus.SAVED)

    manual_workflow.start(
        run_manual_task,
        task_kind="re-evaluate",
        task_key="re-evaluate:tracked",
    )
    assert started.wait(timeout=1)
    app = create_review_app(
        repository,
        TOKEN,
        frozenset({ORIGIN}),
        ai_store=AiProviderStore(paths.ai_config_toml),
        manual_import_workflow=manual_workflow,
    )

    try:
        with TestClient(app, base_url=ORIGIN) as client:
            client.cookies.set("job_scan_session", TOKEN)
            state = client.get("/api/ai/config")
            response = client.put(
                "/api/ai/config",
                json={
                    "ai_runtime": "claude-code",
                    "claude": {
                        "model": "opus",
                        "effort": "high",
                        "thinking_enabled": False,
                    },
                },
                headers=HEADERS,
            )
    finally:
        release.set()

    assert state.status_code == 200
    assert state.json()["locked"] is True
    assert response.status_code == 409


def test_ai_configuration_api_rejects_changes_while_an_ai_call_holds_the_lock(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    repository = JsonlRepository(paths, FileRWLock(paths.lock_file), render_dashboard)
    repository.mutate(lambda snapshot: snapshot)
    app = create_review_app(
        repository,
        TOKEN,
        frozenset({ORIGIN}),
        ai_store=AiProviderStore(paths.ai_config_toml),
    )

    with TestClient(app, base_url=ORIGIN) as client:
        client.cookies.set("job_scan_session", TOKEN)
        with FileRWLock(paths.ai_usage_lock_file).shared():
            assert client.get("/api/ai/config").json()["locked"] is True
            response = client.put(
                "/api/ai/config",
                json={
                    "ai_runtime": "claude-code",
                    "claude": {
                        "model": "opus",
                        "effort": "high",
                        "thinking_enabled": False,
                    },
                },
                headers=HEADERS,
            )
            provider_response = client.post(
                "/api/ai/providers",
                json=ai_provider_json(),
                headers=HEADERS,
            )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "AI is in use; retry the configuration change after it completes."
    }
    assert provider_response.status_code == 409


def test_ai_provider_api_deletes_saved_configuration(
    ai_console_client: tuple[TestClient, AiProviderStore, RecordingDiscovery],
) -> None:
    client, store, _discovery = ai_console_client
    open_console(client)
    store.create(AiProviderDraft.model_validate(ai_provider_json()))

    response = client.delete("/api/ai/providers/deepseek", headers=HEADERS)

    assert response.status_code == 200, response.text
    assert response.json() == {"deleted": True}
    assert store.list() == []


def test_ai_provider_api_rejects_deleting_the_selected_runtime(
    ai_console_client: tuple[TestClient, AiProviderStore, RecordingDiscovery],
) -> None:
    client, store, _discovery = ai_console_client
    open_console(client)
    provider = store.create(AiProviderDraft.model_validate(ai_provider_json()))
    selected = client.put(
        "/api/ai/config",
        json={
            "ai_runtime": f"api:{provider.id}",
            "claude": {
                "model": "sonnet",
                "effort": "medium",
                "thinking_enabled": True,
            },
        },
        headers=HEADERS,
    )
    assert selected.status_code == 200, selected.text

    response = client.delete(
        f"/api/ai/providers/{provider.id}",
        headers=HEADERS,
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "Switch AI runtime before deleting its selected configuration."
    }
    assert store.require(provider.id).model == "deepseek-chat"


def test_ai_provider_api_returns_not_found_when_deleting_unknown_configuration(
    ai_console_client: tuple[TestClient, AiProviderStore, RecordingDiscovery],
) -> None:
    client, _store, _discovery = ai_console_client
    open_console(client)

    response = client.delete("/api/ai/providers/missing", headers=HEADERS)

    assert response.status_code == 404


def test_ai_provider_validation_error_never_echoes_api_key(
    ai_console_client: tuple[TestClient, AiProviderStore, RecordingDiscovery],
) -> None:
    client, store, _discovery = ai_console_client
    open_console(client)
    private_key = "sk-" + ("private" * 700)
    payload = ai_provider_json(private_key)

    response = client.post("/api/ai/providers", json=payload, headers=HEADERS)

    assert response.status_code == 422
    assert private_key not in response.text
    assert "private" not in response.text
    assert store.list() == []


def test_ai_model_discovery_uses_unsaved_form_key_without_persisting_it(
    ai_console_client: tuple[TestClient, AiProviderStore, RecordingDiscovery],
) -> None:
    client, store, discovery = ai_console_client
    open_console(client)

    response = client.post(
        "/api/ai/models/discover",
        json={
            "base_url": "https://api.example.com/anthropic",
            "api_key": "sk-temporary",
        },
        headers=HEADERS,
    )

    assert response.status_code == 200, response.text
    assert response.json() == [
        {
            "id": "deepseek-chat",
            "name": "DeepSeek Chat",
            "supported_reasoning_efforts": ["low", "medium"],
        }
    ]
    assert discovery.providers[0].api_key == "sk-temporary"
    assert store.list() == []


def test_ai_model_discovery_does_not_send_saved_key_to_changed_base_url(
    ai_console_client: tuple[TestClient, AiProviderStore, RecordingDiscovery],
) -> None:
    client, store, discovery = ai_console_client
    open_console(client)
    store.create(AiProviderDraft.model_validate(ai_provider_json()))

    response = client.post(
        "/api/ai/models/discover",
        json={
            "provider_id": "deepseek",
            "base_url": "https://other.example.com/anthropic",
            "api_key": None,
        },
        headers=HEADERS,
    )

    assert response.status_code == 422
    assert discovery.providers == []


def test_ai_model_discovery_rejects_private_url_as_safe_client_error(
    ai_console_client: tuple[TestClient, AiProviderStore, RecordingDiscovery],
) -> None:
    client, _store, discovery = ai_console_client
    open_console(client)
    discovery.error = OutboundAiUrlError("AI provider URL must use public HTTPS.")

    response = client.post(
        "/api/ai/models/discover",
        json={
            "base_url": "https://127.0.0.1/anthropic",
            "api_key": "sk-temporary",
        },
        headers=HEADERS,
    )

    assert response.status_code == 422
    assert "sk-temporary" not in response.text
    assert discovery.providers == []


@pytest.mark.parametrize(
    ("base_url", "api_key"),
    [
        ("http://api.example.com/anthropic", "sk-http-secret"),
        ("https://api.example.com:99999/anthropic", "sk-port-secret"),
        ("https://api.example.com/anthropic", "sk-unicode-secret-\u2603"),
    ],
)
def test_ai_model_discovery_validation_is_opaque(
    ai_console_client: tuple[TestClient, AiProviderStore, RecordingDiscovery],
    base_url: str,
    api_key: str,
) -> None:
    client, _store, discovery = ai_console_client
    open_console(client)

    response = client.post(
        "/api/ai/models/discover",
        json={"base_url": base_url, "api_key": api_key},
        headers=HEADERS,
    )

    assert response.status_code == 422
    assert api_key not in response.text
    assert discovery.providers == []


def test_setup_page_serves_real_form_and_sets_protected_session(
    console_client: tuple[TestClient, RecordingWorkflow],
) -> None:
    client, _workflow = console_client

    response = client.get("/setup")

    assert response.status_code == 200
    assert '<form id="setup-form">' in response.text
    assert 'id="review-link"' in response.text
    assert 'href="#review"' in response.text
    assert "Data revision 1" in response.text
    assert "httponly" in response.headers["set-cookie"].lower()


def test_setup_page_prefills_every_editable_field_from_saved_answers(
    console_client: tuple[TestClient, RecordingWorkflow],
) -> None:
    client, workflow = console_client
    workflow.saved_setup_answers = SetupAnswers.model_validate(
        {
            "search_terms": ["Site Reliability Engineer", "Custom Reliability Role"],
            "locations": ["Munich"],
            "posted_within_days": 1,
            "target_companies": ["bosch", "telekom"],
            "linkedin_limit": 17,
            "indeed_de_limit": 23,
            "stepstone_de_limit": 29,
            "glassdoor_de_limit": 31,
            "simplify_de_limit": 37,
            "german_level": "C1 custom",
            "staffing_penalty": 23,
            "claude": {
                "model": "custom-model",
                "effort": "high",
                "batch_size": 4,
            },
            "scheduler": {"local_time": "06:45"},
        }
    )

    page = BeautifulSoup(client.get("/setup").text, "html.parser")

    def selected_values(selector: str) -> list[str]:
        return [
            option.get("value", option.get_text(strip=True))
            for option in page.select(f"{selector} option[selected]")
        ]

    assert selected_values("#search-terms") == [
        "Site Reliability Engineer",
        "Custom Reliability Role",
    ]
    assert selected_values("#locations") == ["Munich"]
    assert page.select_one("#target-lanes") is None
    assert selected_values("#german-level") == ["C1 custom"]
    assert selected_values("#posted-within-days") == ["1"]
    assert page.select_one("#target-company-bosch").has_attr("checked")
    assert page.select_one("#target-company-telekom").has_attr("checked")
    assert selected_values("#claude-model") == ["custom-model"]
    assert selected_values("#claude-effort") == ["high"]
    assert page.select_one("#linkedin-limit").get("value") == "17"
    assert page.select_one("#indeed-de-limit").get("value") == "23"
    assert page.select_one("#stepstone-de-limit").get("value") == "29"
    assert page.select_one("#glassdoor-de-limit").get("value") == "31"
    assert page.select_one("#simplify-de-limit").get("value") == "37"
    assert page.select_one("#staffing-penalty") is None
    assert page.select_one("#claude-batch-size") is None
    assert page.select_one("#scan-time").get("value") == "06:45"
    assert page.select_one("#company-list") is None


def test_setup_page_has_separate_save_schedule_and_run_scan_buttons(
    console_client: tuple[TestClient, RecordingWorkflow],
) -> None:
    client, _workflow = console_client

    page = BeautifulSoup(client.get("/setup").text, "html.parser")

    save_schedule = page.select_one("#save-schedule")
    run_scan = page.select_one("#run-scan")
    assert save_schedule is not None
    assert save_schedule.get("type") == "button"
    assert save_schedule.get_text(strip=True) == "Save Daily scan time"
    assert run_scan is not None
    assert run_scan.get("type") == "submit"
    assert run_scan.get_text(strip=True) == "Run scan"


def test_setup_and_scan_endpoint_validates_form_and_returns_real_summary(
    console_client: tuple[TestClient, RecordingWorkflow],
) -> None:
    client, workflow = console_client
    open_console(client)

    response = client.post(
        "/api/setup-and-scan",
        files={
            "settings": (None, settings_json()),
            "resume": (
                "candidate.docx",
                b"uploaded resume bytes",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
        },
        headers=HEADERS,
    )

    assert response.status_code == 202, response.text
    assert response.json() == {
        "run_id": "web-run-1",
        "status": "running",
        "stage": "profile",
        "message": "Building candidate profile...",
        "progress_percent": 10.0,
        "ai_runtime": "claude-code",
        "source_progress": None,
        "review_progress": None,
        "company_size_progress": None,
        "result": None,
        "error": None,
    }
    assert len(workflow.runs) == 1
    filename, payload, answers = workflow.runs[0]
    assert filename == "candidate.docx"
    assert payload == b"uploaded resume bytes"
    assert answers.candidate_name == ""
    assert answers.search_terms == ["Backend Engineer", "Platform Engineer"]
    assert answers.locations == ["Berlin", "Hamburg"]
    assert answers.posted_within_days == 14
    assert answers.target_companies == ["bosch", "telekom", "thyssenkrupp"]
    assert answers.indeed_de_limit == 35
    assert answers.stepstone_de_limit == 27
    assert answers.glassdoor_de_limit == 38
    assert answers.simplify_de_limit == 41
    assert answers.staffing_penalty == 10
    assert answers.claude.thinking_enabled is True
    assert answers.scheduler.local_time is None


def test_save_schedule_endpoint_saves_setup_without_starting_a_scan(
    console_client: tuple[TestClient, RecordingWorkflow],
) -> None:
    client, workflow = console_client
    open_console(client)
    settings = json.loads(settings_json())
    settings["scheduler"] = {"local_time": "07:15"}

    response = client.post(
        "/api/schedule",
        files={
            "settings": (None, json.dumps(settings)),
            "resume": ("scheduled.docx", b"scheduled resume"),
        },
        headers=HEADERS,
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"installed": True, "local_time": "07:15"}
    assert len(workflow.schedules) == 1
    filename, payload, answers = workflow.schedules[0]
    assert filename == "scheduled.docx"
    assert payload == b"scheduled resume"
    assert answers.scheduler.local_time == "07:15"
    assert workflow.runs == []


def test_setup_and_scan_uses_the_saved_global_ai_selection(
    console_client: tuple[TestClient, RecordingWorkflow],
) -> None:
    client, workflow = console_client
    open_console(client)
    selected = client.put(
        "/api/ai/config",
        json={
            "ai_runtime": "claude-code",
            "claude": {
                "model": "opus",
                "effort": "high",
                "thinking_enabled": True,
            },
        },
        headers=HEADERS,
    )
    assert selected.status_code == 200, selected.text

    response = client.post(
        "/api/setup-and-scan",
        files={
            "settings": (None, settings_json()),
            "resume": ("candidate.pdf", b"PDF", "application/pdf"),
        },
        headers=HEADERS,
    )

    assert response.status_code == 202, response.text
    submitted = workflow.runs[0][2]
    assert submitted.ai_runtime == "claude-code"
    assert submitted.claude.model == "opus"
    assert submitted.claude.effort == "high"
    assert submitted.claude.thinking_enabled is True


def test_current_setup_and_scan_returns_no_content_without_active_run(
    console_client: tuple[TestClient, RecordingWorkflow],
) -> None:
    client, _workflow = console_client

    response = client.get("/api/setup-and-scan/current")

    assert response.status_code == 204


def test_current_setup_and_scan_returns_active_run(
    console_client: tuple[TestClient, RecordingWorkflow],
) -> None:
    client, _workflow = console_client
    open_console(client)
    started = client.post(
        "/api/setup-and-scan",
        files={
            "settings": (None, settings_json()),
            "resume": ("candidate.docx", b"uploaded resume bytes"),
        },
        headers=HEADERS,
    )

    response = client.get("/api/setup-and-scan/current")

    assert response.status_code == 200
    assert response.json() == started.json()


def test_setup_and_scan_status_returns_real_completed_result(
    console_client: tuple[TestClient, RecordingWorkflow],
) -> None:
    client, workflow = console_client
    open_console(client)
    started = client.post(
        "/api/setup-and-scan",
        files={
            "settings": (None, settings_json()),
            "resume": ("candidate.docx", b"uploaded resume bytes"),
        },
        headers=HEADERS,
    )
    result = workflow.completed_result()
    workflow.run_state = {
        "run_id": "web-run-1",
        "status": "complete",
        "stage": "publish",
        "message": "Review queue published.",
        "progress_percent": 100,
        "review_progress": None,
        "result": result,
        "error": None,
    }

    response = client.get(f"/api/setup-and-scan/{started.json()['run_id']}")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "complete"
    assert body["stage"] == "publish"
    assert body["result"]["summary"]["occurrence_count"] == 42
    assert body["result"]["summary"]["reviewed_count"] == 8
    assert body["result"]["schedule"] == {"installed": False, "local_time": None}


def test_setup_and_scan_status_rejects_unknown_run(
    console_client: tuple[TestClient, RecordingWorkflow],
) -> None:
    client, _workflow = console_client

    response = client.get("/api/setup-and-scan/missing")

    assert response.status_code == 404


def test_setup_and_scan_rejects_invalid_settings_before_workflow(
    console_client: tuple[TestClient, RecordingWorkflow],
) -> None:
    client, workflow = console_client
    open_console(client)

    response = client.post(
        "/api/setup-and-scan",
        files={
            "settings": (None, "{}"),
            "resume": ("candidate.docx", b"uploaded resume bytes"),
        },
        headers=HEADERS,
    )

    assert response.status_code == 422
    assert workflow.runs == []


def test_setup_and_scan_rejects_missing_session(
    console_client: tuple[TestClient, RecordingWorkflow],
) -> None:
    client, workflow = console_client

    response = client.post(
        "/api/setup-and-scan",
        files={
            "settings": (None, settings_json()),
            "resume": ("candidate.docx", b"uploaded resume bytes"),
        },
        headers=HEADERS,
    )

    assert response.status_code == 403
    assert workflow.runs == []


def test_setup_and_scan_returns_conflict_when_web_workflow_is_busy(
    console_client: tuple[TestClient, RecordingWorkflow],
) -> None:
    client, workflow = console_client
    open_console(client)
    workflow.busy = True

    response = client.post(
        "/api/setup-and-scan",
        files={
            "settings": (None, settings_json()),
            "resume": ("candidate.docx", b"uploaded resume bytes"),
        },
        headers=HEADERS,
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "A setup and scan is already running."}


def test_delete_schedule_removes_owned_task(
    console_client: tuple[TestClient, RecordingWorkflow],
) -> None:
    client, workflow = console_client
    open_console(client)

    response = client.delete("/api/schedule", headers=HEADERS)

    assert response.status_code == 200
    assert response.json() == {"installed": False, "local_time": None}
    assert workflow.remove_count == 1


def test_schedule_status_returns_existing_owned_task(
    console_client: tuple[TestClient, RecordingWorkflow],
) -> None:
    client, workflow = console_client

    response = client.get("/api/schedule")

    assert response.status_code == 200
    assert response.json() == {"installed": True, "local_time": "08:30"}
    assert workflow.status_count == 1


def test_delete_schedule_returns_conflict_while_web_workflow_is_busy(
    console_client: tuple[TestClient, RecordingWorkflow],
) -> None:
    client, workflow = console_client
    open_console(client)
    workflow.busy = True

    response = client.delete("/api/schedule", headers=HEADERS)

    assert response.status_code == 409
    assert response.json() == {"detail": "A setup and scan is already running."}
