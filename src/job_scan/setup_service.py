from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from job_scan.ai_runtime import AiRuntimeInvoker
from job_scan.claude_process import ClaudeInvocation, ClaudeRequest
from job_scan.config import (
    STAFFING_AGENCY_PENALTY,
    AppConfig,
    ClaudeSettings,
    MinimumCompanySize,
    PostedWithinDays,
    SchedulerSettings,
    TargetCompany,
    load_config,
    migrate_legacy_opencli_settings,
    serialize_config,
)
from job_scan.paths import AppPaths
from job_scan.prompts import PROFILE_HEADINGS, build_profile_prompt
from job_scan.resume import ExtractedResume, extract_resume

_PROFILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "profile_markdown": {"type": "string", "minLength": 1},
    },
    "required": ["profile_markdown"],
    "additionalProperties": False,
}
_MARKDOWN_HEADING = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)


class SetupError(RuntimeError):
    """Report one safe, actionable setup-domain failure."""


class SetupOutputError(SetupError):
    """Report invalid structured profile output without exposing process bytes."""


class SetupValidationError(SetupError):
    """Report invalid setup data without echoing private values."""


class SetupPersistenceError(SetupError):
    """Report a failed pair publication after cleanup and rollback."""


class SetupAnswers(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_name: str = Field(default="", max_length=200)
    ai_runtime: str = Field(
        default="claude-code",
        pattern=r"^(?:claude-code|api:[a-z0-9]+(?:-[a-z0-9]+)*)$",
    )
    search_terms: list[str] = Field(min_length=1)
    locations: list[str]
    posted_within_days: PostedWithinDays | None = 7
    arbeitsagentur_enabled: bool = True
    target_companies: list[TargetCompany] = Field(default_factory=list)
    linkedin_enabled: bool = True
    linkedin_limit: int = Field(default=10, ge=1, le=100)
    indeed_de_enabled: bool = True
    indeed_de_limit: int = Field(default=10, ge=1, le=100)
    stepstone_de_enabled: bool = True
    stepstone_de_limit: int = Field(default=10, ge=1, le=100)
    glassdoor_de_enabled: bool = True
    glassdoor_de_limit: int = Field(default=10, ge=1, le=100)
    simplify_de_enabled: bool = True
    simplify_de_limit: int = Field(default=10, ge=1, le=100)
    minimum_company_size: MinimumCompanySize = 0
    german_level: str
    staffing_penalty: int = Field(default=STAFFING_AGENCY_PENALTY, ge=0, le=100)
    claude: ClaudeSettings
    scheduler: SchedulerSettings

    @model_validator(mode="before")
    @classmethod
    def migrate_opencli_switches(cls, value: Any) -> Any:
        """Accept legacy browser drafts that used zero as the off switch."""
        return migrate_legacy_opencli_settings(value)

    @field_validator("staffing_penalty", mode="after")
    @classmethod
    def fix_staffing_penalty(cls, _value: int) -> int:
        """Keep legacy setup payloads compatible while enforcing the fixed policy."""
        return STAFFING_AGENCY_PENALTY


class SetupResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config: AppConfig
    profile_path: Path
    profile_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class SetupPreparation(BaseModel):
    """Carry a generated profile/config pair before it is published."""

    model_config = ConfigDict(extra="forbid")

    config: AppConfig
    profile_bytes: bytes
    config_bytes: bytes
    profile_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class ClaudeInvoker(Protocol):
    def invoke(self, request: ClaudeRequest) -> ClaudeInvocation: ...


class SetupService:
    """Reuse or generate one profile and publish it with its validated config."""

    def __init__(
        self,
        paths: AppPaths,
        claude: ClaudeInvoker | None = None,
        *,
        resume_extractor: Callable[[Path], ExtractedResume] = extract_resume,
    ) -> None:
        self._paths = paths
        self._claude = claude if claude is not None else AiRuntimeInvoker(paths)
        self._extract_resume = resume_extractor

    def run(self, resume_path: Path, answers: SetupAnswers) -> SetupResult:
        """Extract a resume, obtain its profile, then publish a consistent pair."""
        prepared = self.prepare(resume_path, answers)
        self._publish_pair(prepared.profile_bytes, prepared.config_bytes)
        return SetupResult(
            config=prepared.config,
            profile_path=self._paths.profile_md,
            profile_hash=prepared.profile_hash,
        )

    def prepare(
        self,
        resume_path: Path,
        answers: SetupAnswers,
        *,
        reuse_current_profile: bool = True,
    ) -> SetupPreparation:
        """Build a profile/config pair without replacing the current setup."""
        extracted = self._extract_resume(resume_path)
        reusable = (
            self._read_reusable_profile(extracted.sha256, answers.ai_runtime)
            if reuse_current_profile
            else None
        )
        if reusable is None:
            prompt = build_profile_prompt(extracted.text, answers)
            request = ClaudeRequest(
                runtime=answers.ai_runtime,
                prompt=prompt,
                json_schema=_PROFILE_SCHEMA,
                model=answers.claude.model,
                effort=answers.claude.effort,
                thinking_enabled=answers.claude.thinking_enabled,
                timeout_seconds=answers.claude.timeout_seconds,
                max_output_bytes=answers.claude.max_output_bytes,
            )
            invocation = self._claude.invoke(request)
            profile_markdown = _validated_profile(invocation)
            try:
                profile_bytes = profile_markdown.encode("utf-8")
            except UnicodeEncodeError:
                raise SetupOutputError(
                    "Claude profile contained invalid Unicode; retry setup."
                ) from None
            ai_model = _api_model_from_invocation(invocation)
        else:
            profile_bytes, ai_model = reusable
        profile_hash = f"sha256:{hashlib.sha256(profile_bytes).hexdigest()}"
        try:
            resolved_resume = resume_path.expanduser().resolve(strict=True)
            config = AppConfig.model_validate(
                {
                    **answers.model_dump(mode="json", warnings=False),
                    "ai_model": ai_model,
                    "country": "DE",
                    "needs_visa_sponsorship": True,
                    "resume_path": resolved_resume,
                    "resume_sha256": extracted.sha256,
                    "profile_sha256": profile_hash,
                }
            )
            serialized_config = serialize_config(config).encode("utf-8")
        except (OSError, UnicodeEncodeError, ValidationError, TypeError, ValueError):
            raise SetupValidationError(
                "Setup values could not produce a valid configuration; correct them and retry."
            ) from None

        return SetupPreparation(
            config=config,
            profile_bytes=profile_bytes,
            config_bytes=serialized_config,
            profile_hash=profile_hash,
        )

    def _read_reusable_profile(
        self,
        resume_sha256: str,
        ai_runtime: str,
    ) -> tuple[bytes, str | None] | None:
        """Return the current profile when it belongs to the same resume and is intact."""
        try:
            config = load_config(self._paths.config_toml)
            profile_bytes = self._paths.profile_md.read_bytes()
            profile = profile_bytes.decode("utf-8")
        except (OSError, UnicodeError, ValueError, ValidationError):
            return None
        actual_hash = f"sha256:{hashlib.sha256(profile_bytes).hexdigest()}"
        if config.resume_sha256 != resume_sha256 or config.profile_sha256 != actual_hash:
            return None
        try:
            _validate_required_sections(profile)
        except SetupOutputError:
            return None
        ai_model = config.ai_model if config.ai_runtime == ai_runtime else None
        return profile_bytes, ai_model

    def _publish_pair(self, profile_bytes: bytes, config_bytes: bytes) -> None:
        """Stage and publish profile/config together, restoring the prior pair on failure."""
        staged: list[Path] = []
        backups: dict[Path, Path | None] = {}
        targets = (self._paths.profile_md, self._paths.config_toml)
        try:
            self._paths.ensure_directories()
            profile_temp = _stage_bytes(self._paths.profile_md, profile_bytes, ".tmp")
            staged.append(profile_temp)
            config_temp = _stage_bytes(self._paths.config_toml, config_bytes, ".tmp")
            staged.append(config_temp)
            for target in targets:
                backups[target] = (
                    _stage_bytes(target, target.read_bytes(), ".bak")
                    if target.exists()
                    else None
                )
        except BaseException as error:
            _cleanup_paths((*staged, *(path for path in backups.values() if path)))
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise SetupPersistenceError(
                "Could not stage profile and configuration; check directory permissions and retry."
            ) from None

        try:
            os.replace(profile_temp, self._paths.profile_md)
            os.replace(config_temp, self._paths.config_toml)
        except BaseException as error:
            rollback_failed = _restore_pair(backups)
            _cleanup_paths((*staged, *(path for path in backups.values() if path)))
            if isinstance(error, (KeyboardInterrupt, SystemExit)) and not rollback_failed:
                raise
            raise SetupPersistenceError(
                "Could not publish profile and configuration; the previous setup was restored."
                if not rollback_failed
                else "Could not publish or fully restore setup files; inspect the data directory."
            ) from None
        _cleanup_paths((*staged, *(path for path in backups.values() if path)))

    def restore_pair(
        self,
        profile_bytes: bytes | None,
        config_bytes: bytes | None,
    ) -> None:
        """Restore the exact setup pair that existed before a failed workflow."""
        desired = {
            self._paths.profile_md: profile_bytes,
            self._paths.config_toml: config_bytes,
        }
        staged: dict[Path, Path] = {}
        backups: dict[Path, Path | None] = {}
        try:
            self._paths.ensure_directories()
            for target, payload in desired.items():
                if payload is not None:
                    staged[target] = _stage_bytes(target, payload, ".tmp")
                backups[target] = (
                    _stage_bytes(target, target.read_bytes(), ".bak")
                    if target.exists()
                    else None
                )
        except BaseException as error:
            _cleanup_paths(
                (*staged.values(), *(path for path in backups.values() if path))
            )
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise SetupPersistenceError(
                "Could not stage the previous setup for restoration."
            ) from None

        try:
            for target, payload in desired.items():
                if payload is None:
                    target.unlink(missing_ok=True)
                else:
                    os.replace(staged[target], target)
        except BaseException as error:
            rollback_failed = _restore_pair(backups)
            _cleanup_paths(
                (*staged.values(), *(path for path in backups.values() if path))
            )
            if isinstance(error, (KeyboardInterrupt, SystemExit)) and not rollback_failed:
                raise
            raise SetupPersistenceError(
                "Could not restore the previous setup files."
                if not rollback_failed
                else "Could not restore setup files; inspect the data directory."
            ) from None
        _cleanup_paths(
            (*staged.values(), *(path for path in backups.values() if path))
        )


def _validated_profile(invocation: ClaudeInvocation) -> str:
    """Return one strict profile string from bounded successful Claude JSON."""
    if invocation.exit_code != 0:
        raise SetupOutputError(_safe_claude_failure(invocation))
    try:
        result = json.loads(invocation.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SetupOutputError(
            "Claude returned invalid profile JSON; retry setup."
        ) from None
    if not isinstance(result, dict):
        raise SetupOutputError("Claude profile result was not a JSON object; retry setup.")
    structured = result.get("structured_output")
    if not isinstance(structured, dict):
        raise SetupOutputError(
            "Claude profile result lacked structured output; retry setup."
        )
    if set(structured) != {"profile_markdown"}:
        raise SetupOutputError(
            "Claude profile output had unexpected fields; retry setup."
        )
    profile = structured["profile_markdown"]
    if not isinstance(profile, str) or not profile.strip():
        raise SetupOutputError(
            "Claude profile output was empty or invalid; retry setup."
        )
    _validate_required_sections(profile)
    return profile


def _api_model_from_invocation(invocation: ClaudeInvocation) -> str | None:
    """Read the non-secret model name recorded by the API runtime adapter."""
    if len(invocation.argv) >= 3 and invocation.argv[0] == "anthropic-api":
        return invocation.argv[2]
    return None


def _safe_claude_failure(invocation: ClaudeInvocation) -> str:
    """Describe a known Claude CLI failure without echoing private process output."""
    stderr = invocation.stderr.decode("utf-8", errors="replace").lower()
    if "model" in stderr and any(
        marker in stderr for marker in ("not available", "unavailable", "invalid")
    ):
        reason = "the configured Claude model is unavailable"
    elif any(marker in stderr for marker in ("not logged in", "authentication")):
        reason = "Claude authentication failed"
    elif any(marker in stderr for marker in ("rate limit", "usage limit")):
        reason = "the Claude usage limit was reached"
    elif "overloaded" in stderr:
        reason = "the Claude service is overloaded"
    else:
        reason = "Claude returned an unclassified CLI error"
    return f"Claude profile generation failed: {reason} (exit code {invocation.exit_code})."


def _validate_required_sections(profile: str) -> None:
    """Require each exact profile heading to own non-empty Markdown content."""
    matches = list(_MARKDOWN_HEADING.finditer(profile))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        heading = match.group(1).strip()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(profile)
        sections.setdefault(heading, profile[match.end() : end])
    missing_or_empty = [
        heading
        for heading in PROFILE_HEADINGS
        if heading not in sections or not sections[heading].strip()
    ]
    if missing_or_empty:
        raise SetupOutputError(
            "Claude profile omitted required non-empty Markdown sections; retry setup."
        )


def _stage_bytes(target: Path, payload: bytes, suffix: str) -> Path:
    """Write and fsync one unique sibling staging file for a target."""
    descriptor, name = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=suffix,
    )
    path = Path(name)
    try:
        try:
            file = os.fdopen(descriptor, "wb")
        except BaseException:
            os.close(descriptor)
            raise
        with file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        return path
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _restore_pair(backups: dict[Path, Path | None]) -> bool:
    """Restore both targets to their exact pre-publication existence and bytes."""
    failed = False
    for target, backup in reversed(tuple(backups.items())):
        try:
            if backup is None:
                target.unlink(missing_ok=True)
            else:
                os.replace(backup, target)
        except OSError:
            failed = True
    return failed


def _cleanup_paths(paths: tuple[Path, ...]) -> None:
    """Remove all leftover staging and backup paths after success or failure."""
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
