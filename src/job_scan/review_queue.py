from __future__ import annotations

from datetime import datetime

from job_scan.domain import (
    AvailabilityStatus,
    JobRecord,
    MachineStatus,
    Snapshot,
)


def select_jobs_for_review(
    snapshot: Snapshot,
    profile_hash: str,
    now: datetime,
    force: bool = False,
) -> list[JobRecord]:
    """Return active complete jobs whose current review inputs are due."""
    selected: list[JobRecord] = []
    for job in snapshot.jobs:
        if job.availability_status is not AvailabilityStatus.ACTIVE:
            continue
        if not _has_complete_primary_description(job):
            job.machine_status = MachineStatus.PENDING_SOURCE
            continue
        if force or _is_due(job, profile_hash, now):
            selected.append(job)
    return sorted(selected, key=lambda item: item.canonical_job_key)


def _has_complete_primary_description(job: JobRecord) -> bool:
    """Return whether the canonical primary occurrence has a complete non-empty JD."""
    primary = next(
        (
            occurrence
            for occurrence in job.source_occurrences
            if occurrence.source_occurrence_key == job.primary_source_occurrence_key
        ),
        None,
    )
    return bool(
        primary is not None
        and primary.detail_complete
        and job.description.strip()
    )


def _is_due(job: JobRecord, profile_hash: str, now: datetime) -> bool:
    """Return whether one non-forced review reason applies to this job."""
    if (
        job.last_review_attempt_content_hash is None
        or job.last_review_attempt_profile_hash is None
    ):
        return True
    if (
        job.last_review_attempt_content_hash != job.content_hash
        or job.last_review_attempt_profile_hash != profile_hash
    ):
        return True
    return any(
        event.reason == "reappeared"
        and event.status is AvailabilityStatus.ACTIVE
        and event.observed_at <= now
        and (
            job.last_review_attempt_at is None
            or event.observed_at > job.last_review_attempt_at
        )
        for occurrence in job.source_occurrences
        for event in occurrence.availability_events
    )
