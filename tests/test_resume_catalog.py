from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from job_scan.paths import AppPaths
from job_scan.resume_catalog import ResumeCatalogStore

NOW = datetime(2026, 8, 19, 10, 0, tzinfo=UTC)
RESUME_ID = "sha256:" + "a" * 64
PROFILE_HASH = "sha256:" + "b" * 64


def test_resume_bundle_keeps_one_resume_and_updates_its_active_profile(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    store = ResumeCatalogStore(paths)

    created = store.register(
        resume_id=RESUME_ID,
        profile_hash=PROFILE_HASH,
        candidate_name="Backend CV",
        filename="backend.pdf",
        profile_bytes=b"# Backend profile\n",
        config_bytes=b'candidate_name = "Backend CV"\n',
        resume_bytes=b"PDF bytes",
        created_at=NOW,
    )
    duplicate = store.register(
        resume_id=RESUME_ID,
        profile_hash="sha256:" + "c" * 64,
        candidate_name="Renamed CV",
        filename="renamed.pdf",
        profile_bytes=b"different profile",
        config_bytes=b"different config",
        resume_bytes=b"PDF bytes",
        created_at=NOW,
    )
    store.register(
        resume_id=RESUME_ID,
        profile_hash=PROFILE_HASH,
        candidate_name="Older CV",
        filename="older.pdf",
        profile_bytes=b"older profile",
        config_bytes=b"older config",
        resume_bytes=b"PDF bytes",
        created_at=NOW - timedelta(days=1),
    )

    reloaded = ResumeCatalogStore(paths)
    bundle = reloaded.read(RESUME_ID)

    assert duplicate.resume_id == created.resume_id
    assert duplicate.profile_hash == "sha256:" + "c" * 64
    assert duplicate.profile_hashes == [PROFILE_HASH, "sha256:" + "c" * 64]
    assert reloaded.list() == [duplicate]
    assert bundle.entry.candidate_name == "Renamed CV"
    assert bundle.entry.filename == "renamed.pdf"
    assert bundle.profile_bytes == b"different profile"
    assert bundle.config_bytes == b"different config"
    assert bundle.resume_bytes == b"PDF bytes"
