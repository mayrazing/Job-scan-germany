from __future__ import annotations

import hashlib
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
    """Persist one evolving ATS result bundle per resume content hash."""

    def __init__(self, paths: AppPaths) -> None:
        self._paths = paths
        self._lock = FileRWLock(paths.ats_history_lock_file)
        paths.ensure_directories()
        with self._lock.exclusive():
            self._recover_interrupted_updates()

    def archive(
        self,
        bundle: AtsCheckBundle,
        resume_bytes: bytes,
    ) -> AtsHistoryEntry:
        """Atomically publish or update one self-contained resume bundle."""
        with self._lock.exclusive():
            existing_run_id = self._find_run_id_by_resume(bundle.resume_id)
            run_id = existing_run_id or _validated_run_id(bundle.run_id)
            stored_bundle = bundle.model_copy(update={"run_id": run_id})
            entry = AtsHistoryEntry(
                run_id=run_id,
                search_run_id=stored_bundle.search_run_id,
                resume_id=stored_bundle.resume_id,
                candidate_name=stored_bundle.candidate_name,
                resume_filename=Path(stored_bundle.resume_filename).name,
                finished_at=stored_bundle.finished_at,
                readiness_score=stored_bundle.resume.readiness_score,
                job_count=len(stored_bundle.jobs),
                failed_job_count=stored_bundle.failed_job_count,
            )
            destination = self._run_dir(run_id)
            if destination.exists() and existing_run_id is None:
                raise ValueError("ATS history run already exists")
            temporary = Path(
                tempfile.mkdtemp(
                    dir=self._paths.ats_history_dir,
                    prefix=".ats-history.",
                )
            )
            previous = self._paths.ats_history_dir / f".previous.{run_id}.{uuid.uuid4().hex}"
            published = False
            committed = False
            try:
                _write_bytes(
                    temporary / "manifest.json",
                    entry.model_dump_json(indent=2).encode("utf-8") + b"\n",
                )
                _write_bytes(
                    temporary / "result.json",
                    stored_bundle.model_dump_json(indent=2).encode("utf-8") + b"\n",
                )
                _write_bytes(temporary / "resume", resume_bytes)
                if destination.exists():
                    os.replace(destination, previous)
                os.replace(temporary, destination)
                published = True
                _fsync_directory(self._paths.ats_history_dir)
                committed = True
                if previous.exists():
                    try:
                        shutil.rmtree(previous)
                    except OSError:
                        # The new record is already durable. Keep a hidden backup
                        # instead of rolling the committed record back.
                        pass
            except BaseException:
                if not committed:
                    if published and destination.exists():
                        shutil.rmtree(destination)
                    if previous.exists():
                        os.replace(previous, destination)
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
        """Load one ATS result bundle."""
        run_id = _validated_run_id(run_id)
        with self._lock.shared():
            return self._load_bundle(run_id)

    def load_for_resume(self, resume_id: str) -> AtsCheckBundle | None:
        """Load the existing ATS record for one resume content hash."""
        with self._lock.shared():
            run_id = self._find_run_id_by_resume(resume_id)
            return self._load_bundle(run_id) if run_id is not None else None

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
            if isinstance(raw, dict) and "resume_id" not in raw:
                raw["resume_id"] = _resume_id((run_dir / "resume").read_bytes())
            entry = AtsHistoryEntry.model_validate(raw)
            if entry.run_id != run_dir.name:
                raise ValueError("ATS manifest run id does not match directory")
            return entry
        except (OSError, ValueError):
            raise ValueError(f"invalid ATS history bundle: {run_dir.name}") from None

    def _load_bundle(self, run_id: str) -> AtsCheckBundle:
        run_dir = self._run_dir(run_id)
        if not run_dir.is_dir():
            raise KeyError(run_id)
        try:
            raw: object = json.loads((run_dir / "result.json").read_bytes())
            if isinstance(raw, dict) and "resume_id" not in raw:
                raw["resume_id"] = _resume_id((run_dir / "resume").read_bytes())
            bundle = AtsCheckBundle.model_validate(raw)
            if bundle.run_id != run_id:
                raise ValueError("ATS result run id does not match directory")
            return bundle
        except (OSError, ValueError):
            raise ValueError(f"invalid ATS history bundle: {run_id}") from None

    def _run_dir(self, run_id: str) -> Path:
        return self._paths.ats_history_dir / run_id

    def _find_run_id_by_resume(self, resume_id: str) -> str | None:
        """Return the existing record ID for one resume content hash."""
        for run_dir in self._paths.ats_history_dir.iterdir():
            if (
                run_dir.is_dir()
                and not run_dir.name.startswith(".")
                and self._read_entry(run_dir).resume_id == resume_id
            ):
                return run_dir.name
        return None

    def _recover_interrupted_updates(self) -> None:
        """Restore the newest backup when a directory swap stopped halfway."""
        backups_by_run: dict[str, list[Path]] = {}
        prefix = ".previous."
        for path in self._paths.ats_history_dir.iterdir():
            if not path.is_dir() or not path.name.startswith(prefix):
                continue
            run_id, separator, _suffix = path.name[len(prefix) :].rpartition(".")
            if separator and _SAFE_RUN_ID.fullmatch(run_id) is not None:
                backups_by_run.setdefault(run_id, []).append(path)

        recovered = False
        for run_id, backups in backups_by_run.items():
            destination = self._run_dir(run_id)
            if destination.exists():
                continue
            newest = max(backups, key=lambda path: path.stat().st_mtime_ns)
            os.replace(newest, destination)
            recovered = True
        if recovered:
            _fsync_directory(self._paths.ats_history_dir)


def _validated_run_id(run_id: str) -> str:
    if _SAFE_RUN_ID.fullmatch(run_id) is None:
        raise ValueError("invalid ATS history run id")
    return run_id


def _resume_id(resume_bytes: bytes) -> str:
    """Return the catalog-compatible content hash for archived resume bytes."""
    return "sha256:" + hashlib.sha256(resume_bytes).hexdigest()
