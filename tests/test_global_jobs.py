from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from job_scan.domain import (
    AvailabilityStatus,
    JobRecord,
    Snapshot,
    SourceKind,
    SourceOccurrence,
    StoreMeta,
    UserStatus,
)
from job_scan.global_jobs import GlobalJobStore
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


def test_first_application_status_records_resume_without_later_overwrite(
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
    assert applied.jobs[0].application_resume_id == resume_a
    assert interviewing.jobs[0].application_resume_id == resume_a


def test_application_resume_can_be_corrected_without_changing_progress(
    store: GlobalJobStore,
) -> None:
    job = _job("job-1", external_ids=("shared",))
    resume_a = "sha256:" + "1" * 64
    resume_b = "sha256:" + "2" * 64
    applied = store.set_status(
        job,
        UserStatus.APPLIED,
        NOW,
        resume_id=resume_a,
        profile_hash="sha256:" + "a" * 64,
    )

    corrected = store.set_application_resume(applied.jobs[0], resume_b)

    assert corrected.application_resume_id == resume_b
    assert corrected.user_status is UserStatus.APPLIED
    assert [entry.status for entry in corrected.user_status_history] == [
        UserStatus.SAVED,
        UserStatus.APPLIED,
    ]


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


def test_overlay_copies_global_status_without_changing_input(store: GlobalJobStore) -> None:
    global_job = _job(
        "global-key",
        external_ids=("shared",),
        status=UserStatus.REJECTED,
        status_at=NOW + timedelta(minutes=1),
    )
    store.import_snapshots([_snapshot(global_job)])
    incoming = _snapshot(_job("local-key", external_ids=("shared",)))

    overlaid = store.overlay(incoming)

    assert overlaid is not incoming
    assert overlaid.jobs[0] is not incoming.jobs[0]
    assert overlaid.jobs[0].user_status is UserStatus.REJECTED
    assert overlaid.jobs[0].user_status_updated_at == NOW + timedelta(minutes=1)
    assert incoming.jobs[0].user_status is UserStatus.NEW


def test_overlay_copies_application_resume_without_changing_input(
    store: GlobalJobStore,
) -> None:
    job = _job("global-key", external_ids=("shared",))
    resume_id = "sha256:" + "1" * 64
    store.set_status(
        job,
        UserStatus.APPLIED,
        NOW,
        resume_id=resume_id,
        profile_hash="sha256:" + "a" * 64,
    )
    incoming = _snapshot(_job("local-key", external_ids=("shared",)))

    overlaid = store.overlay(incoming)

    assert overlaid.jobs[0].application_resume_id == resume_id
    assert incoming.jobs[0].application_resume_id is None


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


def test_same_job_keeps_one_status_but_distinct_resume_matches(
    store: GlobalJobStore,
) -> None:
    resume_a = _job("from-a", external_ids=("shared",))
    resume_a.score = 91
    resume_a.reason = "Strong Java match"
    resume_a.last_successful_review_profile_hash = "sha256:" + "a" * 64
    resume_b = _job(
        "from-b",
        external_ids=("shared",),
        last_seen=NOW + timedelta(minutes=1),
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

    shown_for_a = store.load_for_resume("sha256:" + "1" * 64)
    shown_for_b = store.load_for_resume("sha256:" + "2" * 64)

    assert len(store.load().jobs) == 1
    assert shown_for_a.jobs[0].user_status is UserStatus.APPLIED
    assert shown_for_a.jobs[0].score == 91
    assert shown_for_a.jobs[0].reason == "Strong Java match"
    assert shown_for_b.jobs[0].user_status is UserStatus.APPLIED
    assert shown_for_b.jobs[0].score == 63
    assert shown_for_b.jobs[0].reason == "Missing Kotlin experience"


def test_existing_global_job_can_be_associated_by_profile_hash(
    store: GlobalJobStore,
) -> None:
    existing = _job("job-1", external_ids=("shared",))
    existing.score = 88
    existing.last_successful_review_profile_hash = "sha256:" + "a" * 64
    store.set_status(existing, UserStatus.SAVED, NOW)

    store.associate_profile(
        resume_id="sha256:" + "1" * 64,
        profile_hash="sha256:" + "a" * 64,
    )

    associated = store.load_for_resume("sha256:" + "1" * 64)
    assert [job.canonical_job_key for job in associated.jobs] == ["job-1"]
    assert associated.jobs[0].score == 88


def test_reassociating_known_profiles_does_not_write_new_revisions(
    store: GlobalJobStore,
) -> None:
    existing = _job("job-1", external_ids=("shared",))
    existing.last_successful_review_profile_hash = "sha256:" + "a" * 64
    existing.last_review_attempt_profile_hash = "sha256:" + "b" * 64
    store.set_status(existing, UserStatus.SAVED, NOW)
    associations = (
        ("sha256:" + "1" * 64, "sha256:" + "a" * 64),
        ("sha256:" + "2" * 64, "sha256:" + "b" * 64),
    )
    for resume_id, profile_hash in associations:
        store.associate_profile(resume_id=resume_id, profile_hash=profile_hash)
    first = store.load()

    for resume_id, profile_hash in associations:
        store.associate_profile(resume_id=resume_id, profile_hash=profile_hash)
    second = store.load()

    assert second.meta == first.meta
