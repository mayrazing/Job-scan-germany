from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest

from job_scan.domain import (
    AvailabilityStatus,
    JobRecord,
    SalaryPeriod,
    SalaryValue,
    Snapshot,
    SourceKind,
    SourceOccurrence,
    StoreMeta,
    UserStatus,
    UserStatusHistoryEntry,
)
from job_scan.global_jobs import GlobalJobChanged, GlobalJobStore, filter_untracked_jobs
from job_scan.paths import AppPaths
from job_scan.repository import parse_snapshot, serialize_snapshot

NOW = datetime(2026, 8, 18, 10, 0, tzinfo=UTC)


def _job(
    key: str,
    *,
    external_ids: tuple[str, ...] = (),
    status: UserStatus = UserStatus.NEW,
    status_at: datetime = NOW,
    last_seen: datetime = NOW,
) -> JobRecord:
    occurrences = [
        SourceOccurrence(
            source=SourceKind.LINKEDIN,
            source_instance="acme/jobs",
            external_id=external_id,
            source_generation=1,
            url=f"https://acme.example/jobs/{external_id}",
            company="Acme",
            title="Backend Engineer",
            location="Berlin",
            description="Build APIs",
            posted_at=date(2026, 8, 1),
            content_hash=f"sha256:{external_id}",
            availability_status=AvailabilityStatus.ACTIVE,
        )
        for external_id in external_ids
    ]
    return JobRecord(
        canonical_job_key=key,
        source_occurrences=occurrences,
        primary_source_occurrence_key=(
            occurrences[0].source_occurrence_key if occurrences else f"legacy:{key}"
        ),
        company="Acme",
        title="Backend Engineer",
        location="Berlin",
        url=f"https://acme.example/jobs/{key}",
        description="Build APIs",
        posted_at=date(2026, 8, 1),
        content_hash=f"sha256:{key}",
        first_seen=NOW - timedelta(days=1),
        last_seen=last_seen,
        availability_status=AvailabilityStatus.ACTIVE,
        user_status=status,
        user_status_updated_at=status_at,
    )


def _snapshot(*jobs: JobRecord) -> Snapshot:
    return Snapshot(meta=StoreMeta(data_revision=1, generated_at=NOW), jobs=list(jobs))


@pytest.fixture
def store(tmp_path: Path) -> GlobalJobStore:
    return GlobalJobStore(AppPaths.from_root(tmp_path / "home"))


def test_new_store_exposes_default_tracker_groups(store: GlobalJobStore) -> None:
    snapshot = store.load()

    assert [(group.id, group.name) for group in snapshot.meta.tracker_groups] == [
        ("saved", "Saved"),
        ("applied", "Applied"),
        ("interviewing", "Interviewing"),
        ("offer", "Offer"),
        ("withdrawn", "Withdrawn"),
        ("rejected", "Rejected"),
        ("ignored", "Ignored"),
    ]


def test_custom_tracker_group_can_receive_jobs(store: GlobalJobStore) -> None:
    group = store.create_group("Phone screen")
    job = _job("job-1", external_ids=("shared",))

    saved = store.set_status(job, group.id, NOW)

    assert saved.jobs[0].user_status == group.id
    assert saved.jobs[0].user_status_history[-1].status == group.id
    assert store.load().meta.tracker_groups[-1] == group


def test_batch_status_move_updates_every_job_in_one_revision(
    store: GlobalJobStore,
) -> None:
    first = _job("first", external_ids=("first-source",))
    second = _job("second", external_ids=("second-source",))
    store.set_status(first, UserStatus.SAVED, NOW)
    before = store.set_status(second, UserStatus.SAVED, NOW)

    moved = store.set_status_many(
        ["first", "second"],
        UserStatus.APPLIED,
        NOW + timedelta(minutes=1),
    )

    assert moved.meta.data_revision == before.meta.data_revision + 1
    assert {job.user_status for job in moved.jobs} == {UserStatus.APPLIED}
    assert all(
        [entry.status for entry in job.user_status_history]
        == [UserStatus.SAVED, UserStatus.APPLIED]
        for job in moved.jobs
    )


def test_batch_status_move_rejects_unknown_job_without_moving_anything(
    store: GlobalJobStore,
) -> None:
    store.set_status(
        _job("first", external_ids=("first-source",)),
        UserStatus.SAVED,
        NOW,
    )
    before = store.load()

    with pytest.raises(KeyError, match="missing"):
        store.set_status_many(
            ["first", "missing"],
            UserStatus.APPLIED,
            NOW + timedelta(minutes=1),
        )

    assert store.load() == before


def test_batch_delete_requires_exact_confirmation_and_is_atomic(
    store: GlobalJobStore,
) -> None:
    for key in ("first", "second"):
        store.set_status(
            _job(key, external_ids=(f"{key}-source",)),
            UserStatus.SAVED,
            NOW,
        )
    before = store.load()

    with pytest.raises(ValueError, match="Delete all"):
        store.delete_many(["first", "second"], confirmation_text="delete all")
    with pytest.raises(KeyError, match="missing"):
        store.delete_many(
            ["first", "missing"],
            confirmation_text="Delete all",
        )

    assert store.load() == before
    deleted_count = store.delete_many(
        ["first", "second"],
        confirmation_text="Delete all",
        now=NOW + timedelta(minutes=1),
    )
    deleted = store.load()
    assert deleted_count == 2
    assert deleted.jobs == []
    assert deleted.meta.data_revision == before.meta.data_revision + 1
    assert {
        marker.canonical_job_keys[0]
        for marker in deleted.meta.global_job_deletions
    } == {"first", "second"}


def test_renaming_tracker_group_keeps_its_identity_and_job_history(
    store: GlobalJobStore,
) -> None:
    job = _job("job-1", external_ids=("shared",))
    store.set_status(job, UserStatus.SAVED, NOW)

    renamed = store.rename_group("saved", "Inbox")

    tracked = store.find("job-1")
    assert renamed.id == "saved"
    assert renamed.name == "Inbox"
    assert tracked is not None
    assert tracked.user_status is UserStatus.SAVED
    assert tracked.user_status_history[0].status is UserStatus.SAVED


def test_tracker_group_names_must_be_nonempty_and_unique(
    store: GlobalJobStore,
) -> None:
    store.create_group("Phone screen")

    with pytest.raises(ValueError, match="cannot be blank"):
        store.create_group("  ")
    with pytest.raises(ValueError, match="already exists"):
        store.create_group(" phone SCREEN ")


def test_saved_tracker_group_cannot_be_deleted(store: GlobalJobStore) -> None:
    with pytest.raises(ValueError, match="required starting group"):
        store.delete_group("saved")


def test_applied_tracker_group_cannot_be_renamed(store: GlobalJobStore) -> None:
    with pytest.raises(ValueError, match="cannot be renamed"):
        store.rename_group("applied", "Submitted")


def test_applied_tracker_group_cannot_be_deleted(store: GlobalJobStore) -> None:
    with pytest.raises(ValueError, match="cannot be deleted"):
        store.delete_group("applied")


def test_empty_tracker_group_can_be_deleted_without_name_confirmation(
    store: GlobalJobStore,
) -> None:
    group = store.create_group("Phone screen")

    deleted_count = store.delete_group(group.id)

    assert deleted_count == 0
    assert group.id not in {item.id for item in store.load().meta.tracker_groups}


def test_nonempty_tracker_group_requires_exact_name_before_batch_delete(
    store: GlobalJobStore,
) -> None:
    group = store.create_group("Phone screen")
    job = _job("job-1", external_ids=("shared",))
    store.set_status(job, group.id, NOW)

    with pytest.raises(ValueError, match="exact group name"):
        store.delete_group(group.id)
    with pytest.raises(ValueError, match="exact group name"):
        store.delete_group(group.id, confirmation_name="phone screen")

    assert store.find("job-1") is not None
    assert group.id in {item.id for item in store.load().meta.tracker_groups}


def test_nonempty_tracker_group_deletes_jobs_and_surviving_history_atomically(
    store: GlobalJobStore,
) -> None:
    group = store.create_group("Phone screen")
    deleted_job = _job("deleted", external_ids=("deleted-source",))
    surviving_job = _job("surviving", external_ids=("surviving-source",))
    store.set_status(deleted_job, group.id, NOW)
    store.set_status(surviving_job, group.id, NOW)
    store.set_status(surviving_job, UserStatus.APPLIED, NOW + timedelta(minutes=1))

    deleted_count = store.delete_group(
        group.id,
        confirmation_name="Phone screen",
        now=NOW + timedelta(minutes=2),
    )

    snapshot = store.load()
    assert deleted_count == 1
    assert store.find("deleted") is None
    assert [job.canonical_job_key for job in snapshot.jobs] == ["surviving"]
    assert [entry.status for entry in snapshot.jobs[0].user_status_history] == [
        UserStatus.SAVED,
        UserStatus.APPLIED,
    ]
    assert group.id not in {item.id for item in snapshot.meta.tracker_groups}
    assert len(snapshot.meta.global_job_deletions) == 1
    assert snapshot.meta.global_job_deletions[0].canonical_job_keys == ["deleted"]


def test_import_ignores_new_and_newest_old_status_wins(store: GlobalJobStore) -> None:
    earlier = _job(
        "old-key",
        external_ids=("same",),
        status=UserStatus.SAVED,
        status_at=NOW,
    )
    newer = _job(
        "new-key",
        external_ids=("same",),
        status=UserStatus.REJECTED,
        status_at=NOW + timedelta(minutes=1),
    )
    new_only = _job("new-only", external_ids=("new",))

    imported = store.import_snapshots([_snapshot(earlier, new_only), _snapshot(newer)])

    assert len(imported.jobs) == 1
    assert imported.jobs[0].user_status is UserStatus.REJECTED
    assert imported.jobs[0].canonical_job_key == "new-key"
    assert [
        (entry.status, entry.changed_at)
        for entry in imported.jobs[0].user_status_history
    ] == [(UserStatus.REJECTED, NOW + timedelta(minutes=1))]
    assert store.find("new-only") is None


@pytest.mark.parametrize("legacy_status", [b"reviewed", b"shortlisted"])
def test_import_migrates_legacy_saved_jsonl_statuses(
    store: GlobalJobStore,
    legacy_status: bytes,
) -> None:
    legacy = serialize_snapshot(
        _snapshot(_job("old-key", status=UserStatus.SAVED))
    ).replace(b'"saved"', b'"' + legacy_status + b'"')

    imported = store.import_snapshots([parse_snapshot(legacy)])

    assert imported.jobs[0].user_status is UserStatus.SAVED


def test_set_status_cannot_be_rolled_back_by_older_history(store: GlobalJobStore) -> None:
    historical = _job(
        "job-1",
        external_ids=("shared",),
        status=UserStatus.SAVED,
        status_at=NOW,
    )
    store.import_snapshots([_snapshot(historical)])

    saved = store.set_status(
        historical,
        UserStatus.APPLIED,
        now=NOW + timedelta(minutes=2),
    )
    reimported = store.import_snapshots([_snapshot(historical)])

    assert saved.jobs[0].user_status is UserStatus.APPLIED
    assert reimported.jobs[0].user_status is UserStatus.APPLIED
    assert reimported.jobs[0].user_status_updated_at == NOW + timedelta(minutes=2)


def test_status_date_can_be_changed_without_changing_status_or_time(
    store: GlobalJobStore,
) -> None:
    job = _job("job-1", external_ids=("same",))
    store.set_status(job, UserStatus.SAVED, NOW)
    store.set_status(job, UserStatus.APPLIED, NOW + timedelta(days=2, hours=3))
    tracked = store.find("job-1")
    assert tracked is not None

    updated = store.set_status_date(tracked, 1, date(2026, 8, 25))

    assert updated.user_status is UserStatus.APPLIED
    assert updated.user_status_history[-1].changed_at == datetime(
        2026,
        8,
        25,
        13,
        0,
        tzinfo=UTC,
    )
    assert updated.user_status_updated_at == updated.user_status_history[-1].changed_at
    assert store.find("job-1") == updated


@pytest.mark.parametrize(
    "changed_on",
    [date(2026, 8, 17), date(2026, 8, 23)],
)
def test_status_date_cannot_cross_adjacent_event_dates(
    store: GlobalJobStore,
    changed_on: date,
) -> None:
    job = _job("job-1", external_ids=("same",))
    store.set_status(job, UserStatus.SAVED, NOW)
    store.set_status(job, UserStatus.APPLIED, NOW + timedelta(days=2))
    store.set_status(job, UserStatus.INTERVIEWING, NOW + timedelta(days=4))
    tracked = store.find("job-1")
    assert tracked is not None

    with pytest.raises(ValueError, match="adjacent lifecycle events"):
        store.set_status_date(tracked, 1, changed_on)

    assert store.find("job-1") == tracked


@pytest.mark.parametrize(
    "changed_on",
    [date(2026, 8, 18), date(2026, 8, 22)],
)
def test_status_date_can_equal_an_adjacent_event_date(
    store: GlobalJobStore,
    changed_on: date,
) -> None:
    job = _job("job-1", external_ids=("same",))
    store.set_status(job, UserStatus.SAVED, NOW)
    store.set_status(job, UserStatus.APPLIED, NOW + timedelta(days=2))
    store.set_status(job, UserStatus.INTERVIEWING, NOW + timedelta(days=4))
    tracked = store.find("job-1")
    assert tracked is not None

    updated = store.set_status_date(tracked, 1, changed_on)

    assert len(updated.user_status_history) == 3
    assert updated.user_status_history[1].changed_at.date() == changed_on
    assert [entry.status for entry in updated.user_status_history] == [
        UserStatus.SAVED,
        UserStatus.APPLIED,
        UserStatus.INTERVIEWING,
    ]


def test_status_event_can_be_deleted_without_changing_other_events(
    store: GlobalJobStore,
) -> None:
    job = _job("job-1", external_ids=("same",))
    store.set_status(job, UserStatus.SAVED, NOW)
    store.set_status(job, UserStatus.APPLIED, NOW + timedelta(days=2))
    store.set_status(job, UserStatus.INTERVIEWING, NOW + timedelta(days=4))
    tracked = store.find("job-1")
    assert tracked is not None

    updated = store.delete_status_event(tracked, 1)

    assert updated.user_status_history == [
        tracked.user_status_history[0],
        tracked.user_status_history[2],
    ]
    assert updated.user_status is UserStatus.INTERVIEWING
    assert updated.user_status_updated_at == tracked.user_status_updated_at


def test_deleting_current_status_event_makes_previous_event_current(
    store: GlobalJobStore,
) -> None:
    job = _job("job-1", external_ids=("same",))
    store.set_status(job, UserStatus.SAVED, NOW)
    store.set_status(job, UserStatus.APPLIED, NOW + timedelta(days=2))
    tracked = store.find("job-1")
    assert tracked is not None

    updated = store.delete_status_event(tracked, 1)

    assert [entry.status for entry in updated.user_status_history] == [
        UserStatus.SAVED
    ]
    assert updated.user_status is UserStatus.SAVED
    assert updated.user_status_updated_at == NOW


def test_deleted_lifecycle_event_stays_deleted_after_snapshot_reimport(
    store: GlobalJobStore,
) -> None:
    job = _job(
        "job-1",
        external_ids=("same",),
        status=UserStatus.IGNORED,
        status_at=NOW,
    )
    job.user_status_history = [
        UserStatusHistoryEntry(status=UserStatus.IGNORED, changed_at=NOW),
        UserStatusHistoryEntry(
            status=UserStatus.IGNORED,
            changed_at=NOW + timedelta(days=1),
        ),
        UserStatusHistoryEntry(
            status=UserStatus.SAVED,
            changed_at=NOW + timedelta(days=2),
        ),
        UserStatusHistoryEntry(status=UserStatus.IGNORED, changed_at=NOW),
    ]
    store.import_snapshots([_snapshot(job)])
    tracked = store.find("job-1")
    assert tracked is not None

    deleted = store.delete_status_event(tracked, 0)
    reimported = store.import_snapshots(
        [_snapshot(_job("job-1", external_ids=("same",)))]
    ).jobs[0]

    expected_history = [
        (UserStatus.IGNORED, NOW + timedelta(days=1)),
        (UserStatus.SAVED, NOW + timedelta(days=2)),
        (UserStatus.IGNORED, NOW),
    ]
    assert [
        (entry.status, entry.changed_at) for entry in deleted.user_status_history
    ] == expected_history
    assert [
        (entry.status, entry.changed_at) for entry in reimported.user_status_history
    ] == expected_history


def test_saved_status_event_cannot_be_deleted(store: GlobalJobStore) -> None:
    job = _job("job-1", external_ids=("same",))
    store.set_status(job, UserStatus.SAVED, NOW)
    tracked = store.find("job-1")
    assert tracked is not None

    with pytest.raises(ValueError, match="required starting lifecycle event cannot be deleted"):
        store.delete_status_event(tracked, 0)

    assert store.find("job-1") == tracked


def test_later_imported_legacy_status_replaces_the_first_known_event(
    store: GlobalJobStore,
) -> None:
    saved = _job(
        "job-1",
        external_ids=("shared",),
        status=UserStatus.SAVED,
        status_at=NOW,
    )
    rejected = _job(
        "job-1-new",
        external_ids=("shared",),
        status=UserStatus.REJECTED,
        status_at=NOW + timedelta(minutes=1),
    )
    store.import_snapshots([_snapshot(saved)])

    imported = store.import_snapshots([_snapshot(rejected)])

    assert imported.jobs[0].user_status is UserStatus.REJECTED
    assert [
        (entry.status, entry.changed_at)
        for entry in imported.jobs[0].user_status_history
    ] == [
        (UserStatus.REJECTED, NOW + timedelta(minutes=1)),
    ]


def test_status_changes_append_persisted_history_without_duplicate_events(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    store = GlobalJobStore(paths)
    job = _job("job-1", external_ids=("shared",))

    store.set_status(job, UserStatus.SAVED, NOW)
    store.set_status(job, UserStatus.APPLIED, NOW + timedelta(minutes=1))
    store.set_status(job, UserStatus.APPLIED, NOW + timedelta(minutes=2))

    reloaded = GlobalJobStore(paths).find("job-1")
    assert reloaded is not None
    assert [
        (entry.status, entry.changed_at) for entry in reloaded.user_status_history
    ] == [
        (UserStatus.SAVED, NOW),
        (UserStatus.APPLIED, NOW + timedelta(minutes=1)),
    ]
    assert reloaded.user_status_updated_at == NOW + timedelta(minutes=1)


def test_first_direct_tracker_status_still_starts_the_lifecycle_at_saved(
    store: GlobalJobStore,
) -> None:
    job = _job("job-1", external_ids=("shared",))

    store.set_status(job, UserStatus.APPLIED, NOW)
    updated = store.set_status(
        job,
        UserStatus.INTERVIEWING,
        NOW + timedelta(minutes=1),
    )

    assert [
        (entry.status, entry.changed_at)
        for entry in updated.jobs[0].user_status_history
    ] == [
        (UserStatus.SAVED, NOW),
        (UserStatus.APPLIED, NOW),
        (UserStatus.INTERVIEWING, NOW + timedelta(minutes=1)),
    ]


def test_tracker_status_does_not_attach_a_search_resume(
    store: GlobalJobStore,
) -> None:
    job = _job("job-1", external_ids=("shared",))
    resume_a = "sha256:" + "1" * 64
    resume_b = "sha256:" + "2" * 64

    saved = store.set_status(
        job,
        UserStatus.SAVED,
        NOW,
        resume_id=resume_a,
        profile_hash="sha256:" + "a" * 64,
    )
    applied = store.set_status(
        job,
        UserStatus.APPLIED,
        NOW + timedelta(minutes=1),
        resume_id=resume_a,
        profile_hash="sha256:" + "a" * 64,
    )
    interviewing = store.set_status(
        job,
        UserStatus.INTERVIEWING,
        NOW + timedelta(minutes=2),
        resume_id=resume_b,
        profile_hash="sha256:" + "b" * 64,
    )

    assert saved.jobs[0].application_resume_id is None
    assert applied.jobs[0].application_resume_id is None
    assert interviewing.jobs[0].application_resume_id is None


def test_review_transfer_saves_status_and_application_resume_together(
    store: GlobalJobStore,
) -> None:
    job = _job("job-1", external_ids=("shared",))
    resume_id = "sha256:" + "1" * 64

    transferred = store.set_status(
        job,
        UserStatus.APPLIED,
        NOW,
        resume_id=resume_id,
        profile_hash="sha256:" + "a" * 64,
        application_resume_filename="candidate.pdf",
    )

    assert transferred.meta.data_revision == 1
    assert transferred.jobs[0].user_status is UserStatus.APPLIED
    assert transferred.jobs[0].application_resume_id == resume_id
    assert transferred.jobs[0].application_resume_filename == "candidate.pdf"


def test_tracker_resume_can_be_corrected_without_changing_status(
    store: GlobalJobStore,
) -> None:
    job = _job("job-1", external_ids=("shared",))
    resume_a = "sha256:" + "1" * 64
    resume_b = "sha256:" + "2" * 64
    saved = store.set_status(
        job,
        UserStatus.SAVED,
        NOW,
        resume_id=resume_a,
        profile_hash="sha256:" + "a" * 64,
    )

    corrected = store.set_application_resume(saved.jobs[0], resume_b)

    assert corrected.application_resume_id == resume_b
    assert corrected.user_status is UserStatus.SAVED
    assert [entry.status for entry in corrected.user_status_history] == [
        UserStatus.SAVED,
    ]


def test_tracker_salaries_can_be_saved_and_modified(store: GlobalJobStore) -> None:
    job = _job("job-1", external_ids=("shared",))
    tracked = store.set_status(job, UserStatus.SAVED, NOW).jobs[0]

    store.set_salaries(
        tracked,
        expected_salary=SalaryValue(amount="5,500 EUR", period=SalaryPeriod.MONTH),
        offer_salary=None,
    )
    updated = store.set_salaries(
        tracked,
        expected_salary=SalaryValue(amount="72,000 EUR", period=SalaryPeriod.YEAR),
        offer_salary=SalaryValue(amount="68,000 EUR", period=SalaryPeriod.YEAR),
    )

    assert updated.expected_salary == SalaryValue(
        amount="72,000 EUR",
        period=SalaryPeriod.YEAR,
    )
    assert updated.offer_salary == SalaryValue(
        amount="68,000 EUR",
        period=SalaryPeriod.YEAR,
    )
    assert store.find("job-1") == updated


def test_tracker_notes_can_be_added_edited_deleted_and_survive_import(
    store: GlobalJobStore,
) -> None:
    note_id = UUID("11111111-1111-4111-8111-111111111111")
    job = _job("job-1", external_ids=("shared",))
    tracked = store.set_status(job, UserStatus.SAVED, NOW).jobs[0]

    created = store.add_note(
        tracked,
        "  Follow up with recruiter.  ",
        NOW,
        note_id=note_id,
    )
    edited = store.edit_note(tracked, note_id, "Follow up on Tuesday.")
    store.import_snapshots(
        [
            _snapshot(
                _job(
                    "new-key",
                    external_ids=("shared",),
                    status=UserStatus.SAVED,
                    last_seen=NOW + timedelta(days=1),
                )
            )
        ]
    )

    saved = store.find("new-key")
    assert created.content == "Follow up with recruiter."
    assert created.created_at == NOW
    assert edited.id == note_id
    assert edited.content == "Follow up on Tuesday."
    assert edited.created_at == created.created_at
    assert saved is not None
    assert saved.notes == [edited]

    store.delete_note(saved, note_id)
    deleted = store.find("new-key")
    assert deleted is not None
    assert deleted.notes == []


def test_tracker_manual_facts_survive_later_job_imports(
    store: GlobalJobStore,
) -> None:
    job = _job("job-1", external_ids=("shared",)).model_copy(
        update={"posted_at": None}
    )
    tracked = store.set_status(job, UserStatus.SAVED, NOW).jobs[0]

    tracked = store.set_manual_fact(tracked, "posted_at", date(2026, 8, 12))
    tracked = store.set_manual_fact(tracked, "company_size", 4200)
    store.set_manual_fact(tracked, "company_industry", "Logistics")
    store.import_snapshots(
        [
            _snapshot(
                _job(
                    "new-key",
                    external_ids=("shared",),
                    status=UserStatus.SAVED,
                    last_seen=NOW + timedelta(days=1),
                ).model_copy(update={"posted_at": None})
            )
        ]
    )

    saved = store.find("new-key")
    assert saved is not None
    assert saved.manual_posted_at == date(2026, 8, 12)
    assert saved.manual_company_size == 4200
    assert saved.manual_company_industry == "Logistics"


def test_same_time_saved_and_applied_order_survives_import_and_duplicate_submit(
    store: GlobalJobStore,
) -> None:
    job = _job("job-1", external_ids=("shared",))
    store.set_status(job, UserStatus.APPLIED, NOW)

    store.import_snapshots([_snapshot(job)])
    repeated = store.set_status(job, UserStatus.APPLIED, NOW + timedelta(minutes=1))

    assert [
        (entry.status, entry.changed_at)
        for entry in repeated.jobs[0].user_status_history
    ] == [
        (UserStatus.SAVED, NOW),
        (UserStatus.APPLIED, NOW),
    ]
    assert repeated.jobs[0].user_status_updated_at == NOW


def test_imported_old_status_becomes_the_first_known_history_event(
    store: GlobalJobStore,
) -> None:
    old_job = _job(
        "job-1",
        external_ids=("shared",),
        status=UserStatus.INTERVIEWING,
        status_at=NOW,
    )

    imported = store.import_snapshots([_snapshot(old_job)])

    assert [
        (entry.status, entry.changed_at)
        for entry in imported.jobs[0].user_status_history
    ] == [(UserStatus.INTERVIEWING, NOW)]


def test_delete_hides_job_and_prevents_passive_history_reimport(
    store: GlobalJobStore,
) -> None:
    historical = _job(
        "job-1",
        external_ids=("shared",),
        status=UserStatus.SAVED,
    )
    store.import_snapshots([_snapshot(historical)])

    store.delete("job-1", now=NOW + timedelta(minutes=1))

    assert store.load().jobs == []
    assert store.find("job-1") is None
    assert store.import_snapshots([_snapshot(historical)]).jobs == []


def test_deleted_job_learns_bridge_aliases_before_later_history_import(
    store: GlobalJobStore,
) -> None:
    deleted = _job(
        "deleted",
        external_ids=("one",),
        status=UserStatus.SAVED,
    )
    store.import_snapshots([_snapshot(deleted)])
    store.delete("deleted", now=NOW + timedelta(minutes=1))

    bridge = _job("bridge", external_ids=("one", "two"))
    assert store.import_snapshots([_snapshot(bridge)]).jobs == []

    later_history = _job(
        "later",
        external_ids=("two",),
        status=UserStatus.APPLIED,
        status_at=NOW + timedelta(minutes=2),
    )
    assert store.import_snapshots([_snapshot(later_history)]).jobs == []


def test_delete_removes_job_data_and_keeps_only_minimal_suppression_marker(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    store = GlobalJobStore(paths)
    resume_id = "sha256:" + "1" * 64
    profile_hash = "sha256:" + "a" * 64
    job = _job("job-1", external_ids=("shared",))
    job.reason = "private AI review"
    store.set_status(
        job,
        UserStatus.SAVED,
        NOW,
        resume_id=resume_id,
        profile_hash=profile_hash,
    )
    store.set_status(job, UserStatus.APPLIED, NOW + timedelta(seconds=30))

    store.delete("job-1", now=NOW + timedelta(minutes=1))

    persisted = paths.global_jobs_jsonl.read_text(encoding="utf-8")
    deleted = store._load_unlocked()
    assert deleted.jobs == []
    assert len(deleted.meta.global_job_deletions) == 1
    marker = deleted.meta.global_job_deletions[0]
    assert marker.canonical_job_keys == ["job-1"]
    assert marker.source_job_keys == ["linkedin:acme/jobs:shared"]
    assert marker.deleted_at == NOW + timedelta(minutes=1)
    assert '"record_type":"job"' not in persisted
    assert "private AI review" not in persisted
    assert resume_id not in persisted
    assert '"user_status_history"' not in persisted
    assert '"global_status_deleted_at"' not in persisted


def test_loading_legacy_soft_delete_physically_migrates_it_to_a_minimal_marker(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    store = GlobalJobStore(paths)
    legacy = _job("job-1", external_ids=("shared",), status=UserStatus.SAVED)
    legacy.reason = "private legacy AI review"
    legacy.global_status_deleted_at = NOW + timedelta(minutes=1)
    paths.global_jobs_jsonl.write_bytes(serialize_snapshot(_snapshot(legacy)))

    loaded = store.load()

    persisted = paths.global_jobs_jsonl.read_text(encoding="utf-8")
    assert loaded.jobs == []
    assert '"record_type":"job"' not in persisted
    assert "private legacy AI review" not in persisted
    assert '"global_status_deleted_at"' not in persisted
    assert '"global_job_deletions"' in persisted


def test_loading_old_active_job_persists_its_first_known_status_event(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    store = GlobalJobStore(paths)
    old_job = _job("job-1", status=UserStatus.INTERVIEWING, status_at=NOW)
    paths.global_jobs_jsonl.write_bytes(serialize_snapshot(_snapshot(old_job)))

    loaded = store.load()

    assert [
        (entry.status, entry.changed_at)
        for entry in loaded.jobs[0].user_status_history
    ] == [(UserStatus.INTERVIEWING, NOW)]
    assert '"user_status_history"' in paths.global_jobs_jsonl.read_text(
        encoding="utf-8"
    )


def test_read_only_load_does_not_persist_legacy_migration(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    store = GlobalJobStore(paths)
    old_job = _job("job-1", status=UserStatus.INTERVIEWING, status_at=NOW)
    original = serialize_snapshot(_snapshot(old_job))
    paths.global_jobs_jsonl.write_bytes(original)

    loaded = store.load_read_only()

    assert [entry.status for entry in loaded.jobs[0].user_status_history] == [
        UserStatus.INTERVIEWING
    ]
    assert paths.global_jobs_jsonl.read_bytes() == original


def test_tracker_timestamps_are_timezone_aware_and_normalized_to_utc(
    store: GlobalJobStore,
) -> None:
    job = _job("job-1", external_ids=("shared",))
    berlin_time = datetime(
        2026,
        8,
        18,
        12,
        0,
        tzinfo=timezone(timedelta(hours=2)),
    )

    saved = store.set_status(job, UserStatus.SAVED, berlin_time)

    assert saved.jobs[0].user_status_updated_at == NOW
    assert saved.jobs[0].user_status_history[0].changed_at == NOW
    with pytest.raises(ValueError, match="timezone-aware"):
        store.set_status(
            job,
            UserStatus.APPLIED,
            datetime(2026, 8, 18, 12, 1),  # noqa: DTZ001 - verifies rejection.
        )


def test_setting_status_explicitly_restores_a_deleted_global_job(
    store: GlobalJobStore,
) -> None:
    historical = _job(
        "job-1",
        external_ids=("shared",),
        status=UserStatus.SAVED,
    )
    store.import_snapshots([_snapshot(historical)])
    store.delete("job-1", now=NOW + timedelta(minutes=1))

    restored = store.set_status(
        historical,
        UserStatus.APPLIED,
        now=NOW + timedelta(minutes=2),
    )

    assert [job.user_status for job in restored.jobs] == [UserStatus.APPLIED]
    assert store.find("job-1").global_status_deleted_at is None


def test_reimporting_url_explicitly_restores_a_deleted_global_job(
    store: GlobalJobStore,
) -> None:
    manual = _job("manual", external_ids=("manual-url",))
    store.upsert_with_default_status(manual, UserStatus.SAVED, NOW)
    store.delete("manual", now=NOW + timedelta(minutes=1))

    restored = store.upsert_with_default_status(
        manual,
        UserStatus.SAVED,
        NOW + timedelta(minutes=2),
    )

    assert restored.user_status is UserStatus.SAVED
    assert restored.global_status_deleted_at is None
    assert store.find("manual") is not None


def test_explicit_resave_after_delete_starts_a_new_saved_lifecycle(
    store: GlobalJobStore,
) -> None:
    manual = _job("manual", external_ids=("manual-url",))
    store.upsert_with_default_status(manual, UserStatus.SAVED, NOW)
    store.set_status(manual, UserStatus.APPLIED, NOW + timedelta(minutes=1))
    store.delete("manual", now=NOW + timedelta(minutes=2))

    restored = store.upsert_with_default_status(
        manual,
        UserStatus.SAVED,
        NOW + timedelta(minutes=3),
    )

    assert [
        (entry.status, entry.changed_at) for entry in restored.user_status_history
    ] == [(UserStatus.SAVED, NOW + timedelta(minutes=3))]
    assert store._load_unlocked().meta.global_job_deletions == []


def test_mutate_details_preserves_the_latest_global_status(
    store: GlobalJobStore,
) -> None:
    job = _job(
        "job-1",
        external_ids=("shared",),
        status=UserStatus.SAVED,
        status_at=NOW,
    )
    store.import_snapshots([_snapshot(job)])
    store.set_status(job, UserStatus.APPLIED, NOW + timedelta(minutes=1))

    def add_detail(snapshot: Snapshot) -> Snapshot:
        snapshot.jobs[0].labels.append("Company size checked")
        return snapshot

    saved = store.mutate_details(add_detail)

    assert saved.jobs[0].user_status is UserStatus.APPLIED
    assert saved.jobs[0].labels == ["Company size checked"]


def test_mutate_details_rejects_global_status_changes(store: GlobalJobStore) -> None:
    job = _job(
        "job-1",
        external_ids=("shared",),
        status=UserStatus.SAVED,
    )
    store.import_snapshots([_snapshot(job)])

    def replace_status(snapshot: Snapshot) -> Snapshot:
        snapshot.jobs[0].user_status = UserStatus.NEW
        return snapshot

    with pytest.raises(ValueError, match="cannot change global job status"):
        store.mutate_details(replace_status)

    assert store.load().jobs[0].user_status is UserStatus.SAVED


def test_mutate_details_rejects_application_resume_changes(
    store: GlobalJobStore,
) -> None:
    job = _job("job-1", external_ids=("shared",))
    resume_a = "sha256:" + "1" * 64
    resume_b = "sha256:" + "2" * 64
    store.set_status(
        job,
        UserStatus.APPLIED,
        NOW,
        resume_id=resume_a,
        profile_hash="sha256:" + "a" * 64,
    )
    tracked = store.find("job-1")
    assert tracked is not None
    store.set_application_resume(tracked, resume_a, "resume-a.pdf")

    def replace_resume(snapshot: Snapshot) -> Snapshot:
        snapshot.jobs[0].application_resume_id = resume_b
        return snapshot

    with pytest.raises(ValueError, match="cannot change global job status"):
        store.mutate_details(replace_resume)

    assert store.load().jobs[0].application_resume_id == resume_a


def test_mutate_details_rejects_global_job_removal(store: GlobalJobStore) -> None:
    store.import_snapshots(
        [_snapshot(_job("job-1", external_ids=("shared",), status=UserStatus.APPLIED))]
    )

    def remove_job(snapshot: Snapshot) -> Snapshot:
        snapshot.jobs.clear()
        return snapshot

    with pytest.raises(ValueError, match="cannot add or remove global jobs"):
        store.mutate_details(remove_job)

    assert [job.canonical_job_key for job in store.load().jobs] == ["job-1"]


def test_filter_untracked_jobs_returns_copy_without_changing_inputs() -> None:
    tracked_review = _job("review-tracked", external_ids=("shared",))
    untracked_review = _job("review-untracked", external_ids=("untracked",))
    review = _snapshot(tracked_review, untracked_review)
    tracker = _snapshot(
        _job(
            "tracker",
            external_ids=("shared",),
            status=UserStatus.SAVED,
        )
    )
    review_before = review.model_copy(deep=True)
    tracker_before = tracker.model_copy(deep=True)

    filtered = filter_untracked_jobs(review, tracker)

    assert [job.canonical_job_key for job in filtered.jobs] == ["review-untracked"]
    assert filtered is not review
    assert filtered.jobs[0] is not review.jobs[1]
    assert review == review_before
    assert tracker == tracker_before


def test_set_status_rejects_new(store: GlobalJobStore) -> None:
    with pytest.raises(ValueError, match="global"):
        store.set_status(_job("job-1", external_ids=("one",)), UserStatus.NEW)


def test_import_bridge_merges_two_global_records_by_source_alias(
    store: GlobalJobStore,
) -> None:
    store.import_snapshots(
        [
            _snapshot(
                _job(
                    "first",
                    external_ids=("one",),
                    status=UserStatus.SAVED,
                    status_at=NOW,
                ),
                _job(
                    "second",
                    external_ids=("two",),
                    status=UserStatus.APPLIED,
                    status_at=NOW + timedelta(minutes=1),
                ),
            )
        ]
    )

    merged = store.import_snapshots(
        [_snapshot(_job("bridge", external_ids=("one", "two")))]
    )

    assert len(merged.jobs) == 1
    assert merged.jobs[0].user_status is UserStatus.APPLIED
    assert {item.source_job_key for item in merged.jobs[0].source_occurrences} == {
        "linkedin:acme/jobs:one",
        "linkedin:acme/jobs:two",
    }


def test_selected_jobs_rejects_empty_duplicate_and_missing_keys(
    store: GlobalJobStore,
) -> None:
    store.import_snapshots(
        [
            _snapshot(
                _job("one", status=UserStatus.SAVED),
                _job("two", status=UserStatus.REJECTED),
            )
        ]
    )

    with pytest.raises(ValueError):
        store.selected_jobs([])
    with pytest.raises(ValueError):
        store.selected_jobs(["one", "one"])
    with pytest.raises(ValueError):
        store.selected_jobs(["missing"])
    assert [job.canonical_job_key for job in store.selected_jobs(["two", "one"])] == [
        "two",
        "one",
    ]


def test_persisted_global_status_reloads_from_disk(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    first = GlobalJobStore(paths)
    first.set_status(
        _job("job-1", external_ids=("one",)),
        UserStatus.IGNORED,
        now=NOW,
    )

    reloaded = GlobalJobStore(paths).load()

    assert reloaded.jobs[0].user_status is UserStatus.IGNORED
    assert paths.global_jobs_jsonl.is_file()


def test_reimporting_unchanged_snapshot_does_not_write_a_new_revision(
    store: GlobalJobStore,
) -> None:
    snapshot = _snapshot(_job("job-1", status=UserStatus.SAVED))
    first = store.import_snapshots([snapshot])

    second = store.import_snapshots([snapshot])

    assert second.meta == first.meta


def test_reimporting_unchanged_manual_job_does_not_write_a_new_revision(
    store: GlobalJobStore,
) -> None:
    job = _job("manual", external_ids=("same",))
    resume_id = "sha256:" + "1" * 64
    profile_hash = "sha256:" + "a" * 64
    store.upsert_with_default_status(
        job,
        UserStatus.SAVED,
        NOW,
        resume_id=resume_id,
        profile_hash=profile_hash,
    )
    first = store.load()

    store.upsert_with_default_status(
        job,
        UserStatus.SAVED,
        NOW,
        resume_id=resume_id,
        profile_hash=profile_hash,
    )
    second = store.load()

    assert second.meta == first.meta


def test_manual_upsert_rejects_a_stale_evaluation_snapshot(
    store: GlobalJobStore,
) -> None:
    resume_id = "sha256:" + "1" * 64
    profile_hash = "sha256:" + "a" * 64
    original = _job("manual", external_ids=("same",))
    store.upsert_with_default_status(
        original,
        UserStatus.SAVED,
        resume_id=resume_id,
        profile_hash=profile_hash,
    )
    expected = store.find("manual")
    assert expected is not None
    newer = original.model_copy(
        update={
            "last_review_attempt_at": NOW + timedelta(minutes=1),
            "score": 91,
        },
        deep=True,
    )
    store.upsert_with_default_status(
        newer,
        UserStatus.SAVED,
        resume_id=resume_id,
        profile_hash=profile_hash,
    )

    with pytest.raises(GlobalJobChanged, match="changed while this task was running"):
        store.upsert_with_default_status(
            original,
            UserStatus.SAVED,
            resume_id=resume_id,
            profile_hash=profile_hash,
            expected_job=expected,
        )


def test_reevaluation_rejects_a_stale_evaluation_snapshot(
    store: GlobalJobStore,
) -> None:
    resume_id = "sha256:" + "1" * 64
    original = _job("manual", external_ids=("same",))
    store.set_status(
        original,
        UserStatus.SAVED,
        resume_id=resume_id,
        profile_hash="sha256:" + "a" * 64,
        application_resume_filename="resume.pdf",
    )
    tracked = store.find("manual")
    assert tracked is not None
    expected = tracked.model_copy(deep=True)
    assert expected is not None
    newer = original.model_copy(
        update={
            "last_review_attempt_at": NOW + timedelta(minutes=1),
            "score": 91,
        },
        deep=True,
    )
    store.save_reevaluation(
        tracked.canonical_job_key,
        newer,
        resume_id=resume_id,
    )

    with pytest.raises(GlobalJobChanged, match="changed while this task was running"):
        store.save_reevaluation(
            tracked.canonical_job_key,
            original,
            resume_id=resume_id,
            expected_job=expected,
        )


def test_same_job_keeps_only_the_latest_resume_evaluation(
    store: GlobalJobStore,
) -> None:
    resume_a = _job("from-a", external_ids=("shared",))
    resume_a.score = 91
    resume_a.reason = "Strong Java match"
    resume_a.last_successful_review_profile_hash = "sha256:" + "a" * 64
    resume_b = _job(
        "from-b",
        external_ids=("shared",),
        last_seen=NOW - timedelta(minutes=1),
    )
    resume_b.score = 63
    resume_b.reason = "Missing Kotlin experience"
    resume_b.last_successful_review_profile_hash = "sha256:" + "b" * 64

    store.set_status(
        resume_a,
        UserStatus.SAVED,
        NOW,
        resume_id="sha256:" + "1" * 64,
        profile_hash="sha256:" + "a" * 64,
    )
    store.set_status(
        resume_b,
        UserStatus.APPLIED,
        NOW + timedelta(minutes=2),
        resume_id="sha256:" + "2" * 64,
        profile_hash="sha256:" + "b" * 64,
    )
    tracked = store.load().jobs[0]
    store.set_application_resume(
        tracked,
        "sha256:" + "1" * 64,
        "resume-a.pdf",
    )

    shown_in_tracker = store.load_for_tracker()

    assert len(store.load().jobs) == 1
    assert shown_in_tracker.jobs[0].user_status is UserStatus.APPLIED
    assert shown_in_tracker.jobs[0].last_evaluated_resume_id == (
        "sha256:" + "2" * 64
    )
    assert shown_in_tracker.jobs[0].score == 63
    assert shown_in_tracker.jobs[0].reason == "Missing Kotlin experience"


def test_legacy_resume_history_keeps_the_result_currently_shown_in_tracker(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    store = GlobalJobStore(paths)
    resume_a = _job("from-a", external_ids=("shared",))
    resume_a.score = 91
    resume_a.reason = "Strong Java match"
    resume_b = _job(
        "from-b",
        external_ids=("shared",),
        last_seen=NOW + timedelta(minutes=1),
    )
    resume_b.score = 63
    resume_b.reason = "Missing Kotlin experience"
    resume_a_id = "sha256:" + "1" * 64
    resume_b_id = "sha256:" + "2" * 64

    store.set_status(
        resume_a,
        UserStatus.SAVED,
        NOW,
        resume_id=resume_a_id,
        profile_hash="sha256:" + "a" * 64,
    )
    store.set_status(
        resume_b,
        UserStatus.APPLIED,
        NOW + timedelta(minutes=2),
        resume_id=resume_b_id,
        profile_hash="sha256:" + "b" * 64,
    )
    tracked = store.find("from-b")
    assert tracked is not None
    store.set_application_resume(tracked, resume_a_id, "resume-a.pdf")

    records = [
        json.loads(line)
        for line in paths.global_jobs_jsonl.read_text(encoding="utf-8").splitlines()
    ]
    records[1].pop("last_evaluated_resume_id")
    records[1]["resume_matches"] = [
        {
            "resume_id": resume_a_id,
            "profile_hash": "sha256:" + "a" * 64,
            "machine_status": "pending",
            "score": 91,
            "reason": "Strong Java match",
            "reviewed_at": NOW.isoformat(),
        },
        {
            "resume_id": resume_b_id,
            "profile_hash": "sha256:" + "b" * 64,
            "machine_status": "pending",
            "score": 63,
            "reason": "Missing Kotlin experience",
            "reviewed_at": (NOW + timedelta(minutes=1)).isoformat(),
        },
    ]
    paths.global_jobs_jsonl.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )

    migrated = GlobalJobStore(paths).load()

    assert migrated.jobs[0].last_evaluated_resume_id == resume_a_id
    assert migrated.jobs[0].score == 91
    assert migrated.jobs[0].reason == "Strong Java match"
    persisted = paths.global_jobs_jsonl.read_text(encoding="utf-8")
    assert '"resume_matches"' not in persisted


def test_record_reevaluation_result_persists_terminal_notice(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    store = GlobalJobStore(paths)
    store.set_status(_job("tracked"), UserStatus.SAVED, NOW)
    finished_at = NOW + timedelta(minutes=3)

    recorded = store.record_reevaluation_result(
        "tracked",
        "succeeded",
        finished_at=finished_at,
    )
    reloaded = GlobalJobStore(paths).find("tracked")

    assert recorded.reevaluation_notice is not None
    assert recorded.reevaluation_notice.status == "succeeded"
    assert recorded.reevaluation_notice.finished_at == finished_at
    assert reloaded is not None
    assert reloaded.reevaluation_notice == recorded.reevaluation_notice


def test_acknowledge_reevaluation_result_clears_persisted_notice(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    store = GlobalJobStore(paths)
    store.set_status(_job("tracked"), UserStatus.SAVED, NOW)
    notice = store.record_reevaluation_result(
        "tracked",
        "failed",
        finished_at=NOW + timedelta(minutes=4),
    ).reevaluation_notice
    assert notice is not None

    acknowledged = store.acknowledge_reevaluation_result(
        "tracked",
        notice.finished_at,
    )
    reloaded = GlobalJobStore(paths).find("tracked")

    assert acknowledged.reevaluation_notice is None
    assert reloaded is not None
    assert reloaded.reevaluation_notice is None


def test_acknowledging_stale_result_preserves_newer_result(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    store = GlobalJobStore(paths)
    store.set_status(_job("tracked"), UserStatus.SAVED, NOW)
    older = store.record_reevaluation_result(
        "tracked",
        "succeeded",
        finished_at=NOW + timedelta(minutes=4),
    ).reevaluation_notice
    assert older is not None
    newer = store.record_reevaluation_result(
        "tracked",
        "failed",
        finished_at=NOW + timedelta(minutes=5),
    ).reevaluation_notice
    assert newer is not None

    acknowledged = store.acknowledge_reevaluation_result(
        "tracked",
        older.finished_at,
    )
    reloaded = GlobalJobStore(paths).find("tracked")

    assert acknowledged.reevaluation_notice == newer
    assert reloaded is not None
    assert reloaded.reevaluation_notice == newer


def test_latest_reevaluation_result_overwrites_previous_result(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    store = GlobalJobStore(paths)
    store.set_status(_job("tracked"), UserStatus.SAVED, NOW)
    store.record_reevaluation_result(
        "tracked",
        "succeeded",
        finished_at=NOW + timedelta(minutes=4),
    )

    updated = store.record_reevaluation_result(
        "tracked",
        "failed",
        finished_at=NOW + timedelta(minutes=5),
    )

    assert updated.reevaluation_notice is not None
    assert updated.reevaluation_notice.status == "failed"
    assert updated.reevaluation_notice.finished_at == NOW + timedelta(minutes=5)


def test_import_preserves_unacknowledged_reevaluation_result(
    store: GlobalJobStore,
) -> None:
    tracked = _job("tracked", external_ids=("shared",))
    store.set_status(tracked, UserStatus.SAVED, NOW)
    store.record_reevaluation_result(
        "tracked",
        "succeeded",
        finished_at=NOW + timedelta(minutes=2),
    )
    refreshed = _job(
        "refreshed",
        external_ids=("shared",),
        last_seen=NOW + timedelta(minutes=5),
    )

    store.import_snapshots([_snapshot(refreshed)])
    saved = store.load().jobs[0]

    assert saved.reevaluation_notice is not None
    assert saved.reevaluation_notice.status == "succeeded"


def test_import_does_not_restore_an_acknowledged_reevaluation_result(
    store: GlobalJobStore,
) -> None:
    tracked = _job("tracked", external_ids=("shared",))
    store.set_status(tracked, UserStatus.SAVED, NOW)
    finished_at = NOW + timedelta(minutes=2)
    store.record_reevaluation_result(
        "tracked",
        "succeeded",
        finished_at=finished_at,
    )
    stale_snapshot = store.load().model_copy(deep=True)
    store.acknowledge_reevaluation_result("tracked", finished_at)

    store.import_snapshots([stale_snapshot])
    saved = store.load().jobs[0]

    assert saved.reevaluation_notice is None
    assert saved.reevaluation_acknowledged_at == finished_at


def test_newer_reevaluation_result_is_visible_after_previous_acknowledgement(
    store: GlobalJobStore,
) -> None:
    store.set_status(_job("tracked"), UserStatus.SAVED, NOW)
    acknowledged_at = NOW + timedelta(minutes=2)
    store.record_reevaluation_result(
        "tracked",
        "succeeded",
        finished_at=acknowledged_at,
    )
    store.acknowledge_reevaluation_result("tracked", acknowledged_at)

    updated = store.record_reevaluation_result(
        "tracked",
        "failed",
        finished_at=acknowledged_at + timedelta(minutes=1),
    )

    assert updated.reevaluation_notice is not None
    assert updated.reevaluation_notice.status == "failed"
    assert updated.reevaluation_notice.finished_at == acknowledged_at + timedelta(
        minutes=1
    )
