from pathlib import Path

from job_scan.paths import AppPaths


def test_job_scan_home_overrides_default(tmp_path: Path) -> None:
    paths = AppPaths.from_environment({"JOB_SCAN_HOME": str(tmp_path)})

    assert paths.root == tmp_path
    assert paths.config_toml == tmp_path / "config.toml"
    assert paths.profile_md == tmp_path / "profile.md"
    assert paths.jobs_jsonl == tmp_path / "output" / "jobs.jsonl"
    assert paths.dashboard_html == tmp_path / "output" / "index.html"
    assert paths.lock_file == tmp_path / "output" / ".data.lock"
    assert paths.scan_lock_file == tmp_path / "output" / ".scan.lock"
    assert paths.workflow_lock_file == tmp_path / "output" / ".workflow.lock"
    assert paths.history_dir == tmp_path / "history"
    assert paths.history_lock_file == tmp_path / "history" / ".history.lock"
    assert paths.cache_dir == tmp_path / "cache"
    assert paths.logs_dir == tmp_path / "logs"


def test_ats_history_paths_are_owned_by_job_scan_home(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path)

    assert paths.ats_history_dir == tmp_path / "ats-history"
    assert paths.ats_history_lock_file == tmp_path / "ats-history" / ".ats-history.lock"
    assert paths.global_jobs_jsonl == tmp_path / "global-jobs.jsonl"
    assert paths.global_jobs_lock_file == tmp_path / ".global-jobs.lock"


def test_empty_job_scan_home_uses_default() -> None:
    paths = AppPaths.from_environment({"JOB_SCAN_HOME": "  "})

    assert paths.root == Path.home() / ".job-scan"


def test_ensure_directories_creates_only_data_directories(tmp_path: Path) -> None:
    paths = AppPaths.from_environment({"JOB_SCAN_HOME": str(tmp_path / "job-scan")})

    paths.ensure_directories()

    assert {entry.relative_to(paths.root) for entry in paths.root.rglob("*")} == {
        Path("output"),
        Path("cache"),
        Path("logs"),
        Path("history"),
        Path("ats-history"),
    }
