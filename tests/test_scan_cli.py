from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from job_scan import cli as cli_module
from job_scan.cli import app
from job_scan.config import (
    AppConfig,
    ClaudeSettings,
    SchedulerSettings,
    load_config,
    save_config,
)
from job_scan.domain import Snapshot, StoreMeta
from job_scan.paths import AppPaths
from job_scan.scan_service import (
    ScanAlreadyRunning,
    ScanError,
    ScanProgress,
    ScanSummary,
    SourceProgress,
    read_scan_run_state,
)
from job_scan.search_history import SearchHistoryStore

NOW = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)


def summary(paths: AppPaths) -> ScanSummary:
    return ScanSummary(
        run_id="run-1",
        started_at=NOW,
        finished_at=NOW,
        source_counts={"arbeitsagentur:default": 1, "linkedin:acme/jobs": 3},
        source_errors=[],
        occurrence_count=4,
        new_count=1,
        changed_count=2,
        reviewed_count=3,
        eligible_count=2,
        excluded_count=1,
        uncertain_count=1,
        pending_count=2,
        source_error_count=2,
        claude_model="claude-sonnet-4-5",
        claude_batch_count=2,
        claude_budget_usd=Decimal("0.50"),
        claude_failure_count=1,
        claude_failure_counts={"missing": 1},
        jobs_jsonl=paths.jobs_jsonl,
        dashboard_html=paths.dashboard_html,
    )


class RecordingScanService:
    def __init__(
        self,
        paths: AppPaths,
        *,
        error: ScanError | None = None,
    ) -> None:
        self.paths = paths
        self.error = error
        self.force_review_calls: list[bool] = []

    def run(
        self,
        force_review: bool = False,
        *,
        on_published=None,
        progress=None,
        workflow_lock_held: bool = False,
    ) -> ScanSummary:
        self.force_review_calls.append(force_review)
        if self.error is not None:
            raise self.error
        if progress is not None:
            progress(
                ScanProgress(
                    stage="sources",
                    source_progress=SourceProgress(1, 4, 5, 0),
                )
            )
        result = summary(self.paths)
        if on_published is not None:
            assert workflow_lock_held is True
            on_published(result, Snapshot(meta=StoreMeta(data_revision=1)))
        return result


def install_factory(
    monkeypatch: pytest.MonkeyPatch,
    *,
    error: ScanError | None = None,
) -> list[RecordingScanService]:
    services: list[RecordingScanService] = []

    def factory(paths: AppPaths) -> RecordingScanService:
        service = RecordingScanService(paths, error=error)
        services.append(service)
        return service

    monkeypatch.setattr(cli_module, "_scan_service_factory", factory)
    return services


def test_scan_forwards_force_and_prints_counts_errors_then_output_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    services = install_factory(monkeypatch)
    root = tmp_path / "home"
    monkeypatch.setenv("JOB_SCAN_HOME", str(root))

    result = CliRunner().invoke(app, ["scan", "--force-review"])

    assert result.exit_code == 0, result.output
    assert len(services) == 1
    assert services[0].paths.root == root
    assert services[0].force_review_calls == [True]
    assert result.stdout.splitlines() == [
        "Source occurrences: 4",
        "New jobs: 1",
        "Changed jobs: 2",
        "Reviewed jobs: 3",
        "Eligible: 2",
        "Excluded: 1",
        "Uncertain: 1",
        "Pending: 2",
        "Source errors: 2",
        f"Jobs JSONL: {root / 'output' / 'jobs.jsonl'}",
        f"Dashboard: {root / 'output' / 'index.html'}",
    ]


def test_scan_lock_contention_exits_two_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    services = install_factory(
        monkeypatch, error=ScanAlreadyRunning("A scan is already running.")
    )
    monkeypatch.setenv("JOB_SCAN_HOME", str(tmp_path / "home"))

    result = CliRunner().invoke(app, ["scan"])

    assert services[0].force_review_calls == [False]
    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr.strip() == "Scan failed: A scan is already running."
    assert result.exception is not None


def test_scan_fatal_error_exits_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_factory(monkeypatch, error=ScanError("Could not publish scan results."))
    monkeypatch.setenv("JOB_SCAN_HOME", str(tmp_path / "home"))

    result = CliRunner().invoke(app, ["scan"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.strip() == "Scan failed: Could not publish scan results."


def test_scan_publishes_complete_state_for_the_task_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_factory(monkeypatch)
    root = tmp_path / "home"
    monkeypatch.setenv("JOB_SCAN_HOME", str(root))

    result = CliRunner().invoke(app, ["scan"])

    assert result.exit_code == 0, result.output
    state = read_scan_run_state(AppPaths.from_root(root))
    assert state is not None
    assert state.status == "complete"
    assert state.stage == "publish"
    assert state.message == "Review queue published."
    assert state.progress_percent == 100.0


def test_scan_failure_persists_failed_state_for_the_task_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_factory(monkeypatch, error=ScanError("Could not publish scan results."))
    root = tmp_path / "home"
    monkeypatch.setenv("JOB_SCAN_HOME", str(root))

    result = CliRunner().invoke(app, ["scan"])

    assert result.exit_code == 1
    state = read_scan_run_state(AppPaths.from_root(root))
    assert state is not None
    assert state.status == "failed"
    assert state.message == "Could not publish scan results."


def test_scan_contention_persists_no_task_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_factory(
        monkeypatch, error=ScanAlreadyRunning("A scan is already running.")
    )
    root = tmp_path / "home"
    monkeypatch.setenv("JOB_SCAN_HOME", str(root))

    result = CliRunner().invoke(app, ["scan"])

    assert result.exit_code == 2
    assert read_scan_run_state(AppPaths.from_root(root)) is None


def test_scheduled_scan_uses_the_saved_schedule_and_archives_search_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = install_factory(monkeypatch)
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    resume = paths.root / "resumes" / "scheduled.docx"
    resume.parent.mkdir()
    resume.write_bytes(b"scheduled resume")
    profile_bytes = b"# Scheduled A\n"
    config = AppConfig(
        candidate_name="Scheduled A",
        resume_path=resume,
        resume_sha256="sha256:" + "a" * 64,
        profile_sha256="sha256:" + hashlib.sha256(profile_bytes).hexdigest(),
        search_terms=["backend engineer"],
        locations=["Berlin"],
        german_level="B1",
        claude=ClaudeSettings(model="sonnet", effort="medium"),
        scheduler=SchedulerSettings(local_time="08:30"),
    )
    save_config(paths.scheduled_config_toml, config)
    paths.scheduled_profile_md.write_bytes(profile_bytes)
    monkeypatch.setenv("JOB_SCAN_HOME", str(paths.root))

    result = CliRunner().invoke(app, ["scan", "--scheduled"])

    assert result.exit_code == 0, result.output
    assert load_config(paths.config_toml).candidate_name == "Scheduled A"
    assert paths.profile_md.read_text(encoding="utf-8") == "# Scheduled A\n"
    history = SearchHistoryStore(paths)
    entries = history.list()
    assert len(entries) == 1
    assert entries[0].candidate_name == "Scheduled A"
    assert history.read_resume(entries[0].run_id)[1] == b"scheduled resume"
    assert services[0].force_review_calls == [False]
