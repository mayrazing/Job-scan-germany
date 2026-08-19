from __future__ import annotations

import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from job_scan.config import AppConfig
from job_scan.paths import AppPaths

BackendName = Literal["cron", "launchd"]
CommandRunner = Callable[..., subprocess.CompletedProcess[Any]]

_BEGIN_MARKER = "# BEGIN job-scan-germany"
_END_MARKER = "# END job-scan-germany"
_CRON_LOCATION = f"crontab:{_BEGIN_MARKER}"
_BEGIN_MARKER_BYTES = _BEGIN_MARKER.encode("ascii")
_END_MARKER_BYTES = _END_MARKER.encode("ascii")
_MANAGED_BLOCK_PATTERN = re.compile(
    rb"^"
    + re.escape(_BEGIN_MARKER_BYTES)
    + rb"\n.*?^"
    + re.escape(_END_MARKER_BYTES)
    + rb"(?=\n|\Z)",
    flags=re.MULTILINE | re.DOTALL,
)
_LAUNCHD_LABEL = "com.job-scan.germany"
_PLIST_NAME = f"{_LAUNCHD_LABEL}.plist"
_OPENCLI_ENV = "JOB_SCAN_OPENCLI"
_PATH_ENV = "PATH"


@dataclass(frozen=True)
class SchedulerState:
    backend: BackendName
    installed: bool
    local_time: str | None
    executable: Path | None
    managed_location: str


class SchedulerBackend(Protocol):
    backend: BackendName

    def install(
        self, config: AppConfig, paths: AppPaths, executable: Path
    ) -> SchedulerState: ...

    def remove(self, paths: AppPaths) -> SchedulerState: ...

    def status(self, paths: AppPaths) -> SchedulerState: ...


class SchedulerError(RuntimeError):
    """Report a local scheduler operation that could not be completed."""


class UnsupportedSchedulerPlatform(SchedulerError):
    """Report an OS without a Task 4 scheduler backend."""


def _required_schedule_time(config: AppConfig) -> str:
    """Return the configured time or reject scheduler installation."""
    local_time = config.scheduler.local_time
    if local_time is None:
        raise SchedulerError("Daily scan time is not configured.")
    return local_time


class LinuxCronBackend:
    backend: BackendName = "cron"

    def __init__(
        self,
        *,
        run: CommandRunner = subprocess.run,
        opencli_executable: Path | None = None,
        runtime_path: str | None = None,
    ) -> None:
        self._run = run
        self._opencli_executable = opencli_executable
        self._runtime_path = runtime_path

    def install(
        self, config: AppConfig, paths: AppPaths, executable: Path
    ) -> SchedulerState:
        """Publish exactly one owned cron block while retaining unrelated bytes."""
        local_time = _required_schedule_time(config)
        current = self._read_crontab()
        block = _render_cron_block(
            local_time,
            paths,
            executable,
            self._opencli_executable,
            self._runtime_path,
        )
        updated = _replace_managed_blocks(current, block)
        if updated != current:
            self._write_crontab(updated)
        return SchedulerState(
            backend=self.backend,
            installed=True,
            local_time=local_time,
            executable=executable.resolve(),
            managed_location=_CRON_LOCATION,
        )

    def remove(self, paths: AppPaths) -> SchedulerState:
        """Delete only owned cron blocks and succeed when none exists."""
        del paths
        current = self._read_crontab()
        updated = _remove_managed_blocks(current)
        if updated != current:
            self._write_crontab(updated)
        return SchedulerState(
            backend=self.backend,
            installed=False,
            local_time=None,
            executable=None,
            managed_location=_CRON_LOCATION,
        )

    def status(self, paths: AppPaths) -> SchedulerState:
        """Read schedule and executable from the first owned cron block."""
        del paths
        current = self._read_crontab()
        match = _MANAGED_BLOCK_PATTERN.search(current)
        local_time, executable = _cron_values(match.group(0) if match else None)
        return SchedulerState(
            backend=self.backend,
            installed=match is not None,
            local_time=local_time,
            executable=executable,
            managed_location=_CRON_LOCATION,
        )

    def _read_crontab(self) -> bytes:
        result = self._run(
            ["crontab", "-l"],
            capture_output=True,
            check=False,
        )
        if not isinstance(result.stdout, bytes):
            raise SchedulerError("Could not read crontab.")
        if result.returncode == 0:
            return result.stdout
        if result.returncode == 1 and result.stdout == b"":
            return b""
        raise SchedulerError("Could not read crontab.")

    def _write_crontab(self, content: bytes) -> None:
        result = self._run(
            ["crontab", "-"],
            input=content,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise SchedulerError("Could not update crontab.")


class MacOSLaunchdBackend:
    backend: BackendName = "launchd"

    def __init__(
        self,
        *,
        run: CommandRunner = subprocess.run,
        launch_agents_root: Path | None = None,
        uid: int | None = None,
        opencli_executable: Path | None = None,
        runtime_path: str | None = None,
    ) -> None:
        self._run = run
        self._launch_agents_root = (
            launch_agents_root
            if launch_agents_root is not None
            else Path.home() / "Library" / "LaunchAgents"
        )
        self._uid = os.getuid() if uid is None else uid
        self._opencli_executable = opencli_executable
        self._runtime_path = runtime_path

    @property
    def plist_path(self) -> Path:
        return self._launch_agents_root / _PLIST_NAME

    def install(
        self, config: AppConfig, paths: AppPaths, executable: Path
    ) -> SchedulerState:
        """Atomically publish and load the one owned launchd plist."""
        local_time = _required_schedule_time(config)
        executable = executable.resolve()
        payload = _render_launchd_plist(
            local_time,
            paths,
            executable,
            self._opencli_executable,
            self._runtime_path,
        )
        current = self.plist_path.read_bytes() if self.plist_path.is_file() else None
        changed = current != payload
        loaded = self._is_loaded()
        if not changed and loaded:
            return self._state(local_time, executable, installed=True)
        if loaded:
            self._bootout()
        if changed:
            _write_atomic(self.plist_path, payload)
        self._bootstrap()
        return self._state(local_time, executable, installed=True)

    def remove(self, paths: AppPaths) -> SchedulerState:
        """Unload and delete only the owned launchd plist."""
        del paths
        if self._is_loaded():
            self._bootout()
        self.plist_path.unlink(missing_ok=True)
        return self._state(None, None, installed=False)

    def status(self, paths: AppPaths) -> SchedulerState:
        """Read installed values from the owned launchd plist without changing launchd."""
        del paths
        if not self.plist_path.is_file():
            return self._state(None, None, installed=False)
        try:
            payload = plistlib.loads(self.plist_path.read_bytes())
        except (OSError, plistlib.InvalidFileException, ValueError):
            return self._state(None, None, installed=True)
        local_time, executable = _launchd_values(payload)
        return self._state(local_time, executable, installed=True)

    def _state(
        self,
        local_time: str | None,
        executable: Path | None,
        *,
        installed: bool,
    ) -> SchedulerState:
        return SchedulerState(
            backend=self.backend,
            installed=installed,
            local_time=local_time,
            executable=executable,
            managed_location=str(self.plist_path),
        )

    def _is_loaded(self) -> bool:
        result = self._run(
            ["launchctl", "print", f"gui/{self._uid}/{_LAUNCHD_LABEL}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0

    def _bootout(self) -> None:
        result = self._run(
            ["launchctl", "bootout", f"gui/{self._uid}", str(self.plist_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0 and self._is_loaded():
            raise SchedulerError("Could not unload launchd scheduler.")

    def _bootstrap(self) -> None:
        result = self._run(
            ["launchctl", "bootstrap", f"gui/{self._uid}", str(self.plist_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0 and not self._is_loaded():
            raise SchedulerError("Could not load launchd scheduler.")


def scheduler_for_platform(
    platform: str | None = None,
    *,
    run: CommandRunner = subprocess.run,
    launch_agents_root: Path | None = None,
    uid: int | None = None,
    opencli_executable: Path | None = None,
    runtime_path: str | None = None,
) -> SchedulerBackend:
    """Select the one supported native scheduler backend for a platform."""
    selected = sys.platform if platform is None else platform
    resolved_opencli = opencli_executable or _discover_opencli()
    current_runtime_path = runtime_path or os.environ.get(_PATH_ENV, "").strip() or None
    resolved_runtime_path = _runtime_path_for_opencli(
        current_runtime_path,
        resolved_opencli,
    )
    if selected.startswith("linux"):
        return LinuxCronBackend(
            run=run,
            opencli_executable=resolved_opencli,
            runtime_path=resolved_runtime_path,
        )
    if selected == "darwin":
        return MacOSLaunchdBackend(
            run=run,
            launch_agents_root=launch_agents_root,
            uid=uid,
            opencli_executable=resolved_opencli,
            runtime_path=resolved_runtime_path,
        )
    raise UnsupportedSchedulerPlatform(f"Scheduler is unsupported on {selected}.")


def _discover_opencli() -> Path | None:
    discovered = shutil.which("opencli")
    if discovered is not None:
        return Path(discovered).absolute()
    sibling = Path(sys.executable).with_name("opencli")
    return sibling.absolute() if sibling.is_file() else None


def _runtime_path_for_opencli(
    runtime_path: str | None,
    opencli_executable: Path | None,
) -> str | None:
    entries = [entry for entry in (runtime_path or "").split(os.pathsep) if entry]
    if opencli_executable is not None:
        opencli_bin = str(opencli_executable.absolute().parent)
        entries = [opencli_bin, *(entry for entry in entries if entry != opencli_bin)]
    return os.pathsep.join(entries) or None


def _render_cron_block(
    local_time: str,
    paths: AppPaths,
    executable: Path,
    opencli_executable: Path | None,
    runtime_path: str | None,
) -> bytes:
    hour, minute = local_time.split(":")
    parts = [
        f"{minute} {hour} * * *",
    ]
    if runtime_path:
        parts.append(f"{_PATH_ENV}={_shell_quote(runtime_path)}")
    parts.append(f"JOB_SCAN_HOME={_shell_quote(str(paths.root))}")
    if opencli_executable is not None:
        parts.append(
            f"{_OPENCLI_ENV}={_shell_quote(str(opencli_executable.absolute()))}"
        )
    parts.extend(
        [
            _shell_quote(str(executable.resolve())),
            "scan",
            ">>",
            _shell_quote(str(paths.logs_dir / "scheduler.log")),
            "2>&1",
        ]
    )
    command = " ".join(parts)
    return f"{_BEGIN_MARKER}\n{command}\n{_END_MARKER}".encode()


def _render_launchd_plist(
    local_time: str,
    paths: AppPaths,
    executable: Path,
    opencli_executable: Path | None,
    runtime_path: str | None,
) -> bytes:
    hour, minute = (int(value) for value in local_time.split(":"))
    log_path = str(paths.logs_dir / "scheduler.log")
    environment = {"JOB_SCAN_HOME": str(paths.root)}
    if runtime_path:
        environment[_PATH_ENV] = runtime_path
    if opencli_executable is not None:
        environment[_OPENCLI_ENV] = str(opencli_executable.absolute())
    payload = {
        "Label": _LAUNCHD_LABEL,
        "ProgramArguments": [str(executable), "scan"],
        "EnvironmentVariables": environment,
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _replace_managed_blocks(current: bytes, block: bytes) -> bytes:
    matches = list(_MANAGED_BLOCK_PATTERN.finditer(current))
    if not matches:
        separator = b"" if current == b"" or current.endswith(b"\n") else b"\n"
        return current + separator + block + b"\n"
    parts = [current[: matches[0].start()], block]
    cursor = matches[0].end()
    for match in matches[1:]:
        parts.append(current[cursor : match.start()])
        cursor = match.end()
    parts.append(current[cursor:])
    return b"".join(parts)


def _remove_managed_blocks(current: bytes) -> bytes:
    matches = list(_MANAGED_BLOCK_PATTERN.finditer(current))
    if not matches:
        return current
    parts: list[bytes] = []
    cursor = 0
    for match in matches:
        parts.append(current[cursor : match.start()])
        cursor = match.end()
    parts.append(current[cursor:])
    return b"".join(parts)


def _cron_values(block: bytes | None) -> tuple[str | None, Path | None]:
    if block is None:
        return None, None
    try:
        lines = block.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return None, None
    if len(lines) != 3:
        return None, None
    try:
        tokens = shlex.split(lines[1])
    except ValueError:
        return None, None
    if len(tokens) < 8 or not tokens[0].isdigit() or not tokens[1].isdigit():
        return None, None
    scan_index = next(
        (index for index in range(6, len(tokens)) if tokens[index] == "scan"),
        None,
    )
    if scan_index is None:
        return None, None
    return f"{int(tokens[1]):02d}:{int(tokens[0]):02d}", Path(tokens[scan_index - 1])


def _launchd_values(payload: object) -> tuple[str | None, Path | None]:
    if not isinstance(payload, dict):
        return None, None
    calendar = payload.get("StartCalendarInterval")
    arguments = payload.get("ProgramArguments")
    local_time: str | None = None
    executable: Path | None = None
    if isinstance(calendar, dict):
        hour = calendar.get("Hour")
        minute = calendar.get("Minute")
        if isinstance(hour, int) and isinstance(minute, int):
            local_time = f"{hour:02d}:{minute:02d}"
    if (
        isinstance(arguments, list)
        and len(arguments) >= 2
        and isinstance(arguments[0], str)
        and arguments[1] == "scan"
    ):
        executable = Path(arguments[0])
    return local_time, executable


def _write_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as temporary_file:
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
