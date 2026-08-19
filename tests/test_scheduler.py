from __future__ import annotations

import plistlib
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from job_scan.config import AppConfig, ClaudeSettings, SchedulerSettings
from job_scan.paths import AppPaths
from job_scan.scheduler import (
    LinuxCronBackend,
    MacOSLaunchdBackend,
    SchedulerError,
    UnsupportedSchedulerPlatform,
    scheduler_for_platform,
)


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


class FakeCrontab:
    def __init__(self, current: bytes | None) -> None:
        self.current = current
        self.calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    def __call__(
        self, args: Sequence[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[bytes]:
        argv = tuple(args)
        self.calls.append((argv, kwargs))
        if argv == ("crontab", "-l"):
            if self.current is None:
                return subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=b"")
            return subprocess.CompletedProcess(argv, 0, stdout=self.current, stderr=b"")
        assert argv == ("crontab", "-")
        self.current = kwargs["input"]
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    @property
    def writes(self) -> list[tuple[tuple[str, ...], dict[str, Any]]]:
        return [call for call in self.calls if call[0] == ("crontab", "-")]


class FakeLaunchctl:
    def __init__(self) -> None:
        self.loaded = False
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self, args: Sequence[str], **_kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        argv = tuple(args)
        self.calls.append(argv)
        action = argv[1]
        if action == "print":
            return subprocess.CompletedProcess(
                argv, 0 if self.loaded else 3, stdout="", stderr=""
            )
        if action == "bootout":
            self.loaded = False
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        assert action == "bootstrap"
        self.loaded = True
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def test_linux_install_rejects_a_missing_schedule_before_reading_crontab(
    tmp_path: Path,
) -> None:
    fake = FakeCrontab(b"existing")
    backend = LinuxCronBackend(run=fake)

    with pytest.raises(SchedulerError, match="Daily scan time is not configured"):
        backend.install(
            _config(None),
            AppPaths.from_root(tmp_path / "home"),
            tmp_path / "job-scan",
        )

    assert fake.calls == []


def test_macos_install_rejects_a_missing_schedule_before_touching_launchd(
    tmp_path: Path,
) -> None:
    fake = FakeLaunchctl()
    launch_agents = tmp_path / "LaunchAgents"
    launch_agents.mkdir()
    backend = MacOSLaunchdBackend(
        run=fake,
        launch_agents_root=launch_agents,
        uid=501,
    )

    with pytest.raises(SchedulerError, match="Daily scan time is not configured"):
        backend.install(
            _config(None),
            AppPaths.from_root(tmp_path / "home"),
            tmp_path / "job-scan",
        )

    assert fake.calls == []
    assert not backend.plist_path.exists()


def test_linux_install_renders_exact_owned_block_and_is_idempotent(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "job scan home")
    executable = tmp_path / "bin" / "job-scan"
    unrelated = b"MAILTO=ops@example.com\n@weekly /usr/bin/true\n"
    fake = FakeCrontab(unrelated)
    backend = LinuxCronBackend(run=fake)

    first = backend.install(_config("07:05"), paths, executable)
    second = backend.install(_config("07:05"), paths, executable)

    log_path = paths.logs_dir / "scheduler.log"
    expected_block = "\n".join(
        [
            "# BEGIN job-scan-germany",
            (
                f"05 07 * * * JOB_SCAN_HOME='{paths.root}' "
                f"'{executable.resolve()}' scan >> '{log_path}' 2>&1"
            ),
            "# END job-scan-germany",
        ]
    )
    assert fake.current == unrelated + expected_block.encode("utf-8") + b"\n"
    assert len(fake.writes) == 1
    assert first == second
    assert first.backend == "cron"
    assert first.installed is True
    assert first.local_time == "07:05"
    assert first.executable == executable.resolve()
    assert first.managed_location == "crontab:# BEGIN job-scan-germany"


def test_linux_install_persists_opencli_absolute_path_for_scheduled_scan(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    job_scan = tmp_path / "python-venv" / "bin" / "job-scan"
    opencli = tmp_path / "npm-user-bin" / "opencli"
    opencli.parent.mkdir()
    opencli_target = tmp_path / "npm-package" / "dist" / "opencli.js"
    opencli_target.parent.mkdir(parents=True)
    opencli_target.write_text("fixture", encoding="utf-8")
    opencli.symlink_to(opencli_target)
    runtime_path = f"{opencli.parent}:/usr/bin:/bin"
    fake = FakeCrontab(b"")
    backend = LinuxCronBackend(
        run=fake,
        opencli_executable=opencli,
        runtime_path=runtime_path,
    )

    backend.install(_config(), paths, job_scan)
    state = backend.status(paths)

    assert fake.current is not None
    assert f"PATH='{runtime_path}'".encode() in fake.current
    assert f"JOB_SCAN_OPENCLI='{opencli.absolute()}'".encode() in fake.current
    assert str(opencli_target.resolve()).encode() not in fake.current
    assert state.executable == job_scan.resolve()


def test_linux_replaces_and_removes_only_owned_block(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    executable = tmp_path / "job-scan"
    prefix = b"SHELL=/bin/zsh\n"
    suffix = b"\n15 4 * * 1 /usr/bin/true\n"
    fake = FakeCrontab(
        prefix
        + b"# BEGIN job-scan-germany\nold managed line\n# END job-scan-germany"
        + suffix
    )
    backend = LinuxCronBackend(run=fake)

    backend.install(_config(), paths, executable)
    assert fake.current is not None
    assert fake.current.startswith(prefix)
    assert fake.current.endswith(suffix)
    assert b"old managed line" not in fake.current

    removed = backend.remove(paths)
    backend.remove(paths)

    assert fake.current == prefix + suffix
    assert removed.installed is False
    assert len(fake.writes) == 2


def test_linux_remove_ignores_end_marker_that_is_not_a_complete_line(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    current = (
        b"# BEGIN job-scan-germany\n"
        b"unowned content\n"
        b"# END job-scan-germany-disabled\n"
    )
    fake = FakeCrontab(current)

    LinuxCronBackend(run=fake).remove(paths)

    assert fake.current == current
    assert fake.writes == []


def test_linux_install_preserves_arbitrary_bytes_outside_owned_block(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    executable = tmp_path / "job-scan"
    prefix = b"MAILTO=ops@example.com\r\n\xff\x00prefix\n"
    suffix = b"\n\x80suffix\r\n"
    fake = FakeCrontab(
        prefix
        + b"# BEGIN job-scan-germany\nold\n# END job-scan-germany"
        + suffix
    )

    LinuxCronBackend(run=fake).install(_config(), paths, executable)

    assert fake.current is not None
    assert fake.current.startswith(prefix)
    assert fake.current.endswith(suffix)
    assert isinstance(fake.writes[0][1]["input"], bytes)
    assert "text" not in fake.calls[0][1]
    assert "text" not in fake.writes[0][1]


def test_linux_accepts_empty_missing_crontab_and_status_reads_managed_values(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    executable = tmp_path / "job-scan"
    fake = FakeCrontab(None)
    backend = LinuxCronBackend(run=fake)

    backend.install(_config("23:59"), paths, executable)
    state = backend.status(paths)

    assert state.installed is True
    assert state.local_time == "23:59"
    assert state.executable == executable.resolve()


def test_macos_writes_exact_plist_reconciles_loaded_job_and_removes_only_its_file(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "job scan home")
    executable = tmp_path / "bin" / "job-scan"
    opencli = tmp_path / "npm-user-bin" / "opencli"
    runtime_path = f"{opencli.parent}:/usr/bin:/bin"
    launch_agents = tmp_path / "LaunchAgents"
    launch_agents.mkdir()
    unrelated = launch_agents / "org.example.other.plist"
    unrelated.write_bytes(b"other")
    fake = FakeLaunchctl()
    backend = MacOSLaunchdBackend(
        run=fake,
        launch_agents_root=launch_agents,
        uid=501,
        opencli_executable=opencli,
        runtime_path=runtime_path,
    )

    first = backend.install(_config("06:45"), paths, executable)
    second = backend.install(_config("06:45"), paths, executable)

    plist_path = launch_agents / "com.job-scan.germany.plist"
    payload = plistlib.loads(plist_path.read_bytes())
    log_path = paths.logs_dir / "scheduler.log"
    assert payload == {
        "EnvironmentVariables": {
            "JOB_SCAN_HOME": str(paths.root),
            "JOB_SCAN_OPENCLI": str(opencli.absolute()),
            "PATH": runtime_path,
        },
        "Label": "com.job-scan.germany",
        "ProgramArguments": [str(executable.resolve()), "scan"],
        "StandardErrorPath": str(log_path),
        "StandardOutPath": str(log_path),
        "StartCalendarInterval": {"Hour": 6, "Minute": 45},
    }
    assert first == second
    assert first.backend == "launchd"
    assert first.managed_location == str(plist_path)
    assert [call[1] for call in fake.calls].count("bootstrap") == 1
    assert [call[1] for call in fake.calls].count("bootout") == 0

    calls_before_reconcile = len(fake.calls)
    backend.install(_config("07:15"), paths, executable)

    reconcile_calls = fake.calls[calls_before_reconcile:]
    assert reconcile_calls == [
        ("launchctl", "print", "gui/501/com.job-scan.germany"),
        ("launchctl", "bootout", "gui/501", str(plist_path)),
        ("launchctl", "bootstrap", "gui/501", str(plist_path)),
    ]
    assert [call[1] for call in fake.calls].count("bootstrap") == 2
    assert [call[1] for call in fake.calls].count("bootout") == 1
    assert backend.status(paths).local_time == "07:15"

    removed = backend.remove(paths)
    backend.remove(paths)

    assert removed.installed is False
    assert plist_path.exists() is False
    assert unrelated.read_bytes() == b"other"


def test_platform_selection_supports_only_linux_and_macos(tmp_path: Path) -> None:
    fake_cron = FakeCrontab(b"")
    fake_launchctl = FakeLaunchctl()

    assert isinstance(
        scheduler_for_platform("linux2", run=fake_cron), LinuxCronBackend
    )
    assert isinstance(
        scheduler_for_platform(
            "darwin",
            run=fake_launchctl,
            launch_agents_root=tmp_path,
            uid=501,
        ),
        MacOSLaunchdBackend,
    )
    with pytest.raises(UnsupportedSchedulerPlatform):
        scheduler_for_platform("win32", run=fake_cron)


def test_platform_scheduler_passes_resolved_opencli_to_backend(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    job_scan = tmp_path / "python-venv" / "bin" / "job-scan"
    opencli = tmp_path / "npm-user-bin" / "opencli"
    runtime_path = "/usr/bin:/bin"
    expected_runtime_path = f"{opencli.parent}:{runtime_path}"
    fake = FakeCrontab(b"")
    backend = scheduler_for_platform(
        "linux",
        run=fake,
        opencli_executable=opencli,
        runtime_path=runtime_path,
    )

    backend.install(_config(), paths, job_scan)

    assert fake.current is not None
    assert f"PATH='{expected_runtime_path}'".encode() in fake.current
    assert f"JOB_SCAN_OPENCLI='{opencli.absolute()}'".encode() in fake.current
