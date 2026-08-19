from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path

from job_scan.ats_models import AtsCheckBundle, AtsHistoryEntry
from job_scan.locking import FileRWLock
from job_scan.paths import AppPaths
from job_scan.search_history import _SAFE_RUN_ID, _fsync_directory, _write_bytes


class AtsHistoryStore:
    """Persist each completed ATS check as an independent local bundle."""

    def __init__(self, paths: AppPaths) -> None:
        self._paths = paths
        self._lock = FileRWLock(paths.ats_history_lock_file)
        paths.ensure_directories()

    def archive(
        self,
        bundle: AtsCheckBundle,
        resume_bytes: bytes,
    ) -> AtsHistoryEntry:
        """Atomically publish one self-contained completed ATS bundle."""
        run_id = _validated_run_id(bundle.run_id)
        entry = AtsHistoryEntry(
            run_id=run_id,
            search_run_id=bundle.search_run_id,
            candidate_name=bundle.candidate_name,
            resume_filename=Path(bundle.resume_filename).name,
            finished_at=bundle.finished_at,
            readiness_score=bundle.resume.readiness_score,
            job_count=len(bundle.jobs),
            failed_job_count=bundle.failed_job_count,
        )
        with self._lock.exclusive():
            destination = self._run_dir(run_id)
            if destination.exists():
                raise ValueError("ATS history run already exists")
            temporary = Path(
                tempfile.mkdtemp(
                    dir=self._paths.ats_history_dir,
                    prefix=".ats-history.",
                )
            )
            published = False
            try:
                _write_bytes(
                    temporary / "manifest.json",
                    entry.model_dump_json(indent=2).encode("utf-8") + b"\n",
                )
                _write_bytes(
                    temporary / "result.json",
                    bundle.model_dump_json(indent=2).encode("utf-8") + b"\n",
                )
                _write_bytes(temporary / "resume", resume_bytes)
                os.replace(temporary, destination)
                published = True
                _fsync_directory(self._paths.ats_history_dir)
            except BaseException:
                if published and destination.exists():
                    shutil.rmtree(destination)
                    _fsync_directory(self._paths.ats_history_dir)
                raise
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        return entry

    def list(self) -> list[AtsHistoryEntry]:
        """Return completed ATS checks newest first."""
        with self._lock.shared():
            entries = [
                self._read_entry(path)
                for path in self._paths.ats_history_dir.iterdir()
                if path.is_dir() and not path.name.startswith(".")
            ]
        return sorted(
            entries,
            key=lambda entry: (entry.finished_at, entry.run_id),
            reverse=True,
        )

    def load(self, run_id: str) -> AtsCheckBundle:
        """Load one immutable ATS result bundle."""
        run_id = _validated_run_id(run_id)
        with self._lock.shared():
            run_dir = self._run_dir(run_id)
            if not run_dir.is_dir():
                raise KeyError(run_id)
            try:
                raw: object = json.loads((run_dir / "result.json").read_bytes())
                bundle = AtsCheckBundle.model_validate(raw)
                if bundle.run_id != run_id:
                    raise ValueError("ATS result run id does not match directory")
                return bundle
            except (OSError, ValueError):
                raise ValueError(f"invalid ATS history bundle: {run_id}") from None

    def read_resume(self, run_id: str) -> tuple[str, bytes]:
        """Return the original upload name and archived resume bytes."""
        run_id = _validated_run_id(run_id)
        with self._lock.shared():
            run_dir = self._run_dir(run_id)
            if not run_dir.is_dir():
                raise KeyError(run_id)
            entry = self._read_entry(run_dir)
            try:
                return entry.resume_filename, (run_dir / "resume").read_bytes()
            except OSError:
                raise ValueError(f"invalid ATS history bundle: {run_id}") from None

    def delete(self, run_id: str) -> None:
        """Delete only the requested ATS bundle through an atomic tombstone rename."""
        run_id = _validated_run_id(run_id)
        with self._lock.exclusive():
            target = self._run_dir(run_id)
            if not target.is_dir():
                raise KeyError(run_id)
            tombstone = (
                self._paths.ats_history_dir
                / f".deleted.{run_id}.{uuid.uuid4().hex}"
            )
            os.replace(target, tombstone)
            _fsync_directory(self._paths.ats_history_dir)
            try:
                shutil.rmtree(tombstone)
            except OSError:
                # Atomic rename already hid the owned data. A hidden cleanup
                # remnant is safer than exposing a half-deleted record.
                pass

    def _read_entry(self, run_dir: Path) -> AtsHistoryEntry:
        try:
            raw: object = json.loads((run_dir / "manifest.json").read_bytes())
            entry = AtsHistoryEntry.model_validate(raw)
            if entry.run_id != run_dir.name:
                raise ValueError("ATS manifest run id does not match directory")
            return entry
        except (OSError, ValueError):
            raise ValueError(f"invalid ATS history bundle: {run_dir.name}") from None

    def _run_dir(self, run_id: str) -> Path:
        return self._paths.ats_history_dir / run_id


def _validated_run_id(run_id: str) -> str:
    if _SAFE_RUN_ID.fullmatch(run_id) is None:
        raise ValueError("invalid ATS history run id")
    return run_id
