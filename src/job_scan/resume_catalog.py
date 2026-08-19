from __future__ import annotations

import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from job_scan.locking import FileRWLock
from job_scan.paths import AppPaths

_RESUME_ID = re.compile(r"^sha256:[0-9a-f]{64}$")


class ResumeCatalogEntry(BaseModel):
    """Describe one resume and the candidate profile used to review jobs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    resume_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    profile_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    profile_hashes: list[str] = Field(default_factory=list)
    candidate_name: str = Field(min_length=1, max_length=200)
    filename: str = Field(min_length=1, max_length=255)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must include a timezone")
        return value

    @field_validator("profile_hashes")
    @classmethod
    def validate_profile_hashes(cls, values: list[str]) -> list[str]:
        if any(not _RESUME_ID.fullmatch(value) for value in values):
            raise ValueError("invalid profile hash")
        return list(dict.fromkeys(values))

    @property
    def all_profile_hashes(self) -> tuple[str, ...]:
        """Return every profile version known for this resume."""
        return tuple(dict.fromkeys([*self.profile_hashes, self.profile_hash]))


@dataclass(frozen=True, slots=True)
class ResumeBundle:
    entry: ResumeCatalogEntry
    profile_bytes: bytes
    config_bytes: bytes
    resume_bytes: bytes | None


class ResumeCatalogStore:
    """Persist self-contained resume profiles used by Global Job Status."""

    def __init__(self, paths: AppPaths) -> None:
        self._paths = paths
        self._lock = FileRWLock(paths.resume_catalog_lock_file)
        paths.ensure_directories()

    def register(
        self,
        *,
        resume_id: str,
        profile_hash: str,
        candidate_name: str,
        filename: str,
        profile_bytes: bytes,
        config_bytes: bytes,
        resume_bytes: bytes | None,
        created_at: datetime,
    ) -> ResumeCatalogEntry:
        """Save one immutable resume bundle, deduplicated by resume content hash."""
        entry = ResumeCatalogEntry(
            resume_id=resume_id,
            profile_hash=profile_hash,
            profile_hashes=[profile_hash],
            candidate_name=candidate_name.strip(),
            filename=Path(filename).name.strip(),
            created_at=created_at,
        )
        with self._lock.exclusive():
            destination = self._entry_dir(resume_id)
            if destination.is_dir():
                existing = self._read_entry(destination)
                if (
                    profile_hash in existing.all_profile_hashes
                    and created_at <= existing.created_at
                ):
                    return existing
                existing_resume = destination / "resume"
                existing_resume_bytes = (
                    existing_resume.read_bytes() if existing_resume.is_file() else None
                )
                known_hashes = [*existing.all_profile_hashes, profile_hash]
                if created_at < existing.created_at:
                    entry = existing.model_copy(
                        update={"profile_hashes": known_hashes}
                    )
                    profile_bytes = (destination / "profile.md").read_bytes()
                    config_bytes = (destination / "config.toml").read_bytes()
                    resume_bytes = existing_resume_bytes
                else:
                    entry = entry.model_copy(
                        update={
                            "profile_hashes": known_hashes,
                            "created_at": created_at,
                        }
                    )
                    if resume_bytes is None:
                        resume_bytes = existing_resume_bytes
            temporary = Path(
                tempfile.mkdtemp(
                    dir=self._paths.resume_catalog_dir,
                    prefix=".resume.",
                )
            )
            published = False
            backup: Path | None = None
            try:
                _write_bytes(
                    temporary / "manifest.json",
                    entry.model_dump_json(indent=2).encode("utf-8") + b"\n",
                )
                _write_bytes(temporary / "profile.md", profile_bytes)
                _write_bytes(temporary / "config.toml", config_bytes)
                if resume_bytes is not None:
                    _write_bytes(temporary / "resume", resume_bytes)
                if destination.is_dir():
                    backup = destination.parent / f".replaced.{uuid.uuid4().hex}"
                    os.replace(destination, backup)
                try:
                    os.replace(temporary, destination)
                except BaseException:
                    if backup is not None and backup.exists():
                        os.replace(backup, destination)
                    raise
                published = True
                _fsync_directory(self._paths.resume_catalog_dir)
                if backup is not None:
                    try:
                        shutil.rmtree(backup)
                    except OSError:
                        pass
            finally:
                if not published and temporary.exists():
                    shutil.rmtree(temporary)
        return entry

    def list(self) -> list[ResumeCatalogEntry]:
        """Return saved resumes newest first."""
        with self._lock.shared():
            entries = [
                self._read_entry(path)
                for path in self._paths.resume_catalog_dir.iterdir()
                if path.is_dir() and not path.name.startswith(".")
            ]
        return sorted(
            entries,
            key=lambda item: (item.created_at, item.resume_id),
            reverse=True,
        )

    def read(self, resume_id: str) -> ResumeBundle:
        """Read one saved profile/config pair and its optional source resume."""
        with self._lock.shared():
            directory = self._entry_dir(resume_id)
            if not directory.is_dir():
                raise KeyError(resume_id)
            resume_path = directory / "resume"
            return ResumeBundle(
                entry=self._read_entry(directory),
                profile_bytes=(directory / "profile.md").read_bytes(),
                config_bytes=(directory / "config.toml").read_bytes(),
                resume_bytes=resume_path.read_bytes() if resume_path.is_file() else None,
            )

    def delete(self, resume_id: str) -> None:
        """Delete one catalog bundle created by a failed higher-level operation."""
        with self._lock.exclusive():
            directory = self._entry_dir(resume_id)
            if not directory.is_dir():
                raise KeyError(resume_id)
            shutil.rmtree(directory)
            _fsync_directory(self._paths.resume_catalog_dir)

    def _entry_dir(self, resume_id: str) -> Path:
        if not _RESUME_ID.fullmatch(resume_id):
            raise ValueError("invalid resume id")
        return self._paths.resume_catalog_dir / resume_id.removeprefix("sha256:")

    @staticmethod
    def _read_entry(directory: Path) -> ResumeCatalogEntry:
        return ResumeCatalogEntry.model_validate_json(
            (directory / "manifest.json").read_bytes()
        )


def _write_bytes(path: Path, contents: bytes) -> None:
    with path.open("wb") as output:
        output.write(contents)
        output.flush()
        os.fsync(output.fileno())


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
