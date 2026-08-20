from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from job_scan.domain import (
    GlobalJobDeletion,
    JobRecord,
    ResumeMatch,
    Snapshot,
    SourceOccurrence,
    StoreMeta,
    UserStatus,
    UserStatusHistoryEntry,
)
from job_scan.locking import FileRWLock
from job_scan.paths import AppPaths
from job_scan.repository import parse_snapshot, serialize_snapshot

GLOBAL_USER_STATUSES = frozenset(
    {
        UserStatus.SAVED,
        UserStatus.APPLIED,
        UserStatus.INTERVIEWING,
        UserStatus.OFFER,
        UserStatus.WITHDRAWN,
        UserStatus.REJECTED,
        UserStatus.IGNORED,
    }
)


class GlobalJobStore:
    """Persist user-selected job states across independent search snapshots."""

    def __init__(self, paths: AppPaths) -> None:
        self._paths = paths
        self._lock = FileRWLock(paths.global_jobs_lock_file)
        paths.ensure_directories()

    def load(self) -> Snapshot:
        """Load the global job snapshot, returning an empty snapshot when absent."""
        with self._lock.shared():
            current, migration_needed = self._read_unlocked()
        if migration_needed:
            with self._lock.exclusive():
                current = self._load_and_migrate_unlocked()
        return _visible_snapshot(current)

    def load_for_resume(self, resume_id: str) -> Snapshot:
        """Load global jobs associated with one resume and show its match result."""
        current = self.load()
        jobs: list[JobRecord] = []
        for job in current.jobs:
            match = next(
                (item for item in job.resume_matches if item.resume_id == resume_id),
                None,
            )
            if match is None:
                continue
            shown = job.model_copy(deep=True)
            _apply_resume_match(shown, match)
            jobs.append(shown)
        return Snapshot(meta=current.meta.model_copy(deep=True), jobs=jobs)

    def mutate_details(self, mutator: Callable[[Snapshot], Snapshot]) -> Snapshot:
        """Update global job details without changing membership or user decisions."""
        with self._lock.exclusive():
            current = self._load_and_migrate_unlocked()
            proposed = mutator(current.model_copy(deep=True))
            if not isinstance(proposed, Snapshot):
                raise TypeError("mutator must return Snapshot")
            current_by_key = {job.canonical_job_key: job for job in current.jobs}
            proposed_by_key = {job.canonical_job_key: job for job in proposed.jobs}
            if current_by_key.keys() != proposed_by_key.keys():
                raise ValueError("detail mutation cannot add or remove global jobs")
            for key, job in proposed_by_key.items():
                current_job = current_by_key[key]
                if (
                    job.user_status not in GLOBAL_USER_STATUSES
                    or job.user_status != current_job.user_status
                    or job.user_status_updated_at != current_job.user_status_updated_at
                    or job.user_status_history != current_job.user_status_history
                    or job.global_status_deleted_at
                    != current_job.global_status_deleted_at
                ):
                    raise ValueError("detail mutation cannot change global job status")
            return _visible_snapshot(self._persist_unlocked(current, proposed.jobs))

    def import_snapshots(self, snapshots: Iterable[Snapshot]) -> Snapshot:
        """Import historical selected states and refresh matching job details."""
        with self._lock.exclusive():
            current = self._load_and_migrate_unlocked()
            jobs = [job.model_copy(deep=True) for job in current.jobs]
            deletions = [
                item.model_copy(deep=True)
                for item in current.meta.global_job_deletions
            ]
            changed = False
            for snapshot in snapshots:
                for job in snapshot.jobs:
                    candidate = job.model_copy(deep=True)
                    deletion = _matching_deletion(deletions, candidate)
                    if deletion is not None:
                        changed = _learn_deletion_aliases(deletion, candidate) or changed
                        continue
                    _merged, did_change = _merge_into(jobs, candidate)
                    changed = changed or did_change
            if not changed:
                return _visible_snapshot(current)
            return _visible_snapshot(
                self._persist_unlocked(current, jobs, deletions=deletions)
            )

    def overlay(self, snapshot: Snapshot) -> Snapshot:
        """Return a copy whose matching jobs carry their persisted user state."""
        current = self.load()
        result = snapshot.model_copy(deep=True)
        for job in result.jobs:
            matches = _matching_jobs(current.jobs, job)
            if not matches:
                continue
            status_job = _newest_status_job(matches)
            if status_job is None:
                continue
            job.user_status = status_job.user_status
            job.user_status_updated_at = status_job.user_status_updated_at
            job.user_status_history = [
                entry.model_copy(deep=True)
                for entry in status_job.user_status_history
            ]
        return result

    def set_status(
        self,
        job: JobRecord,
        status: UserStatus,
        now: datetime | None = None,
        *,
        resume_id: str | None = None,
        profile_hash: str | None = None,
    ) -> Snapshot:
        """Persist one selected status for a job, regardless of its source snapshot."""
        try:
            selected_status = UserStatus(status)
        except ValueError as exc:
            raise ValueError("status is not a global user status") from exc
        if selected_status not in GLOBAL_USER_STATUSES:
            raise ValueError("global job status cannot be new")
        updated_at = _utc_timestamp(now if now is not None else datetime.now(UTC))

        with self._lock.exclusive():
            current = self._load_and_migrate_unlocked()
            jobs = [item.model_copy(deep=True) for item in current.jobs]
            deletions = [
                item.model_copy(deep=True)
                for item in current.meta.global_job_deletions
            ]
            candidate = job.model_copy(deep=True)
            restored = _remove_matching_deletions(deletions, candidate)
            if restored:
                candidate.user_status_history = []
            previous = _newest_status_job(_matching_jobs(jobs, candidate))
            if previous is None:
                candidate.user_status_history = []
            else:
                candidate.user_status_history = [
                    entry.model_copy(deep=True)
                    for entry in previous.user_status_history
                ]
            candidate.user_status = selected_status
            candidate.user_status_updated_at = (
                previous.user_status_updated_at
                if previous is not None and previous.user_status is selected_status
                else updated_at
            )
            candidate.global_status_deleted_at = None
            if resume_id is not None and profile_hash is not None:
                _save_resume_match(candidate, resume_id, profile_hash)
            merged, _changed = _merge_into(jobs, candidate)
            if previous is None:
                merged.user_status_history = []
                _record_status(merged, UserStatus.SAVED, updated_at)
            _record_status(merged, selected_status, updated_at)
            merged.global_status_deleted_at = None
            return _visible_snapshot(
                self._persist_unlocked(current, jobs, deletions=deletions)
            )

    def upsert_with_default_status(
        self,
        job: JobRecord,
        default_status: UserStatus,
        now: datetime | None = None,
        *,
        resume_id: str | None = None,
        profile_hash: str | None = None,
    ) -> JobRecord:
        """Insert with a default state, but preserve an existing user decision."""
        try:
            selected_default = UserStatus(default_status)
        except ValueError as exc:
            raise ValueError("status is not a global user status") from exc
        if selected_default not in GLOBAL_USER_STATUSES:
            raise ValueError("global job status cannot be new")
        updated_at = _utc_timestamp(now if now is not None else datetime.now(UTC))

        with self._lock.exclusive():
            current = self._load_and_migrate_unlocked()
            jobs = [item.model_copy(deep=True) for item in current.jobs]
            deletions = [
                item.model_copy(deep=True)
                for item in current.meta.global_job_deletions
            ]
            candidate = job.model_copy(deep=True)
            restored = _remove_matching_deletions(deletions, candidate)
            if restored:
                candidate.user_status_history = []
            candidate.global_status_deleted_at = None
            if resume_id is not None and profile_hash is not None:
                _save_resume_match(candidate, resume_id, profile_hash)
            existing_status = _newest_status_job(_matching_jobs(jobs, candidate))
            if existing_status is None:
                candidate.user_status_history = []
                candidate.user_status = selected_default
                candidate.user_status_updated_at = updated_at
            else:
                candidate.user_status_history = [
                    entry.model_copy(deep=True)
                    for entry in existing_status.user_status_history
                ]
                candidate.user_status = existing_status.user_status
                candidate.user_status_updated_at = existing_status.user_status_updated_at
            merged, _changed = _merge_into(jobs, candidate)
            _record_status(
                merged,
                candidate.user_status,
                candidate.user_status_updated_at,
            )
            merged.global_status_deleted_at = None
            if jobs != current.jobs or restored:
                self._persist_unlocked(current, jobs, deletions=deletions)
            return merged.model_copy(deep=True)

    def associate_profile(self, *, resume_id: str, profile_hash: str) -> None:
        """Associate migrated global jobs whose active review used one profile."""
        with self._lock.exclusive():
            current = self._load_and_migrate_unlocked()
            jobs = [item.model_copy(deep=True) for item in current.jobs]
            changed = False
            for job in jobs:
                if profile_hash not in _active_profile_hashes(job):
                    continue
                before = job.resume_matches
                _save_resume_match(job, resume_id, profile_hash)
                changed = changed or job.resume_matches != before
            if changed:
                self._persist_unlocked(current, jobs)

    def delete(self, key: str, now: datetime | None = None) -> None:
        """Delete one tracked job while retaining only re-import identifiers."""
        deleted_at = _utc_timestamp(now if now is not None else datetime.now(UTC))
        with self._lock.exclusive():
            current = self._load_and_migrate_unlocked()
            jobs = [item.model_copy(deep=True) for item in current.jobs]
            index = next(
                (index for index, item in enumerate(jobs) if item.canonical_job_key == key),
                None,
            )
            if index is None:
                raise KeyError(key)
            job = jobs.pop(index)
            deletions = [
                item.model_copy(deep=True)
                for item in current.meta.global_job_deletions
            ]
            deletions.append(_deletion_for(job, deleted_at))
            self._persist_unlocked(current, jobs, deletions=deletions)

    def find(self, key: str) -> JobRecord | None:
        """Return a copy of one globally stored job by its canonical key."""
        job = next(
            (
                item
                for item in self.load().jobs
                if item.canonical_job_key == key
                and item.global_status_deleted_at is None
            ),
            None,
        )
        return job.model_copy(deep=True) if job is not None else None

    def selected_jobs(self, keys: Sequence[str]) -> tuple[JobRecord, ...]:
        """Return requested global jobs in caller order after strict key validation."""
        if not keys or len(set(keys)) != len(keys):
            raise ValueError("selected job keys must be non-empty and unique")
        snapshot = self.load()
        by_key = {job.canonical_job_key: job for job in snapshot.jobs}
        try:
            return tuple(by_key[key].model_copy(deep=True) for key in keys)
        except KeyError as exc:
            raise ValueError(f"unknown global job key: {exc.args[0]}") from exc

    def _load_unlocked(self) -> Snapshot:
        return self._read_unlocked()[0]

    def _load_and_migrate_unlocked(self) -> Snapshot:
        current, migration_needed = self._read_unlocked()
        if not migration_needed:
            return current
        return self._persist_unlocked(current, current.jobs)

    def _read_unlocked(self) -> tuple[Snapshot, bool]:
        if not self._paths.global_jobs_jsonl.exists():
            return Snapshot(meta=StoreMeta(data_revision=0)), False
        loaded = parse_snapshot(self._paths.global_jobs_jsonl.read_bytes())
        deletions = [
            item.model_copy(deep=True) for item in loaded.meta.global_job_deletions
        ]
        jobs: list[JobRecord] = []
        migration_needed = False
        for loaded_job in loaded.jobs:
            job = loaded_job.model_copy(deep=True)
            if job.global_status_deleted_at is not None:
                deletions.append(_deletion_for(job, job.global_status_deleted_at))
                migration_needed = True
                continue
            had_history = bool(job.user_status_history)
            _ensure_status_history(job)
            migration_needed = migration_needed or (
                not had_history and bool(job.user_status_history)
            )
            jobs.append(job)
        return (
            Snapshot(
                meta=loaded.meta.model_copy(
                    update={"global_job_deletions": deletions}
                ),
                jobs=jobs,
            ),
            migration_needed,
        )

    def _persist_unlocked(
        self,
        current: Snapshot,
        jobs: list[JobRecord],
        *,
        deletions: list[GlobalJobDeletion] | None = None,
    ) -> Snapshot:
        for job in jobs:
            _ensure_status_history(job)
        snapshot = Snapshot(
            meta=StoreMeta(
                data_revision=current.meta.data_revision + 1,
                generated_at=datetime.now(UTC),
                global_job_deletions=(
                    [item.model_copy(deep=True) for item in deletions]
                    if deletions is not None
                    else [
                        item.model_copy(deep=True)
                        for item in current.meta.global_job_deletions
                    ]
                ),
            ),
            jobs=jobs,
        )
        temporary = _write_temp(
            self._paths.global_jobs_jsonl,
            serialize_snapshot(snapshot),
        )
        try:
            os.replace(temporary, self._paths.global_jobs_jsonl)
            _fsync_directory(self._paths.global_jobs_jsonl.parent)
        finally:
            temporary.unlink(missing_ok=True)
        return snapshot.model_copy(deep=True)


def _merge_into(jobs: list[JobRecord], incoming: JobRecord) -> tuple[JobRecord, bool]:
    matches = _matching_indices(jobs, incoming)
    if not matches:
        if incoming.user_status not in GLOBAL_USER_STATUSES:
            return incoming.model_copy(deep=True), False
        jobs.append(incoming.model_copy(deep=True))
        return jobs[-1], True

    candidates = [*(jobs[index] for index in matches), incoming]
    merged = _merge_jobs(candidates)
    first = matches[0]
    changed = len(matches) > 1 or jobs[first] != merged
    jobs[first] = merged
    for index in reversed(matches[1:]):
        del jobs[index]
    return jobs[first], changed


def _deletion_for(job: JobRecord, deleted_at: datetime) -> GlobalJobDeletion:
    return GlobalJobDeletion(
        canonical_job_keys=[job.canonical_job_key],
        source_job_keys=sorted(_source_aliases(job)),
        deleted_at=deleted_at,
    )


def _matching_deletion(
    deletions: Sequence[GlobalJobDeletion],
    job: JobRecord,
) -> GlobalJobDeletion | None:
    aliases = _source_aliases(job)
    return next(
        (
            deletion
            for deletion in deletions
            if job.canonical_job_key in deletion.canonical_job_keys
            or bool(aliases & set(deletion.source_job_keys))
        ),
        None,
    )


def _learn_deletion_aliases(
    deletion: GlobalJobDeletion,
    job: JobRecord,
) -> bool:
    canonical_keys = sorted({*deletion.canonical_job_keys, job.canonical_job_key})
    source_job_keys = sorted({*deletion.source_job_keys, *_source_aliases(job)})
    if (
        canonical_keys == deletion.canonical_job_keys
        and source_job_keys == deletion.source_job_keys
    ):
        return False
    deletion.canonical_job_keys = canonical_keys
    deletion.source_job_keys = source_job_keys
    return True


def _remove_matching_deletions(
    deletions: list[GlobalJobDeletion],
    job: JobRecord,
) -> bool:
    matching = [
        index
        for index, deletion in enumerate(deletions)
        if _matching_deletion((deletion,), job) is not None
    ]
    for index in reversed(matching):
        del deletions[index]
    return bool(matching)


def _matching_indices(jobs: Sequence[JobRecord], incoming: JobRecord) -> list[int]:
    return [
        index
        for index, existing in enumerate(jobs)
        if _same_job(existing, incoming)
    ]


def _matching_jobs(jobs: Sequence[JobRecord], incoming: JobRecord) -> list[JobRecord]:
    return [job for job in jobs if _same_job(job, incoming)]


def _same_job(left: JobRecord, right: JobRecord) -> bool:
    if not left.source_occurrences or not right.source_occurrences:
        return left.canonical_job_key == right.canonical_job_key
    return bool(_source_aliases(left) & _source_aliases(right))


def _source_aliases(job: JobRecord) -> set[str]:
    return {item.source_job_key for item in job.source_occurrences}


def _merge_jobs(candidates: Sequence[JobRecord]) -> JobRecord:
    profile = max(
        candidates,
        key=lambda job: (
            job.last_seen,
            job.user_status_updated_at,
            job.canonical_job_key,
        ),
    ).model_copy(deep=True)
    profile.first_seen = min(job.first_seen for job in candidates)
    profile.last_seen = max(job.last_seen for job in candidates)
    status_job = _newest_status_job(candidates)
    if status_job is not None:
        profile.user_status = status_job.user_status
        profile.user_status_updated_at = status_job.user_status_updated_at
    if status_job is not None:
        profile.user_status_history = (
            _merged_status_history(candidates)
            if status_job.user_status_history
            else []
        )
        _record_status(
            profile,
            status_job.user_status,
            status_job.user_status_updated_at,
        )
    else:
        profile.user_status_history = []
    profile.source_occurrences = _merged_occurrences(candidates)
    profile.resume_matches = _merged_resume_matches(candidates)
    return profile


def _newest_status_job(candidates: Sequence[JobRecord]) -> JobRecord | None:
    statuses = [job for job in candidates if job.user_status in GLOBAL_USER_STATUSES]
    if not statuses:
        return None
    return max(
        statuses,
        key=lambda job: (
            job.user_status_updated_at,
            bool(job.user_status_history),
            job.canonical_job_key,
        ),
    )


def _ensure_status_history(job: JobRecord) -> None:
    if job.user_status not in GLOBAL_USER_STATUSES or job.user_status_history:
        return
    entry = UserStatusHistoryEntry(
        status=job.user_status,
        changed_at=job.user_status_updated_at,
    )
    job.user_status_history = [entry]
    job.user_status_updated_at = entry.changed_at


def _record_status(job: JobRecord, status: UserStatus, changed_at: datetime) -> None:
    if job.user_status_history and job.user_status_history[-1].status is status:
        job.user_status = status
        job.user_status_updated_at = job.user_status_history[-1].changed_at
        return
    job.user_status_history = [
        *job.user_status_history,
        UserStatusHistoryEntry(status=status, changed_at=changed_at),
    ]
    job.user_status = status
    job.user_status_updated_at = job.user_status_history[-1].changed_at


def _utc_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("tracker timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _merged_status_history(
    candidates: Sequence[JobRecord],
) -> list[UserStatusHistoryEntry]:
    entries: dict[tuple[datetime, UserStatus], UserStatusHistoryEntry] = {}
    for candidate in candidates:
        for entry in candidate.user_status_history:
            entries.setdefault(
                (entry.changed_at, entry.status),
                entry.model_copy(deep=True),
            )
    return sorted(entries.values(), key=lambda entry: entry.changed_at)


def _merged_occurrences(candidates: Sequence[JobRecord]) -> list[SourceOccurrence]:
    occurrences: dict[str, SourceOccurrence] = {}
    for candidate in candidates:
        for occurrence in candidate.source_occurrences:
            occurrences.setdefault(
                occurrence.source_occurrence_key,
                occurrence.model_copy(deep=True),
            )
    return list(occurrences.values())


def _save_resume_match(job: JobRecord, resume_id: str, profile_hash: str) -> None:
    match = ResumeMatch(
        resume_id=resume_id,
        profile_hash=profile_hash,
        machine_status=job.machine_status,
        manual_override=job.manual_override,
        manual_override_content_hash=job.manual_override_content_hash,
        manual_override_profile_hash=job.manual_override_profile_hash,
        ai_review=job.ai_review.model_copy(deep=True) if job.ai_review else None,
        score=job.score,
        reason=job.reason,
        review_model=job.review_model,
        reviewed_at=job.reviewed_at,
        last_review_attempt_content_hash=job.last_review_attempt_content_hash,
        last_review_attempt_profile_hash=job.last_review_attempt_profile_hash,
        last_review_attempt_at=job.last_review_attempt_at,
        last_successful_review_content_hash=job.last_successful_review_content_hash,
        last_successful_review_profile_hash=job.last_successful_review_profile_hash,
        exclusion_reasons=list(job.exclusion_reasons),
        labels=list(job.labels),
        last_error=job.last_error,
    )
    for index, existing in enumerate(job.resume_matches):
        if existing.resume_id != resume_id:
            continue
        if existing == match:
            return
        updated = list(job.resume_matches)
        updated[index] = match
        job.resume_matches = updated
        return
    job.resume_matches = [*job.resume_matches, match]


def _apply_resume_match(job: JobRecord, match: ResumeMatch) -> None:
    for field_name in ResumeMatch.model_fields:
        if field_name in {"resume_id", "profile_hash"}:
            continue
        value = getattr(match, field_name)
        setattr(
            job,
            field_name,
            value.model_copy(deep=True) if hasattr(value, "model_copy") else value,
        )
    job.exclusion_reasons = list(match.exclusion_reasons)
    job.labels = list(match.labels)


def _merged_resume_matches(candidates: Sequence[JobRecord]) -> list[ResumeMatch]:
    matches: dict[str, ResumeMatch] = {}
    for candidate in candidates:
        for match in candidate.resume_matches:
            matches[match.resume_id] = match.model_copy(deep=True)
    return list(matches.values())


def _active_profile_hashes(job: JobRecord) -> set[str]:
    return {
        value
        for value in (
            job.last_successful_review_profile_hash,
            job.last_review_attempt_profile_hash,
            job.manual_override_profile_hash,
        )
        if value is not None
    }


def _visible_snapshot(snapshot: Snapshot) -> Snapshot:
    """Return the public global snapshot without deleted tombstone records."""
    return Snapshot(
        meta=snapshot.meta.model_copy(deep=True),
        jobs=[
            job.model_copy(deep=True)
            for job in snapshot.jobs
            if job.global_status_deleted_at is None
        ],
    )


def _write_temp(destination: Path, data: bytes) -> Path:
    descriptor, name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    path = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
