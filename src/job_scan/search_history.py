from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from job_scan.dashboard.view_model import build_dashboard
from job_scan.domain import PrimaryView, Snapshot, StoreMeta
from job_scan.locking import FileRWLock
from job_scan.paths import AppPaths
from job_scan.repository import parse_snapshot, serialize_snapshot

_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")


class SearchHistoryEntry(BaseModel):
    """Describe one completed, isolated candidate search."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1, max_length=100)
    candidate_name: str = Field(min_length=1, max_length=200)
    finished_at: datetime
    resume_filename: str = Field(min_length=1, max_length=255)
    job_count: int = Field(ge=0)
    recommended_count: int = Field(ge=0)

    @field_validator("finished_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("finished_at must include a timezone")
        return value


@dataclass(frozen=True, slots=True)
class SearchHistoryAtsInput:
    entry: SearchHistoryEntry
    snapshot: Snapshot
    resume_bytes: bytes
    config_bytes: bytes


@dataclass(frozen=True, slots=True)
class SearchHistoryReviewInput:
    profile_bytes: bytes
    config_bytes: bytes


class SearchHistoryStore:
    """Persist each completed browser search as an independent local bundle."""

    def __init__(self, paths: AppPaths) -> None:
        self._paths = paths
        self._lock = FileRWLock(paths.history_lock_file)
        paths.ensure_directories()

    def archive(
        self,
        *,
        run_id: str,
        candidate_name: str,
        resume_filename: str,
        resume_path: Path,
        snapshot: Snapshot,
        finished_at: datetime,
        profile_bytes: bytes | None = None,
        config_bytes: bytes | None = None,
    ) -> SearchHistoryEntry:
        """Atomically publish one self-contained completed-search bundle."""
        run_id = _validated_run_id(run_id)
        name = candidate_name.strip()
        if not name:
            raise ValueError("candidate name is required")
        original_name = Path(resume_filename).name.strip()
        if not original_name:
            raise ValueError("resume filename is required")
        dashboard = build_dashboard(snapshot)
        entry = SearchHistoryEntry(
            run_id=run_id,
            candidate_name=name,
            finished_at=finished_at,
            resume_filename=original_name,
            job_count=len(snapshot.jobs),
            recommended_count=dashboard.active_groups[PrimaryView.RECOMMENDED].count,
        )
        with self._lock.exclusive():
            destination = self._run_dir(run_id)
            if destination.exists():
                raise ValueError("search history run already exists")
            temporary = Path(
                tempfile.mkdtemp(dir=self._paths.history_dir, prefix=".history.")
            )
            published = False
            try:
                _write_bytes(
                    temporary / "manifest.json",
                    entry.model_dump_json(indent=2).encode("utf-8") + b"\n",
                )
                _write_bytes(temporary / "jobs.jsonl", serialize_snapshot(snapshot))
                _write_bytes(temporary / "resume", resume_path.read_bytes())
                _write_bytes(
                    temporary / "profile.md",
                    profile_bytes
                    if profile_bytes is not None
                    else self._paths.profile_md.read_bytes(),
                )
                _write_bytes(
                    temporary / "config.toml",
                    config_bytes
                    if config_bytes is not None
                    else self._paths.config_toml.read_bytes(),
                )
                os.replace(temporary, destination)
                published = True
                _fsync_directory(self._paths.history_dir)
            except BaseException:
                if published and destination.exists():
                    shutil.rmtree(destination)
                    _fsync_directory(self._paths.history_dir)
                raise
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        return entry

    def list(self) -> list[SearchHistoryEntry]:
        """Return completed searches newest first."""
        with self._lock.shared():
            entries = [
                self._read_entry(path)
                for path in self._paths.history_dir.iterdir()
                if path.is_dir() and not path.name.startswith(".")
            ]
        return sorted(entries, key=lambda entry: (entry.finished_at, entry.run_id), reverse=True)

    def load(self, run_id: str) -> Snapshot:
        """Load the immutable review results for one search."""
        run_id = _validated_run_id(run_id)
        with self._lock.shared():
            path = self._run_dir(run_id) / "jobs.jsonl"
            if not path.is_file():
                raise KeyError(run_id)
            return parse_snapshot(path.read_bytes())

    def read_resume(self, run_id: str) -> tuple[str, bytes]:
        """Return the original upload name and archived resume bytes."""
        run_id = _validated_run_id(run_id)
        with self._lock.shared():
            run_dir = self._run_dir(run_id)
            if not run_dir.is_dir():
                raise KeyError(run_id)
            entry = self._read_entry(run_dir)
            return entry.resume_filename, (run_dir / "resume").read_bytes()

    def read_ats_input(self, run_id: str) -> SearchHistoryAtsInput:
        """Read one archived ATS input from a consistent search bundle."""
        run_id = _validated_run_id(run_id)
        with self._lock.shared():
            run_dir = self._run_dir(run_id)
            if not run_dir.is_dir():
                raise KeyError(run_id)
            return SearchHistoryAtsInput(
                entry=self._read_entry(run_dir),
                snapshot=parse_snapshot((run_dir / "jobs.jsonl").read_bytes()),
                resume_bytes=(run_dir / "resume").read_bytes(),
                config_bytes=(run_dir / "config.toml").read_bytes(),
            )

    def read_review_input(self, run_id: str) -> SearchHistoryReviewInput:
        """Read one archived candidate profile and its matching configuration."""
        run_id = _validated_run_id(run_id)
        with self._lock.shared():
            run_dir = self._run_dir(run_id)
            if not run_dir.is_dir():
                raise KeyError(run_id)
            return SearchHistoryReviewInput(
                profile_bytes=(run_dir / "profile.md").read_bytes(),
                config_bytes=(run_dir / "config.toml").read_bytes(),
            )

    def delete(self, run_id: str) -> bool:
        """Delete one bundle and report whether it was the newest search."""
        with self.delete_transaction(run_id) as was_latest:
            pass
        return was_latest

    @contextmanager
    def delete_transaction(self, run_id: str) -> Iterator[bool]:
        """Hide one bundle, restoring it if a related deletion fails."""
        run_id = _validated_run_id(run_id)
        with self._lock.exclusive():
            entries = [
                self._read_entry(path)
                for path in self._paths.history_dir.iterdir()
                if path.is_dir() and not path.name.startswith(".")
            ]
            ordered = sorted(
                entries,
                key=lambda entry: (entry.finished_at, entry.run_id),
                reverse=True,
            )
            target = self._run_dir(run_id)
            if not target.is_dir():
                raise KeyError(run_id)
            was_latest = bool(ordered and ordered[0].run_id == run_id)
            targets = [target]
            run_cache = self._paths.run_cache_dir(run_id)
            if run_cache.is_dir():
                targets.append(run_cache)
            renamed: list[tuple[Path, Path]] = []
            try:
                for item in targets:
                    tombstone = (
                        item.parent / f".deleted.{run_id}.{uuid.uuid4().hex}"
                    )
                    os.replace(item, tombstone)
                    renamed.append((item, tombstone))
                    _fsync_directory(item.parent)
                yield was_latest
            except BaseException as deletion_error:
                rollback_error: OSError | None = None
                for item, tombstone in reversed(renamed):
                    if tombstone.exists():
                        try:
                            os.replace(tombstone, item)
                            _fsync_directory(item.parent)
                        except OSError as error:
                            if rollback_error is None:
                                rollback_error = error
                if rollback_error is not None:
                    raise rollback_error from deletion_error
                raise
            else:
                for _item, tombstone in renamed:
                    try:
                        if tombstone.is_dir():
                            shutil.rmtree(tombstone)
                        else:
                            tombstone.unlink(missing_ok=True)
                    except OSError:
                        # Atomic renames already hid the owned data. Hidden cleanup
                        # remnants are safer than exposing a half-deleted record.
                        pass

    def is_latest(self, run_id: str) -> bool:
        """Return whether the requested existing bundle is newest."""
        run_id = _validated_run_id(run_id)
        entries = self.list()
        if not any(entry.run_id == run_id for entry in entries):
            raise KeyError(run_id)
        return bool(entries and entries[0].run_id == run_id)

    def mutate(
        self,
        run_id: str,
        mutator: Callable[[Snapshot], Snapshot],
    ) -> Snapshot:
        """Mutate only one historical search result under the history lock."""
        run_id = _validated_run_id(run_id)
        with self._lock.exclusive():
            run_dir = self._run_dir(run_id)
            if not run_dir.is_dir():
                raise KeyError(run_id)
            old = parse_snapshot((run_dir / "jobs.jsonl").read_bytes())
            proposed = mutator(old)
            if not isinstance(proposed, Snapshot):
                raise TypeError("mutator must return Snapshot")
            revisioned = proposed.model_copy(
                update={
                    "meta": StoreMeta(
                        data_revision=old.meta.data_revision + 1,
                        generated_at=datetime.now(UTC),
                    )
                },
                deep=True,
            )
            self._replace_snapshot_unlocked(run_dir, revisioned)
            return revisioned

    def replace_snapshot(self, run_id: str, snapshot: Snapshot) -> None:
        """Replace the stored result for one completed run without changing its identity."""
        run_id = _validated_run_id(run_id)
        with self._lock.exclusive():
            run_dir = self._run_dir(run_id)
            if not run_dir.is_dir():
                raise KeyError(run_id)
            self._replace_snapshot_unlocked(run_dir, snapshot)

    def latest(self) -> SearchHistoryEntry | None:
        """Return the newest completed search, if one exists."""
        entries = self.list()
        return entries[0] if entries else None

    def _read_entry(self, run_dir: Path) -> SearchHistoryEntry:
        try:
            raw: object = json.loads((run_dir / "manifest.json").read_bytes())
            entry = SearchHistoryEntry.model_validate(raw)
            snapshot = parse_snapshot((run_dir / "jobs.jsonl").read_bytes())
            dashboard = build_dashboard(snapshot)
            return entry.model_copy(
                update={
                    "job_count": len(snapshot.jobs),
                    "recommended_count": dashboard.active_groups[
                        PrimaryView.RECOMMENDED
                    ].count,
                }
            )
        except (OSError, ValueError):
            raise ValueError(f"invalid search history bundle: {run_dir.name}") from None

    def _replace_snapshot_unlocked(self, run_dir: Path, snapshot: Snapshot) -> None:
        jobs_temp = _stage_bytes(run_dir / "jobs.jsonl", serialize_snapshot(snapshot))
        try:
            os.replace(jobs_temp, run_dir / "jobs.jsonl")
            _fsync_directory(run_dir)
        finally:
            jobs_temp.unlink(missing_ok=True)

    def _run_dir(self, run_id: str) -> Path:
        return self._paths.history_dir / run_id


def _validated_run_id(run_id: str) -> str:
    if _SAFE_RUN_ID.fullmatch(run_id) is None:
        raise ValueError("invalid search history run id")
    return run_id


def _write_bytes(path: Path, contents: bytes) -> None:
    with path.open("wb") as stream:
        stream.write(contents)
        stream.flush()
        os.fsync(stream.fileno())


def _stage_bytes(destination: Path, contents: bytes) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
