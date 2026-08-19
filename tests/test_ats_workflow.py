from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from threading import Event, Lock, Thread

import pytest
from pydantic import HttpUrl

from job_scan.ats_history import AtsHistoryStore
from job_scan.ats_models import (
    AtsCheckBundle,
    AtsFailure,
    AtsJobAssessment,
    AtsJobResult,
    AtsResumeAssessment,
    AtsResumeFinding,
    AtsRunState,
)
from job_scan.ats_service import (
    AtsCheckError,
    AtsCheckInput,
    AtsCheckService,
    AtsProgressUpdate,
)
from job_scan.ats_workflow import (
    AtsInputError,
    AtsInvalidJobSelection,
    AtsWorkflow,
    AtsWorkflowBusy,
    AtsWorkflowInput,
)
from job_scan.config import AppConfig, ClaudeSettings, SchedulerSettings
from job_scan.domain import AvailabilityStatus, JobRecord, MachineStatus
from job_scan.paths import AppPaths

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


def config() -> AppConfig:
    return AppConfig(
        candidate_name="Ada",
        ai_runtime="api:deepseek",
        ai_model="deepseek-chat",
        resume_path=Path("/current/Ada.pdf"),
        resume_sha256="sha256:" + ("a" * 64),
        profile_sha256="sha256:" + ("b" * 64),
        search_terms=["backend"],
        locations=["Berlin"],
        german_level="A2",
        claude=ClaudeSettings(
            model="sonnet",
            effort="medium",
            thinking_enabled=True,
            timeout_seconds=30,
            max_output_bytes=100_000,
        ),
        scheduler=SchedulerSettings(),
    )


def job(
    key: str,
    *,
    status: MachineStatus = MachineStatus.ELIGIBLE,
    source: str = "test",
) -> JobRecord:
    return JobRecord(
        canonical_job_key=key,
        primary_source_occurrence_key=f"{source}:{key}:1",
        company="Example GmbH",
        title=f"{key.title()} Engineer",
        location="Berlin",
        url=HttpUrl(f"https://example.test/jobs/{key}"),
        description=f"Complete JD for {key}",
        posted_at=date(2026, 8, 9),
        content_hash=f"sha256:{key}",
        first_seen=NOW,
        last_seen=NOW,
        availability_status=AvailabilityStatus.ACTIVE,
        machine_status=status,
        score=90,
        user_status_updated_at=NOW,
    )


def ats_input(
    *jobs: JobRecord,
    search_run_id: str = "global",
    candidate_name: str = "Ada Lovelace",
    resume_filename: str = "custom-resume.pdf",
    resume_bytes: bytes = b"CURRENT CUSTOM RESUME",
    config_value: AppConfig | None = None,
) -> AtsWorkflowInput:
    """Build caller-owned ATS input through its intended public API."""
    return AtsWorkflowInput(
        search_run_id=search_run_id,
        candidate_name=candidate_name,
        resume_filename=resume_filename,
        resume_bytes=resume_bytes,
        jobs=jobs,
        config=config_value or config(),
    )


def successful_bundle(
    inputs: AtsCheckInput,
    config_value: AppConfig,
    *,
    failed_job_key: str | None = None,
) -> AtsCheckBundle:
    return AtsCheckBundle(
        run_id=inputs.run_id,
        search_run_id=inputs.search_run_id,
        candidate_name=inputs.candidate_name,
        resume_filename=inputs.resume_filename,
        started_at=NOW,
        finished_at=NOW,
        ai_runtime=config_value.ai_runtime,
        ai_model=config_value.selected_model,
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
        jobs=[
            job_result(item, failed=item.canonical_job_key == failed_job_key)
            for item in inputs.jobs
        ],
    )


def job_result(item: JobRecord, *, failed: bool) -> AtsJobResult:
    if failed:
        return AtsJobResult(
            job_key=item.canonical_job_key,
            title=item.title,
            company=item.company,
            location=item.location,
            url=str(item.url),
            content_hash=item.content_hash,
            failure=AtsFailure(
                category="schema",
                message="AI check returned invalid structured output.",
            ),
        )
    return AtsJobResult(
        job_key=item.canonical_job_key,
        title=item.title,
        company=item.company,
        location=item.location,
        url=str(item.url),
        content_hash=item.content_hash,
        assessment=AtsJobAssessment(
            job_key=item.canonical_job_key,
            match_score=80,
            match_label="strong",
            required_skills_score=80,
            experience_score=80,
            keyword_score=80,
            matched=[],
            needs_attention=[],
            suggestions=[],
        ),
    )


class RecordingAtsService(AtsCheckService):
    def __init__(self, *, block: bool = False) -> None:
        self._block = block
        self._release = Event()
        self._started = Event()
        self._state_lock = Lock()
        self._progress: Callable[[AtsProgressUpdate], None] | None = None
        self._failure: BaseException | None = None
        self._failed_job_key: str | None = None
        self.received_inputs: AtsCheckInput | None = None
        self.received_config: AppConfig | None = None

    def check(
        self,
        inputs: AtsCheckInput,
        config_value: AppConfig,
        progress: Callable[[AtsProgressUpdate], None] | None = None,
    ) -> AtsCheckBundle:
        with self._state_lock:
            self._progress = progress
            self.received_inputs = inputs
            self.received_config = config_value
        self._started.set()
        if self._block and not self._release.wait(timeout=1.5):
            raise AssertionError("test ATS service was not released")
        if self._failure is not None:
            raise self._failure
        self._finish_progress(inputs)
        return successful_bundle(inputs, config_value, failed_job_key=self._failed_job_key)

    def emit(self, update: AtsProgressUpdate) -> None:
        assert self._started.wait(timeout=1.5), "ATS service did not start"
        with self._state_lock:
            progress = self._progress
        assert progress is not None, "ATS workflow did not supply a progress callback"
        progress(update)

    def finish_with_bundle(self, *, failed_job_key: str | None = None) -> None:
        self._failed_job_key = failed_job_key
        self._release.set()

    def fail_with(self, error: BaseException) -> None:
        self._failure = error
        self._release.set()

    def _finish_progress(self, inputs: AtsCheckInput) -> None:
        with self._state_lock:
            progress = self._progress
        if progress is None:
            return
        progress(AtsProgressUpdate("resume", "complete", "Resume complete."))
        for item in inputs.jobs:
            progress(AtsProgressUpdate(item.canonical_job_key, "running", "Checking job..."))
            progress(
                AtsProgressUpdate(
                    item.canonical_job_key,
                    "failed" if item.canonical_job_key == self._failed_job_key else "complete",
                    "AI check returned invalid structured output."
                    if item.canonical_job_key == self._failed_job_key
                    else "Job check complete.",
                )
            )


def wait_for_terminal(workflow: AtsWorkflow, run_id: str) -> AtsRunState:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        state = workflow.read_run(run_id)
        assert state is not None
        if state.status != "running":
            return state
        time.sleep(0.005)
    raise AssertionError("ATS workflow did not finish within 2 seconds")


def new_workflow(
    tmp_path: Path,
    *,
    block: bool = False,
) -> tuple[AtsWorkflow, RecordingAtsService, AtsHistoryStore]:
    paths = AppPaths.from_root(tmp_path / "home")
    service = RecordingAtsService(block=block)
    history = AtsHistoryStore(paths)
    return AtsWorkflow(service, history), service, history


def test_start_uses_caller_input_and_archives_its_actual_resume(tmp_path: Path) -> None:
    workflow, service, history = new_workflow(tmp_path, block=True)
    current_config = config().model_copy(update={"ai_model": "current-model"})
    inputs = ats_input(
        job("native-job", source="native"),
        job("cross-source-job", source="imported"),
        search_run_id="external-search",
        candidate_name="Grace Hopper",
        resume_filename="grace-current.pdf",
        resume_bytes=b"GRACE CURRENT RESUME",
        config_value=current_config,
    )

    state = workflow.start(inputs)
    service.finish_with_bundle()
    finished = wait_for_terminal(workflow, state.run_id)

    assert state.search_run_id == "external-search"
    assert [task.task_id for task in state.tasks] == ["resume", "native-job", "cross-source-job"]
    assert finished.status == "complete"
    assert service.received_inputs is not None
    assert service.received_inputs.candidate_name == "Grace Hopper"
    assert service.received_inputs.resume_filename == "grace-current.pdf"
    assert service.received_inputs.resume_bytes == b"GRACE CURRENT RESUME"
    assert [item.canonical_job_key for item in service.received_inputs.jobs] == [
        "native-job",
        "cross-source-job",
    ]
    assert service.received_config is current_config
    assert history.load(state.run_id).search_run_id == "external-search"
    assert history.read_resume(state.run_id) == ("grace-current.pdf", b"GRACE CURRENT RESUME")


@pytest.mark.parametrize(
    "inputs",
    [
        lambda: ats_input(search_run_id=""),
        lambda: ats_input(candidate_name=""),
        lambda: ats_input(resume_filename=""),
        lambda: ats_input(resume_bytes=b""),
    ],
)
def test_start_rejects_missing_required_caller_input(
    tmp_path: Path,
    inputs: Callable[[], AtsWorkflowInput],
) -> None:
    workflow, _service, _history = new_workflow(tmp_path)

    with pytest.raises(AtsInputError):
        workflow.start(inputs())

    assert not workflow.is_busy()


@pytest.mark.parametrize(
    "jobs",
    [(), (job("duplicate"), job("duplicate"))],
)
def test_start_rejects_empty_or_duplicate_caller_jobs(
    tmp_path: Path,
    jobs: tuple[JobRecord, ...],
) -> None:
    workflow, _service, _history = new_workflow(tmp_path)

    with pytest.raises(AtsInvalidJobSelection):
        workflow.start(ats_input(*jobs))

    assert not workflow.is_busy()


def test_job_updates_settle_and_partial_failures_are_archived(tmp_path: Path) -> None:
    workflow, service, history = new_workflow(tmp_path, block=True)
    state = workflow.start(ats_input(job("job-1"), job("job-2")))
    service.emit(AtsProgressUpdate("resume", "running", "Checking resume..."))
    running = workflow.read_run(state.run_id)
    assert running is not None
    assert [task.status for task in running.tasks] == ["running", "waiting", "waiting"]
    assert running.progress_percent == 0

    service.emit(AtsProgressUpdate("resume", "complete", "Resume complete."))
    resume_done = workflow.read_run(state.run_id)
    assert resume_done is not None
    assert [task.status for task in resume_done.tasks] == ["complete", "waiting", "waiting"]
    assert resume_done.stage == "jobs"
    assert resume_done.progress_percent == pytest.approx(100 / 3)

    service.emit(AtsProgressUpdate("job-1", "running", "Checking job..."))
    service.finish_with_bundle(failed_job_key="job-2")
    finished = wait_for_terminal(workflow, state.run_id)

    assert finished.status == "complete"
    assert finished.stage == "archive"
    assert finished.progress_percent == 100
    assert [task.status for task in finished.tasks] == ["complete", "complete", "failed"]
    assert finished.message == "ATS check complete with 1 failed jobs."
    assert history.load(state.run_id).failed_job_count == 1


def test_common_failure_marks_jobs_skipped_and_does_not_archive(tmp_path: Path) -> None:
    workflow, service, history = new_workflow(tmp_path, block=True)
    service.fail_with(AtsCheckError("Could not read the current resume."))
    state = workflow.start(ats_input(job("job-1"), job("job-2")))
    failed = wait_for_terminal(workflow, state.run_id)

    assert failed.status == "failed"
    assert failed.tasks[0].status == "failed"
    assert [task.status for task in failed.tasks[1:]] == ["skipped", "skipped"]
    assert failed.message == "Could not read the current resume."
    assert failed.error == "Could not read the current resume."
    assert failed.progress_percent == 100
    assert history.list() == []
    assert not workflow.is_busy()


def test_one_run_lock_rejects_overlap_then_releases_after_completion(tmp_path: Path) -> None:
    workflow, service, _history = new_workflow(tmp_path, block=True)
    inputs = ats_input(job("job-1"))
    first = workflow.start(inputs)

    assert workflow.is_busy()
    with pytest.raises(AtsWorkflowBusy):
        workflow.start(inputs)

    service.finish_with_bundle()
    wait_for_terminal(workflow, first.run_id)
    assert not workflow.is_busy()


def test_read_current_run_returns_only_the_active_run(tmp_path: Path) -> None:
    workflow, service, _history = new_workflow(tmp_path, block=True)
    started = workflow.start(ats_input(job("job-1")))

    assert workflow.read_current_run() == started
    service.finish_with_bundle()
    wait_for_terminal(workflow, started.run_id)
    assert workflow.read_current_run() is None


def test_unexpected_worker_failure_uses_safe_message(tmp_path: Path) -> None:
    workflow, service, history = new_workflow(tmp_path, block=True)
    service.fail_with(RuntimeError("CURRENT RESUME and JD leaked here"))
    state = workflow.start(ats_input(job("job-1")))
    failed = wait_for_terminal(workflow, state.run_id)

    assert failed.status == "failed"
    assert failed.message == "ATS check failed."
    assert failed.error == "ATS check failed."
    assert "CURRENT" not in failed.model_dump_json()
    assert history.list() == []


def test_returned_states_cannot_mutate_published_run_state(tmp_path: Path) -> None:
    workflow, service, _history = new_workflow(tmp_path, block=True)
    state = workflow.start(ats_input(job("job-1")))

    try:
        state.tasks.clear()
        first_read = workflow.read_run(state.run_id)
        assert first_read is not None
        assert [task.task_id for task in first_read.tasks] == ["resume", "job-1"]

        first_read.tasks.clear()
        second_read = workflow.read_run(state.run_id)
        assert second_read is not None
        assert [task.task_id for task in second_read.tasks] == ["resume", "job-1"]
    finally:
        service.finish_with_bundle()
        wait_for_terminal(workflow, state.run_id)


def test_concurrent_progress_updates_do_not_overwrite_other_tasks(tmp_path: Path) -> None:
    workflow, service, _history = new_workflow(tmp_path, block=True)
    state = workflow.start(ats_input(job("job-1"), job("job-2")))
    service.emit(AtsProgressUpdate("resume", "complete", "Resume complete."))
    updates = [
        AtsProgressUpdate("job-1", "complete", "Job one complete."),
        AtsProgressUpdate("job-2", "failed", "Job two failed."),
    ]
    threads = [Thread(target=service.emit, args=(update,)) for update in updates]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=1)
        assert not thread.is_alive()

    progress = workflow.read_run(state.run_id)
    assert progress is not None
    assert [task.status for task in progress.tasks] == ["complete", "complete", "failed"]
    assert progress.progress_percent == 100

    service.finish_with_bundle(failed_job_key="job-2")
    wait_for_terminal(workflow, state.run_id)
