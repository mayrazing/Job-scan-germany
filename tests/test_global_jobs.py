from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
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
        status=UserStatus.SHORTLISTED,
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
    assert store.find("new-only") is None


def test_import_migrates_legacy_reviewed_jsonl_to_shortlisted(
    store: GlobalJobStore,
) -> None:
    legacy = serialize_snapshot(
        _snapshot(_job("old-key", status=UserStatus.SHORTLISTED))
    ).replace(b'"shortlisted"', b'"reviewed"')

    imported = store.import_snapshots([parse_snapshot(legacy)])

    assert imported.jobs[0].user_status is UserStatus.SHORTLISTED


def test_set_status_cannot_be_rolled_back_by_older_history(store: GlobalJobStore) -> None:
    historical = _job(
        "job-1",
        external_ids=("shared",),
        status=UserStatus.SHORTLISTED,
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


def test_delete_hides_job_and_prevents_passive_history_reimport(
    store: GlobalJobStore,
) -> None:
    historical = _job(
        "job-1",
        external_ids=("shared",),
        status=UserStatus.SHORTLISTED,
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
        status=UserStatus.SHORTLISTED,
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


def test_deletion_marker_serializes_only_for_deleted_global_jobs(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    store = GlobalJobStore(paths)
    store.set_status(
        _job("job-1", external_ids=("shared",)),
        UserStatus.SHORTLISTED,
        NOW,
    )

    assert b'"global_status_deleted_at"' not in paths.global_jobs_jsonl.read_bytes()

    store.delete("job-1", now=NOW + timedelta(minutes=1))

    assert b'"global_status_deleted_at"' in paths.global_jobs_jsonl.read_bytes()


def test_setting_status_explicitly_restores_a_deleted_global_job(
    store: GlobalJobStore,
) -> None:
    historical = _job(
        "job-1",
        external_ids=("shared",),
        status=UserStatus.SHORTLISTED,
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
    store.upsert_with_default_status(manual, UserStatus.SHORTLISTED, NOW)
    store.delete("manual", now=NOW + timedelta(minutes=1))

    restored = store.upsert_with_default_status(
        manual,
        UserStatus.SHORTLISTED,
        NOW + timedelta(minutes=2),
    )

    assert restored.user_status is UserStatus.SHORTLISTED
    assert restored.global_status_deleted_at is None
    assert store.find("manual") is not None


def test_mutate_details_preserves_the_latest_global_status(
    store: GlobalJobStore,
) -> None:
    job = _job(
        "job-1",
        external_ids=("shared",),
        status=UserStatus.SHORTLISTED,
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
        status=UserStatus.SHORTLISTED,
    )
    store.import_snapshots([_snapshot(job)])

    def replace_status(snapshot: Snapshot) -> Snapshot:
        snapshot.jobs[0].user_status = UserStatus.NEW
        return snapshot

    with pytest.raises(ValueError, match="cannot change global job status"):
        store.mutate_details(replace_status)

    assert store.load().jobs[0].user_status is UserStatus.SHORTLISTED


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
                    status=UserStatus.SHORTLISTED,
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
                _job("one", status=UserStatus.SHORTLISTED),
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
    snapshot = _snapshot(_job("job-1", status=UserStatus.SHORTLISTED))
    first = store.import_snapshots([snapshot])

    second = store.import_snapshots([snapshot])

    assert second.meta == first.meta
