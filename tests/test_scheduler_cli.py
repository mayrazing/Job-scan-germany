from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from job_scan import cli
from job_scan.config import (
    AppConfig,
    ClaudeSettings,
    SchedulerSettings,
    load_config,
    save_config,
)
from job_scan.paths import AppPaths
from job_scan.scheduler import SchedulerError, SchedulerState, UnsupportedSchedulerPlatform


def _config(local_time: str | None = "08:30") -> AppConfig:
    return AppConfig(
        resume_path=Path("/resume.pdf"),
        resume_sha256="sha256:" + "a" * 64,
        profile_sha256="sha256:" + "b" * 64,
        search_terms=["backend engineer"],
        locations=["Berlin"],
        german_level="B1",
        needs_visa_sponsorship=True,
        claude=ClaudeSettings(
            model="sonnet",
            effort="medium",
        ),
        scheduler=SchedulerSettings(local_time=local_time),
    )


class RecordingBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.install_error: SchedulerError | None = None

    def install(
        self, config: AppConfig, paths: AppPaths, executable: Path
    ) -> SchedulerState:
        persisted = load_config(paths.scheduled_config_toml)
        self.calls.append(("install", persisted.scheduler.local_time))
        if self.install_error is not None:
            raise self.install_error
        return SchedulerState(
            backend="cron",
            installed=True,
            local_time=config.scheduler.local_time,
            executable=executable,
            managed_location="crontab:# BEGIN job-scan-germany",
        )

    def remove(self, paths: AppPaths) -> SchedulerState:
        self.calls.append(("remove", paths.root))
        return SchedulerState(
            backend="cron",
            installed=False,
            local_time=None,
            executable=None,
            managed_location="crontab:# BEGIN job-scan-germany",
        )

    def status(self, paths: AppPaths) -> SchedulerState:
        self.calls.append(("status", paths.root))
        return SchedulerState(
            backend="cron",
            installed=True,
            local_time="08:30",
            executable=Path("/opt/job-scan"),
            managed_location="crontab:# BEGIN job-scan-germany",
        )


def _install_cli_fakes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[AppPaths, RecordingBackend, Path]:
    paths = AppPaths.from_root(tmp_path / "home")
    paths.ensure_directories()
    save_config(paths.config_toml, _config())
    paths.profile_md.write_text("# Current profile\n", encoding="utf-8")
    executable = (tmp_path / "bin" / "job-scan").resolve()
    backend = RecordingBackend()
    monkeypatch.setenv("JOB_SCAN_HOME", str(paths.root))
    monkeypatch.setattr(cli, "_scheduler_backend_factory", lambda: backend)
    monkeypatch.setattr(cli, "_scheduler_executable_factory", lambda: executable)
    return paths, backend, executable


def test_scheduler_install_time_is_saved_before_reconciliation_and_prints_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, backend, executable = _install_cli_fakes(tmp_path, monkeypatch)

    result = CliRunner().invoke(cli.app, ["scheduler", "install", "--time", "07:15"])

    assert result.exit_code == 0, result.output
    assert backend.calls == [("install", "07:15")]
    assert load_config(paths.config_toml).scheduler.local_time == "08:30"
    assert load_config(paths.scheduled_config_toml).scheduler.local_time == "07:15"
    assert paths.scheduled_profile_md.read_text(encoding="utf-8") == (
        "# Current profile\n"
    )
    assert result.stdout.splitlines() == [
        "Backend: cron",
        "Installed: yes",
        "Schedule: 07:15",
        f"Executable: {executable}",
        f"Data root: {paths.root}",
        f"Log path: {paths.logs_dir / 'scheduler.log'}",
    ]


@pytest.mark.parametrize("command", ["remove", "status"])
def test_scheduler_remove_and_status_use_selected_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    paths, backend, _executable = _install_cli_fakes(tmp_path, monkeypatch)

    result = CliRunner().invoke(cli.app, ["scheduler", command])

    assert result.exit_code == 0, result.output
    assert backend.calls == [(command, paths.root)]
    assert "Backend: cron\n" in result.stdout
    assert f"Data root: {paths.root}\n" in result.stdout
    assert f"Log path: {paths.logs_dir / 'scheduler.log'}\n" in result.stdout


def test_scheduler_install_rejects_invalid_time_without_changing_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, backend, _executable = _install_cli_fakes(tmp_path, monkeypatch)

    result = CliRunner().invoke(cli.app, ["scheduler", "install", "--time", "25:00"])

    assert result.exit_code == 1
    assert result.stderr.strip() == "Scheduler failed: time must use HH:MM."
    assert load_config(paths.config_toml).scheduler.local_time == "08:30"
    assert backend.calls == []


def test_scheduler_install_failure_restores_the_previous_scheduled_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, backend, _executable = _install_cli_fakes(tmp_path, monkeypatch)
    save_config(paths.scheduled_config_toml, _config("08:30"))
    paths.scheduled_profile_md.write_text("# Scheduled profile\n", encoding="utf-8")
    backend.install_error = SchedulerError("native install failed")

    result = CliRunner().invoke(cli.app, ["scheduler", "install", "--time", "07:15"])

    assert result.exit_code == 1
    assert load_config(paths.scheduled_config_toml).scheduler.local_time == "08:30"


def test_scheduler_install_rejects_a_missing_time_without_calling_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, backend, _executable = _install_cli_fakes(tmp_path, monkeypatch)
    save_config(paths.config_toml, _config(None))

    result = CliRunner().invoke(cli.app, ["scheduler", "install"])

    assert result.exit_code == 1
    assert "daily scan time is not configured" in result.stderr.lower()
    assert load_config(paths.config_toml).scheduler.local_time is None
    assert backend.calls == []


def test_scheduler_remove_clears_the_persisted_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, backend, _executable = _install_cli_fakes(tmp_path, monkeypatch)
    save_config(paths.scheduled_config_toml, _config())
    paths.scheduled_profile_md.write_text("# Scheduled profile\n", encoding="utf-8")

    result = CliRunner().invoke(cli.app, ["scheduler", "remove"])

    assert result.exit_code == 0, result.output
    assert backend.calls == [("remove", paths.root)]
    assert load_config(paths.config_toml).scheduler.local_time == "08:30"
    assert load_config(paths.scheduled_config_toml).scheduler.local_time is None


def test_scheduler_remove_reports_an_invalid_config_without_a_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, backend, _executable = _install_cli_fakes(tmp_path, monkeypatch)
    paths.scheduled_config_toml.write_text(
        "[scheduler]\nlocal_time = '25:00'\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(cli.app, ["scheduler", "remove"])

    assert result.exit_code == 1
    assert result.stderr.startswith("Scheduler failed:")
    assert "Traceback" not in result.output
    assert backend.calls == [("remove", paths.root)]


def test_scheduler_cli_exits_one_on_unsupported_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    monkeypatch.setenv("JOB_SCAN_HOME", str(paths.root))

    def unsupported() -> RecordingBackend:
        raise UnsupportedSchedulerPlatform("Scheduler is unsupported on win32.")

    monkeypatch.setattr(cli, "_scheduler_backend_factory", unsupported)

    result = CliRunner().invoke(cli.app, ["scheduler", "status"])

    assert result.exit_code == 1
    assert result.stderr.strip() == "Scheduler failed: Scheduler is unsupported on win32."
