from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from job_scan.domain import (
    GlobalJobDeletion,
    JobNote,
    JobRecord,
    ReevaluationNotice,
    SalaryValue,
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
_EVALUATION_FIELDS = (
    "machine_status",
    "manual_override",
    "manual_override_content_hash",
    "manual_override_profile_hash",
    "ai_review",
    "score",
    "reason",
    "review_model",
    "reviewed_at",
    "last_review_attempt_content_hash",
    "last_review_attempt_profile_hash",
    "last_review_attempt_at",
    "last_successful_review_content_hash",
    "last_successful_review_profile_hash",
    "exclusion_reasons",
    "labels",
    "last_error",
)


class GlobalJobChanged(RuntimeError):
    """Report that an AI result is stale relative to the tracked job."""


def _evaluation_state(job: JobRecord | None) -> tuple[object, ...] | None:
    """Return only fields that identify the current source and AI evaluation."""
    if job is None:
        return None
    return (
        job.content_hash,
        job.application_resume_id,
        job.application_resume_filename,
        job.last_evaluated_resume_id,
        *(getattr(job, field) for field in _EVALUATION_FIELDS),
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

    def load_read_only(self) -> Snapshot:
        """Load visible global jobs without persisting legacy migrations."""
        with self._lock.shared():
            current, _migration_needed = self._read_unlocked()
        return _visible_snapshot(current)

    def load_for_tracker(self) -> Snapshot:
        """Load every global job with only its latest review result."""
        return self.load()

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
                    or job.application_resume_id != current_job.application_resume_id
                    or job.application_resume_filename
                    != current_job.application_resume_filename
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

    def set_status(
        self,
        job: JobRecord,
        status: UserStatus,
        now: datetime | None = None,
        *,
        resume_id: str | None = None,
        profile_hash: str | None = None,
        application_resume_filename: str | None = None,
    ) -> Snapshot:
        """Persist one selected status for a job, regardless of its source snapshot."""
        try:
            selected_status = UserStatus(status)
        except ValueError as exc:
            raise ValueError("status is not a global user status") from exc
        if selected_status not in GLOBAL_USER_STATUSES:
            raise ValueError("global job status cannot be new")
        if application_resume_filename is not None and resume_id is None:
            raise ValueError("application resume filename requires a resume id")
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
                candidate.application_resume_id = previous.application_resume_id
                candidate.application_resume_filename = (
                    previous.application_resume_filename
                )
            candidate.user_status = selected_status
            candidate.user_status_updated_at = (
                previous.user_status_updated_at
                if previous is not None and previous.user_status is selected_status
                else updated_at
            )
            candidate.global_status_deleted_at = None
            if resume_id is not None and profile_hash is not None:
                candidate.last_evaluated_resume_id = resume_id
            merged, _changed = _merge_into(jobs, candidate)
            if resume_id is not None and profile_hash is not None:
                _replace_evaluation(merged, candidate)
                merged.last_evaluated_resume_id = resume_id
            if application_resume_filename is not None:
                merged.application_resume_id = resume_id
                merged.application_resume_filename = application_resume_filename
            if previous is None:
                merged.user_status_history = []
                _record_status(merged, UserStatus.SAVED, updated_at)
            _record_status(merged, selected_status, updated_at)
            merged.global_status_deleted_at = None
            return _visible_snapshot(
                self._persist_unlocked(current, jobs, deletions=deletions)
            )

    def set_application_resume(
        self,
        job: JobRecord,
        resume_id: str | None,
        filename: str | None = None,
    ) -> JobRecord:
        """Replace or clear the resume attached to one tracked job."""
        with self._lock.exclusive():
            current = self._load_and_migrate_unlocked()
            jobs = [item.model_copy(deep=True) for item in current.jobs]
            matches = _matching_indices(jobs, job)
            if not matches:
                raise KeyError(job.canonical_job_key)
            merged = _merge_jobs([jobs[index] for index in matches])
            merged.application_resume_id = resume_id
            merged.application_resume_filename = filename
            first = matches[0]
            jobs[first] = merged
            for index in reversed(matches[1:]):
                del jobs[index]
            return self._persist_unlocked(current, jobs).jobs[first].model_copy(
                deep=True
            )

    def record_reevaluation_result(
        self,
        key: str,
        result: Literal["succeeded", "failed"],
        *,
        finished_at: datetime | None = None,
    ) -> JobRecord:
        """Persist one terminal re-evaluation result until the user acknowledges it."""
        notice = ReevaluationNotice(
            status=result,
            finished_at=finished_at if finished_at is not None else datetime.now(UTC),
        )
        with self._lock.exclusive():
            current = self._load_and_migrate_unlocked()
            jobs = [item.model_copy(deep=True) for item in current.jobs]
            index = next(
                (
                    index
                    for index, item in enumerate(jobs)
                    if item.canonical_job_key == key
                ),
                None,
            )
            if index is None:
                raise KeyError(key)
            previous = jobs[index].model_copy(deep=True)
            _set_reevaluation_notice(jobs[index], notice)
            if jobs[index] == previous:
                return previous
            return self._persist_unlocked(current, jobs).jobs[index].model_copy(
                deep=True
            )

    def acknowledge_reevaluation_result(
        self,
        key: str,
        finished_at: datetime,
    ) -> JobRecord:
        """Clear only the re-evaluation result the user actually opened."""
        acknowledged_at = _utc_timestamp(finished_at)
        with self._lock.exclusive():
            current = self._load_and_migrate_unlocked()
            jobs = [item.model_copy(deep=True) for item in current.jobs]
            index = next(
                (
                    index
                    for index, item in enumerate(jobs)
                    if item.canonical_job_key == key
                ),
                None,
            )
            if index is None:
                raise KeyError(key)
            notice = jobs[index].reevaluation_notice
            if notice is None or notice.finished_at != acknowledged_at:
                return jobs[index].model_copy(deep=True)
            previous_acknowledgement = jobs[index].reevaluation_acknowledged_at
            jobs[index].reevaluation_acknowledged_at = max(
                acknowledged_at,
                previous_acknowledgement or acknowledged_at,
            )
            jobs[index].reevaluation_notice = None
            return self._persist_unlocked(current, jobs).jobs[index].model_copy(
                deep=True
            )

    def set_status_date(
        self,
        job: JobRecord,
        event_index: int,
        changed_on: date,
    ) -> JobRecord:
        """Replace the calendar date of one persisted status event."""
        with self._lock.exclusive():
            current = self._load_and_migrate_unlocked()
            jobs = [item.model_copy(deep=True) for item in current.jobs]
            matches = _matching_indices(jobs, job)
            if not matches:
                raise KeyError(job.canonical_job_key)
            merged = _merge_jobs([jobs[index] for index in matches])
            if event_index < 0 or event_index >= len(merged.user_status_history):
                raise IndexError(event_index)
            if (
                event_index > 0
                and changed_on
                < merged.user_status_history[event_index - 1].changed_at.date()
            ) or (
                event_index < len(merged.user_status_history) - 1
                and changed_on
                > merged.user_status_history[event_index + 1].changed_at.date()
            ):
                raise ValueError(
                    "Lifecycle dates must stay between adjacent lifecycle events."
                )
            previous = merged.user_status_history[event_index]
            changed_at = previous.changed_at.replace(
                year=changed_on.year,
                month=changed_on.month,
                day=changed_on.day,
            )
            merged.user_status_history[event_index] = UserStatusHistoryEntry(
                status=previous.status,
                changed_at=changed_at,
            )
            if event_index == len(merged.user_status_history) - 1:
                merged.user_status_updated_at = changed_at
            first = matches[0]
            jobs[first] = merged
            for index in reversed(matches[1:]):
                del jobs[index]
            return self._persist_unlocked(current, jobs).jobs[first].model_copy(
                deep=True
            )

    def delete_status_event(self, job: JobRecord, event_index: int) -> JobRecord:
        """Delete one persisted status event while preserving the Saved start."""
        with self._lock.exclusive():
            current = self._load_and_migrate_unlocked()
            jobs = [item.model_copy(deep=True) for item in current.jobs]
            matches = _matching_indices(jobs, job)
            if not matches:
                raise KeyError(job.canonical_job_key)
            merged = _merge_jobs([jobs[index] for index in matches])
            if event_index < 0 or event_index >= len(merged.user_status_history):
                raise IndexError(event_index)
            if merged.user_status_history[event_index].status is UserStatus.SAVED:
                raise ValueError("The Saved lifecycle event cannot be deleted.")
            del merged.user_status_history[event_index]
            current_event = merged.user_status_history[-1]
            merged.user_status = current_event.status
            merged.user_status_updated_at = current_event.changed_at
            first = matches[0]
            jobs[first] = merged
            for index in reversed(matches[1:]):
                del jobs[index]
            return self._persist_unlocked(current, jobs).jobs[first].model_copy(
                deep=True
            )

    def set_salaries(
        self,
        job: JobRecord,
        *,
        expected_salary: SalaryValue | None,
        offer_salary: SalaryValue | None,
    ) -> JobRecord:
        """Replace the user-entered salary values for one tracked job."""
        with self._lock.exclusive():
            current = self._load_and_migrate_unlocked()
            jobs = [item.model_copy(deep=True) for item in current.jobs]
            matches = _matching_indices(jobs, job)
            if not matches:
                raise KeyError(job.canonical_job_key)
            merged = _merge_jobs([jobs[index] for index in matches])
            merged.expected_salary = (
                expected_salary.model_copy(deep=True)
                if expected_salary is not None
                else None
            )
            merged.offer_salary = (
                offer_salary.model_copy(deep=True)
                if offer_salary is not None
                else None
            )
            first = matches[0]
            jobs[first] = merged
            for index in reversed(matches[1:]):
                del jobs[index]
            return self._persist_unlocked(current, jobs).jobs[first].model_copy(
                deep=True
            )

    def add_note(
        self,
        job: JobRecord,
        content: str,
        now: datetime | None = None,
        *,
        note_id: UUID | None = None,
    ) -> JobNote:
        """Append one dated note to a tracked job."""
        note = JobNote(
            id=note_id or uuid4(),
            content=content,
            created_at=_utc_timestamp(now if now is not None else datetime.now(UTC)),
        )
        with self._lock.exclusive():
            current = self._load_and_migrate_unlocked()
            jobs = [item.model_copy(deep=True) for item in current.jobs]
            matches = _matching_indices(jobs, job)
            if not matches:
                raise KeyError(job.canonical_job_key)
            merged = _merge_jobs([jobs[index] for index in matches])
            merged.notes = [*merged.notes, note]
            first = matches[0]
            jobs[first] = merged
            for index in reversed(matches[1:]):
                del jobs[index]
            self._persist_unlocked(current, jobs)
        return note.model_copy(deep=True)

    def edit_note(self, job: JobRecord, note_id: UUID, content: str) -> JobNote:
        """Replace one tracked job note while preserving its date and identity."""
        with self._lock.exclusive():
            current = self._load_and_migrate_unlocked()
            jobs = [item.model_copy(deep=True) for item in current.jobs]
            matches = _matching_indices(jobs, job)
            if not matches:
                raise KeyError(job.canonical_job_key)
            merged = _merge_jobs([jobs[index] for index in matches])
            note_index = next(
                (index for index, note in enumerate(merged.notes) if note.id == note_id),
                None,
            )
            if note_index is None:
                raise KeyError(note_id)
            previous = merged.notes[note_index]
            updated = JobNote(
                id=previous.id,
                content=content,
                created_at=previous.created_at,
            )
            merged.notes[note_index] = updated
            first = matches[0]
            jobs[first] = merged
            for index in reversed(matches[1:]):
                del jobs[index]
            self._persist_unlocked(current, jobs)
        return updated.model_copy(deep=True)

    def delete_note(self, job: JobRecord, note_id: UUID) -> None:
        """Delete one note from a tracked job."""
        with self._lock.exclusive():
            current = self._load_and_migrate_unlocked()
            jobs = [item.model_copy(deep=True) for item in current.jobs]
            matches = _matching_indices(jobs, job)
            if not matches:
                raise KeyError(job.canonical_job_key)
            merged = _merge_jobs([jobs[index] for index in matches])
            note_index = next(
                (index for index, note in enumerate(merged.notes) if note.id == note_id),
                None,
            )
            if note_index is None:
                raise KeyError(note_id)
            del merged.notes[note_index]
            first = matches[0]
            jobs[first] = merged
            for index in reversed(matches[1:]):
                del jobs[index]
            self._persist_unlocked(current, jobs)

    def set_manual_fact(
        self,
        job: JobRecord,
        field_name: str,
        value: date | int | str,
    ) -> JobRecord:
        """Fill one missing Job Tracker fact with a user-entered value."""
        with self._lock.exclusive():
            current = self._load_and_migrate_unlocked()
            jobs = [item.model_copy(deep=True) for item in current.jobs]
            matches = _matching_indices(jobs, job)
            if not matches:
                raise KeyError(job.canonical_job_key)
            merged = _merge_jobs([jobs[index] for index in matches])
            if field_name == "posted_at":
                if merged.manual_posted_at is not None or merged.posted_at is not None:
                    raise ValueError("Posted is already known.")
                if not isinstance(value, date) or isinstance(value, datetime):
                    raise ValueError("Posted must be a date.")
                merged.manual_posted_at = value
            elif field_name == "company_size":
                known_size = merged.company_size is not None and (
                    merged.company_size.band.value != "unknown"
                    or merged.company_size.employee_count is not None
                    or merged.company_size.reported_size is not None
                    or merged.company_size.minimum_employees is not None
                )
                if merged.manual_company_size is not None or known_size:
                    raise ValueError("Company size is already known.")
                if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                    raise ValueError("Company size must be a positive whole number.")
                merged.manual_company_size = value
            elif field_name == "company_industry":
                if (
                    merged.manual_company_industry is not None
                    or merged.company_industry is not None
                ):
                    raise ValueError("Company industry is already known.")
                if not isinstance(value, str) or not value.strip():
                    raise ValueError("Company industry cannot be empty.")
                merged.manual_company_industry = value.strip()
            else:
                raise ValueError("Unsupported manual fact.")
            first = matches[0]
            jobs[first] = merged
            for index in reversed(matches[1:]):
                del jobs[index]
            return self._persist_unlocked(current, jobs).jobs[first].model_copy(
                deep=True
            )

    def upsert_with_default_status(
        self,
        job: JobRecord,
        default_status: UserStatus,
        now: datetime | None = None,
        *,
        resume_id: str | None = None,
        profile_hash: str | None = None,
        application_resume_filename: str | None = None,
        expected_job: JobRecord | None = None,
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
                candidate.last_evaluated_resume_id = resume_id
            existing_status = _newest_status_job(_matching_jobs(jobs, candidate))
            if (
                expected_job is not None
                and _evaluation_state(existing_status) != _evaluation_state(expected_job)
            ):
                raise GlobalJobChanged(
                    "This job changed while this task was running. Run it again."
                )
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
                candidate.application_resume_id = existing_status.application_resume_id
                candidate.application_resume_filename = (
                    existing_status.application_resume_filename
                )
            if application_resume_filename is not None:
                candidate.application_resume_id = resume_id
                candidate.application_resume_filename = application_resume_filename
            merged, _changed = _merge_into(jobs, candidate)
            if resume_id is not None and profile_hash is not None:
                _replace_evaluation(merged, candidate)
                merged.last_evaluated_resume_id = resume_id
            if application_resume_filename is not None:
                merged.application_resume_id = resume_id
                merged.application_resume_filename = application_resume_filename
            _record_status(
                merged,
                candidate.user_status,
                candidate.user_status_updated_at,
            )
            merged.global_status_deleted_at = None
            if jobs != current.jobs or restored:
                self._persist_unlocked(current, jobs, deletions=deletions)
            return merged.model_copy(deep=True)

    def save_reevaluation(
        self,
        key: str,
        evaluated: JobRecord,
        *,
        resume_id: str,
        expected_job: JobRecord | None = None,
    ) -> JobRecord:
        """Replace one tracked job's AI result while preserving user-owned data."""
        with self._lock.exclusive():
            current = self._load_and_migrate_unlocked()
            jobs = [item.model_copy(deep=True) for item in current.jobs]
            index = next(
                (
                    index
                    for index, item in enumerate(jobs)
                    if item.canonical_job_key == key
                ),
                None,
            )
            if index is None:
                raise KeyError(key)
            existing = jobs[index]
            if (
                expected_job is not None
                and _evaluation_state(existing) != _evaluation_state(expected_job)
            ):
                raise GlobalJobChanged(
                    "This job changed while this task was running. Run it again."
                )
            candidate = evaluated.model_copy(
                update={
                    "canonical_job_key": existing.canonical_job_key,
                    "primary_source_occurrence_key": (
                        existing.primary_source_occurrence_key
                    ),
                },
                deep=True,
            )
            existing_occurrences = [
                occurrence.model_copy(deep=True)
                for occurrence in existing.source_occurrences
            ]
            existing_primary_index = next(
                (
                    index
                    for index, occurrence in enumerate(existing_occurrences)
                    if occurrence.source_occurrence_key
                    == existing.primary_source_occurrence_key
                ),
                None,
            )
            evaluated_primary = next(
                (
                    occurrence
                    for occurrence in evaluated.source_occurrences
                    if occurrence.source_occurrence_key
                    == evaluated.primary_source_occurrence_key
                ),
                None,
            )
            if existing_primary_index is not None and evaluated_primary is not None:
                existing_occurrences[existing_primary_index] = existing_occurrences[
                    existing_primary_index
                ].model_copy(
                    update={
                        "url": evaluated_primary.url,
                        "company": evaluated_primary.company,
                        "title": evaluated_primary.title,
                        "location": evaluated_primary.location,
                        "description": evaluated_primary.description,
                        "posted_at": evaluated_primary.posted_at,
                        "content_hash": evaluated_primary.content_hash,
                        "availability_status": evaluated_primary.availability_status,
                        "detail_complete": evaluated_primary.detail_complete,
                        "last_fetch_error_code": evaluated_primary.last_fetch_error_code,
                        "job_snapshot": evaluated_primary.job_snapshot,
                        "job_snapshot_error_code": (
                            evaluated_primary.job_snapshot_error_code
                        ),
                        "closed_at": evaluated_primary.closed_at,
                    },
                    deep=True,
                )
            candidate.source_occurrences = existing_occurrences
            candidate.last_evaluated_resume_id = resume_id
            merged = _merge_jobs((existing, candidate))
            merged.source_occurrences = [
                occurrence.model_copy(deep=True)
                for occurrence in candidate.source_occurrences
            ]
            merged.primary_source_occurrence_key = (
                existing.primary_source_occurrence_key
            )
            _replace_evaluation(merged, candidate)
            merged.last_evaluated_resume_id = resume_id
            _set_reevaluation_notice(
                merged,
                ReevaluationNotice(
                    status="succeeded",
                    finished_at=datetime.now(UTC),
                ),
            )
            if existing.company_size is not None:
                merged.company_size = existing.company_size.model_copy(deep=True)
            jobs[index] = merged
            return self._persist_unlocked(current, jobs).jobs[index].model_copy(
                deep=True
            )

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
        contents = self._paths.global_jobs_jsonl.read_bytes()
        loaded = parse_snapshot(contents)
        deletions = [
            item.model_copy(deep=True) for item in loaded.meta.global_job_deletions
        ]
        jobs: list[JobRecord] = []
        migration_needed = b'"resume_matches"' in contents
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


def filter_untracked_jobs(
    review_snapshot: Snapshot,
    tracker_snapshot: Snapshot,
) -> Snapshot:
    """Return a review copy without jobs already present in Job Tracker."""
    result = review_snapshot.model_copy(deep=True)
    result.jobs = filter_untracked_job_records(result.jobs, tracker_snapshot)
    return result


def filter_untracked_job_records(
    review_jobs: Sequence[JobRecord],
    tracker_snapshot: Snapshot,
) -> list[JobRecord]:
    """Return Review job records that do not exist in Job Tracker."""
    return [
        job
        for job in review_jobs
        if not _matching_jobs(tracker_snapshot.jobs, job)
    ]


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
        profile.user_status_history = [
            entry.model_copy(deep=True)
            for entry in status_job.user_status_history
        ]
        _record_status(
            profile,
            status_job.user_status,
            status_job.user_status_updated_at,
        )
    else:
        profile.user_status_history = []
    application_job = max(
        (job for job in candidates if job.application_resume_id is not None),
        key=lambda job: (
            job.user_status_updated_at,
            job.last_seen,
            job.canonical_job_key,
        ),
        default=None,
    )
    profile.application_resume_id = (
        application_job.application_resume_id
        if application_job is not None
        else None
    )
    profile.application_resume_filename = (
        application_job.application_resume_filename
        if application_job is not None
        else None
    )
    acknowledgement_times = [
        job.reevaluation_acknowledged_at
        for job in candidates
        if job.reevaluation_acknowledged_at is not None
    ]
    profile.reevaluation_acknowledged_at = max(
        acknowledgement_times,
        default=None,
    )
    notice_job = max(
        (
            job
            for job in candidates
            if job.reevaluation_notice is not None
            and (
                profile.reevaluation_acknowledged_at is None
                or job.reevaluation_notice.finished_at
                > profile.reevaluation_acknowledged_at
            )
        ),
        key=lambda job: (
            job.reevaluation_notice.finished_at
            if job.reevaluation_notice is not None
            else datetime.min.replace(tzinfo=UTC)
        ),
        default=None,
    )
    profile.reevaluation_notice = (
        notice_job.reevaluation_notice.model_copy(deep=True)
        if notice_job is not None and notice_job.reevaluation_notice is not None
        else None
    )
    salary_job = max(
        (
            job
            for job in candidates
            if job.expected_salary is not None or job.offer_salary is not None
        ),
        key=lambda job: (
            job.user_status_updated_at,
            job.last_seen,
            job.canonical_job_key,
        ),
        default=None,
    )
    profile.expected_salary = (
        salary_job.expected_salary.model_copy(deep=True)
        if salary_job is not None and salary_job.expected_salary is not None
        else None
    )
    profile.offer_salary = (
        salary_job.offer_salary.model_copy(deep=True)
        if salary_job is not None and salary_job.offer_salary is not None
        else None
    )
    notes: dict[UUID, JobNote] = {}
    for candidate in candidates:
        for note in candidate.notes:
            notes.setdefault(note.id, note.model_copy(deep=True))
    profile.notes = sorted(
        notes.values(),
        key=lambda note: (note.created_at, str(note.id)),
    )
    for field_name in (
        "manual_posted_at",
        "manual_company_size",
        "manual_company_industry",
    ):
        fact_job = max(
            (job for job in candidates if getattr(job, field_name) is not None),
            key=lambda job: (
                job.user_status_updated_at,
                job.last_seen,
                job.canonical_job_key,
            ),
            default=None,
        )
        setattr(
            profile,
            field_name,
            getattr(fact_job, field_name) if fact_job is not None else None,
        )
    profile.source_occurrences = _merged_occurrences(candidates)
    return profile


def _set_reevaluation_notice(job: JobRecord, notice: ReevaluationNotice) -> None:
    acknowledged_at = job.reevaluation_acknowledged_at
    if acknowledged_at is not None and notice.finished_at <= acknowledged_at:
        return
    current_notice = job.reevaluation_notice
    if current_notice is not None and notice.finished_at < current_notice.finished_at:
        return
    job.reevaluation_notice = notice.model_copy(deep=True)


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


def _merged_occurrences(candidates: Sequence[JobRecord]) -> list[SourceOccurrence]:
    occurrences: dict[str, SourceOccurrence] = {}
    for candidate in candidates:
        for occurrence in candidate.source_occurrences:
            occurrences.setdefault(
                occurrence.source_occurrence_key,
                occurrence.model_copy(deep=True),
            )
    return list(occurrences.values())


def _replace_evaluation(target: JobRecord, source: JobRecord) -> None:
    """Copy one review result and its status fields as an atomic bundle."""
    source_copy = source.model_copy(deep=True)
    for field_name in _EVALUATION_FIELDS:
        setattr(target, field_name, getattr(source_copy, field_name))


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
