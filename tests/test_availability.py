from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from job_scan.availability import update_availability
from job_scan.dedup import merge_occurrences
from job_scan.domain import (
    AIReview,
    AvailabilityStatus,
    MachineStatus,
    Snapshot,
    SourceKind,
    StoreMeta,
    UserStatus,
)
from job_scan.normalization import content_hash
from job_scan.sources.base import FetchedOccurrence, SourceRunResult

NOW = datetime(2026, 8, 3, 12, tzinfo=UTC)
LATER = datetime(2026, 8, 4, 12, tzinfo=UTC)


def fetched(
    source: SourceKind = SourceKind.LINKEDIN,
    external_id: str = "REQ-1",
    *,
    source_instance: str | None = None,
    url: str | None = None,
    company: str = "Acme",
    title: str = "Backend Engineer",
    location: str = "Berlin",
    description: str = "Build reliable Python services for customers in Germany.",
    posted_at: date | None = date(2026, 8, 1),
    detail_complete: bool = True,
    fetch_error_code: str | None = None,
) -> FetchedOccurrence:
    instance = source_instance or f"{source.value}.example"
    actual_description = description if detail_complete else ""
    return FetchedOccurrence(
        source=source,
        source_instance=instance,
        external_id=external_id,
        url=url or f"https://{instance}/jobs/{external_id}",
        company=company,
        title=title,
        location=location,
        description=actual_description,
        posted_at=posted_at,
        content_hash=content_hash(company, title, location, actual_description),
        detail_complete=detail_complete,
        fetch_error_code=fetch_error_code,
    )


def snapshot_with(*occurrences: FetchedOccurrence) -> Snapshot:
    return merge_occurrences(
        Snapshot(meta=StoreMeta(data_revision=4), jobs=[]),
        list(occurrences),
        NOW,
    )


def result(
    source: SourceKind,
    source_instance: str,
    *,
    occurrences: list[FetchedOccurrence] | None = None,
    discovered: set[str] | None = None,
    closed: set[str] | None = None,
    completed_listing: bool = True,
) -> SourceRunResult:
    return SourceRunResult(
        source=source,
        source_instance=source_instance,
        occurrences=occurrences or [],
        discovered_source_job_keys=discovered or set(),
        explicitly_closed_source_job_keys=closed or set(),
        errors=[],
        completed_listing=completed_listing,
    )


def review(job_key: str) -> AIReview:
    return AIReview(
        job_key=job_key,
        german_requirement="optional",
        visa_sponsorship="offered",
        existing_work_authorization="not_mentioned",
        citizenship_requirement="none",
        security_clearance="none",
        staffing_agency="no",
        eligibility_evidence=["Relocation support"],
        company_industry=None,
        company_industry_confidence="low",
        company_industry_evidence=[],
        score=88,
        reason="Strong match",
        confidence="high",
    )


def test_complete_listing_marks_missing_active_occurrence_stale_with_event() -> None:
    item = fetched()
    previous = snapshot_with(item)

    updated = update_availability(
        previous,
        [result(item.source, item.source_instance)],
        LATER,
    )

    occurrence = updated.jobs[0].source_occurrences[0]
    assert occurrence.availability_status is AvailabilityStatus.STALE
    assert occurrence.availability_events[-1].model_dump() == {
        "status": AvailabilityStatus.STALE,
        "reason": "missing_from_complete_listing",
        "observed_at": LATER,
    }
    assert updated.jobs[0].availability_status is AvailabilityStatus.STALE


def test_failed_listing_keeps_previous_availability_and_events_unchanged() -> None:
    item = fetched()
    previous = snapshot_with(item)
    old_events = previous.jobs[0].source_occurrences[0].availability_events.copy()

    updated = update_availability(
        previous,
        [
            result(
                item.source,
                item.source_instance,
                completed_listing=False,
            )
        ],
        LATER,
    )

    occurrence = updated.jobs[0].source_occurrences[0]
    assert occurrence.availability_status is AvailabilityStatus.ACTIVE
    assert occurrence.availability_events == old_events
    assert updated.jobs[0].availability_status is AvailabilityStatus.ACTIVE


def test_incomplete_listing_merges_successful_occurrences_without_staling_missing() -> None:
    previous_occurrence = fetched(external_id="OLD")
    new_occurrence = fetched(external_id="NEW")
    previous = snapshot_with(previous_occurrence)

    updated = update_availability(
        previous,
        [
            result(
                new_occurrence.source,
                new_occurrence.source_instance,
                occurrences=[new_occurrence],
                discovered={new_occurrence.source_job_key},
                completed_listing=False,
            )
        ],
        LATER,
    )

    assert {job.source_occurrences[0].external_id for job in updated.jobs} == {
        "OLD",
        "NEW",
    }
    old = next(
        job for job in updated.jobs if job.source_occurrences[0].external_id == "OLD"
    )
    assert old.availability_status is AvailabilityStatus.ACTIVE


def test_failed_listing_explicit_closure_does_not_close_active_occurrence() -> None:
    item = fetched()
    previous = snapshot_with(item)
    old_events = previous.jobs[0].source_occurrences[0].availability_events.copy()

    updated = update_availability(
        previous,
        [
            result(
                item.source,
                item.source_instance,
                closed={item.source_job_key},
                completed_listing=False,
            )
        ],
        LATER,
    )

    occurrence = updated.jobs[0].source_occurrences[0]
    assert occurrence.availability_status is AvailabilityStatus.ACTIVE
    assert occurrence.closed_at is None
    assert occurrence.availability_events == old_events


def test_failed_listing_closure_cannot_override_same_namespace_success() -> None:
    item = fetched()
    previous = snapshot_with(item)
    old_events = previous.jobs[0].source_occurrences[0].availability_events.copy()

    updated = update_availability(
        previous,
        [
            result(
                item.source,
                item.source_instance,
                occurrences=[item],
                discovered={item.source_job_key},
            ),
            result(
                item.source,
                item.source_instance,
                closed={item.source_job_key},
                completed_listing=False,
            ),
        ],
        LATER,
    )

    occurrence = updated.jobs[0].source_occurrences[0]
    assert occurrence.availability_status is AvailabilityStatus.ACTIVE
    assert occurrence.closed_at is None
    assert occurrence.availability_events == old_events


@pytest.mark.parametrize(
    "previous_status",
    [AvailabilityStatus.STALE, AvailabilityStatus.CLOSED],
)
def test_incomplete_listing_seen_occurrence_reactivates_previous_status(
    previous_status: AvailabilityStatus,
) -> None:
    item = fetched()
    previous = snapshot_with(item)
    if previous_status is AvailabilityStatus.STALE:
        transitioned = update_availability(
            previous,
            [result(item.source, item.source_instance)],
            LATER,
        )
    else:
        transitioned = update_availability(
            previous,
            [
                result(
                    item.source,
                    item.source_instance,
                    discovered={item.source_job_key},
                    closed={item.source_job_key},
                )
            ],
            LATER,
        )
    updated = update_availability(
        transitioned,
        [
            result(
                item.source,
                item.source_instance,
                occurrences=[item],
                discovered={item.source_job_key},
                completed_listing=False,
            )
        ],
        datetime(2026, 8, 5, 12, tzinfo=UTC),
    )

    occurrence = updated.jobs[0].source_occurrences[0]
    assert occurrence.availability_status is AvailabilityStatus.ACTIVE
    assert occurrence.closed_at is None
    assert occurrence.availability_events[-1].reason == "reappeared"
    assert updated.jobs[0].availability_status is AvailabilityStatus.ACTIVE


def test_discovered_partial_preserves_complete_jd_and_availability_but_records_error() -> None:
    complete = fetched()
    previous = snapshot_with(complete)
    previous.jobs[0].machine_status = MachineStatus.ELIGIBLE
    partial = fetched(detail_complete=False, fetch_error_code="timeout")

    updated = update_availability(
        previous,
        [
            result(
                complete.source,
                complete.source_instance,
                occurrences=[partial],
                discovered={complete.source_job_key},
            )
        ],
        LATER,
    )

    job = updated.jobs[0]
    occurrence = job.source_occurrences[0]
    assert occurrence.description == complete.description
    assert occurrence.content_hash == complete.content_hash
    assert occurrence.detail_complete is True
    assert occurrence.last_fetch_error_code == "timeout"
    assert occurrence.availability_status is AvailabilityStatus.ACTIVE
    assert job.machine_status is MachineStatus.ELIGIBLE
    assert job.last_seen == LATER


def test_new_discovered_partial_is_active_pending_source() -> None:
    partial = fetched(detail_complete=False, fetch_error_code="missing_description")

    updated = update_availability(
        Snapshot(meta=StoreMeta(data_revision=4), jobs=[]),
        [
            result(
                partial.source,
                partial.source_instance,
                occurrences=[partial],
                discovered={partial.source_job_key},
            )
        ],
        LATER,
    )

    assert len(updated.jobs) == 1
    assert updated.jobs[0].machine_status is MachineStatus.PENDING_SOURCE
    assert updated.jobs[0].availability_status is AvailabilityStatus.ACTIVE
    assert updated.jobs[0].source_occurrences[0].last_fetch_error_code == (
        "missing_description"
    )


def test_explicitly_closed_key_closes_current_generation_without_occurrence() -> None:
    item = fetched()
    previous = snapshot_with(item)

    updated = update_availability(
        previous,
        [
            result(
                item.source,
                item.source_instance,
                discovered={item.source_job_key},
                closed={item.source_job_key},
            )
        ],
        LATER,
    )

    occurrence = updated.jobs[0].source_occurrences[0]
    assert occurrence.availability_status is AvailabilityStatus.CLOSED
    assert occurrence.closed_at == LATER
    assert occurrence.availability_events[-1].reason == "explicitly_closed"
    assert updated.jobs[0].availability_status is AvailabilityStatus.CLOSED


def test_canonical_availability_uses_active_then_stale_then_all_closed() -> None:
    description = "Shared exact complete job description."
    workday = fetched(SourceKind.LINKEDIN, "WD", description=description)
    teamtailor = fetched(SourceKind.INDEED, "TT", description=description)
    previous = snapshot_with(workday, teamtailor)
    assert len(previous.jobs) == 1

    mixed = update_availability(
        previous,
        [result(workday.source, workday.source_instance)],
        LATER,
    )
    assert mixed.jobs[0].availability_status is AvailabilityStatus.ACTIVE

    stale = update_availability(
        mixed,
        [result(teamtailor.source, teamtailor.source_instance)],
        LATER,
    )
    assert stale.jobs[0].availability_status is AvailabilityStatus.STALE

    closed = update_availability(
        stale,
        [
            result(
                workday.source,
                workday.source_instance,
                closed={workday.source_job_key},
            ),
            result(
                teamtailor.source,
                teamtailor.source_instance,
                closed={teamtailor.source_job_key},
            ),
        ],
        LATER,
    )
    assert closed.jobs[0].availability_status is AvailabilityStatus.CLOSED
    assert all(
        item.availability_status is AvailabilityStatus.CLOSED
        for item in closed.jobs[0].source_occurrences
    )


def test_primary_switches_from_closed_to_active_and_invalidates_old_review() -> None:
    shared_url = "https://apply.example/jobs/42?jobId=42"
    workday = fetched(
        SourceKind.LINKEDIN,
        "WD-42",
        url=shared_url,
        company="ATS Company",
        title="ATS Backend Engineer",
        description="Primary content from the company ATS.",
    )
    jobsuche = fetched(
        SourceKind.ARBEITSAGENTUR,
        "BA-42",
        source_instance="default",
        url=shared_url,
        company="Jobsuche Company",
        title="Jobsuche Backend Engineer",
        location="Hamburg",
        description="Still-active content from Jobsuche.",
    )
    previous = snapshot_with(workday, jobsuche)
    assert len(previous.jobs) == 1
    job = previous.jobs[0]
    assert job.primary_source_occurrence_key == f"{workday.source_job_key}@1"

    old_job_key = job.canonical_job_key
    old_members = {
        occurrence.source_occurrence_key for occurrence in job.source_occurrences
    }
    old_merge_evidence = {
        occurrence.source_occurrence_key: [
            evidence.model_dump(mode="json")
            for evidence in occurrence.merge_evidence
        ]
        for occurrence in job.source_occurrences
    }
    old_hash = job.content_hash
    job.machine_status = MachineStatus.ELIGIBLE
    job.user_status = UserStatus.SAVED
    job.ai_review = review(old_job_key)
    job.score = 88

    updated = update_availability(
        previous,
        [
            result(
                workday.source,
                workday.source_instance,
                discovered={workday.source_job_key},
                closed={workday.source_job_key},
            )
        ],
        LATER,
    )

    job = updated.jobs[0]
    statuses = {
        occurrence.source: occurrence.availability_status
        for occurrence in job.source_occurrences
    }
    assert statuses == {
        SourceKind.LINKEDIN: AvailabilityStatus.CLOSED,
        SourceKind.ARBEITSAGENTUR: AvailabilityStatus.ACTIVE,
    }
    assert job.availability_status is AvailabilityStatus.ACTIVE
    assert job.primary_source_occurrence_key == f"{jobsuche.source_job_key}@1"
    assert str(job.url) == str(jobsuche.url)
    assert job.content_hash == jobsuche.content_hash
    assert job.content_hash != old_hash
    assert job.machine_status is MachineStatus.PENDING
    assert job.ai_review is None
    assert job.score is None
    assert job.user_status is UserStatus.SAVED
    assert job.canonical_job_key == old_job_key
    assert {
        occurrence.source_occurrence_key for occurrence in job.source_occurrences
    } == old_members
    assert {
        occurrence.source_occurrence_key: [
            evidence.model_dump(mode="json")
            for evidence in occurrence.merge_evidence
        ]
        for occurrence in job.source_occurrences
    } == old_merge_evidence


def test_primary_prefers_stale_occurrence_when_remaining_occurrences_are_closed() -> None:
    shared_url = "https://apply.example/jobs/43?jobId=43"
    workday = fetched(
        SourceKind.LINKEDIN,
        "WD-43",
        url=shared_url,
        title="ATS Backend Engineer",
        description="Company ATS content.",
    )
    jobsuche = fetched(
        SourceKind.ARBEITSAGENTUR,
        "BA-43",
        source_instance="default",
        url=shared_url,
        title="Jobsuche Backend Engineer",
        description="Jobsuche content.",
    )
    previous = snapshot_with(workday, jobsuche)

    updated = update_availability(
        previous,
        [
            result(
                workday.source,
                workday.source_instance,
                discovered={workday.source_job_key},
                closed={workday.source_job_key},
            ),
            result(jobsuche.source, jobsuche.source_instance),
        ],
        LATER,
    )

    job = updated.jobs[0]
    statuses = {
        occurrence.source: occurrence.availability_status
        for occurrence in job.source_occurrences
    }
    assert statuses == {
        SourceKind.LINKEDIN: AvailabilityStatus.CLOSED,
        SourceKind.ARBEITSAGENTUR: AvailabilityStatus.STALE,
    }
    assert job.availability_status is AvailabilityStatus.STALE
    assert job.primary_source_occurrence_key == f"{jobsuche.source_job_key}@1"
    assert job.content_hash == jobsuche.content_hash


def test_noop_availability_refresh_preserves_review_when_primary_is_unchanged() -> None:
    item = fetched()
    previous = snapshot_with(item)
    job_key = previous.jobs[0].canonical_job_key
    old_hash = previous.jobs[0].content_hash
    previous.jobs[0].machine_status = MachineStatus.ELIGIBLE
    previous.jobs[0].user_status = UserStatus.SAVED
    previous.jobs[0].ai_review = review(job_key)
    previous.jobs[0].score = 88

    updated = update_availability(
        previous,
        [
            result(
                item.source,
                item.source_instance,
                occurrences=[item],
                discovered={item.source_job_key},
            )
        ],
        LATER,
    )

    job = updated.jobs[0]
    assert job.availability_status is AvailabilityStatus.ACTIVE
    assert job.content_hash == old_hash
    assert job.machine_status is MachineStatus.ELIGIBLE
    assert job.ai_review == review(job_key)
    assert job.score == 88
    assert job.user_status is UserStatus.SAVED


def test_complete_reappearance_returns_stale_occurrence_and_canonical_to_active() -> None:
    item = fetched()
    previous = snapshot_with(item)
    job_key = previous.jobs[0].canonical_job_key
    previous.jobs[0].machine_status = MachineStatus.ELIGIBLE
    previous.jobs[0].user_status = UserStatus.SAVED
    previous.jobs[0].manual_override = "show"
    previous.jobs[0].manual_override_content_hash = item.content_hash
    previous.jobs[0].manual_override_profile_hash = "profile"
    previous.jobs[0].ai_review = review(job_key)
    previous.jobs[0].score = 88
    stale = update_availability(
        previous,
        [result(item.source, item.source_instance)],
        LATER,
    )

    active = update_availability(
        stale,
        [
            result(
                item.source,
                item.source_instance,
                occurrences=[item],
                discovered={item.source_job_key},
            )
        ],
        datetime(2026, 8, 5, 12, tzinfo=UTC),
    )

    occurrence = active.jobs[0].source_occurrences[0]
    assert occurrence.availability_status is AvailabilityStatus.ACTIVE
    assert occurrence.availability_events[-1].reason == "reappeared"
    assert active.jobs[0].availability_status is AvailabilityStatus.ACTIVE
    assert active.jobs[0].machine_status is MachineStatus.PENDING
    assert active.jobs[0].ai_review is None
    assert active.jobs[0].score is None
    assert active.jobs[0].manual_override is None
    assert active.jobs[0].manual_override_content_hash is None
    assert active.jobs[0].manual_override_profile_hash is None
    assert active.jobs[0].user_status is UserStatus.SAVED


def test_partial_discovery_keeps_stale_availability_and_review() -> None:
    complete = fetched()
    previous = snapshot_with(complete)
    job_key = previous.jobs[0].canonical_job_key
    previous.jobs[0].machine_status = MachineStatus.ELIGIBLE
    previous.jobs[0].ai_review = review(job_key)
    previous.jobs[0].score = 88
    stale = update_availability(
        previous,
        [result(complete.source, complete.source_instance)],
        LATER,
    )
    partial = fetched(detail_complete=False, fetch_error_code="timeout")

    updated = update_availability(
        stale,
        [
            result(
                complete.source,
                complete.source_instance,
                occurrences=[partial],
                discovered={complete.source_job_key},
            )
        ],
        datetime(2026, 8, 5, 12, tzinfo=UTC),
    )

    occurrence = updated.jobs[0].source_occurrences[0]
    assert occurrence.availability_status is AvailabilityStatus.STALE
    assert occurrence.last_fetch_error_code == "timeout"
    assert updated.jobs[0].availability_status is AvailabilityStatus.STALE
    assert updated.jobs[0].machine_status is MachineStatus.ELIGIBLE
    assert updated.jobs[0].ai_review == review(job_key)
    assert updated.jobs[0].score == 88


def test_failed_listing_preserves_duplicate_edge_observed_at() -> None:
    left = fetched(
        SourceKind.LINKEDIN,
        "A",
        description="abcdefghijklmnopqrst",
    )
    right = fetched(
        SourceKind.INDEED,
        "B",
        description="abcdefghijklmnopqrsx",
    )
    previous = snapshot_with(left, right)
    assert all(job.possible_duplicates for job in previous.jobs)

    updated = update_availability(
        previous,
        [
            result(
                left.source,
                left.source_instance,
                completed_listing=False,
            )
        ],
        LATER,
    )

    assert [
        evidence.observed_at
        for job in updated.jobs
        for evidence in job.possible_duplicates
    ] == [NOW, NOW]


def test_explicit_close_targets_only_max_generation_and_preserves_old_events() -> None:
    original = fetched(
        title="Old Role",
        description="a" * 100,
        posted_at=date(2026, 1, 1),
    )
    first = snapshot_with(original)
    replacement = fetched(
        title="New Role",
        description="z" * 100,
        posted_at=date(2026, 3, 2),
    )
    rolled = merge_occurrences(first, [replacement], LATER)
    old_occurrence = next(
        item
        for job in rolled.jobs
        for item in job.source_occurrences
        if item.source_generation == 1
    )
    old_events = old_occurrence.availability_events.copy()

    updated = update_availability(
        rolled,
        [
            result(
                replacement.source,
                replacement.source_instance,
                closed={replacement.source_job_key},
            )
        ],
        datetime(2026, 8, 5, 12, tzinfo=UTC),
    )

    generations = sorted(
        (
            item.source_generation,
            item.availability_status,
            item.availability_events,
        )
        for job in updated.jobs
        for item in job.source_occurrences
    )
    assert generations[0] == (1, AvailabilityStatus.CLOSED, old_events)
    assert generations[1][0:2] == (2, AvailabilityStatus.CLOSED)
    assert generations[1][2][-1].reason == "explicitly_closed"
