from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from job_scan.sources.base import SourceError

if TYPE_CHECKING:
    from job_scan.doctor import DoctorReport
    from job_scan.scan_service import ScanSummary


class RunLogger:
    """Append privacy-bounded operational records to local JSONL files."""

    def __init__(self, logs_dir: Path) -> None:
        self._logs_dir = logs_dir

    def write(self, summary: ScanSummary) -> Path:
        """Append one strictly whitelisted scan summary and return its log path."""
        path = self._logs_dir / "scan.jsonl"
        payload = {
            "run_id": summary.run_id,
            "started_at": _utc_text(summary.started_at),
            "finished_at": _utc_text(summary.finished_at),
            "source_counts": summary.source_counts,
            "source_errors": [_source_error(error) for error in summary.source_errors],
            "new_count": summary.new_count,
            "changed_count": summary.changed_count,
            "reviewed_count": summary.reviewed_count,
            "excluded_count": summary.excluded_count,
            "pending_count": summary.pending_count,
            "claude_model": summary.claude_model,
            "claude_batch_count": summary.claude_batch_count,
            "claude_budget_usd": str(summary.claude_budget_usd),
            "claude_failure_counts": summary.claude_failure_counts,
            "jobs_jsonl": str(summary.jobs_jsonl),
            "dashboard_html": str(summary.dashboard_html),
        }
        _append_json_line(path, payload)
        return path

    def write_doctor(self, report: DoctorReport) -> Path:
        """Append only names and statuses from one explicitly requested doctor run."""
        path = self._logs_dir / "doctor.jsonl"
        payload = {
            "checks": [
                {"name": check.name, "status": check.status}
                for check in report.checks
            ]
        }
        _append_json_line(path, payload)
        return path


def _append_json_line(path: Path, payload: dict[str, Any]) -> None:
    """Append one compact JSON line while enforcing owner-only file access."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as log_file:
            descriptor = -1
            log_file.write(json.dumps(payload, separators=(",", ":"), sort_keys=True))
            log_file.write("\n")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _source_error(error: SourceError) -> dict[str, Any]:
    """Return only program-owned source error fields safe for operational logs."""
    return {
        "category": error.category,
        "host": _normalized_host(error.source_instance),
        "status_code": error.status_code,
        "error_code": _source_error_code(error),
    }


def _normalized_host(source_instance: str) -> str:
    """Return a lowercase host without path, query, fragment, or user information."""
    candidate = source_instance.strip()
    parsed = urlsplit(candidate if "://" in candidate else f"//{candidate}")
    return (parsed.hostname or "unknown").lower()


def _source_error_code(error: SourceError) -> str:
    """Map source failure facts to a bounded program-owned error code."""
    if error.error_code is not None:
        return error.error_code
    if error.category == "http" and error.status_code is not None:
        return f"http_{error.status_code}"
    return {
        "http": "http_error",
        "blocked": "blocked",
        "contract": "contract",
        "incomplete": "incomplete",
        "browser": "browser",
    }[error.category]


def _utc_text(value: datetime) -> str:
    """Return an ISO 8601 UTC timestamp using the compact Z suffix."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
