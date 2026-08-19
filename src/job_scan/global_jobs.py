from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from job_scan.domain import JobRecord, Snapshot, SourceOccurrence, StoreMeta, UserStatus
from job_scan.locking import FileRWLock
from job_scan.paths import AppPaths
from job_scan.repository import parse_snapshot, serialize_snapshot

GLOBAL_USER_STATUSES = frozenset(
    {
        UserStatus.SHORTLISTED,
        UserStatus.APPLIED,
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
            return _visible_snapshot(self._load_unlocked())

    def mutate_details(self, mutator: Callable[[Snapshot], Snapshot]) -> Snapshot:
        """Update global job details without changing membership or user decisions."""
        with self._lock.exclusive():
            current = self._load_unlocked()
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
                    or job.global_status_deleted_at
                    != current_job.global_status_deleted_at
                ):
                    raise ValueError("detail mutation cannot change global job status")
            return _visible_snapshot(self._persist_unlocked(current, proposed.jobs))

    def import_snapshots(self, snapshots: Iterable[Snapshot]) -> Snapshot:
        """Import historical selected states and refresh matching job details."""
        with self._lock.exclusive():
            current = self._load_unlocked()
            jobs = [job.model_copy(deep=True) for job in current.jobs]
            changed = False
            for snapshot in snapshots:
                for job in snapshot.jobs:
                    _merged, did_change = _merge_into(jobs, job)
                    changed = changed or did_change
            if not changed:
                return _visible_snapshot(current)
            return _visible_snapshot(self._persist_unlocked(current, jobs))

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
        return result

    def set_status(
        self,
        job: JobRecord,
        status: UserStatus,
        now: datetime | None = None,
    ) -> Snapshot:
        """Persist one selected status for a job, regardless of its source snapshot."""
        try:
            selected_status = UserStatus(status)
        except ValueError as exc:
            raise ValueError("status is not a global user status") from exc
        if selected_status not in GLOBAL_USER_STATUSES:
            raise ValueError("global job status cannot be new")
        updated_at = now if now is not None else datetime.now(UTC)

        with self._lock.exclusive():
            current = self._load_unlocked()
            jobs = [item.model_copy(deep=True) for item in current.jobs]
            candidate = job.model_copy(deep=True)
            candidate.user_status = selected_status
            candidate.user_status_updated_at = updated_at
            candidate.global_status_deleted_at = None
            _restore_deleted_matches(jobs, candidate)
            merged, _changed = _merge_into(jobs, candidate)
            merged.user_status = selected_status
            merged.user_status_updated_at = updated_at
            merged.global_status_deleted_at = None
            return _visible_snapshot(self._persist_unlocked(current, jobs))

    def upsert_with_default_status(
        self,
        job: JobRecord,
        default_status: UserStatus,
        now: datetime | None = None,
    ) -> JobRecord:
        """Insert with a default state, but preserve an existing user decision."""
        try:
            selected_default = UserStatus(default_status)
        except ValueError as exc:
            raise ValueError("status is not a global user status") from exc
        if selected_default not in GLOBAL_USER_STATUSES:
            raise ValueError("global job status cannot be new")
        updated_at = now if now is not None else datetime.now(UTC)

        with self._lock.exclusive():
            current = self._load_unlocked()
            jobs = [item.model_copy(deep=True) for item in current.jobs]
            candidate = job.model_copy(deep=True)
            candidate.global_status_deleted_at = None
            _restore_deleted_matches(jobs, candidate)
            existing_status = _newest_status_job(_matching_jobs(jobs, candidate))
            if existing_status is None:
                candidate.user_status = selected_default
                candidate.user_status_updated_at = updated_at
            else:
                candidate.user_status = existing_status.user_status
                candidate.user_status_updated_at = existing_status.user_status_updated_at
            merged, _changed = _merge_into(jobs, candidate)
            merged.global_status_deleted_at = None
            self._persist_unlocked(current, jobs)
            return merged.model_copy(deep=True)

    def delete(self, key: str, now: datetime | None = None) -> None:
        """Hide one global job and prevent passive history imports from restoring it."""
        deleted_at = now if now is not None else datetime.now(UTC)
        with self._lock.exclusive():
            current = self._load_unlocked()
            jobs = [item.model_copy(deep=True) for item in current.jobs]
            job = next(
                (
                    item
                    for item in jobs
                    if item.canonical_job_key == key
                    and item.global_status_deleted_at is None
                ),
                None,
            )
            if job is None:
                raise KeyError(key)
            job.global_status_deleted_at = deleted_at
            self._persist_unlocked(current, jobs)

    def find(self, key: str) -> JobRecord | None:
        """Return a copy of one globally stored job by its canonical key."""
        with self._lock.shared():
            job = next(
                (
                    item
                    for item in self._load_unlocked().jobs
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
        if not self._paths.global_jobs_jsonl.exists():
            return Snapshot(meta=StoreMeta(data_revision=0))
        return parse_snapshot(self._paths.global_jobs_jsonl.read_bytes())

    def _persist_unlocked(self, current: Snapshot, jobs: list[JobRecord]) -> Snapshot:
        snapshot = Snapshot(
            meta=StoreMeta(
                data_revision=current.meta.data_revision + 1,
                generated_at=datetime.now(UTC),
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
    deleted = next(
        (jobs[index] for index in matches if jobs[index].global_status_deleted_at),
        None,
    )
    if deleted is not None:
        merged_occurrences = _merged_occurrences(
            [*(jobs[index] for index in matches), incoming]
        )
        changed = len(matches) > 1 or deleted.source_occurrences != merged_occurrences
        deleted.source_occurrences = merged_occurrences
        deleted_index = next(
            index for index in matches if jobs[index] is deleted
        )
        for index in reversed(matches):
            if index != deleted_index:
                del jobs[index]
        return deleted, changed
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


def _restore_deleted_matches(jobs: Sequence[JobRecord], incoming: JobRecord) -> None:
    """Clear deletion markers when the user explicitly selects the job again."""
    for index in _matching_indices(jobs, incoming):
        jobs[index].global_status_deleted_at = None


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
    profile.source_occurrences = _merged_occurrences(candidates)
    return profile


def _newest_status_job(candidates: Sequence[JobRecord]) -> JobRecord | None:
    statuses = [job for job in candidates if job.user_status in GLOBAL_USER_STATUSES]
    if not statuses:
        return None
    return max(
        statuses,
        key=lambda job: (job.user_status_updated_at, job.canonical_job_key),
    )


def _merged_occurrences(candidates: Sequence[JobRecord]) -> list[SourceOccurrence]:
    occurrences: dict[str, SourceOccurrence] = {}
    for candidate in candidates:
        for occurrence in candidate.source_occurrences:
            occurrences.setdefault(
                occurrence.source_occurrence_key,
                occurrence.model_copy(deep=True),
            )
    return list(occurrences.values())


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
