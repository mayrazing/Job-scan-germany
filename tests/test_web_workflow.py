from __future__ import annotations

import hashlib
import json
import time
from datetime import date
from pathlib import Path
from threading import Event, Thread
from typing import Any

import pytest

from job_scan.claude_process import ClaudeInvocation, ClaudeRequest
from job_scan.company_size import CompanySizeProgress
from job_scan.config import ClaudeSettings, SchedulerSettings, load_config
from job_scan.domain import SourceKind
from job_scan.locking import FileRWLock
from job_scan.normalization import content_hash
from job_scan.paths import AppPaths
from job_scan.resume import ResumeError
from job_scan.reviewer import ReviewBatchOutcome, ReviewBatchProgress
from job_scan.scan_service import ScanError, ScanProgress, ScanService, SourceProgress
from job_scan.scheduler import BackendName, SchedulerState
from job_scan.setup_service import SetupAnswers, SetupService
from job_scan.sources.base import FetchedOccurrence, JobReference
from job_scan.web_workflow import (
    WebWorkflow,
    WebWorkflowBusy,
    _progress_message,
    _progress_percent,
)

RESUME = Path(__file__).parent / "fixtures" / "resume" / "sample.docx"
PROFILE = """# Target roles
Backend Engineer

# Technical skills
Python, SQL

# Experience
Backend delivery

# Languages
English, German B1

# Work authorization and visa
Needs visa sponsorship

# Preferences
Berlin or remote
"""


class FakeClaude:
    def invoke(self, request: ClaudeRequest) -> ClaudeInvocation:
        del request
        payload = {"structured_output": {"profile_markdown": PROFILE}}
        return ClaudeInvocation(
            argv=["explicit-local-fake"],
            stdout=json.dumps(payload).encode(),
            stderr=b"",
            exit_code=0,
            duration_seconds=0.01,
        )


class FakeScheduler:
    backend: BackendName = "cron"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def install(self, config: Any, paths: AppPaths, executable: Path) -> SchedulerState:
        del paths
        self.calls.append("install")
        return SchedulerState(
            backend=self.backend,
            installed=True,
            local_time=config.scheduler.local_time,
            executable=executable,
            managed_location="fixture:scheduler",
        )

    def remove(self, paths: AppPaths) -> SchedulerState:
        del paths
        self.calls.append("remove")
        return SchedulerState(
            backend=self.backend,
            installed=False,
            local_time=None,
            executable=None,
            managed_location="fixture:scheduler",
        )

    def status(self, paths: AppPaths) -> SchedulerState:
        del paths
        raise AssertionError("status is not used by this workflow")


def answers(
    *,
    local_time: str | None = "08:30",
    batch_size: int = 10,
) -> SetupAnswers:
    return SetupAnswers(
        candidate_name="Ada Lovelace",
        search_terms=["Backend Engineer"],
        locations=["Berlin"],
        linkedin_limit=50,
        indeed_de_limit=35,
        stepstone_de_limit=27,
        glassdoor_de_limit=38,
        simplify_de_limit=41,
        german_level="B1",
        staffing_penalty=10,
        claude=ClaudeSettings(model="sonnet", effort="medium", batch_size=batch_size),
        scheduler=SchedulerSettings(local_time=local_time),
    )


def workflow_at(tmp_path: Path) -> tuple[WebWorkflow, AppPaths, FakeScheduler]:
    paths = AppPaths.from_root(tmp_path / "home")
    scheduler = FakeScheduler()
    workflow = WebWorkflow(
        paths,
        setup_service=SetupService(paths, claude=FakeClaude()),
        scan_service=ScanService(paths, source_factory=lambda config: []),
        scheduler=scheduler,
        executable=Path("/opt/job-scan/bin/job-scan"),
    )
    return workflow, paths, scheduler


def test_run_persists_uploaded_resume_then_runs_real_setup_and_scan(
    tmp_path: Path,
) -> None:
    workflow, paths, scheduler = workflow_at(tmp_path)
    resume_bytes = RESUME.read_bytes()

    result = workflow.run("candidate.docx", resume_bytes, answers())

    digest = hashlib.sha256(resume_bytes).hexdigest()
    saved_resume = paths.root / "resumes" / f"{digest}.docx"
    assert saved_resume.read_bytes() == resume_bytes
    config = load_config(paths.config_toml)
    assert config.candidate_name == "candidate"
    assert config.resume_path == saved_resume.resolve()
    assert config.search_terms == ["Backend Engineer"]
    assert config.linkedin_limit == 50
    assert config.indeed_de_limit == 35
    assert config.stepstone_de_limit == 27
    assert config.glassdoor_de_limit == 38
    assert config.simplify_de_limit == 41
    assert result.summary.occurrence_count == 0
    assert result.summary.reviewed_count == 0
    assert result.schedule.installed is True
    assert result.schedule.local_time == "08:30"
    assert scheduler.calls == ["install"]


def test_successful_browser_run_is_archived_as_one_search_history(
    tmp_path: Path,
) -> None:
    from job_scan.search_history import SearchHistoryStore

    workflow, paths, _scheduler = workflow_at(tmp_path)

    result = workflow.run("Ada original.docx", RESUME.read_bytes(), answers())

    history = SearchHistoryStore(paths)
    entries = history.list()
    assert len(entries) == 1
    assert entries[0].run_id == result.summary.run_id
    assert entries[0].candidate_name == "Ada original"
    assert entries[0].resume_filename == "Ada original.docx"
    assert history.load(entries[0].run_id).jobs == []
    assert history.read_resume(entries[0].run_id)[1] == RESUME.read_bytes()


def test_archive_failure_restores_setup_files_and_removes_new_resume(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")

    class FailingHistory:
        def archive(self, **_kwargs: object) -> None:
            raise OSError("history disk failed")

    workflow = WebWorkflow(
        paths,
        setup_service=SetupService(paths, claude=FakeClaude()),
        scan_service=ScanService(paths, source_factory=lambda config: []),
        scheduler=FakeScheduler(),
        executable=Path("/opt/job-scan/bin/job-scan"),
        history_store=FailingHistory(),  # type: ignore[arg-type]
    )

    with pytest.raises(ScanError, match="finalize"):
        workflow.run("candidate.docx", RESUME.read_bytes(), answers())

    assert not paths.config_toml.exists()
    assert not paths.profile_md.exists()
    assert list((paths.root / "resumes").glob("*")) == []


def test_load_setup_answers_returns_the_last_saved_editable_values(
    tmp_path: Path,
) -> None:
    workflow, _paths, _scheduler = workflow_at(tmp_path)
    saved_answers = answers(local_time="06:45").model_copy(
        update={
            "search_terms": ["Site Reliability Engineer"],
            "locations": ["Munich"],
            "linkedin_limit": 17,
            "indeed_de_limit": 23,
            "stepstone_de_limit": 29,
            "glassdoor_de_limit": 31,
            "simplify_de_limit": 37,
            "german_level": "C1",
            "staffing_penalty": 23,
        }
    )
    workflow.run("candidate.docx", RESUME.read_bytes(), saved_answers)

    assert workflow.load_setup_answers() == saved_answers.model_copy(
        update={"candidate_name": "candidate"}
    )


@pytest.mark.parametrize("config_state", ["missing", "invalid"])
def test_load_setup_answers_falls_back_when_config_is_unavailable(
    tmp_path: Path,
    config_state: str,
) -> None:
    workflow, paths, _scheduler = workflow_at(tmp_path)
    if config_state == "invalid":
        paths.root.mkdir(parents=True, exist_ok=True)
        paths.config_toml.write_text("invalid = [", encoding="utf-8")

    assert workflow.load_setup_answers() is None


def test_remove_schedule_clears_native_entry_and_saved_time(tmp_path: Path) -> None:
    workflow, paths, scheduler = workflow_at(tmp_path)
    workflow.run("candidate.docx", RESUME.read_bytes(), answers())

    state = workflow.remove_schedule()

    assert state.installed is False
    assert load_config(paths.config_toml).scheduler.local_time is None
    assert scheduler.calls == ["install", "remove"]


def test_failed_setup_removes_new_uploaded_resume(tmp_path: Path) -> None:
    workflow, paths, _scheduler = workflow_at(tmp_path)

    with pytest.raises(ResumeError):
        workflow.run("candidate.docx", b"not a docx", answers(local_time=None))

    assert not list((paths.root / "resumes").glob("*"))


def test_second_web_run_is_rejected_while_first_setup_is_active(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    entered = Event()
    release = Event()
    real_setup = SetupService(paths, claude=FakeClaude())

    class BlockingSetup:
        def run(self, resume_path: Path, setup_answers: SetupAnswers):
            entered.set()
            if not release.wait(timeout=5):
                raise AssertionError("test did not release setup")
            return real_setup.run(resume_path, setup_answers)

    workflow = WebWorkflow(
        paths,
        setup_service=BlockingSetup(),  # type: ignore[arg-type]
        scan_service=ScanService(paths, source_factory=lambda config: []),
        scheduler=FakeScheduler(),
        executable=Path("/opt/job-scan/bin/job-scan"),
    )
    first_errors: list[Exception] = []

    def run_first() -> None:
        try:
            workflow.run("candidate.docx", RESUME.read_bytes(), answers())
        except (RuntimeError, AssertionError) as error:
            first_errors.append(error)

    thread = Thread(target=run_first)
    thread.start()
    assert entered.wait(timeout=5)

    with pytest.raises(WebWorkflowBusy, match="already running"):
        workflow.run("candidate.docx", RESUME.read_bytes(), answers())
    with pytest.raises(WebWorkflowBusy, match="already running"):
        workflow.remove_schedule()

    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert first_errors == []


def test_web_run_is_rejected_before_setup_when_another_process_owns_workflow_lock(
    tmp_path: Path,
) -> None:
    workflow, paths, _scheduler = workflow_at(tmp_path)

    with (
        FileRWLock(paths.workflow_lock_file).exclusive(),
        pytest.raises(WebWorkflowBusy, match="setup or scan"),
    ):
        workflow.run("candidate.docx", RESUME.read_bytes(), answers())

    assert not paths.config_toml.exists()
    assert not paths.profile_md.exists()


def test_background_run_returns_immediately_and_exposes_real_stage_progress(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    setup_entered = Event()
    release_setup = Event()
    source_entered = Event()
    release_source = Event()
    real_setup = SetupService(paths, claude=FakeClaude())

    class BlockingSetup:
        def run(self, resume_path: Path, setup_answers: SetupAnswers):
            setup_entered.set()
            if not release_setup.wait(timeout=5):
                raise AssertionError("test did not release setup")
            return real_setup.run(resume_path, setup_answers)

    class BlockingSource:
        source = SourceKind.LINKEDIN
        source_instance = "blocking"

        def discover(self):
            source_entered.set()
            if not release_source.wait(timeout=5):
                raise AssertionError("test did not release source")
            return []

        def fetch_detail(self, reference):
            raise AssertionError(f"unexpected detail fetch: {reference}")

    workflow = WebWorkflow(
        paths,
        setup_service=BlockingSetup(),  # type: ignore[arg-type]
        scan_service=ScanService(
            paths,
            source_factory=lambda _config: [BlockingSource()],
        ),
        scheduler=FakeScheduler(),
        executable=Path("/opt/job-scan/bin/job-scan"),
    )

    started = workflow.start("candidate.docx", RESUME.read_bytes(), answers())

    assert started.status == "running"
    assert workflow.read_current_run() == started
    assert setup_entered.wait(timeout=5)
    assert workflow.read_run(started.run_id).stage == "profile"

    release_setup.set()
    assert source_entered.wait(timeout=5)
    current = workflow.read_run(started.run_id)
    assert current.stage == "sources"
    assert current.source_progress == SourceProgress(0, 1, 0, 0)

    release_source.set()
    for _attempt in range(100):
        current = workflow.read_run(started.run_id)
        if current.status == "complete":
            break
        time.sleep(0.01)

    assert current.status == "complete"
    assert current.stage == "publish"
    assert current.result is not None
    assert current.result.summary.occurrence_count == 0
    assert workflow.read_current_run() is None


def test_started_run_captures_submitted_ai_runtime(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    setup_entered = Event()
    release_setup = Event()

    class BlockingSetup:
        def run(self, _resume_path: Path, _setup_answers: SetupAnswers):
            setup_entered.set()
            if not release_setup.wait(timeout=5):
                raise AssertionError("test did not release setup")
            raise RuntimeError("stop after observing initial state")

    workflow = WebWorkflow(
        paths,
        setup_service=BlockingSetup(),  # type: ignore[arg-type]
        scan_service=ScanService(paths, source_factory=lambda _config: []),
        scheduler=FakeScheduler(),
        executable=Path("/opt/job-scan/bin/job-scan"),
    )
    submitted = answers().model_copy(update={"ai_runtime": "api:deepseek"})

    started = workflow.start("candidate.docx", RESUME.read_bytes(), submitted)

    assert setup_entered.wait(timeout=5)
    assert started.ai_runtime == "api:deepseek"
    assert workflow.read_current_run().ai_runtime == "api:deepseek"
    release_setup.set()
    for _attempt in range(100):
        if workflow.read_run(started.run_id).status == "failed":
            break
        time.sleep(0.01)
    assert workflow.read_run(started.run_id).status == "failed"


def test_background_run_exposes_completed_review_batch_progress(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    review_entered = Event()
    release_review = Event()
    occurrences = [
        FetchedOccurrence(
            source=SourceKind.LINKEDIN,
            source_instance="progress",
            external_id=f"job-{index}",
            url=f"https://example.test/jobs/{index}",
            company="Acme",
            title=f"Backend Engineer {index}",
            location="Berlin",
            description="Complete backend job description.",
            posted_at=date(2026, 8, 5),
            content_hash=content_hash(
                "Acme",
                f"Backend Engineer {index}",
                "Berlin",
                "Complete backend job description.",
            ),
            detail_complete=True,
        )
        for index in range(2)
    ]

    class ReviewSource:
        source = SourceKind.LINKEDIN
        source_instance = "progress"

        def discover(self):
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
                for item in occurrences
            ]

        def fetch_detail(self, reference):
            return next(
                item for item in occurrences if item.external_id == reference.external_id
            )

    class BlockingReviewer:
        def review(self, jobs, profile, config, *, progress=None):
            del profile, config
            assert len(jobs) == 2
            assert progress is not None
            progress(ReviewBatchProgress(1, 2, 1, 2))
            review_entered.set()
            if not release_review.wait(timeout=5):
                raise AssertionError("test did not release review")
            progress(ReviewBatchProgress(2, 2, 2, 2))
            return ReviewBatchOutcome(accepted={}, failed={}, invocations=[])

    workflow = WebWorkflow(
        paths,
        setup_service=SetupService(paths, claude=FakeClaude()),
        scan_service=ScanService(
            paths,
            reviewer=BlockingReviewer(),  # type: ignore[arg-type]
            source_factory=lambda _config: [ReviewSource()],
        ),
        scheduler=FakeScheduler(),
        executable=Path("/opt/job-scan/bin/job-scan"),
    )

    started = workflow.start(
        "candidate.docx",
        RESUME.read_bytes(),
        answers(batch_size=1),
    )

    assert review_entered.wait(timeout=5)
    current = workflow.read_run(started.run_id)
    assert current is not None
    assert current.status == "running"
    assert current.stage == "review"
    assert current.progress_percent == 85
    assert current.message == (
        "Reviewing complete job descriptions: 1/2 batches, 1/2 jobs..."
    )
    assert current.review_progress == ReviewBatchProgress(1, 2, 1, 2)

    release_review.set()
    for _attempt in range(100):
        current = workflow.read_run(started.run_id)
        if current is not None and current.status == "complete":
            break
        time.sleep(0.01)
    assert current is not None
    assert current.status == "complete"


def test_background_run_exposes_company_size_progress(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    company_size_entered = Event()
    release_company_size = Event()

    class BlockingCompanySizeService:
        def restore(self, _snapshot, _config):
            return None

        def apply(self, _snapshot, _config, _checked_at, *, progress=None):
            company_size_entered.set()
            assert progress is not None
            progress(CompanySizeProgress(1, 4))
            if not release_company_size.wait(timeout=5):
                raise AssertionError("test did not release company-size lookup")
            progress(CompanySizeProgress(4, 4))

    workflow = WebWorkflow(
        paths,
        setup_service=SetupService(paths, claude=FakeClaude()),
        scan_service=ScanService(
            paths,
            company_size_service=BlockingCompanySizeService(),  # type: ignore[arg-type]
            source_factory=lambda _config: [],
        ),
        scheduler=FakeScheduler(),
        executable=Path("/opt/job-scan/bin/job-scan"),
    )

    started = workflow.start("candidate.docx", RESUME.read_bytes(), answers())

    assert company_size_entered.wait(timeout=5)
    current = workflow.read_run(started.run_id)
    assert current is not None
    assert current.status == "running"
    assert current.stage == "company_size"
    assert current.progress_percent == 96
    assert current.message == "Checking company sizes: 1/4 companies..."
    assert current.company_size_progress == CompanySizeProgress(1, 4)

    release_company_size.set()
    for _attempt in range(100):
        current = workflow.read_run(started.run_id)
        if current is not None and current.status == "complete":
            break
        time.sleep(0.01)
    assert current is not None
    assert current.status == "complete"


def test_source_percent_moves_with_completed_sources() -> None:
    current = ScanProgress(
        stage="sources",
        source_progress=SourceProgress(2, 4, 17, 1),
    )

    assert _progress_percent(current) == 55


def test_source_message_reports_completed_sources_and_found_jobs() -> None:
    current = ScanProgress(
        stage="sources",
        source_progress=SourceProgress(2, 4, 17, 1),
    )

    assert _progress_message(current) == (
        "Searching job sources: 2/4 sources, 17 jobs found, 1 warning..."
    )


def test_background_run_records_safe_failure_instead_of_staying_running(
    tmp_path: Path,
) -> None:
    workflow, _paths, _scheduler = workflow_at(tmp_path)

    started = workflow.start("candidate.docx", b"not a docx", answers())

    for _attempt in range(100):
        current = workflow.read_run(started.run_id)
        if current.status == "failed":
            break
        time.sleep(0.01)

    assert current.status == "failed"
    assert current.error == "Could not read uploaded resume."
    assert current.result is None
