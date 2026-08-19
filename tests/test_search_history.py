from __future__ import annotations

import os
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from job_scan.dashboard.render import render_dashboard
from job_scan.domain import (
    AvailabilityStatus,
    JobRecord,
    MachineStatus,
    Snapshot,
    StoreMeta,
    UserStatus,
)
from job_scan.global_jobs import GlobalJobStore
from job_scan.locking import FileRWLock
from job_scan.paths import AppPaths
from job_scan.repository import JsonlRepository
from job_scan.review_server import create_review_app
from job_scan.search_history import SearchHistoryStore


def _snapshot(key: str, *, recommended: bool = False) -> Snapshot:
    now = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
    return Snapshot(
        meta=StoreMeta(data_revision=1, generated_at=now),
        jobs=[
            JobRecord(
                canonical_job_key=key,
                primary_source_occurrence_key=f"glassdoor:{key}:1",
                company="Example GmbH",
                title="Backend Engineer",
                location="Berlin",
                url=f"https://example.test/jobs/{key}",
                description="Build backend systems.",
                posted_at=date(2026, 8, 7),
                content_hash=f"sha256:{key}",
                first_seen=now,
                last_seen=now,
                availability_status=AvailabilityStatus.ACTIVE,
                machine_status=(
                    MachineStatus.ELIGIBLE if recommended else MachineStatus.PENDING
                ),
                score=90 if recommended else None,
                user_status_updated_at=now,
            )
        ],
    )


def test_archive_is_an_independent_search_bundle(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    resume = tmp_path / "candidate.pdf"
    resume.write_bytes(b"resume bytes")
    paths.profile_md.parent.mkdir(parents=True, exist_ok=True)
    paths.profile_md.write_text("# Profile\n", encoding="utf-8")
    paths.config_toml.write_text("country = \"DE\"\n", encoding="utf-8")
    store = SearchHistoryStore(paths)

    entry = store.archive(
        run_id="run-1",
        candidate_name="Ada Lovelace",
        resume_filename="Ada CV.pdf",
        resume_path=resume,
        snapshot=_snapshot("new", recommended=True),
        finished_at=datetime(2026, 8, 7, 12, 0, tzinfo=UTC),
    )

    assert entry.candidate_name == "Ada Lovelace"
    assert entry.job_count == 1
    assert entry.recommended_count == 1
    assert store.list() == [entry]
    assert store.load("run-1").jobs[0].canonical_job_key == "new"
    download_name, download_bytes = store.read_resume("run-1")
    assert download_name == "Ada CV.pdf"
    assert download_bytes == b"resume bytes"

    resume.write_bytes(b"changed")
    paths.profile_md.write_text("changed", encoding="utf-8")
    assert store.read_resume("run-1")[1] == b"resume bytes"
    assert store.load("run-1").jobs[0].canonical_job_key == "new"


def test_read_ats_input_returns_one_archived_resume_snapshot_and_config(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    store = SearchHistoryStore(paths)
    resume = tmp_path / "candidate.pdf"
    resume.write_bytes(b"original resume")
    paths.profile_md.parent.mkdir(parents=True, exist_ok=True)
    paths.profile_md.write_bytes(b"profile")
    paths.config_toml.write_bytes(b'candidate_name = "Ada"\n')
    store.archive(
        run_id="search-1",
        candidate_name="Ada",
        resume_filename="Ada.pdf",
        resume_path=resume,
        snapshot=_snapshot("job-1", recommended=True),
        finished_at=datetime(2026, 8, 9, tzinfo=UTC),
    )

    source = store.read_ats_input("search-1")

    assert source.entry.run_id == "search-1"
    assert source.snapshot.jobs[0].canonical_job_key == "job-1"
    assert source.resume_bytes == b"original resume"
    assert source.config_bytes == b'candidate_name = "Ada"\n'


def test_delete_reports_whether_the_latest_search_was_deleted(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    resume = tmp_path / "candidate.docx"
    resume.write_bytes(b"resume")
    paths.profile_md.parent.mkdir(parents=True, exist_ok=True)
    paths.profile_md.write_text("profile", encoding="utf-8")
    paths.config_toml.write_text("config", encoding="utf-8")
    store = SearchHistoryStore(paths)
    first = datetime(2026, 8, 7, 10, 0, tzinfo=UTC)
    second = datetime(2026, 8, 7, 11, 0, tzinfo=UTC)
    for run_id, finished_at in (("run-1", first), ("run-2", second)):
        store.archive(
            run_id=run_id,
            candidate_name=run_id,
            resume_filename="candidate.docx",
            resume_path=resume,
            snapshot=_snapshot(run_id),
            finished_at=finished_at,
        )
        run_cache = paths.cache_dir / "runs" / run_id
        run_cache.mkdir(parents=True)
        (run_cache / "response.json").write_text(run_id, encoding="utf-8")
    legacy_cache = paths.cache_dir / "legacy-response.json"
    legacy_cache.write_text("legacy", encoding="utf-8")
    orphan_cache = paths.cache_dir / "runs" / "orphaned-run"
    orphan_cache.mkdir()
    (orphan_cache / "response.json").write_text("orphan", encoding="utf-8")

    assert store.delete("run-1") is False
    assert [entry.run_id for entry in store.list()] == ["run-2"]
    assert not (paths.cache_dir / "runs" / "run-1").exists()
    assert (paths.cache_dir / "runs" / "run-2" / "response.json").read_text(
        encoding="utf-8"
    ) == "run-2"
    assert orphan_cache.is_dir()
    assert legacy_cache.read_text(encoding="utf-8") == "legacy"
    assert store.delete("run-2") is True
    assert store.list() == []
    assert not (paths.cache_dir / "runs" / "run-2").exists()
    assert (orphan_cache / "response.json").read_text(encoding="utf-8") == "orphan"
    assert legacy_cache.read_text(encoding="utf-8") == "legacy"


def test_history_api_downloads_resume_and_deleting_latest_clears_live_results(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    repository = JsonlRepository(paths, FileRWLock(paths.lock_file), render_dashboard)
    repository.mutate(lambda _old: _snapshot("latest", recommended=True))
    resume = tmp_path / "Ada CV.pdf"
    resume.write_bytes(b"resume bytes")
    paths.profile_md.write_text("profile", encoding="utf-8")
    paths.config_toml.write_text("config", encoding="utf-8")
    history = SearchHistoryStore(paths)
    history.archive(
        run_id="run-latest",
        candidate_name="Ada Lovelace",
        resume_filename="Ada CV.pdf",
        resume_path=resume,
        snapshot=repository.load(),
        finished_at=datetime(2026, 8, 7, 12, 0, tzinfo=UTC),
    )
    origin = "http://127.0.0.1:8765"
    app = create_review_app(
        repository,
        "token",
        frozenset({origin}),
        history_store=history,
    )

    with TestClient(app, base_url=origin) as client:
        client.get("/")
        download = client.get("/api/scan-history/run-latest/resume")
        assert download.status_code == 200
        assert download.content == b"resume bytes"
        assert "Ada%20CV.pdf" in download.headers["content-disposition"]

        with FileRWLock(paths.scan_lock_file).exclusive():
            blocked = client.delete(
                "/api/scan-history/run-latest",
                headers={"Origin": origin, "Host": "127.0.0.1:8765"},
            )
        assert blocked.status_code == 409
        assert history.load("run-latest").jobs[0].canonical_job_key == "latest"

        deleted = client.delete(
            "/api/scan-history/run-latest",
            headers={"Origin": origin, "Host": "127.0.0.1:8765"},
        )

    assert deleted.status_code == 200
    assert deleted.json() == {"deleted_latest": True}
    assert repository.load().jobs == []
    assert not paths.config_toml.exists()
    assert not paths.profile_md.exists()
    with pytest.raises(KeyError):
        history.read_resume("run-latest")


def test_latest_delete_restores_history_and_live_files_when_one_rename_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    repository = JsonlRepository(paths, FileRWLock(paths.lock_file), render_dashboard)
    repository.mutate(lambda _old: _snapshot("latest", recommended=True))
    resume = tmp_path / "Ada CV.pdf"
    resume.write_bytes(b"resume bytes")
    paths.profile_md.write_text("profile", encoding="utf-8")
    paths.config_toml.write_text("config", encoding="utf-8")
    history = SearchHistoryStore(paths)
    history.archive(
        run_id="run-latest",
        candidate_name="Ada Lovelace",
        resume_filename="Ada CV.pdf",
        resume_path=resume,
        snapshot=repository.load(),
        finished_at=datetime(2026, 8, 7, 12, 0, tzinfo=UTC),
    )
    run_cache = paths.cache_dir / "runs" / "run-latest"
    run_cache.mkdir(parents=True)
    (run_cache / "response.json").write_text("run cache", encoding="utf-8")
    legacy_cache = paths.cache_dir / "legacy-response.json"
    legacy_cache.write_text("legacy cache", encoding="utf-8")
    expected_jobs = paths.jobs_jsonl.read_bytes()
    expected_dashboard = paths.dashboard_html.read_bytes()
    real_replace = os.replace

    def fail_config_quarantine(source: Path | str, destination: Path | str) -> None:
        if Path(destination).name.startswith(".deleted.config.toml."):
            raise OSError("injected config quarantine failure")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_config_quarantine)
    origin = "http://127.0.0.1:8765"
    app = create_review_app(
        repository,
        "token",
        frozenset({origin}),
        history_store=history,
    )

    with TestClient(app, base_url=origin) as client:
        client.get("/")
        with pytest.raises(OSError, match="injected config quarantine failure"):
            client.delete(
                "/api/scan-history/run-latest",
                headers={"Origin": origin, "Host": "127.0.0.1:8765"},
            )

    assert paths.jobs_jsonl.read_bytes() == expected_jobs
    assert paths.dashboard_html.read_bytes() == expected_dashboard
    assert paths.profile_md.read_text(encoding="utf-8") == "profile"
    assert paths.config_toml.read_text(encoding="utf-8") == "config"
    assert history.load("run-latest").jobs[0].canonical_job_key == "latest"
    assert (run_cache / "response.json").read_text(encoding="utf-8") == "run cache"
    assert legacy_cache.read_text(encoding="utf-8") == "legacy cache"


def test_delete_rollback_attempts_every_restore_after_one_restore_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    resume = tmp_path / "candidate.pdf"
    resume.write_bytes(b"resume")
    paths.profile_md.parent.mkdir(parents=True, exist_ok=True)
    paths.profile_md.write_text("profile", encoding="utf-8")
    paths.config_toml.write_text("config", encoding="utf-8")
    history = SearchHistoryStore(paths)
    history.archive(
        run_id="run-latest",
        candidate_name="Candidate",
        resume_filename="candidate.pdf",
        resume_path=resume,
        snapshot=_snapshot("latest"),
        finished_at=datetime(2026, 8, 7, 12, 0, tzinfo=UTC),
    )
    run_cache = paths.cache_dir / "runs" / "run-latest"
    run_cache.mkdir(parents=True)
    (run_cache / "response.json").write_text("run cache", encoding="utf-8")
    legacy_cache = paths.cache_dir / "legacy-response.json"
    legacy_cache.write_text("legacy cache", encoding="utf-8")
    real_replace = os.replace

    def fail_run_cache_restore(source: Path | str, destination: Path | str) -> None:
        if Path(destination) == run_cache:
            raise OSError("injected run cache restore failure")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_run_cache_restore)

    with (
        pytest.raises(OSError, match="injected run cache restore failure"),
        history.delete_transaction("run-latest"),
    ):
        raise ValueError("related deletion failed")

    assert history.load("run-latest").jobs[0].canonical_job_key == "latest"
    assert not run_cache.exists()
    assert legacy_cache.read_text(encoding="utf-8") == "legacy cache"


def test_deleting_older_history_keeps_live_setup_and_public_cache(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    repository = JsonlRepository(paths, FileRWLock(paths.lock_file), render_dashboard)
    repository.mutate(lambda _old: _snapshot("live", recommended=True))
    resume = tmp_path / "candidate.pdf"
    resume.write_bytes(b"resume")
    paths.profile_md.write_text("profile", encoding="utf-8")
    paths.config_toml.write_text("config", encoding="utf-8")
    cache_sentinel = paths.cache_dir / "company-sizes.json"
    cache_sentinel.write_text("public cache", encoding="utf-8")
    history = SearchHistoryStore(paths)
    for run_id, finished_at in (
        ("run-old", datetime(2026, 8, 7, 10, 0, tzinfo=UTC)),
        ("run-live", datetime(2026, 8, 7, 12, 0, tzinfo=UTC)),
    ):
        history.archive(
            run_id=run_id,
            candidate_name=run_id,
            resume_filename="candidate.pdf",
            resume_path=resume,
            snapshot=_snapshot(run_id),
            finished_at=finished_at,
        )
    origin = "http://127.0.0.1:8765"
    app = create_review_app(
        repository,
        "token",
        frozenset({origin}),
        history_store=history,
    )

    with TestClient(app, base_url=origin) as client:
        client.get("/")
        response = client.delete(
            "/api/scan-history/run-old",
            headers={"Origin": origin, "Host": "127.0.0.1:8765"},
        )

    assert response.status_code == 200
    assert response.json() == {"deleted_latest": False}
    assert repository.load().jobs[0].canonical_job_key == "live"
    assert paths.profile_md.read_text(encoding="utf-8") == "profile"
    assert paths.config_toml.read_text(encoding="utf-8") == "config"
    assert cache_sentinel.read_text(encoding="utf-8") == "public cache"
    assert [entry.run_id for entry in history.list()] == ["run-live"]


def test_historical_job_status_mutation_is_saved_globally(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    repository = JsonlRepository(paths, FileRWLock(paths.lock_file), render_dashboard)
    repository.mutate(lambda _old: _snapshot("live"))
    resume = tmp_path / "candidate.pdf"
    resume.write_bytes(b"resume")
    paths.profile_md.write_text("profile", encoding="utf-8")
    paths.config_toml.write_text("config", encoding="utf-8")
    history = SearchHistoryStore(paths)
    history.archive(
        run_id="run-old",
        candidate_name="Old Candidate",
        resume_filename="candidate.pdf",
        resume_path=resume,
        snapshot=_snapshot("old"),
        finished_at=datetime(2026, 8, 7, 10, 0, tzinfo=UTC),
    )
    origin = "http://127.0.0.1:8765"
    app = create_review_app(
        repository,
        "token",
        frozenset({origin}),
        history_store=history,
    )

    with TestClient(app, base_url=origin) as client:
        client.get("/")
        response = client.post(
            "/api/scan-history/run-old/jobs/old/status",
            json={"status": "applied"},
            headers={"Origin": origin, "Host": "127.0.0.1:8765"},
        )

    assert response.status_code == 204
    assert history.load("run-old").jobs[0].user_status is UserStatus.NEW
    assert repository.load().jobs[0].canonical_job_key == "live"
    assert repository.load().jobs[0].user_status is UserStatus.NEW
    assert GlobalJobStore(paths).find("old").user_status is UserStatus.APPLIED


def test_live_status_does_not_overwrite_an_unrelated_latest_history(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    repository = JsonlRepository(paths, FileRWLock(paths.lock_file), render_dashboard)
    repository.mutate(lambda _old: _snapshot("live"))
    resume = tmp_path / "candidate.pdf"
    resume.write_bytes(b"resume")
    paths.profile_md.write_text("profile", encoding="utf-8")
    paths.config_toml.write_text("config", encoding="utf-8")
    history = SearchHistoryStore(paths)
    history.archive(
        run_id="run-other",
        candidate_name="Other Candidate",
        resume_filename="candidate.pdf",
        resume_path=resume,
        snapshot=_snapshot("other"),
        finished_at=datetime(2026, 8, 7, 10, 0, tzinfo=UTC),
    )
    origin = "http://127.0.0.1:8765"
    app = create_review_app(
        repository,
        "token",
        frozenset({origin}),
        history_store=history,
    )

    with TestClient(app, base_url=origin) as client:
        client.get("/")
        response = client.post(
            "/api/jobs/live/status",
            json={"status": "applied"},
            headers={"Origin": origin, "Host": "127.0.0.1:8765"},
        )

    assert response.status_code == 204
    assert repository.load().jobs[0].user_status is UserStatus.NEW
    archived = history.load("run-other")
    assert archived.jobs[0].canonical_job_key == "other"
    assert archived.jobs[0].user_status is UserStatus.NEW
    assert GlobalJobStore(paths).find("live").user_status is UserStatus.APPLIED


def test_live_status_leaves_matching_history_snapshot_unchanged(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    repository = JsonlRepository(paths, FileRWLock(paths.lock_file), render_dashboard)
    repository.mutate(lambda _old: _snapshot("live"))
    resume = tmp_path / "candidate.pdf"
    resume.write_bytes(b"resume")
    paths.profile_md.write_text("profile", encoding="utf-8")
    paths.config_toml.write_text("config", encoding="utf-8")
    history = SearchHistoryStore(paths)
    history.archive(
        run_id="run-live",
        candidate_name="Current Candidate",
        resume_filename="candidate.pdf",
        resume_path=resume,
        snapshot=repository.load(),
        finished_at=datetime(2026, 8, 7, 10, 0, tzinfo=UTC),
    )
    repository.mutate(lambda current: current)
    assert history.load("run-live").meta != repository.load().meta
    origin = "http://127.0.0.1:8765"
    app = create_review_app(
        repository,
        "token",
        frozenset({origin}),
        history_store=history,
    )

    with TestClient(app, base_url=origin) as client:
        client.get("/")
        response = client.post(
            "/api/jobs/live/status",
            json={"status": "applied"},
            headers={"Origin": origin, "Host": "127.0.0.1:8765"},
        )

    assert response.status_code == 204
    assert repository.load().jobs[0].user_status is UserStatus.NEW
    assert history.load("run-live").jobs[0].user_status is UserStatus.NEW
    assert GlobalJobStore(paths).find("live").user_status is UserStatus.APPLIED


def test_live_status_is_rejected_while_scan_lock_is_held(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    repository = JsonlRepository(paths, FileRWLock(paths.lock_file), render_dashboard)
    repository.mutate(lambda _old: _snapshot("live"))
    origin = "http://127.0.0.1:8765"
    app = create_review_app(repository, "token", frozenset({origin}))

    with TestClient(app, base_url=origin) as client:
        client.get("/")
        with FileRWLock(paths.scan_lock_file).exclusive():
            response = client.post(
                "/api/jobs/live/status",
                json={"status": "applied"},
                headers={"Origin": origin, "Host": "127.0.0.1:8765"},
            )

    assert response.status_code == 409
    assert repository.load().jobs[0].user_status is UserStatus.NEW
