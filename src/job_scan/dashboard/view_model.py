from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType

from job_scan.domain import (
    AvailabilityStatus,
    CompanySizeEvidence,
    JobRecord,
    MachineStatus,
    PrimaryView,
    Snapshot,
    UserStatus,
)
from job_scan.status import effective_status, primary_view

ACTIVE_VIEW_ORDER = (
    PrimaryView.RECOMMENDED,
    PrimaryView.PENDING,
    PrimaryView.SAVED,
    PrimaryView.EXCLUDED,
    PrimaryView.APPLIED,
    PrimaryView.INTERVIEWING,
    PrimaryView.OFFER,
    PrimaryView.WITHDRAWN,
    PrimaryView.REJECTED,
    PrimaryView.IGNORED,
)

CURRENT_VIEW_ORDER = (
    PrimaryView.RECOMMENDED,
    PrimaryView.PENDING,
    PrimaryView.EXCLUDED,
)

GLOBAL_VIEW_ORDER = (
    PrimaryView.SAVED,
    PrimaryView.APPLIED,
    PrimaryView.INTERVIEWING,
    PrimaryView.OFFER,
    PrimaryView.WITHDRAWN,
    PrimaryView.REJECTED,
    PrimaryView.IGNORED,
)

_GROUP_TITLES = {
    PrimaryView.RECOMMENDED: "Recommended",
    PrimaryView.SAVED: "Saved",
    PrimaryView.PENDING: "Pending review",
    PrimaryView.EXCLUDED: "Excluded",
    PrimaryView.APPLIED: "Applied",
    PrimaryView.INTERVIEWING: "Interviewing",
    PrimaryView.OFFER: "Offer",
    PrimaryView.WITHDRAWN: "Withdrawn",
    PrimaryView.REJECTED: "Rejected",
    PrimaryView.IGNORED: "Ignored",
}

_COMPANY_SIZE_BOUNDS = {
    "1-49": (1, 49),
    "50-249": (50, 249),
    "250-999": (250, 999),
    "1000-9999": (1000, 9999),
    "10000+": (10000, None),
    "unknown": (None, None),
}


@dataclass(frozen=True, slots=True)
class JobStatusEvent:
    """Expose one immutable Job Tracker status change."""

    status: UserStatus
    changed_at: datetime


@dataclass(frozen=True, slots=True)
class JobCard:
    """Expose one job's display facts without adding dashboard-owned state."""

    canonical_key: str
    company: str
    title: str
    location: str
    description: str
    posted_at: date | None
    url: str
    job_snapshot_id: str | None
    job_snapshot_error_code: str | None
    job_snapshot_triggerable: bool
    score: int | None
    reason: str
    source_error: str | None
    review_error: str | None
    german_requirement: str | None
    labels: tuple[str, ...]
    exclusion_reasons: tuple[str, ...]
    verbatim_evidence: tuple[str, ...]
    source_names: tuple[str, ...]
    first_seen: datetime
    last_seen: datetime
    user_status: UserStatus
    user_status_history: tuple[JobStatusEvent, ...]
    application_resume_id: str | None
    machine_status: MachineStatus
    effective_status: MachineStatus
    availability_status: AvailabilityStatus
    restored: bool
    restorable: bool
    company_size_label: str | None
    company_size_source_url: str | None
    company_size_source_title: str | None
    company_size_checked_at: datetime | None
    company_size_minimum: int | None
    company_size_maximum: int | None
    company_industry_label: str | None
    company_industry_lookup_method: str | None
    company_industry_source_url: str | None
    company_industry_source_title: str | None
    company_industry_source_name: str | None
    company_industry_checked_at: datetime | None
    company_industry_confidence: str | None
    company_industry_evidence: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DashboardGroup:
    """Store one named, ordered sequence of immutable job cards."""

    id: str
    title: str
    cards: tuple[JobCard, ...]

    @property
    def count(self) -> int:
        """Return the number displayed with this group."""
        return len(self.cards)


@dataclass(frozen=True, slots=True)
class DashboardViewModel:
    """Store one ordered set of visible Review groups."""

    active_groups: Mapping[PrimaryView, DashboardGroup]


def build_dashboard(snapshot: Snapshot) -> DashboardViewModel:
    """Project one validated snapshot into deterministic dashboard groups."""
    return _build_dashboard(snapshot, ACTIVE_VIEW_ORDER, primary_view)


def build_current_dashboard(snapshot: Snapshot) -> DashboardViewModel:
    """Project one search snapshot into its three machine-owned groups."""
    return _build_dashboard(snapshot, CURRENT_VIEW_ORDER, primary_view)


def build_global_dashboard(snapshot: Snapshot) -> DashboardViewModel:
    """Project globally decided jobs into the seven user-owned groups."""
    user_views = {
        UserStatus.SAVED: PrimaryView.SAVED,
        UserStatus.APPLIED: PrimaryView.APPLIED,
        UserStatus.INTERVIEWING: PrimaryView.INTERVIEWING,
        UserStatus.OFFER: PrimaryView.OFFER,
        UserStatus.WITHDRAWN: PrimaryView.WITHDRAWN,
        UserStatus.REJECTED: PrimaryView.REJECTED,
        UserStatus.IGNORED: PrimaryView.IGNORED,
    }
    return _build_dashboard(
        snapshot,
        GLOBAL_VIEW_ORDER,
        lambda job: user_views.get(job.user_status),
    )


def _build_dashboard(
    snapshot: Snapshot,
    order: tuple[PrimaryView, ...],
    view_for_job: Callable[[JobRecord], PrimaryView | None],
) -> DashboardViewModel:
    """Project jobs into one explicitly ordered set of dashboard groups."""
    active_jobs: dict[PrimaryView, list[JobRecord]] = {
        view: [] for view in order
    }

    for job in snapshot.jobs:
        view = view_for_job(job)
        if view in active_jobs:
            active_jobs[view].append(job)

    groups = {
        view: DashboardGroup(
            id=view.value,
            title=_GROUP_TITLES[view],
            cards=tuple(_card(job) for job in _sort_active(active_jobs[view])),
        )
        for view in order
    }
    return DashboardViewModel(active_groups=MappingProxyType(groups))


def _card(job: JobRecord) -> JobCard:
    """Copy raw display facts from one record into an immutable card."""
    status = effective_status(job)
    source_names = tuple(
        sorted({occurrence.source.value for occurrence in job.source_occurrences})
    )
    if not source_names and job.primary_source_occurrence_key:
        source_names = (job.primary_source_occurrence_key.partition(":")[0],)
    evidence = (
        tuple(job.ai_review.eligibility_evidence) if job.ai_review is not None else ()
    )
    company_size = job.company_size
    company_industry = job.company_industry
    job_snapshot_id, job_snapshot_error_code = _job_snapshot_state(job)
    company_size_minimum, company_size_maximum = _company_size_bounds(company_size)
    return JobCard(
        canonical_key=job.canonical_job_key,
        company=job.company,
        title=job.title,
        location=job.location,
        description=job.description,
        posted_at=job.posted_at,
        url=str(job.url),
        job_snapshot_id=job_snapshot_id,
        job_snapshot_error_code=job_snapshot_error_code,
        job_snapshot_triggerable=(
            job_snapshot_id is None
            and bool(job.source_occurrences)
        ),
        score=job.score,
        reason=job.reason,
        source_error=(
            job.last_error
            if job.machine_status is MachineStatus.PENDING_SOURCE
            else None
        ),
        review_error=(
            job.last_error if job.machine_status is MachineStatus.PENDING else None
        ),
        german_requirement=(
            job.ai_review.german_requirement if job.ai_review is not None else None
        ),
        labels=tuple(job.labels),
        exclusion_reasons=tuple(job.exclusion_reasons),
        verbatim_evidence=evidence,
        source_names=source_names,
        first_seen=job.first_seen,
        last_seen=job.last_seen,
        user_status=job.user_status,
        user_status_history=tuple(
            JobStatusEvent(status=entry.status, changed_at=entry.changed_at)
            for entry in job.user_status_history
        ),
        application_resume_id=job.application_resume_id,
        machine_status=job.machine_status,
        effective_status=status,
        availability_status=job.availability_status,
        restored=status is not job.machine_status,
        restorable=job.last_successful_review_profile_hash is not None,
        company_size_label=(
            company_size.reported_size
            or _company_size_label(company_size.band.value)
            if company_size is not None
            else None
        ),
        company_size_source_url=(
            str(company_size.source_url)
            if company_size is not None and company_size.source_url is not None
            else None
        ),
        company_size_source_title=(
            company_size.source_title if company_size is not None else None
        ),
        company_size_checked_at=(
            company_size.checked_at if company_size is not None else None
        ),
        company_size_minimum=company_size_minimum,
        company_size_maximum=company_size_maximum,
        company_industry_label=(
            company_industry.industry if company_industry is not None else None
        ),
        company_industry_lookup_method=(
            company_industry.lookup_method if company_industry is not None else None
        ),
        company_industry_source_url=(
            str(company_industry.source_url)
            if company_industry is not None
            else None
        ),
        company_industry_source_title=(
            company_industry.source_title if company_industry is not None else None
        ),
        company_industry_source_name=(
            company_industry.source_name if company_industry is not None else None
        ),
        company_industry_checked_at=(
            company_industry.checked_at if company_industry is not None else None
        ),
        company_industry_confidence=(
            company_industry.confidence if company_industry is not None else None
        ),
        company_industry_evidence=(
            tuple(company_industry.evidence) if company_industry is not None else ()
        ),
    )


def _job_snapshot_state(job: JobRecord) -> tuple[str | None, str | None]:
    """Prefer any usable occurrence snapshot, then expose an attempted failure."""
    primary = next(
        (
            occurrence
            for occurrence in job.source_occurrences
            if occurrence.source_occurrence_key == job.primary_source_occurrence_key
        ),
        None,
    )
    occurrences = [
        occurrence
        for occurrence in [primary, *job.source_occurrences]
        if occurrence is not None
    ]
    for occurrence in occurrences:
        if occurrence.job_snapshot is not None:
            return occurrence.job_snapshot.snapshot_id, None
    for occurrence in occurrences:
        if occurrence.job_snapshot_error_code is not None:
            return None, occurrence.job_snapshot_error_code
    return None, None


def _company_size_bounds(
    company_size: CompanySizeEvidence | None,
) -> tuple[int | None, int | None]:
    """Return numeric bounds used by review-page company-size filtering."""
    if company_size is None:
        return None, None
    if company_size.minimum_employees is not None:
        return company_size.minimum_employees, company_size.maximum_employees
    if company_size.employee_count is not None:
        return company_size.employee_count, company_size.employee_count
    return _COMPANY_SIZE_BOUNDS[company_size.band.value]


def _company_size_label(band: str) -> str:
    """Return the compact human label for one persisted size band."""
    return {
        "1-49": "1-49",
        "50-249": "50-249",
        "250-999": "250-999",
        "1000-9999": "1,000-9,999",
        "10000+": "10,000+",
        "unknown": "Unknown",
    }[band]


def _sort_active(jobs: list[JobRecord]) -> list[JobRecord]:
    """Sort every active group by adjusted score, posting date, then stable key."""
    return sorted(jobs, key=_score_sort_key)


def _score_sort_key(job: JobRecord) -> tuple[int, int, str]:
    """Put high adjusted scores and recent posting dates first, then stable keys."""
    score = job.score if job.score is not None else -1
    posted_ordinal = job.posted_at.toordinal() if job.posted_at is not None else -1
    return (-score, -posted_ordinal, job.canonical_job_key)
