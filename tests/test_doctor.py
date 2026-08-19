from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Literal

import pytest
from typer.testing import CliRunner

from job_scan import cli
from job_scan.ai_config import AiProviderDraft, AiProviderStore
from job_scan.claude_process import ClaudeAuthStatus, ClaudeProcessError
from job_scan.config import (
    AppConfig,
    ClaudeSettings,
    SchedulerSettings,
    load_config,
    save_config,
)
from job_scan.doctor import DoctorCheck, DoctorReport
from job_scan.paths import AppPaths

PROFILE = "# Candidate profile\n\nSynthetic facts only.\n"
PROFILE_HASH = f"sha256:{hashlib.sha256(PROFILE.encode()).hexdigest()}"


class HealthyClaude:
    def version(self) -> str:
        return "2.1.7 (Claude Code)"

    def auth_status(self) -> ClaudeAuthStatus:
        return ClaudeAuthStatus(authenticated=True, account_label="test")


def _config(resume_path: Path) -> AppConfig:
    return AppConfig(
        resume_path=resume_path,
        resume_sha256="sha256:" + "a" * 64,
        profile_sha256=PROFILE_HASH,
        search_terms=["backend engineer"],
        locations=["Berlin"],
        german_level="B1",
        needs_visa_sponsorship=True,
        claude=ClaudeSettings(
            model="sonnet",
            effort="medium",
        ),
        scheduler=SchedulerSettings(local_time="08:30"),
    )


def _ready_paths(tmp_path: Path, *, resume_exists: bool = True) -> AppPaths:
    paths = AppPaths.from_root(tmp_path / "job-scan-home")
    paths.ensure_directories()
    resume_path = tmp_path / "resume.pdf"
    if resume_exists:
        resume_path.write_bytes(b"synthetic resume")
    save_config(paths.config_toml, _config(resume_path))
    paths.profile_md.write_text(PROFILE, encoding="utf-8")
    return paths


def _checks_by_name(report: DoctorReport) -> dict[str, DoctorCheck]:
    return {check.name: check for check in report.checks}


def test_doctor_reports_every_named_check_as_an_independent_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from job_scan import doctor

    paths = _ready_paths(tmp_path)
    monkeypatch.setattr(doctor, "ClaudeProcess", HealthyClaude)

    report = doctor.run_doctor(paths)

    assert [check.name for check in report.checks] == [
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
    assert [check.status for check in report.checks] == ["ok"] * 9
    assert report.has_errors is False


def test_doctor_keeps_germany_check_visible_when_typed_config_is_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from job_scan import doctor

    paths = _ready_paths(tmp_path)
    raw = paths.config_toml.read_text(encoding="utf-8")
    paths.config_toml.write_text(
        raw.replace('country = "DE"', 'country = "US"'), encoding="utf-8"
    )
    monkeypatch.setattr(doctor, "ClaudeProcess", HealthyClaude)

    checks = _checks_by_name(doctor.run_doctor(paths))

    assert checks["config"].status == "error"
    assert checks["germany_only"].status == "error"
    assert checks["claude_version"].status == "ok"
    assert checks["claude_auth"].status == "ok"


def test_doctor_warns_for_missing_resume_when_profile_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from job_scan import doctor

    paths = _ready_paths(tmp_path, resume_exists=False)
    monkeypatch.setattr(doctor, "ClaudeProcess", HealthyClaude)

    checks = _checks_by_name(doctor.run_doctor(paths))

    assert checks["profile"].status == "ok"
    assert checks["original_resume"].status == "warning"
    assert doctor.run_doctor(paths).has_errors is False


@pytest.mark.parametrize("profile_state", ["missing", "hash_mismatch", "invalid_utf8"])
def test_doctor_treats_missing_or_invalid_profile_as_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile_state: str,
) -> None:
    from job_scan import doctor

    paths = _ready_paths(tmp_path)
    if profile_state == "missing":
        paths.profile_md.unlink()
    elif profile_state == "hash_mismatch":
        paths.profile_md.write_text("changed", encoding="utf-8")
    else:
        paths.profile_md.write_bytes(b"\xff")
    monkeypatch.setattr(doctor, "ClaudeProcess", HealthyClaude)

    report = doctor.run_doctor(paths)

    assert _checks_by_name(report)["profile"].status == "error"
    assert report.has_errors is True


def test_doctor_runs_claude_auth_even_when_version_check_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from job_scan import doctor

    class VersionFailureClaude(HealthyClaude):
        def version(self) -> str:
            raise ClaudeProcessError("safe version failure")

    paths = _ready_paths(tmp_path)
    monkeypatch.setattr(doctor, "ClaudeProcess", VersionFailureClaude)

    checks = _checks_by_name(doctor.run_doctor(paths))

    assert checks["claude_version"].status == "error"
    assert checks["claude_auth"].status == "ok"


def test_doctor_checks_saved_api_runtime_without_calling_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from job_scan import doctor

    class ClaudeMustNotRun:
        def __init__(self) -> None:
            raise AssertionError("Claude Code check ran for API runtime")

    paths = _ready_paths(tmp_path)
    store = AiProviderStore(paths.ai_config_toml)
    saved = store.create(
        AiProviderDraft(
            display_name="DeepSeek",
            base_url="https://api.example.com/anthropic",
            api_key="sk-test",
            model="deepseek-chat",
            reasoning_effort="low",
        )
    )
    config = _config(Path(load_config(paths.config_toml).resume_path))
    config = config.model_copy(update={"ai_runtime": f"api:{saved.id}"})
    save_config(paths.config_toml, config)
    monkeypatch.setattr(doctor, "ClaudeProcess", ClaudeMustNotRun)

    checks = _checks_by_name(doctor.run_doctor(paths))

    assert checks["claude_version"].status == "ok"
    assert checks["claude_auth"].status == "ok"


def test_unsupported_scheduler_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from job_scan import doctor

    paths = _ready_paths(tmp_path)
    monkeypatch.setattr(doctor, "ClaudeProcess", HealthyClaude)
    monkeypatch.setattr(sys, "platform", "win32")

    report = doctor.run_doctor(paths)
    checks = _checks_by_name(report)

    assert checks["scheduler"].status == "error"
    assert report.has_errors is True


@pytest.mark.parametrize("status, expected_exit", [("ok", 0), ("error", 1)])
def test_cli_doctor_prints_one_line_per_check_and_sets_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: Literal["ok", "error"],
    expected_exit: int,
) -> None:
    from job_scan.doctor import DoctorCheck, DoctorReport

    paths = AppPaths.from_root(tmp_path / "home")
    report = DoctorReport(
        checks=[
            DoctorCheck(name="first", status=status, message="First result."),
            DoctorCheck(name="second", status="warning", message="Second result."),
        ]
    )
    monkeypatch.setenv("JOB_SCAN_HOME", str(paths.root))
    monkeypatch.setattr(cli, "run_doctor", lambda _paths: report)

    result = CliRunner().invoke(cli.app, ["doctor"])

    assert result.exit_code == expected_exit
    assert result.output.splitlines() == [
        f"[{status}] first: First result.",
        "[warning] second: Second result.",
    ]
