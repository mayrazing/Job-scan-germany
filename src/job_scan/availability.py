from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from typing import Literal

from job_scan.dedup import merge_occurrences
from job_scan.domain import (
    AvailabilityEvent,
    AvailabilityStatus,
    JobRecord,
    Snapshot,
    SourceKind,
    SourceOccurrence,
)
from job_scan.sources.base import SourceRunResult


def update_availability(
    previous: Snapshot,
    results: Sequence[SourceRunResult],
    now: datetime,
) -> Snapshot:
    """Merge source results and apply only evidence-backed availability transitions."""
    occurrences = [
        occurrence
        for result in results
        for occurrence in result.occurrences
    ]
    snapshot = merge_occurrences(previous, occurrences, now)
    grouped: dict[tuple[SourceKind, str], list[SourceRunResult]] = defaultdict(list)
    for result in results:
        grouped[(result.source, result.source_instance)].append(result)

    for namespace in sorted(grouped, key=lambda value: (value[0].value, value[1])):
        source_results = grouped[namespace]
        successful = [result for result in source_results if result.completed_listing]
        if not successful:
            continue
        explicitly_closed = {
            key
            for result in successful
            for key in result.explicitly_closed_source_job_keys
        }
        for source_job_key in sorted(explicitly_closed):
            current = _current_generation(snapshot.jobs, source_job_key)
            if current is not None:
                _transition(current, AvailabilityStatus.CLOSED, "explicitly_closed", now)

        discovered = {
            key
            for result in successful
            for key in result.discovered_source_job_keys
        }
        fetched_complete = {
            occurrence.source_job_key
            for result in successful
            for occurrence in result.occurrences
            if occurrence.detail_complete
        }
        current_by_key = _current_generations_for_namespace(snapshot.jobs, namespace)
        for source_job_key, occurrence in sorted(current_by_key.items()):
            if source_job_key in explicitly_closed:
                continue
            if source_job_key not in discovered:
                if occurrence.availability_status is AvailabilityStatus.ACTIVE:
                    _transition(
                        occurrence,
                        AvailabilityStatus.STALE,
                        "missing_from_complete_listing",
                        now,
                    )
            elif (
                source_job_key in fetched_complete
                and occurrence.availability_status is not AvailabilityStatus.ACTIVE
            ):
                _transition(
                    occurrence,
                    AvailabilityStatus.ACTIVE,
                    "reappeared",
                    now,
                )

    for job in snapshot.jobs:
        job.availability_status = _canonical_availability(job)

    # Refresh primary selection and duplicate labels after active members changed.
    return merge_occurrences(snapshot, [], now)


def _current_generation(
    jobs: Sequence[JobRecord], source_job_key: str
) -> SourceOccurrence | None:
    occurrences = [
        occurrence
        for job in jobs
        for occurrence in job.source_occurrences
        if occurrence.source_job_key == source_job_key
    ]
    return max(occurrences, key=lambda item: item.source_generation, default=None)


def _current_generations_for_namespace(
    jobs: Sequence[JobRecord], namespace: tuple[SourceKind, str]
) -> dict[str, SourceOccurrence]:
    current: dict[str, SourceOccurrence] = {}
    for job in jobs:
        for occurrence in job.source_occurrences:
            if (occurrence.source, occurrence.source_instance) != namespace:
                continue
            stored = current.get(occurrence.source_job_key)
            if stored is None or occurrence.source_generation > stored.source_generation:
                current[occurrence.source_job_key] = occurrence
    return current


def _transition(
    occurrence: SourceOccurrence,
    status: AvailabilityStatus,
    reason: Literal[
        "missing_from_complete_listing",
        "explicitly_closed",
        "reappeared",
    ],
    now: datetime,
) -> None:
    if occurrence.availability_status is status:
        return
    occurrence.availability_status = status
    occurrence.closed_at = now if status is AvailabilityStatus.CLOSED else None
    occurrence.availability_events.append(
        AvailabilityEvent(
            status=status,
            reason=reason,
            observed_at=now,
        )
    )


def _canonical_availability(job: JobRecord) -> AvailabilityStatus:
    statuses = {item.availability_status for item in job.source_occurrences}
    if AvailabilityStatus.ACTIVE in statuses:
        return AvailabilityStatus.ACTIVE
    if AvailabilityStatus.STALE in statuses:
        return AvailabilityStatus.STALE
    return AvailabilityStatus.CLOSED
