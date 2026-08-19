from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from job_scan.domain import JobRecord, Snapshot, StoreMeta
from job_scan.locking import FileRWLock
from job_scan.paths import AppPaths

_REVISION_META = re.compile(
    rb'<meta\s+name=["\']job-scan-revision["\']\s+content=["\'](\d+)["\'][^>]*>'
)


class DashboardBuildError(RuntimeError):
    """Report dashboard rendering or revision-contract failure."""


def render_revision_page(snapshot: Snapshot) -> str:
    """Render the minimal Phase 1 page carrying the JSONL revision."""
    revision = snapshot.meta.data_revision
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        f'  <meta name="job-scan-revision" content="{revision}">\n'
        "  <title>job-scan</title>\n"
        "</head>\n"
        "<body>\n"
        "  <p>Run <code>job-scan review</code> to review jobs.</p>\n"
        "</body>\n"
        "</html>\n"
    )


def serialize_snapshot(snapshot: Snapshot) -> bytes:
    """Serialize one complete snapshot as the canonical JSONL format."""
    records = [snapshot.meta.model_dump_json()]
    records.extend(job.model_dump_json() for job in snapshot.jobs)
    return ("\n".join(records) + "\n").encode("utf-8")


def parse_snapshot(contents: bytes) -> Snapshot:
    """Parse one complete canonical JSONL snapshot."""
    if not contents:
        raise ValueError("jobs JSONL must start with one meta record")
    text = contents.decode("utf-8")
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()

    raw_meta: object = json.loads(lines[0])
    if not isinstance(raw_meta, dict) or raw_meta.get("record_type") != "meta":
        raise ValueError("first JSONL record must be meta")
    meta = StoreMeta.model_validate(raw_meta)

    jobs: list[JobRecord] = []
    for line in lines[1:]:
        raw_job: object = json.loads(line)
        if not isinstance(raw_job, dict) or raw_job.get("record_type") != "job":
            raise ValueError("later JSONL records must be jobs")
        jobs.append(JobRecord.model_validate(raw_job))
    return Snapshot(meta=meta, jobs=jobs)


class JsonlRepository:
    """Publish revision-matched JSONL facts and derived dashboard HTML."""

    def __init__(
        self,
        paths: AppPaths,
        lock: FileRWLock,
        html_builder: Callable[[Snapshot], str] = render_revision_page,
    ) -> None:
        self.paths = paths
        self.lock = lock
        self.html_builder = html_builder
        self.paths.ensure_directories()

    def load(self) -> Snapshot:
        """Load and validate one snapshot while holding a shared lock."""
        with self.lock.shared():
            return self.load_unlocked()

    def load_unlocked(self) -> Snapshot:
        """Load every JSONL record without acquiring the repository lock."""
        if not self.paths.jobs_jsonl.exists():
            return Snapshot(meta=StoreMeta(data_revision=0))

        return parse_snapshot(self.paths.jobs_jsonl.read_bytes())

    def mutate(self, mutator: Callable[[Snapshot], Snapshot]) -> Snapshot:
        """Apply one mutation to the latest snapshot and publish a new revision."""
        with self.lock.exclusive():
            old = self.load_unlocked()
            old_revision = old.meta.data_revision
            proposed = mutator(old)
            if not isinstance(proposed, Snapshot):
                raise TypeError("mutator must return Snapshot")
            generated_at = datetime.now(UTC)
            proposed_data = proposed.model_dump(
                mode="json",
                round_trip=True,
                warnings=False,
            )
            proposed_data["meta"] = StoreMeta(
                data_revision=old_revision + 1,
                generated_at=generated_at,
            ).model_dump()
            revisioned = Snapshot.model_validate(proposed_data)
            jsonl_temp = self._write_temp(
                self.paths.jobs_jsonl,
                serialize_snapshot(revisioned),
            )
            html_temp: Path | None = None
            build_failure: DashboardBuildError | None = None
            try:
                try:
                    html = self._render_dashboard_bytes(revisioned)
                    html_temp = self._write_temp(self.paths.dashboard_html, html)
                except DashboardBuildError as exc:
                    build_failure = exc

                os.replace(jsonl_temp, self.paths.jobs_jsonl)
                self._fsync_output_directory()
                if html_temp is not None:
                    os.replace(html_temp, self.paths.dashboard_html)
                    self._fsync_output_directory()

                if build_failure is not None:
                    raise build_failure
                return revisioned
            finally:
                jsonl_temp.unlink(missing_ok=True)
                if html_temp is not None:
                    html_temp.unlink(missing_ok=True)

    def read_dashboard_bytes(self) -> bytes:
        """Return full dashboard bytes matching the current JSONL revision."""
        response: bytes | None = None
        with self.lock.shared():
            snapshot = self.load_unlocked()
            dashboard = self._read_dashboard_or_empty()
            if self._html_revision(dashboard) == snapshot.meta.data_revision:
                response = dashboard

        if response is None:
            with self.lock.exclusive():
                snapshot = self.load_unlocked()
                dashboard = self._read_dashboard_or_empty()
                if self._html_revision(dashboard) != snapshot.meta.data_revision:
                    self._rebuild_dashboard_unlocked(snapshot)
                    dashboard = self.paths.dashboard_html.read_bytes()
                response = dashboard

        return response

    def rebuild_dashboard(self) -> None:
        """Republish HTML from current JSONL without changing its revision."""
        with self.lock.exclusive():
            self._rebuild_dashboard_unlocked(self.load_unlocked())

    def clear(self) -> None:
        """Remove the live result files without touching setup or shared caches."""
        with self.lock.exclusive():
            self.paths.jobs_jsonl.unlink(missing_ok=True)
            self.paths.dashboard_html.unlink(missing_ok=True)
            self._fsync_output_directory()

    def _rebuild_dashboard_unlocked(self, snapshot: Snapshot) -> None:
        html = self._render_dashboard_bytes(snapshot)
        html_temp = self._write_temp(self.paths.dashboard_html, html)
        try:
            os.replace(html_temp, self.paths.dashboard_html)
            self._fsync_output_directory()
        finally:
            html_temp.unlink(missing_ok=True)

    @staticmethod
    def _html_revision(html: bytes) -> int | None:
        match = _REVISION_META.search(html)
        return int(match.group(1)) if match is not None else None

    def _render_dashboard_bytes(self, snapshot: Snapshot) -> bytes:
        expected_revision = snapshot.meta.data_revision
        renderer_snapshot = Snapshot.model_validate(
            snapshot.model_dump(
                mode="json",
                round_trip=True,
                warnings=False,
            )
        )
        try:
            html = self.html_builder(renderer_snapshot)
            rendered = html.encode("utf-8")
        except Exception as exc:
            raise DashboardBuildError("dashboard rendering failed") from exc

        rendered_revision = self._html_revision(rendered)
        if rendered_revision is None:
            raise DashboardBuildError(
                f"dashboard revision tag missing; expected {expected_revision}"
            )
        if rendered_revision != expected_revision:
            raise DashboardBuildError(
                "dashboard revision mismatch: "
                f"expected {expected_revision}, got {rendered_revision}"
            )
        return rendered

    def _read_dashboard_or_empty(self) -> bytes:
        try:
            return self.paths.dashboard_html.read_bytes()
        except FileNotFoundError:
            return b""

    @staticmethod
    def _write_temp(destination: Path, data: bytes) -> Path:
        descriptor, name = tempfile.mkstemp(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
        )
        path = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return path

    def _fsync_output_directory(self) -> None:
        descriptor = os.open(self.paths.jobs_jsonl.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
