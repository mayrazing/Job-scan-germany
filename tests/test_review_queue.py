from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from job_scan.domain import (
    AvailabilityEvent,
    AvailabilityStatus,
    JobRecord,
    MachineStatus,
    Snapshot,
    SourceKind,
    SourceOccurrence,
    StoreMeta,
)
from job_scan.review_queue import select_jobs_for_review

NOW = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)
PROFILE_HASH = "sha256:profile"
CONTENT_HASH = "sha256:content"


def job(key: str, **updates: object) -> JobRecord:
    occurrence = SourceOccurrence(
        source=SourceKind.LINKEDIN,
        source_instance="acme/jobs",
        external_id=key,
        source_generation=1,
        url=f"https://acme.example/jobs/{key}",
        company="Acme",
        title="Backend Engineer",
        location="Berlin",
        description="Build reliable backend services.",
        posted_at=None,
        content_hash=CONTENT_HASH,
        availability_status=AvailabilityStatus.ACTIVE,
        detail_complete=True,
    )
    values: dict[str, object] = {
        "canonical_job_key": key,
        "source_occurrences": [occurrence],
        "primary_source_occurrence_key": occurrence.source_occurrence_key,
        "company": occurrence.company,
        "title": occurrence.title,
        "location": occurrence.location,
        "url": occurrence.url,
        "description": occurrence.description,
        "posted_at": None,
        "content_hash": CONTENT_HASH,
        "first_seen": NOW - timedelta(days=3),
        "last_seen": NOW,
        "availability_status": AvailabilityStatus.ACTIVE,
        "machine_status": MachineStatus.ELIGIBLE,
        "user_status_updated_at": NOW - timedelta(days=3),
        "last_review_attempt_content_hash": CONTENT_HASH,
        "last_review_attempt_profile_hash": PROFILE_HASH,
    }
    values.update(updates)
    return JobRecord.model_validate(values)


def snapshot(*jobs: JobRecord) -> Snapshot:
    return Snapshot(meta=StoreMeta(data_revision=1), jobs=list(jobs))


@pytest.mark.parametrize(
    "candidate",
    [
        job(
            "new",
            machine_status=MachineStatus.PENDING,
            last_review_attempt_content_hash=None,
            last_review_attempt_profile_hash=None,
        ),
        job("content-changed", last_review_attempt_content_hash="sha256:old"),
        job("profile-changed", last_review_attempt_profile_hash="sha256:old"),
        job(
            "reappeared",
            source_occurrences=[
                job("event-source").source_occurrences[0].model_copy(
                    update={
                        "external_id": "reappeared",
                        "availability_events": [
                            AvailabilityEvent(
                                status=AvailabilityStatus.ACTIVE,
                                reason="reappeared",
                                observed_at=NOW,
                            )
                        ],
                    }
                )
            ],
            primary_source_occurrence_key="linkedin:acme/jobs:reappeared@1",
        ),
    ],
    ids=["new", "content-changed", "profile-changed", "reappeared"],
)
def test_selects_each_due_active_complete_review_reason(candidate: JobRecord) -> None:
    selected = select_jobs_for_review(snapshot(candidate), PROFILE_HASH, NOW)

    assert [item.canonical_job_key for item in selected] == [
        candidate.canonical_job_key
    ]


def test_force_selects_unchanged_successful_active_complete_job() -> None:
    candidate = job("forced")

    selected = select_jobs_for_review(
        snapshot(candidate), PROFILE_HASH, NOW, force=True
    )

    assert selected == [candidate]


def test_due_legacy_retry_time_does_not_select_unchanged_failed_job() -> None:
    candidate = job(
        "legacy-retry",
        machine_status=MachineStatus.PENDING,
        next_review_at=NOW,
    )

    selected = select_jobs_for_review(snapshot(candidate), PROFILE_HASH, NOW)

    assert selected == []


def test_reappearance_already_reviewed_at_event_time_is_unchanged() -> None:
    occurrence = job("already-reviewed").source_occurrences[0].model_copy(
        update={
            "availability_events": [
                AvailabilityEvent(
                    status=AvailabilityStatus.ACTIVE,
                    reason="reappeared",
                    observed_at=NOW,
                )
            ]
        }
    )
    candidate = job(
        "already-reviewed",
        source_occurrences=[occurrence],
        last_review_attempt_at=NOW,
    )

    selected = select_jobs_for_review(snapshot(candidate), PROFILE_HASH, NOW)

    assert selected == []


def test_skips_unchanged_and_inactive_jobs() -> None:
    unchanged = job("unchanged")
    stale = job(
        "stale",
        availability_status=AvailabilityStatus.STALE,
        last_review_attempt_content_hash="sha256:old",
    )
    closed = job(
        "closed",
        availability_status=AvailabilityStatus.CLOSED,
        last_review_attempt_content_hash="sha256:old",
    )

    selected = select_jobs_for_review(
        snapshot(unchanged, stale, closed), PROFILE_HASH, NOW
    )

    assert selected == []


def test_active_incomplete_job_becomes_pending_source_and_is_never_forced() -> None:
    incomplete_occurrence = job("partial").source_occurrences[0].model_copy(
        update={"detail_complete": False, "description": ""}
    )
    incomplete = job(
        "partial",
        source_occurrences=[incomplete_occurrence],
        description="",
        machine_status=MachineStatus.PENDING,
        last_review_attempt_content_hash=None,
        last_review_attempt_profile_hash=None,
    )
    current = snapshot(incomplete)

    selected = select_jobs_for_review(current, PROFILE_HASH, NOW, force=True)

    assert selected == []
    assert current.jobs[0].machine_status is MachineStatus.PENDING_SOURCE
