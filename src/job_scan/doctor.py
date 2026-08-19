from __future__ import annotations

import hashlib
import os
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from job_scan.ai_config import AiConfigError, AiProviderStore
from job_scan.claude_process import ClaudeProcess, ClaudeProcessError
from job_scan.config import AppConfig, load_config
from job_scan.http_client import PublicHttpClient
from job_scan.paths import AppPaths
from job_scan.run_log import RunLogger
from job_scan.sources.jobsuche import JobsucheAdapter

DoctorStatus = Literal["ok", "warning", "error"]


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: DoctorStatus
    message: str


@dataclass(frozen=True)
class DoctorReport:
    checks: list[DoctorCheck]

    @property
    def has_errors(self) -> bool:
        """Return whether any diagnostic prevents a normal scan."""
        return any(check.status == "error" for check in self.checks)


def run_doctor(paths: AppPaths, *, log: bool = False) -> DoctorReport:
    """Run bounded local readiness checks without fetching jobs or reading resume text."""
    checks: list[DoctorCheck] = [_check_data_directories(paths)]
    config_check, config, raw_config = _check_config(paths)
    checks.extend(
        [
            config_check,
            _check_germany_only(config, raw_config),
            _check_profile(paths, config),
            _check_original_resume(paths, config),
            _check_ai_runtime(paths, config),
            _check_ai_credentials(paths, config),
            _check_jobsuche_adapter(paths, config),
            _check_scheduler(),
        ]
    )
    report = DoctorReport(checks=checks)
    if log:
        RunLogger(paths.logs_dir).write_doctor(report)
    return report


def _check_data_directories(paths: AppPaths) -> DoctorCheck:
    """Verify every configured data directory exists and is writable."""
    directories = (
        paths.root,
        paths.jobs_jsonl.parent,
        paths.history_dir,
        paths.cache_dir,
        paths.logs_dir,
    )
    if any(not path.is_dir() or not os.access(path, os.W_OK) for path in directories):
        return DoctorCheck(
            name="data_directories",
            status="error",
            message="Run `job-scan setup` and ensure all data directories are writable.",
        )
    return DoctorCheck(
        name="data_directories",
        status="ok",
        message="All data directories exist and are writable.",
    )


def _check_config(
    paths: AppPaths,
) -> tuple[DoctorCheck, AppConfig | None, Mapping[str, Any] | None]:
    """Load typed config and retain raw values for the independent Germany check."""
    try:
        config = load_config(paths.config_toml)
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return (
            DoctorCheck(
                name="config",
                status="error",
                message="Config is missing or invalid; run `job-scan setup` again.",
            ),
            None,
            _read_raw_config(paths),
        )
    return (
        DoctorCheck(name="config", status="ok", message="Config parsed successfully."),
        config,
        config.model_dump(mode="python"),
    )


def _read_raw_config(paths: AppPaths) -> Mapping[str, Any] | None:
    """Read TOML only when typed validation failed so fixed policy values remain visible."""
    try:
        with paths.config_toml.open("rb") as config_file:
            return tomllib.load(config_file)
    except (OSError, tomllib.TOMLDecodeError):
        return None


def _check_germany_only(
    config: AppConfig | None, raw_config: Mapping[str, Any] | None
) -> DoctorCheck:
    """Verify fixed Germany and visa-sponsorship policy values independently."""
    values: Mapping[str, Any] | None = (
        config.model_dump(mode="python") if config is not None else raw_config
    )
    if values is None:
        return DoctorCheck(
            name="germany_only",
            status="error",
            message="Germany-only values could not be read; repair config.",
        )
    if values.get("country") != "DE" or values.get("needs_visa_sponsorship") is not True:
        return DoctorCheck(
            name="germany_only",
            status="error",
            message="Set country to DE and visa sponsorship need to true via setup.",
        )
    return DoctorCheck(
        name="germany_only",
        status="ok",
        message="Germany-only country and visa values are fixed.",
    )


def _check_profile(paths: AppPaths, config: AppConfig | None) -> DoctorCheck:
    """Verify the profile is non-empty UTF-8 and matches its configured hash."""
    if not paths.profile_md.is_file():
        return DoctorCheck(
            name="profile",
            status="error",
            message="Profile is missing; run `job-scan setup` again.",
        )
    try:
        profile_bytes = paths.profile_md.read_bytes()
        profile_text = profile_bytes.decode("utf-8")
    except (OSError, UnicodeError):
        return DoctorCheck(
            name="profile",
            status="error",
            message="Profile is unreadable or invalid UTF-8; regenerate it with setup.",
        )
    if not profile_text.strip():
        return DoctorCheck(
            name="profile",
            status="error",
            message="Profile is empty; regenerate it with setup.",
        )
    if config is None:
        return DoctorCheck(
            name="profile",
            status="error",
            message="Profile hash cannot be verified until config is repaired.",
        )
    actual_hash = f"sha256:{hashlib.sha256(profile_bytes).hexdigest()}"
    if actual_hash != config.profile_sha256:
        return DoctorCheck(
            name="profile",
            status="error",
            message="Profile hash differs from config; rerun setup before scanning.",
        )
    return DoctorCheck(
        name="profile",
        status="ok",
        message="Profile is present and its hash matches config.",
    )


def _check_original_resume(paths: AppPaths, config: AppConfig | None) -> DoctorCheck:
    """Verify the configured original resume remains available for future setup runs."""
    if config is None:
        return DoctorCheck(
            name="original_resume",
            status="error",
            message="Original resume path cannot be checked until config is repaired.",
        )
    if config.resume_path.is_file():
        return DoctorCheck(
            name="original_resume",
            status="ok",
            message="Original resume path is present.",
        )
    if paths.profile_md.is_file():
        return DoctorCheck(
            name="original_resume",
            status="warning",
            message="Original resume is missing; keep the existing profile or rerun setup.",
        )
    return DoctorCheck(
        name="original_resume",
        status="error",
        message="Original resume and profile are missing; rerun setup with a resume.",
    )


def _check_ai_runtime(paths: AppPaths, config: AppConfig | None) -> DoctorCheck:
    """Verify the selected CLI or saved API runtime exists locally."""
    if config is not None and config.ai_runtime.startswith("api:"):
        try:
            provider = AiProviderStore(paths.ai_config_toml).require(
                config.ai_runtime.removeprefix("api:")
            )
        except AiConfigError as error:
            return DoctorCheck(name="claude_version", status="error", message=str(error))
        return DoctorCheck(
            name="claude_version",
            status="ok",
            message=f"API runtime is configured: {provider.display_name} / {provider.model}.",
        )
    try:
        version = ClaudeProcess().version()
    except ClaudeProcessError as error:
        return DoctorCheck(name="claude_version", status="error", message=str(error))
    return DoctorCheck(
        name="claude_version",
        status="ok",
        message=f"Claude Code version is available: {version}.",
    )


def _check_ai_credentials(paths: AppPaths, config: AppConfig | None) -> DoctorCheck:
    """Verify credentials exist for the selected CLI or saved API runtime."""
    if config is not None and config.ai_runtime.startswith("api:"):
        try:
            AiProviderStore(paths.ai_config_toml).require(
                config.ai_runtime.removeprefix("api:")
            )
        except AiConfigError as error:
            return DoctorCheck(name="claude_auth", status="error", message=str(error))
        return DoctorCheck(
            name="claude_auth",
            status="ok",
            message="Selected API runtime has a saved API key.",
        )
    try:
        ClaudeProcess().auth_status()
    except ClaudeProcessError as error:
        return DoctorCheck(name="claude_auth", status="error", message=str(error))
    return DoctorCheck(
        name="claude_auth",
        status="ok",
        message="Claude Code is authenticated.",
    )


def _check_jobsuche_adapter(paths: AppPaths, config: AppConfig | None) -> DoctorCheck:
    """Construct the Jobsuche adapter without issuing a network request."""
    if config is None or not paths.cache_dir.is_dir():
        return DoctorCheck(
            name="jobsuche_adapter",
            status="error",
            message="Repair config and data directories before initializing Jobsuche.",
        )
    try:
        JobsucheAdapter(config, PublicHttpClient(paths.cache_dir))
    except (OSError, ValueError):
        return DoctorCheck(
            name="jobsuche_adapter",
            status="error",
            message="Jobsuche adapter could not be initialized; repair config.",
        )
    return DoctorCheck(
        name="jobsuche_adapter",
        status="ok",
        message="Jobsuche adapter initializes without network access.",
    )


def _check_scheduler() -> DoctorCheck:
    """Report whether the current OS supports the local scheduler integration."""
    if not (sys.platform.startswith("linux") or sys.platform == "darwin"):
        return DoctorCheck(
            name="scheduler",
            status="error",
            message="Automatic scheduling is unsupported here; run `job-scan scan` manually.",
        )
    backend = "cron" if sys.platform.startswith("linux") else "launchd"
    return DoctorCheck(
        name="scheduler",
        status="ok",
        message=f"Current platform supports local {backend} scheduling.",
    )
