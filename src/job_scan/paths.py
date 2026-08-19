from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppPaths:
    root: Path
    config_toml: Path
    ai_config_toml: Path
    ai_selection_toml: Path
    profile_md: Path
    jobs_jsonl: Path
    dashboard_html: Path
    lock_file: Path
    scan_lock_file: Path
    workflow_lock_file: Path
    ai_usage_lock_file: Path
    history_dir: Path
    history_lock_file: Path
    ats_history_dir: Path
    ats_history_lock_file: Path
    global_jobs_jsonl: Path
    global_jobs_lock_file: Path
    resume_catalog_dir: Path
    resume_catalog_lock_file: Path
    cache_dir: Path
    logs_dir: Path

    @classmethod
    def from_root(cls, root: Path) -> AppPaths:
        output_dir = root / "output"
        return cls(
            root=root,
            config_toml=root / "config.toml",
            ai_config_toml=root / "ai-config.toml",
            ai_selection_toml=root / "ai-selection.toml",
            profile_md=root / "profile.md",
            jobs_jsonl=output_dir / "jobs.jsonl",
            dashboard_html=output_dir / "index.html",
            lock_file=output_dir / ".data.lock",
            scan_lock_file=output_dir / ".scan.lock",
            workflow_lock_file=output_dir / ".workflow.lock",
            ai_usage_lock_file=root / ".ai-usage.lock",
            history_dir=root / "history",
            history_lock_file=root / "history" / ".history.lock",
            ats_history_dir=root / "ats-history",
            ats_history_lock_file=root / "ats-history" / ".ats-history.lock",
            global_jobs_jsonl=root / "global-jobs.jsonl",
            global_jobs_lock_file=root / ".global-jobs.lock",
            resume_catalog_dir=root / "global-resumes",
            resume_catalog_lock_file=root / ".global-resumes.lock",
            cache_dir=root / "cache",
            logs_dir=root / "logs",
        )

    @classmethod
    def from_environment(cls, env: Mapping[str, str]) -> AppPaths:
        override = env.get("JOB_SCAN_HOME", "").strip()
        root = Path(override).expanduser() if override else Path.home() / ".job-scan"
        return cls.from_root(root)

    def ensure_directories(self) -> None:
        for directory in (
            self.root,
            self.jobs_jsonl.parent,
            self.history_dir,
            self.ats_history_dir,
            self.resume_catalog_dir,
            self.cache_dir,
            self.logs_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def run_cache_dir(self, run_id: str) -> Path:
        """Return the cache directory owned by one scan run."""
        return self.cache_dir / "runs" / run_id
