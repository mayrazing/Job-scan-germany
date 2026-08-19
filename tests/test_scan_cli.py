from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from job_scan import cli as cli_module
from job_scan.cli import app
from job_scan.paths import AppPaths
from job_scan.scan_service import ScanAlreadyRunning, ScanError, ScanSummary

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

    def run(self, force_review: bool = False) -> ScanSummary:
        self.force_review_calls.append(force_review)
        if self.error is not None:
            raise self.error
        return summary(self.paths)


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
