from __future__ import annotations

import os
import tempfile
import tomllib
from pathlib import Path
from typing import Any, Literal

import tomli_w
from pydantic import BaseModel, Field, model_validator

PostedWithinDays = Literal[0, 1, 3, 7, 14]
MinimumCompanySize = Literal[0, 50, 250, 1000, 10000]
TargetCompany = Literal[
    "advantech",
    "bosch",
    "dallmeier",
    "dhl",
    "haier",
    "johnson-electric",
    "nexperia",
    "rohde-schwarz",
    "siemens",
    "telekom",
    "thyssenkrupp",
    "vossloh",
]
_NO_POSTING_WINDOW = "no-limit"
STAFFING_AGENCY_PENALTY = 10
OPENCLI_SOURCE_FIELDS = (
    ("linkedin_enabled", "linkedin_limit"),
    ("indeed_de_enabled", "indeed_de_limit"),
    ("stepstone_de_enabled", "stepstone_de_limit"),
    ("glassdoor_de_enabled", "glassdoor_de_limit"),
    ("simplify_de_enabled", "simplify_de_limit"),
)


def migrate_legacy_opencli_settings(value: Any) -> Any:
    """Convert legacy zero limits into explicit disabled source switches."""
    if not isinstance(value, dict):
        return value
    migrated = dict(value)
    for enabled_field, limit_field in OPENCLI_SOURCE_FIELDS:
        if enabled_field in migrated:
            continue
        legacy_limit = migrated.get(limit_field, 10)
        migrated[enabled_field] = legacy_limit not in (0, "0")
        if legacy_limit in (0, "0"):
            migrated[limit_field] = 10
    return migrated


class ClaudeSettings(BaseModel):
    model: str = Field(min_length=1)
    effort: Literal["low", "medium", "high", "xhigh", "max", "ultra"]
    thinking_enabled: bool = True
    batch_size: int = Field(default=10, gt=0)
    timeout_seconds: int = Field(default=180, gt=0)
    max_output_bytes: int = Field(default=2_000_000, gt=0)


class SchedulerSettings(BaseModel):
    local_time: str | None = Field(
        default=None,
        pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$",
    )


class AppConfig(BaseModel):
    candidate_name: str = Field(default="", max_length=200)
    ai_runtime: str = Field(
        default="claude-code",
        pattern=r"^(?:claude-code|codex-cli|api:[a-z0-9]+(?:-[a-z0-9]+)*)$",
    )
    ai_model: str | None = Field(default=None, min_length=1)
    resume_path: Path
    resume_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    profile_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    country: Literal["DE"] = "DE"
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
    needs_visa_sponsorship: Literal[True] = True
    staffing_penalty: int = Field(default=STAFFING_AGENCY_PENALTY, ge=0, le=100)
    claude: ClaudeSettings
    scheduler: SchedulerSettings

    @model_validator(mode="before")
    @classmethod
    def migrate_opencli_switches(cls, value: Any) -> Any:
        """Load pre-switch configs without re-enabling zero-limit sources."""
        return migrate_legacy_opencli_settings(value)

    @property
    def selected_model(self) -> str:
        """Return the actual persisted API or selected local CLI model."""
        return self.ai_model or self.claude.model


def load_config(path: Path) -> AppConfig:
    return load_config_bytes(path.read_bytes())


def load_config_bytes(contents: bytes) -> AppConfig:
    """Parse validated configuration bytes, including persisted sentinel values."""
    data = tomllib.loads(contents.decode("utf-8"))
    if data.get("posted_within_days") == _NO_POSTING_WINDOW:
        data["posted_within_days"] = None
    return AppConfig.model_validate(data)


def serialize_config(config: AppConfig) -> str:
    """Serialize one validated config while preserving an explicit no-limit window."""
    validated = AppConfig.model_validate(
        config.model_dump(mode="json", warnings=False)
    )
    data = validated.model_dump(mode="json", exclude_none=True)
    if validated.posted_within_days is None:
        data["posted_within_days"] = _NO_POSTING_WINDOW
    return tomli_w.dumps(data)


def save_config(path: Path, config: AppConfig) -> None:
    serialized = serialize_config(config)
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as temporary_file:
            temporary_file.write(serialized)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
