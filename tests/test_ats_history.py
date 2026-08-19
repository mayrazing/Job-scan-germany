from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

import job_scan.ats_history as ats_history_module
from job_scan.ats_history import AtsHistoryStore
from job_scan.ats_models import (
    AtsCheckBundle,
    AtsJobAssessment,
    AtsJobResult,
    AtsResumeAssessment,
    AtsResumeFinding,
)
from job_scan.paths import AppPaths


def ats_bundle(
    run_id: str,
    *,
    finished_at: datetime = datetime(2026, 8, 9, tzinfo=UTC),
) -> AtsCheckBundle:
    job_key = f"job-{run_id}"
    return AtsCheckBundle(
        run_id=run_id,
        search_run_id="search-1",
        candidate_name="Ada",
        resume_filename="Ada CV.pdf",
        started_at=finished_at,
        finished_at=finished_at,
        ai_runtime="api:deepseek",
        ai_model="deepseek-chat",
        resume=AtsResumeAssessment(
            readiness_score=88,
            verdict="ready",
            title="Resume content is ATS ready",
            summary="Core resume content is clear.",
            findings=[
                AtsResumeFinding(
                    label="Text extraction",
                    status="pass",
                    detail="Selectable text was extracted.",
                )
            ],
        ),
        jobs=[
            AtsJobResult(
                job_key=job_key,
                title="Backend Engineer",
                company="Example GmbH",
                location="Berlin",
                url="https://example.test/jobs/1",
                content_hash=f"sha256:{job_key}",
                assessment=AtsJobAssessment(
                    job_key=job_key,
                    match_score=81,
                    match_label="strong",
                    required_skills_score=84,
                    experience_score=82,
                    keyword_score=73,
                    matched=["Python backend delivery"],
                    needs_attention=["Kubernetes is not shown"],
                    suggestions=["Add Kubernetes only if it is real experience."],
                ),
            )
        ],
    )


def test_archive_is_self_contained_and_newest_first(tmp_path: Path) -> None:
    store = AtsHistoryStore(AppPaths.from_root(tmp_path / "home"))
    older = ats_bundle("ats-1", finished_at=datetime(2026, 8, 8, tzinfo=UTC))
    newer = ats_bundle("ats-2", finished_at=datetime(2026, 8, 9, tzinfo=UTC))

    store.archive(older, b"resume one")
    store.archive(newer, b"resume two")

    assert [item.run_id for item in store.list()] == ["ats-2", "ats-1"]
    assert store.load("ats-1") == older
    assert store.read_resume("ats-1") == (older.resume_filename, b"resume one")


def test_delete_removes_only_the_selected_ats_bundle(tmp_path: Path) -> None:
    store = AtsHistoryStore(AppPaths.from_root(tmp_path / "home"))
    store.archive(ats_bundle("ats-1"), b"one")
    store.archive(ats_bundle("ats-2"), b"two")

    store.delete("ats-1")

    assert [item.run_id for item in store.list()] == ["ats-2"]
    assert store.read_resume("ats-2")[1] == b"two"
    with pytest.raises(KeyError):
        store.load("ats-1")


def test_corrupt_manifest_reports_the_invalid_bundle(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    store = AtsHistoryStore(paths)
    store.archive(ats_bundle("ats-1"), b"resume")
    (paths.ats_history_dir / "ats-1" / "manifest.json").write_bytes(b"{")

    with pytest.raises(ValueError, match=r"^invalid ATS history bundle: ats-1$"):
        store.list()


def test_corrupt_result_reports_the_invalid_bundle(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    store = AtsHistoryStore(paths)
    store.archive(ats_bundle("ats-1"), b"resume")
    (paths.ats_history_dir / "ats-1" / "result.json").write_bytes(b"{")

    with pytest.raises(ValueError, match=r"^invalid ATS history bundle: ats-1$"):
        store.load("ats-1")


def test_missing_resume_reports_the_invalid_bundle(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    store = AtsHistoryStore(paths)
    store.archive(ats_bundle("ats-1"), b"resume")
    (paths.ats_history_dir / "ats-1" / "resume").unlink()

    with pytest.raises(ValueError, match=r"^invalid ATS history bundle: ats-1$"):
        store.read_resume("ats-1")


@pytest.mark.parametrize(
    ("run_id", "error_type"),
    [
        ("../ats-keep", ValueError),
        ("ats-missing", KeyError),
    ],
)
def test_invalid_delete_target_does_not_touch_another_bundle(
    tmp_path: Path,
    run_id: str,
    error_type: type[Exception],
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    store = AtsHistoryStore(paths)
    original = ats_bundle("ats-keep")
    store.archive(original, b"keep me")

    with pytest.raises(error_type):
        store.delete(run_id)

    assert store.load("ats-keep") == original
    assert store.read_resume("ats-keep") == ("Ada CV.pdf", b"keep me")


def test_duplicate_archive_preserves_the_existing_bundle(tmp_path: Path) -> None:
    store = AtsHistoryStore(AppPaths.from_root(tmp_path / "home"))
    original = ats_bundle("ats-1")
    store.archive(original, b"original resume")

    with pytest.raises(ValueError, match="ATS history run already exists"):
        store.archive(ats_bundle("ats-1"), b"replacement resume")

    assert store.load("ats-1") == original
    assert store.read_resume("ats-1") == ("Ada CV.pdf", b"original resume")


def test_publish_fsync_failure_removes_the_new_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    store = AtsHistoryStore(paths)
    store.archive(ats_bundle("ats-keep"), b"keep me")
    failed = False

    def fail_first_fsync(path: Path) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("simulated publish fsync failure")

    monkeypatch.setattr(ats_history_module, "_fsync_directory", fail_first_fsync)

    with pytest.raises(OSError, match="simulated publish fsync failure"):
        store.archive(ats_bundle("ats-new"), b"new resume")

    assert [entry.run_id for entry in store.list()] == ["ats-keep"]
    assert not (paths.ats_history_dir / "ats-new").exists()
    assert not any(
        path.is_dir() and path.name.startswith(".ats-history.")
        for path in paths.ats_history_dir.iterdir()
    )
