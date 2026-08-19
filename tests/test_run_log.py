from __future__ import annotations

import hashlib
import json
import stat
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from job_scan import cli
from job_scan import doctor as doctor_module
from job_scan.config import AppConfig, ClaudeSettings, SchedulerSettings, save_config
from job_scan.domain import SourceKind
from job_scan.paths import AppPaths
from job_scan.run_log import RunLogger
from job_scan.scan_service import ScanService, ScanSummary
from job_scan.sources.base import FetchedOccurrence, JobReference, SourceError


def _minimal_summary(tmp_path: Path) -> ScanSummary:
    return ScanSummary(
        run_id="run-minimal",
        started_at=datetime(2026, 8, 3, 9, 0, tzinfo=UTC),
        finished_at=datetime(2026, 8, 3, 9, 1, tzinfo=UTC),
        source_counts={},
        source_errors=[],
        occurrence_count=0,
        new_count=0,
        changed_count=0,
        reviewed_count=0,
        eligible_count=0,
        excluded_count=0,
        uncertain_count=0,
        pending_count=0,
        source_error_count=0,
        claude_model="claude-sonnet-4-5",
        claude_batch_count=0,
        claude_budget_usd=Decimal(0),
        claude_failure_count=0,
        claude_failure_counts={},
        jobs_jsonl=tmp_path / "output" / "jobs.jsonl",
        dashboard_html=tmp_path / "output" / "index.html",
    )


def _ready_paths(tmp_path: Path) -> AppPaths:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    profile = "# Synthetic profile\n"
    resume_path = paths.root / "resume.pdf"
    resume_path.write_bytes(b"synthetic resume")
    paths.profile_md.write_text(profile, encoding="utf-8")
    save_config(
        paths.config_toml,
        AppConfig(
            resume_path=resume_path,
            resume_sha256="sha256:" + "a" * 64,
            profile_sha256=f"sha256:{hashlib.sha256(profile.encode()).hexdigest()}",
            search_terms=["backend"],
            locations=["Berlin"],
            german_level="B1",
            claude=ClaudeSettings(
                model="claude-sonnet-4-5",
                effort="medium",
            ),
            scheduler=SchedulerSettings(local_time="09:00"),
        ),
    )
    return paths


class PartialAdapter:
    source = SourceKind.GLASSDOOR
    source_instance = "Careers.Example.COM"

    def discover(self) -> list[JobReference]:
        raise TimeoutError("PRIVATE FULL JD FROM SOURCE EXCEPTION")

    def fetch_detail(self, reference: JobReference) -> FetchedOccurrence:
        raise AssertionError(f"unexpected fetch for {reference.external_id}")


class FailingRunLogger(RunLogger):
    def write(self, summary: ScanSummary) -> Path:
        raise OSError(f"disk unavailable for {summary.run_id}")


class HealthyClaude:
    def version(self) -> str:
        return "2.1.7 (Claude Code)"

    def auth_status(self) -> dict[str, str]:
        return {"token": "PRIVATE_AUTH_JSON"}


def test_scan_run_log_privacy_whitelist_excludes_sensitive_exception_text(
    tmp_path: Path,
) -> None:
    resume_text = "PRIVATE RESUME: name, address, employment history"
    full_jd = "PRIVATE FULL JD: internal hiring details"
    prompt = "PRIVATE PROMPT: score this candidate"
    claude_stdout = '{"structured_output":{"secret":"PRIVATE CLAUDE STDOUT"}}'
    authentication_json = '{"account":"candidate@example.com","token":"secret"}'
    summary = ScanSummary(
        run_id="run-private",
        started_at=datetime(2026, 8, 3, 11, 0, tzinfo=timezone(timedelta(hours=2))),
        finished_at=datetime(2026, 8, 3, 11, 5, tzinfo=timezone(timedelta(hours=2))),
        source_counts={"linkedin:acme/jobs": 4},
        source_errors=[
            SourceError(
                category="http",
                source=SourceKind.LINKEDIN,
                source_instance="Careers.Example.COM/private-path",
                item_key=f"linkedin:acme/jobs:{full_jd}",
                status_code=503,
                message=(
                    f"{resume_text} | {full_jd} | {prompt} | "
                    f"{claude_stdout} | {authentication_json}"
                ),
            )
        ],
        occurrence_count=4,
        new_count=2,
        changed_count=1,
        reviewed_count=3,
        eligible_count=2,
        excluded_count=1,
        uncertain_count=0,
        pending_count=1,
        source_error_count=1,
        claude_model="claude-sonnet-4-5",
        claude_batch_count=2,
        claude_budget_usd=Decimal("0.42"),
        claude_failure_count=1,
        claude_failure_counts={"timeout": 1},
        jobs_jsonl=tmp_path / "output" / "jobs.jsonl",
        dashboard_html=tmp_path / "output" / "index.html",
    )

    log_path = RunLogger(tmp_path).write(summary)

    assert log_path == tmp_path / "scan.jsonl"
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == {
        "run_id": "run-private",
        "started_at": "2026-08-03T09:00:00Z",
        "finished_at": "2026-08-03T09:05:00Z",
        "source_counts": {"linkedin:acme/jobs": 4},
        "source_errors": [
            {
                "category": "http",
                "host": "careers.example.com",
                "status_code": 503,
                "error_code": "http_503",
            }
        ],
        "new_count": 2,
        "changed_count": 1,
        "reviewed_count": 3,
        "excluded_count": 1,
        "pending_count": 1,
        "claude_model": "claude-sonnet-4-5",
        "claude_batch_count": 2,
        "claude_budget_usd": "0.42",
        "claude_failure_counts": {"timeout": 1},
        "jobs_jsonl": str(tmp_path / "output" / "jobs.jsonl"),
        "dashboard_html": str(tmp_path / "output" / "index.html"),
    }
    for private_value in (
        resume_text,
        full_jd,
        prompt,
        claude_stdout,
        authentication_json,
    ):
        assert private_value not in lines[0]


def test_scan_run_log_preserves_program_owned_browser_error_code(tmp_path: Path) -> None:
    summary = _minimal_summary(tmp_path)
    summary.source_errors = [
        SourceError(
            category="browser",
            source=SourceKind.LINKEDIN,
            source_instance="default",
            error_code="linkedin_auth_required",
            message="private browser detail",
        )
    ]
    summary.source_error_count = 1

    log_path = RunLogger(tmp_path).write(summary)
    payload = json.loads(log_path.read_text(encoding="utf-8"))

    assert payload["source_errors"] == [
        {
            "category": "browser",
            "host": "default",
            "status_code": None,
            "error_code": "linkedin_auth_required",
        }
    ]


def test_scan_run_log_appends_without_replacing_existing_records(tmp_path: Path) -> None:
    logger = RunLogger(tmp_path)
    summary = _minimal_summary(tmp_path)

    logger.write(summary)
    logger.write(summary.model_copy(update={"run_id": "run-second"}))

    records = [
        json.loads(line)
        for line in (tmp_path / "scan.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [record["run_id"] for record in records] == ["run-minimal", "run-second"]


def test_scan_run_log_has_owner_read_write_mode(tmp_path: Path) -> None:
    log_path = RunLogger(tmp_path).write(_minimal_summary(tmp_path))

    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600


def test_partial_scan_writes_run_log_after_publishing_snapshot(tmp_path: Path) -> None:
    paths = _ready_paths(tmp_path)
    times = iter(
        [
            datetime(2026, 8, 3, 9, 0, tzinfo=UTC),
            datetime(2026, 8, 3, 9, 1, tzinfo=UTC),
        ]
    )
    service = ScanService(
        paths,
        source_factory=lambda _config: [PartialAdapter()],
        clock=lambda: next(times),
        run_id_factory=lambda: "run-partial",
    )

    summary = service.run()

    assert paths.jobs_jsonl.is_file()
    assert paths.dashboard_html.is_file()
    record = json.loads((paths.logs_dir / "scan.jsonl").read_text(encoding="utf-8"))
    assert summary.run_id == "run-partial"
    assert record["run_id"] == "run-partial"
    assert record["source_errors"] == [
        {
            "category": "http",
            "host": "careers.example.com",
            "status_code": None,
            "error_code": "timeout",
        }
    ]


def test_scan_logging_failure_warns_without_rolling_back_published_snapshot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = _ready_paths(tmp_path)
    service = ScanService(
        paths,
        source_factory=lambda _config: [],
        clock=lambda: datetime(2026, 8, 3, 9, 0, tzinfo=UTC),
        run_id_factory=lambda: "run-log-failure",
        run_logger=FailingRunLogger(paths.logs_dir),
    )

    summary = service.run()

    assert summary.run_id == "run-log-failure"
    assert paths.jobs_jsonl.is_file()
    assert paths.dashboard_html.is_file()
    assert capsys.readouterr().err == "Warning: Could not write scan log.\n"


def test_doctor_log_is_written_only_for_explicit_log_option(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _ready_paths(tmp_path)
    monkeypatch.setenv("JOB_SCAN_HOME", str(paths.root))
    monkeypatch.setattr(doctor_module, "ClaudeProcess", HealthyClaude)

    normal = CliRunner().invoke(cli.app, ["doctor"])

    assert normal.exit_code == 0, normal.output
    assert not (paths.logs_dir / "doctor.jsonl").exists()

    logged = CliRunner().invoke(cli.app, ["doctor", "--log"])

    assert logged.exit_code == 0, logged.output
    log_path = paths.logs_dir / "doctor.jsonl"
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
    record = json.loads(log_path.read_text(encoding="utf-8"))
    assert [check["name"] for check in record["checks"]] == [
        "data_directories",
        "config",
        "germany_only",
        "profile",
        "original_resume",
        "claude_version",
        "claude_auth",
        "jobsuche_adapter",
        "scheduler",
    ]
    assert all(set(check) == {"name", "status"} for check in record["checks"])
    assert "PRIVATE_AUTH_JSON" not in log_path.read_text(encoding="utf-8")
