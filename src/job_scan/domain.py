from __future__ import annotations

import ipaddress
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Literal, Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    computed_field,
    field_validator,
    model_validator,
)

from job_scan.job_snapshot import JobSnapshotReference


class SourceKind(StrEnum):
    ARBEITSAGENTUR = "arbeitsagentur"
    BOSCH = "bosch"
    DALLMEIER = "dallmeier"
    DHL = "dhl"
    GLASSDOOR = "glassdoor"
    INDEED = "indeed"
    LINKEDIN = "linkedin"
    MANUAL = "manual"
    SIEMENS = "siemens"
    SIMPLIFY = "simplify"
    SMARTRECRUITERS = "smartrecruiters"
    STEPSTONE = "stepstone"
    SUCCESSFACTORS = "successfactors"
    TELEKOM = "telekom"
    THYSSENKRUPP = "thyssenkrupp"


class MachineStatus(StrEnum):
    PENDING_SOURCE = "pending_source"
    PENDING = "pending"
    ELIGIBLE = "eligible"
    EXCLUDED = "excluded"
    UNCERTAIN = "uncertain"


class UserStatus(StrEnum):
    NEW = "new"
    SAVED = "saved"
    APPLIED = "applied"
    INTERVIEWING = "interviewing"
    OFFER = "offer"
    WITHDRAWN = "withdrawn"
    REJECTED = "rejected"
    IGNORED = "ignored"


class UserStatusHistoryEntry(BaseModel):
    """Record one confirmed Job Tracker status change."""

    status: UserStatus
    changed_at: datetime

    @field_validator("status")
    @classmethod
    def require_tracker_status(cls, value: UserStatus) -> UserStatus:
        if value is UserStatus.NEW:
            raise ValueError("status history cannot contain new")
        return value

    @field_validator("changed_at")
    @classmethod
    def normalize_changed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("changed_at must be timezone-aware")
        return value.astimezone(UTC)


class SalaryPeriod(StrEnum):
    MONTH = "month"
    YEAR = "year"


class SalaryValue(BaseModel):
    """Store one user-entered salary amount and its period."""

    amount: str = Field(min_length=1, max_length=100)
    period: SalaryPeriod

    @field_validator("amount")
    @classmethod
    def trim_amount(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("salary amount cannot be blank")
        return value


class JobNote(BaseModel):
    """Store one dated user note attached to a Job Tracker job."""

    id: UUID
    content: str = Field(min_length=1, max_length=2000)
    created_at: datetime

    @field_validator("content")
    @classmethod
    def trim_content(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("note cannot be empty")
        return value

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("note created_at must be timezone-aware")
        return value.astimezone(UTC)


class ReevaluationNotice(BaseModel):
    """Persist one unacknowledged Job Tracker re-evaluation result."""

    status: Literal["succeeded", "failed"]
    finished_at: datetime

    @field_validator("finished_at")
    @classmethod
    def normalize_finished_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("re-evaluation finished_at must be timezone-aware")
        return value.astimezone(UTC)


class AvailabilityStatus(StrEnum):
    ACTIVE = "active"
    STALE = "stale"
    CLOSED = "closed"


class PrimaryView(StrEnum):
    RECOMMENDED = "recommended"
    SAVED = "saved"
    PENDING = "pending"
    EXCLUDED = "excluded"
    APPLIED = "applied"
    INTERVIEWING = "interviewing"
    OFFER = "offer"
    WITHDRAWN = "withdrawn"
    REJECTED = "rejected"
    IGNORED = "ignored"


class CompanySizeBand(StrEnum):
    UNDER_50 = "1-49"
    FROM_50_TO_249 = "50-249"
    FROM_250_TO_999 = "250-999"
    FROM_1000_TO_9999 = "1000-9999"
    FROM_10000 = "10000+"
    UNKNOWN = "unknown"


class CompanySizeEvidence(BaseModel):
    """Store one dated company-size conclusion with its public source."""

    model_config = ConfigDict(extra="forbid")

    company_name: str = Field(min_length=1, max_length=300)
    band: CompanySizeBand
    employee_count: int | None = Field(default=None, ge=1)
    reported_size: str | None = Field(default=None, min_length=1, max_length=100)
    minimum_employees: int | None = Field(default=None, ge=1)
    maximum_employees: int | None = Field(default=None, ge=1)
    source_url: HttpUrl | None = None
    source_title: str | None = Field(default=None, max_length=500)
    checked_at: datetime
    confidence: Literal["high", "medium", "low"]
    lookup_method: Literal["native", "ai", "unknown"] = "ai"
    source_name: Literal[
        "arbeitsagentur",
        "glassdoor",
        "linkedin",
        "indeed",
        "simplify",
        "stepstone",
        "ai",
    ] | None = None

    @field_validator("checked_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("checked_at must include a timezone")
        return value

    @model_validator(mode="after")
    def require_source_for_known_size(self) -> Self:
        has_range = self.minimum_employees is not None or self.maximum_employees is not None
        if (self.band is not CompanySizeBand.UNKNOWN or has_range) and self.source_url is None:
            raise ValueError("known company size requires a public source URL")
        if self.band is CompanySizeBand.UNKNOWN and self.employee_count is not None:
            raise ValueError("unknown company size cannot include an employee count")
        if self.maximum_employees is not None and self.minimum_employees is None:
            raise ValueError("company size maximum requires a minimum")
        if (
            self.minimum_employees is not None
            and self.maximum_employees is not None
            and self.maximum_employees < self.minimum_employees
        ):
            raise ValueError("company size maximum must not be below minimum")
        if self.reported_size is not None and self.minimum_employees is None:
            raise ValueError("reported company size requires a parsed minimum")
        if self.lookup_method == "native" and (
            self.source_name
            not in {
                "arbeitsagentur",
                "glassdoor",
                "linkedin",
                "indeed",
                "simplify",
                "stepstone",
            }
            or self.reported_size is None
        ):
            raise ValueError("native company size requires its source and reported value")
        if (
            self.employee_count is not None
            and _company_size_band(self.employee_count) is not self.band
        ):
            raise ValueError("employee count does not match company size band")
        if (
            self.employee_count is not None
            and self.minimum_employees is not None
            and self.employee_count < self.minimum_employees
        ):
            raise ValueError("employee count is below company size minimum")
        if (
            self.employee_count is not None
            and self.maximum_employees is not None
            and self.employee_count > self.maximum_employees
        ):
            raise ValueError("employee count is above company size maximum")
        if self.source_url is not None:
            _require_public_source_url(self.source_url)
        return self


class CompanySizeSource(BaseModel):
    """Store one source-native location that may publish company size."""

    model_config = ConfigDict(extra="forbid")

    source_name: Literal[
        "arbeitsagentur", "glassdoor", "linkedin", "indeed", "simplify", "stepstone"
    ]
    lookup_url: HttpUrl
    public_url: HttpUrl
    source_title: str = Field(min_length=1, max_length=500)
    reported_size: str | None = Field(default=None, min_length=1, max_length=100)


class CompanyIndustryEvidence(BaseModel):
    """Store one sourced or JD-grounded company-industry conclusion."""

    model_config = ConfigDict(extra="forbid")

    company_name: str = Field(min_length=1, max_length=300)
    industry: str = Field(min_length=1, max_length=300)
    source_url: HttpUrl
    source_title: str = Field(min_length=1, max_length=500)
    checked_at: datetime
    confidence: Literal["high", "medium", "low"]
    lookup_method: Literal["native", "ai"]
    source_name: Literal[
        "glassdoor",
        "linkedin",
        "indeed",
        "smartrecruiters",
        "stepstone",
        "ai",
    ]
    evidence: list[str] = Field(default_factory=list)

    @field_validator("checked_at")
    @classmethod
    def require_industry_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("checked_at must include a timezone")
        return value

    @model_validator(mode="after")
    def require_method_evidence(self) -> Self:
        if self.lookup_method == "native" and self.source_name == "ai":
            raise ValueError("native company industry requires a source name")
        if self.lookup_method == "ai" and (
            self.source_name != "ai" or not self.evidence
        ):
            raise ValueError("AI company industry requires JD evidence")
        _require_public_source_url(self.source_url)
        return self


class CompanyIndustrySource(BaseModel):
    """Store one source-native location that may publish company industry."""

    model_config = ConfigDict(extra="forbid")

    source_name: Literal[
        "glassdoor", "linkedin", "indeed", "smartrecruiters", "stepstone"
    ]
    lookup_url: HttpUrl
    public_url: HttpUrl
    source_title: str = Field(min_length=1, max_length=500)
    reported_industry: str | None = Field(default=None, min_length=1, max_length=300)


def _company_size_band(employee_count: int) -> CompanySizeBand:
    if employee_count < 50:
        return CompanySizeBand.UNDER_50
    if employee_count < 250:
        return CompanySizeBand.FROM_50_TO_249
    if employee_count < 1000:
        return CompanySizeBand.FROM_250_TO_999
    if employee_count < 10000:
        return CompanySizeBand.FROM_1000_TO_9999
    return CompanySizeBand.FROM_10000


def _require_public_source_url(value: HttpUrl) -> None:
    parsed = urlsplit(str(value))
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or host.casefold() == "localhost"
        or host.casefold().endswith((".localhost", ".local"))
    ):
        raise ValueError("company size source must use public HTTPS")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return
    if not address.is_global:
        raise ValueError("company size source must use public HTTPS")


class MergeEvidence(BaseModel):
    other_source_occurrence_key: str
    rule: Literal["job_url", "text_similarity"]
    normalized_url: str | None = None
    normalized_company: str
    normalized_title: str
    normalized_location: str
    posted_at_delta_days: int | None = None
    similarity: float | None = Field(default=None, ge=0, le=1)
    observed_at: datetime


class DuplicateEvidence(BaseModel):
    other_canonical_job_key: str
    reason: Literal["candidate_conflict", "similarity_band"]
    similarity: float | None = Field(default=None, ge=0, le=1)
    observed_at: datetime
    decision_source_occurrence_key: str | None = None


class AvailabilityEvent(BaseModel):
    status: AvailabilityStatus
    reason: Literal[
        "listed",
        "detail_open",
        "missing_from_complete_listing",
        "explicitly_closed",
        "reappeared",
    ]
    observed_at: datetime


class SourceOccurrence(BaseModel):
    source: SourceKind
    source_instance: str
    external_id: str
    source_generation: int
    url: HttpUrl
    company: str
    title: str
    location: str
    description: str
    posted_at: date | None = None
    content_hash: str
    availability_status: AvailabilityStatus
    detail_complete: bool = False
    last_fetch_error_code: str | None = None
    job_snapshot: JobSnapshotReference | None = None
    job_snapshot_error_code: str | None = None
    company_size_source: CompanySizeSource | None = None
    company_industry_source: CompanyIndustrySource | None = None
    closed_at: datetime | None = None
    identity_baseline_title: str = ""
    identity_baseline_description: str = ""
    merge_evidence: list[MergeEvidence] = Field(default_factory=list)
    availability_events: list[AvailabilityEvent] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def source_job_key(self) -> str:
        return f"{self.source.value}:{self.source_instance}:{self.external_id}"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def source_occurrence_key(self) -> str:
        return f"{self.source_job_key}@{self.source_generation}"


class AIReview(BaseModel):
    job_key: str
    german_requirement: Literal["required", "optional", "none", "uncertain"]
    visa_sponsorship: Literal["offered", "not_offered", "not_mentioned", "uncertain"]
    existing_work_authorization: Literal[
        "required", "not_required", "not_mentioned", "uncertain"
    ]
    citizenship_requirement: Literal["german_or_eu", "other", "none", "uncertain"]
    security_clearance: Literal["required", "optional", "none", "uncertain"]
    staffing_agency: Literal["yes", "no", "uncertain"]
    eligibility_evidence: list[str] = Field(default_factory=list)
    company_industry: str | None = Field(min_length=1, max_length=300)
    company_industry_confidence: Literal["high", "medium", "low"]
    company_industry_evidence: list[str]
    score: int = Field(ge=0, le=100)
    reason: str
    confidence: Literal["high", "medium", "low"]


class ReviewHistoryEntry(BaseModel):
    attempted_at: datetime
    content_hash: str
    profile_hash: str
    model: str
    outcome: Literal["accepted", "failed"]
    review: AIReview | None = None
    failure_category: str | None = None


class _LegacyResumeMatch(BaseModel):
    """Read one legacy per-resume review while collapsing old tracker data."""

    resume_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    profile_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    machine_status: MachineStatus
    manual_override: Literal["show"] | None = None
    manual_override_content_hash: str | None = None
    manual_override_profile_hash: str | None = None
    ai_review: AIReview | None = None
    score: int | None = Field(default=None, ge=0, le=100)
    reason: str = ""
    review_model: str | None = None
    reviewed_at: datetime | None = None
    last_review_attempt_content_hash: str | None = None
    last_review_attempt_profile_hash: str | None = None
    last_review_attempt_at: datetime | None = None
    last_successful_review_content_hash: str | None = None
    last_successful_review_profile_hash: str | None = None
    exclusion_reasons: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    last_error: str | None = None

    @field_validator("reviewed_at", "last_review_attempt_at")
    @classmethod
    def normalize_legacy_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class JobRecord(BaseModel):
    record_type: Literal["job"] = "job"
    canonical_job_key: str
    source_occurrences: list[SourceOccurrence] = Field(default_factory=list)
    primary_source_occurrence_key: str
    company: str
    title: str
    location: str
    url: HttpUrl
    description: str
    posted_at: date | None
    content_hash: str
    first_seen: datetime
    last_seen: datetime
    availability_status: AvailabilityStatus
    machine_status: MachineStatus = MachineStatus.PENDING
    user_status: UserStatus = UserStatus.NEW
    user_status_updated_at: datetime
    user_status_history: list[UserStatusHistoryEntry] = Field(
        default_factory=list,
        exclude_if=lambda value: not value,
    )
    application_resume_id: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
        exclude_if=lambda value: value is None,
    )
    application_resume_filename: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        exclude_if=lambda value: value is None,
    )
    last_evaluated_resume_id: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
        exclude_if=lambda value: value is None,
    )
    reevaluation_notice: ReevaluationNotice | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    reevaluation_acknowledged_at: datetime | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    expected_salary: SalaryValue | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    offer_salary: SalaryValue | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    notes: list[JobNote] = Field(
        default_factory=list,
        exclude_if=lambda value: not value,
    )
    manual_posted_at: date | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    manual_company_size: int | None = Field(
        default=None,
        ge=1,
        exclude_if=lambda value: value is None,
    )
    manual_company_industry: str | None = Field(
        default=None,
        min_length=1,
        max_length=300,
        exclude_if=lambda value: value is None,
    )
    manual_import_errors: list[str] = Field(
        default_factory=list,
        exclude_if=lambda value: not value,
    )
    global_status_deleted_at: datetime | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    manual_override: Literal["show"] | None = None
    manual_override_content_hash: str | None = None
    manual_override_profile_hash: str | None = None
    ai_review: AIReview | None = None
    review_history: list[ReviewHistoryEntry] = Field(default_factory=list)
    possible_duplicates: list[DuplicateEvidence] = Field(default_factory=list)
    score: int | None = Field(default=None, ge=0, le=100)
    reason: str = ""
    review_model: str | None = None
    reviewed_at: datetime | None = None
    last_review_attempt_content_hash: str | None = None
    last_review_attempt_profile_hash: str | None = None
    last_review_attempt_at: datetime | None = None
    last_successful_review_content_hash: str | None = None
    last_successful_review_profile_hash: str | None = None
    exclusion_reasons: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    last_error: str | None = None
    company_size: CompanySizeEvidence | None = None
    company_industry: CompanyIndustryEvidence | None = None

    @model_validator(mode="before")
    @classmethod
    def collapse_legacy_resume_matches(cls, value: object) -> object:
        """Keep one visible review when reading legacy per-resume history."""
        if not isinstance(value, dict) or "resume_matches" not in value:
            return value
        migrated = dict(value)
        raw_matches = migrated.pop("resume_matches")
        if migrated.get("last_evaluated_resume_id") is not None:
            return migrated
        if not isinstance(raw_matches, list):
            raise TypeError("resume_matches must be a list")
        matches = [_LegacyResumeMatch.model_validate(item) for item in raw_matches]
        if not matches:
            return migrated
        current_resume_id = migrated.get("application_resume_id")
        selected = next(
            (
                match
                for match in reversed(matches)
                if match.resume_id == current_resume_id
            ),
            None,
        )
        if selected is None:
            selected = max(
                enumerate(matches),
                key=lambda item: (_resume_match_timestamp(item[1]), item[0]),
            )[1]
        migrated["last_evaluated_resume_id"] = selected.resume_id
        for field_name, field_value in selected.model_dump(
            mode="python",
            exclude={"resume_id", "profile_hash"},
        ).items():
            migrated[field_name] = field_value
        return migrated

    @field_validator("user_status", mode="before")
    @classmethod
    def migrate_legacy_saved_statuses(cls, value: object) -> object:
        """Keep old reviewed and shortlisted jobs visible as saved jobs."""
        return UserStatus.SAVED if value in {"reviewed", "shortlisted"} else value

    @field_validator("reevaluation_acknowledged_at")
    @classmethod
    def normalize_reevaluation_acknowledged_at(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("re-evaluation acknowledgement must be timezone-aware")
        return value.astimezone(UTC)


def _resume_match_timestamp(match: _LegacyResumeMatch) -> datetime:
    timestamps = [
        value
        for value in (match.reviewed_at, match.last_review_attempt_at)
        if value is not None
    ]
    return max(timestamps, default=datetime.min.replace(tzinfo=UTC))


class GlobalJobDeletion(BaseModel):
    """Keep only job identifiers needed to suppress passive re-imports."""

    canonical_job_keys: list[str]
    source_job_keys: list[str]
    deleted_at: datetime

    @field_validator("deleted_at")
    @classmethod
    def normalize_deleted_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("deleted_at must be timezone-aware")
        return value.astimezone(UTC)


class StoreMeta(BaseModel):
    record_type: Literal["meta"] = "meta"
    data_revision: int
    generated_at: datetime | None = None
    global_job_deletions: list[GlobalJobDeletion] = Field(
        default_factory=list,
        exclude_if=lambda value: not value,
    )


class Snapshot(BaseModel):
    meta: StoreMeta
    jobs: list[JobRecord] = Field(default_factory=list)

    def with_job(self, job: JobRecord) -> Snapshot:
        """Return a validated snapshot with one job appended."""
        return Snapshot(meta=self.meta, jobs=[*self.jobs, job])

    @model_validator(mode="after")
    def require_unique_job_and_occurrence_keys(self) -> Self:
        canonical_keys: set[str] = set()
        occurrence_keys: set[str] = set()

        for job in self.jobs:
            if job.canonical_job_key in canonical_keys:
                raise ValueError(
                    f"duplicate canonical_job_key: {job.canonical_job_key}"
                )
            canonical_keys.add(job.canonical_job_key)

            for occurrence in job.source_occurrences:
                if occurrence.source_occurrence_key in occurrence_keys:
                    raise ValueError(
                        "duplicate source_occurrence_key: "
                        f"{occurrence.source_occurrence_key}"
                    )
                occurrence_keys.add(occurrence.source_occurrence_key)

        return self
