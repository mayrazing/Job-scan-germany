from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal, cast

import pytest
from pydantic import ValidationError

import job_scan.repository as repository_module
from job_scan.domain import (
    AvailabilityStatus,
    JobRecord,
    MachineStatus,
    Snapshot,
    SourceKind,
    SourceOccurrence,
    StoreMeta,
)
from job_scan.locking import FileRWLock
from job_scan.paths import AppPaths
from job_scan.repository import DashboardBuildError, JsonlRepository

NOW = datetime(2026, 8, 2, 10, 30, tzinfo=UTC)
REVISION_PATTERN = re.compile(rb'<meta name="job-scan-revision" content="(\d+)">')


def revision_html(snapshot: Snapshot, suffix: str = "") -> str:
    return (
        '<!doctype html><html><head><meta name="job-scan-revision" '
        f'content="{snapshot.meta.data_revision}"></head><body>{suffix}</body></html>'
    )


def extract_html_revision(path: Path) -> int | None:
    if not path.exists():
        return None
    match = REVISION_PATTERN.search(path.read_bytes())
    return int(match.group(1)) if match else None


@pytest.fixture
def paths(tmp_path: Path) -> AppPaths:
    value = AppPaths.from_root(tmp_path / "job-scan")
    value.ensure_directories()
    return value


@pytest.fixture
def make_repo(
    paths: AppPaths,
) -> Callable[[Callable[[Snapshot], str]], JsonlRepository]:
    def build(html_builder: Callable[[Snapshot], str] = revision_html) -> JsonlRepository:
        return JsonlRepository(paths, FileRWLock(paths.lock_file), html_builder)

    return build


@pytest.fixture
def repo(paths: AppPaths) -> JsonlRepository:
    return JsonlRepository(paths, FileRWLock(paths.lock_file))


@pytest.fixture
def sample_job() -> JobRecord:
    occurrence = SourceOccurrence(
        source=SourceKind.LINKEDIN,
        source_instance="acme/jobs",
        external_id="REQ-42",
        source_generation=1,
        url="https://acme.example/jobs/REQ-42",
        company="Acme",
        title="Backend Engineer",
        location="Berlin",
        description="Build APIs",
        posted_at=date(2026, 8, 1),
        content_hash="sha256:job",
        availability_status=AvailabilityStatus.ACTIVE,
    )
    return JobRecord(
        canonical_job_key="canonical-42",
        source_occurrences=[occurrence],
        primary_source_occurrence_key=occurrence.source_occurrence_key,
        company="Acme",
        title="Backend Engineer",
        location="Berlin",
        url=occurrence.url,
        description="Build APIs",
        posted_at=date(2026, 8, 1),
        content_hash="sha256:job",
        first_seen=NOW,
        last_seen=NOW,
        availability_status=AvailabilityStatus.ACTIVE,
        user_status_updated_at=NOW,
    )


def test_empty_store_has_meta_revision_zero(repo: JsonlRepository) -> None:
    snapshot = repo.load()

    assert snapshot.meta.data_revision == 0
    assert snapshot.meta.generated_at is None
    assert snapshot.jobs == []


def test_mutate_increments_revision_and_writes_meta_first(
    repo: JsonlRepository, sample_job: JobRecord
) -> None:
    snapshot = repo.mutate(lambda old: old.with_job(sample_job))

    lines = repo.paths.jobs_jsonl.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["record_type"] == "meta"
    assert json.loads(lines[0])["data_revision"] == 1
    assert json.loads(lines[1])["record_type"] == "job"
    assert snapshot.meta.data_revision == 1
    assert snapshot.meta.generated_at is not None
    assert snapshot.meta.generated_at.utcoffset() == UTC.utcoffset(snapshot.meta.generated_at)


def test_each_mutation_reloads_latest_snapshot_and_advances_one_revision(
    paths: AppPaths, sample_job: JobRecord
) -> None:
    first = JsonlRepository(paths, FileRWLock(paths.lock_file), revision_html)
    second = JsonlRepository(paths, FileRWLock(paths.lock_file), revision_html)
    another_job = sample_job.model_copy(
        update={"canonical_job_key": "canonical-43", "source_occurrences": []}
    )

    first.mutate(lambda old: old.with_job(sample_job))
    snapshot = second.mutate(lambda old: old.with_job(another_job))

    assert snapshot.meta.data_revision == 2
    assert [job.canonical_job_key for job in snapshot.jobs] == [
        "canonical-42",
        "canonical-43",
    ]


def test_mutation_uses_one_utc_generated_at_for_jsonl_html_and_result(
    make_repo: Callable[[Callable[[Snapshot], str]], JsonlRepository],
    sample_job: JobRecord,
) -> None:
    rendered_meta: list[StoreMeta] = []

    def capture(snapshot: Snapshot) -> str:
        rendered_meta.append(snapshot.meta.model_copy())
        return revision_html(snapshot)

    repo = make_repo(capture)
    result = repo.mutate(lambda old: old.with_job(sample_job))
    persisted = repo.load()

    assert len(rendered_meta) == 1
    assert persisted.meta.generated_at == rendered_meta[0].generated_at
    assert result.meta.generated_at == rendered_meta[0].generated_at
    assert result.meta.generated_at is not None
    assert result.meta.generated_at.tzinfo is UTC


def test_mutation_revision_ignores_mutator_changes_to_old_and_proposed_meta(
    repo: JsonlRepository,
    sample_job: JobRecord,
) -> None:
    repo.mutate(lambda old: old.with_job(sample_job))

    def corrupt_revisions(old: Snapshot) -> Snapshot:
        old.meta.data_revision = 40
        return Snapshot(meta=StoreMeta(data_revision=90), jobs=old.jobs)

    result = repo.mutate(corrupt_revisions)
    persisted = repo.load()

    assert result.meta.data_revision == 2
    assert persisted.meta.data_revision == 2


def test_mutation_deep_revalidates_raw_valid_nested_enum_assignments(
    make_repo: Callable[[Callable[[Snapshot], str]], JsonlRepository],
    sample_job: JobRecord,
) -> None:
    rendered_statuses: list[tuple[object, object]] = []

    def capture_statuses(snapshot: Snapshot) -> str:
        rendered_statuses.append(
            (
                snapshot.jobs[0].machine_status,
                snapshot.jobs[0].source_occurrences[0].availability_status,
            )
        )
        return revision_html(snapshot)

    def assign_raw_valid_statuses(old: Snapshot) -> Snapshot:
        changed = sample_job.model_copy(deep=True)
        changed.machine_status = "excluded"  # type: ignore[assignment]
        changed.source_occurrences[0].availability_status = "closed"  # type: ignore[assignment]
        return old.with_job(changed)

    repo = make_repo(capture_statuses)
    result = repo.mutate(assign_raw_valid_statuses)
    persisted = repo.load()

    assert rendered_statuses == [(MachineStatus.EXCLUDED, AvailabilityStatus.CLOSED)]
    assert result.jobs[0].machine_status is MachineStatus.EXCLUDED
    assert result.jobs[0].source_occurrences[0].availability_status is (
        AvailabilityStatus.CLOSED
    )
    assert persisted.jobs[0].machine_status is MachineStatus.EXCLUDED
    assert persisted.jobs[0].source_occurrences[0].availability_status is (
        AvailabilityStatus.CLOSED
    )


def test_mutation_canonicalizes_raw_source_before_computed_fields(
    make_repo: Callable[[Callable[[Snapshot], str]], JsonlRepository],
    sample_job: JobRecord,
) -> None:
    rendered_occurrences: list[tuple[object, str, str]] = []

    def capture_occurrence(snapshot: Snapshot) -> str:
        occurrence = snapshot.jobs[0].source_occurrences[0]
        rendered_occurrences.append(
            (
                occurrence.source,
                occurrence.source_job_key,
                occurrence.source_occurrence_key,
            )
        )
        return revision_html(snapshot)

    repo = make_repo(capture_occurrence)
    repo.mutate(lambda old: old.with_job(sample_job))
    rendered_occurrences.clear()

    def assign_raw_valid_source(old: Snapshot) -> Snapshot:
        old.jobs[0].source_occurrences[0].source = "linkedin"  # type: ignore[assignment]
        return old

    result = repo.mutate(assign_raw_valid_source)
    persisted = repo.load()

    expected = (
        SourceKind.LINKEDIN,
        "linkedin:acme/jobs:REQ-42",
        "linkedin:acme/jobs:REQ-42@1",
    )
    assert rendered_occurrences == [expected]
    for snapshot in (result, persisted):
        occurrence = snapshot.jobs[0].source_occurrences[0]
        assert occurrence.source is SourceKind.LINKEDIN
        assert occurrence.source_job_key == "linkedin:acme/jobs:REQ-42"
        assert occurrence.source_occurrence_key == "linkedin:acme/jobs:REQ-42@1"


def test_repository_snapshot_dumps_use_pydantic_29_keyword_surface(
    repo: JsonlRepository,
    sample_job: JobRecord,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_model_dump = Snapshot.model_dump

    def pydantic_29_model_dump(
        self: Snapshot,
        *,
        mode: Literal["json", "python"] | str = "python",
        include: Any = None,
        exclude: Any = None,
        context: Any | None = None,
        by_alias: bool = False,
        exclude_unset: bool = False,
        exclude_defaults: bool = False,
        exclude_none: bool = False,
        round_trip: bool = False,
        warnings: bool | Literal["none", "warn", "error"] = True,
        serialize_as_any: bool = False,
    ) -> dict[str, Any]:
        return original_model_dump(
            self,
            mode=mode,
            include=include,
            exclude=exclude,
            context=context,
            by_alias=by_alias,
            exclude_unset=exclude_unset,
            exclude_defaults=exclude_defaults,
            exclude_none=exclude_none,
            round_trip=round_trip,
            warnings=warnings,
            serialize_as_any=serialize_as_any,
        )

    monkeypatch.setattr(Snapshot, "model_dump", pydantic_29_model_dump)

    result = repo.mutate(lambda old: old.with_job(sample_job))
    persisted = repo.load()

    assert result.jobs == [sample_job]
    assert persisted == result
    assert extract_html_revision(repo.paths.dashboard_html) == 1


def corrupt_machine_status(snapshot: Snapshot) -> Snapshot:
    snapshot.jobs[0].machine_status = "not-a-status"  # type: ignore[assignment]
    return snapshot


def corrupt_job_record_type(snapshot: Snapshot) -> Snapshot:
    snapshot.jobs[0].record_type = "meta"  # type: ignore[assignment]
    return snapshot


def duplicate_source_occurrence(snapshot: Snapshot) -> Snapshot:
    snapshot.jobs[0].source_occurrences.append(snapshot.jobs[0].source_occurrences[0])
    return snapshot


@pytest.mark.parametrize(
    "corrupt",
    [corrupt_machine_status, corrupt_job_record_type, duplicate_source_occurrence],
    ids=["invalid-status", "invalid-record-type", "duplicate-nested-list-item"],
)
def test_invalid_nested_mutation_rejects_before_any_publication(
    repo: JsonlRepository,
    sample_job: JobRecord,
    corrupt: Callable[[Snapshot], Snapshot],
) -> None:
    repo.mutate(lambda old: old.with_job(sample_job))
    jsonl_before = repo.paths.jobs_jsonl.read_bytes()
    html_before = repo.paths.dashboard_html.read_bytes()

    with pytest.raises(ValidationError):
        repo.mutate(corrupt)

    assert repo.paths.jobs_jsonl.read_bytes() == jsonl_before
    assert repo.paths.dashboard_html.read_bytes() == html_before
    assert repo.load().meta.data_revision == 1


class SnapshotDuck:
    def __init__(self) -> None:
        self.jobs: list[JobRecord] = []


@pytest.mark.parametrize("invalid_result", [None, SnapshotDuck()], ids=["none", "duck"])
def test_mutation_rejects_non_snapshot_without_publishing(
    repo: JsonlRepository,
    invalid_result: object,
) -> None:
    def return_invalid(_old: Snapshot) -> Snapshot:
        return cast(Snapshot, invalid_result)

    with pytest.raises(TypeError, match="mutator must return Snapshot"):
        repo.mutate(return_invalid)

    assert repo.load().meta.data_revision == 0
    assert not repo.paths.jobs_jsonl.exists()
    assert not repo.paths.dashboard_html.exists()


def test_jsonl_serialization_ends_with_newline(
    repo: JsonlRepository, sample_job: JobRecord
) -> None:
    repo.mutate(lambda old: old.with_job(sample_job))

    data = repo.paths.jobs_jsonl.read_bytes()
    assert data.endswith(b"\n")
    assert not data.endswith(b"\n\n")


def test_load_preserves_unicode_line_separator_inside_json_string(
    repo: JsonlRepository, sample_job: JobRecord
) -> None:
    job = sample_job.model_copy(update={"description": "Build\u2028APIs"})
    repo.mutate(lambda old: old.with_job(job))

    loaded = repo.load()

    assert loaded.jobs[0].description == "Build\u2028APIs"


@pytest.mark.parametrize(
    "contents",
    [
        b"not-json\n",
        b'{"record_type":"meta","data_revision":1}\n\n',
        b'{"record_type":"job"}\n',
        (
            b'{"record_type":"meta","data_revision":1}\n'
            b'{"record_type":"meta","data_revision":2}\n'
        ),
    ],
    ids=["invalid-json", "blank-record", "meta-not-first", "second-meta"],
)
def test_load_rejects_every_malformed_or_misordered_record(
    repo: JsonlRepository, contents: bytes
) -> None:
    repo.paths.jobs_jsonl.write_bytes(contents)

    with pytest.raises((ValueError, ValidationError, json.JSONDecodeError)):
        repo.load()


def test_load_requires_explicit_meta_record_type(repo: JsonlRepository) -> None:
    repo.paths.jobs_jsonl.write_text(
        '{"data_revision":1,"generated_at":"2026-08-02T10:30:00Z"}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="first JSONL record must be meta"):
        repo.load()


def test_load_requires_explicit_job_record_type(
    repo: JsonlRepository, sample_job: JobRecord
) -> None:
    job_data = sample_job.model_dump(mode="json")
    del job_data["record_type"]
    repo.paths.jobs_jsonl.write_text(
        StoreMeta(data_revision=1, generated_at=NOW).model_dump_json()
        + "\n"
        + json.dumps(job_data)
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="later JSONL records must be jobs"):
        repo.load()


def test_load_rejects_duplicate_canonical_keys(
    repo: JsonlRepository, sample_job: JobRecord
) -> None:
    repo.paths.jobs_jsonl.write_text(
        StoreMeta(data_revision=1, generated_at=NOW).model_dump_json()
        + "\n"
        + sample_job.model_dump_json()
        + "\n"
        + sample_job.model_dump_json()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="duplicate canonical_job_key"):
        repo.load()


def test_load_rejects_duplicate_occurrence_keys_across_jobs(
    repo: JsonlRepository, sample_job: JobRecord
) -> None:
    other = sample_job.model_copy(update={"canonical_job_key": "canonical-43"})
    repo.paths.jobs_jsonl.write_text(
        StoreMeta(data_revision=1, generated_at=NOW).model_dump_json()
        + "\n"
        + sample_job.model_dump_json()
        + "\n"
        + other.model_dump_json()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="duplicate source_occurrence_key"):
        repo.load()


def test_mutation_fsyncs_directory_after_each_replace_in_publication_order(
    repo: JsonlRepository,
    sample_job: JobRecord,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_fsync = repository_module.os.fsync
    real_replace = repository_module.os.replace
    events: list[str] = []

    def observed_fsync(fd: int) -> None:
        mode = os.fstat(fd).st_mode
        events.append("fsync:directory" if stat.S_ISDIR(mode) else "fsync:file")
        real_fsync(fd)

    def observed_replace(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
        events.append(f"replace:{Path(destination).name}")
        real_replace(source, destination)

    monkeypatch.setattr(repository_module.os, "fsync", observed_fsync)
    monkeypatch.setattr(repository_module.os, "replace", observed_replace)

    repo.mutate(lambda old: old.with_job(sample_job))

    assert events == [
        "fsync:file",
        "fsync:file",
        f"replace:{repo.paths.jobs_jsonl.name}",
        "fsync:directory",
        f"replace:{repo.paths.dashboard_html.name}",
        "fsync:directory",
    ]


def test_html_builder_failure_keeps_new_jsonl_and_old_html_detectable(
    make_repo: Callable[[Callable[[Snapshot], str]], JsonlRepository],
    sample_job: JobRecord,
) -> None:
    old_html = revision_html(Snapshot(meta=StoreMeta(data_revision=0)), "old").encode()

    def failing_render(snapshot: Snapshot) -> str:
        raise RuntimeError("renderer failed")

    repo = make_repo(failing_render)
    repo.paths.dashboard_html.write_bytes(old_html)
    unrelated = repo.paths.dashboard_html.parent / "keep-me.tmp"
    unrelated.write_text("keep", encoding="utf-8")

    with pytest.raises(DashboardBuildError) as caught:
        repo.mutate(lambda old: old.with_job(sample_job))

    assert isinstance(caught.value.__cause__, RuntimeError)
    assert repo.load().meta.data_revision == 1
    assert extract_html_revision(repo.paths.dashboard_html) != 1
    assert repo.paths.dashboard_html.read_bytes() == old_html
    assert unrelated.read_text(encoding="utf-8") == "keep"


def test_mutating_html_builder_cannot_change_returned_or_persisted_snapshot(
    make_repo: Callable[[Callable[[Snapshot], str]], JsonlRepository],
    sample_job: JobRecord,
) -> None:
    def mutating_builder(snapshot: Snapshot) -> str:
        expected_revision = snapshot.meta.data_revision
        snapshot.meta.data_revision = 999
        snapshot.jobs[0].title = "renderer mutation"
        snapshot.jobs.clear()
        return revision_html(Snapshot(meta=StoreMeta(data_revision=expected_revision)))

    repo = make_repo(mutating_builder)
    result = repo.mutate(lambda old: old.with_job(sample_job))
    persisted = repo.load()

    assert result.meta.data_revision == 1
    assert [job.title for job in result.jobs] == ["Backend Engineer"]
    assert persisted == result
    assert extract_html_revision(repo.paths.dashboard_html) == 1


@pytest.mark.parametrize(
    "invalid_html",
    [
        "<html><head></head><body>missing</body></html>",
        '<html><head><meta name="job-scan-revision" content="99"></head></html>',
    ],
    ids=["missing-revision", "wrong-revision"],
)
def test_mutation_rejects_invalid_rendered_revision_after_jsonl_publication(
    make_repo: Callable[[Callable[[Snapshot], str]], JsonlRepository],
    sample_job: JobRecord,
    invalid_html: str,
) -> None:
    repo = make_repo(lambda _snapshot: invalid_html)

    with pytest.raises(DashboardBuildError, match="dashboard revision"):
        repo.mutate(lambda old: old.with_job(sample_job))

    assert repo.load().meta.data_revision == 1
    assert not repo.paths.dashboard_html.exists()


@pytest.mark.parametrize(
    "invalid_html",
    [
        "<html><head></head><body>missing</body></html>",
        '<html><head><meta name="job-scan-revision" content="99"></head></html>',
    ],
    ids=["missing-revision", "wrong-revision"],
)
def test_dashboard_mismatch_repair_rejects_invalid_rendered_revision(
    paths: AppPaths,
    sample_job: JobRecord,
    invalid_html: str,
) -> None:
    seed_repo = JsonlRepository(paths, FileRWLock(paths.lock_file), revision_html)
    seed_repo.mutate(lambda old: old.with_job(sample_job))
    stale_html = revision_html(Snapshot(meta=StoreMeta(data_revision=0)), "stale").encode()
    paths.dashboard_html.write_bytes(stale_html)
    repo = JsonlRepository(
        paths,
        FileRWLock(paths.lock_file),
        lambda _snapshot: invalid_html,
    )

    with pytest.raises(DashboardBuildError, match="dashboard revision"):
        repo.read_dashboard_bytes()

    assert paths.dashboard_html.read_bytes() == stale_html
    assert repo.load().meta.data_revision == 1


@pytest.mark.parametrize(
    "invalid_html",
    [
        "<html><head></head><body>missing</body></html>",
        '<html><head><meta name="job-scan-revision" content="99"></head></html>',
    ],
    ids=["missing-revision", "wrong-revision"],
)
def test_rebuild_dashboard_rejects_invalid_rendered_revision(
    paths: AppPaths,
    sample_job: JobRecord,
    invalid_html: str,
) -> None:
    seed_repo = JsonlRepository(paths, FileRWLock(paths.lock_file), revision_html)
    seed_repo.mutate(lambda old: old.with_job(sample_job))
    html_before = paths.dashboard_html.read_bytes()
    jsonl_before = paths.jobs_jsonl.read_bytes()
    repo = JsonlRepository(
        paths,
        FileRWLock(paths.lock_file),
        lambda _snapshot: invalid_html,
    )

    with pytest.raises(DashboardBuildError, match="dashboard revision"):
        repo.rebuild_dashboard()

    assert paths.dashboard_html.read_bytes() == html_before
    assert paths.jobs_jsonl.read_bytes() == jsonl_before


def test_crash_between_replaces_is_repaired_before_dashboard_bytes_are_returned(
    repo: JsonlRepository,
    sample_job: JobRecord,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo.paths.dashboard_html.write_text(
        revision_html(Snapshot(meta=StoreMeta(data_revision=0)), "old"),
        encoding="utf-8",
    )
    real_fsync = repository_module.os.fsync
    real_replace = repository_module.os.replace
    publication_events: list[str] = []

    def observed_fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            publication_events.append("fsync:directory")
        real_fsync(fd)

    def crash_before_html_replace(
        source: str | os.PathLike[str], destination: str | os.PathLike[str]
    ) -> None:
        publication_events.append(f"replace:{Path(destination).name}")
        if Path(destination) == repo.paths.dashboard_html:
            raise OSError("injected crash between replaces")
        real_replace(source, destination)

    monkeypatch.setattr(repository_module.os, "fsync", observed_fsync)
    monkeypatch.setattr(repository_module.os, "replace", crash_before_html_replace)

    with pytest.raises(OSError, match="injected crash"):
        repo.mutate(lambda old: old.with_job(sample_job))

    assert repo.load().meta.data_revision == 1
    assert extract_html_revision(repo.paths.dashboard_html) == 0
    assert publication_events == [
        f"replace:{repo.paths.jobs_jsonl.name}",
        "fsync:directory",
        f"replace:{repo.paths.dashboard_html.name}",
    ]

    monkeypatch.setattr(repository_module.os, "replace", real_replace)
    returned = repo.read_dashboard_bytes()

    assert REVISION_PATTERN.search(returned).group(1) == b"1"  # type: ignore[union-attr]
    assert returned == repo.paths.dashboard_html.read_bytes()


def test_consistent_dashboard_read_returns_all_bytes_read_under_shared_lock(
    make_repo: Callable[[Callable[[Snapshot], str]], JsonlRepository],
    sample_job: JobRecord,
) -> None:
    suffix = "prefix-" + ("x" * 16_384) + "-final-byte"
    repo = make_repo(lambda snapshot: revision_html(snapshot, suffix))
    repo.mutate(lambda old: old.with_job(sample_job))

    returned = repo.read_dashboard_bytes()

    assert returned == repo.paths.dashboard_html.read_bytes()
    assert returned.endswith(b"-final-byte</body></html>")


class ObservedRealLock:
    def __init__(self, real: FileRWLock, before_exclusive: Callable[[], None] | None = None):
        self.real = real
        self.before_exclusive = before_exclusive
        self.shared_depth = 0

    @contextmanager
    def shared(self, blocking: bool = True) -> Iterator[None]:
        with self.real.shared(blocking):
            self.shared_depth += 1
            try:
                yield
            finally:
                self.shared_depth -= 1

    @contextmanager
    def exclusive(self, blocking: bool = True) -> Iterator[None]:
        assert self.shared_depth == 0, "attempted in-place shared-to-exclusive upgrade"
        with self.real.exclusive(blocking):
            if self.before_exclusive is not None:
                self.before_exclusive()
            yield


def test_mismatch_releases_shared_lock_then_rechecks_after_exclusive_acquisition(
    paths: AppPaths,
) -> None:
    paths.jobs_jsonl.write_text(
        StoreMeta(data_revision=1, generated_at=NOW).model_dump_json() + "\n",
        encoding="utf-8",
    )
    paths.dashboard_html.write_text(
        revision_html(Snapshot(meta=StoreMeta(data_revision=0)), "stale"),
        encoding="utf-8",
    )
    current_bytes = revision_html(
        Snapshot(meta=StoreMeta(data_revision=1, generated_at=NOW)), "other-writer"
    ).encode()

    def other_writer_wins_race() -> None:
        paths.dashboard_html.write_bytes(current_bytes)

    def must_not_render(snapshot: Snapshot) -> str:
        raise AssertionError("exclusive recheck should observe current HTML")

    lock = ObservedRealLock(FileRWLock(paths.lock_file), other_writer_wins_race)
    repo = JsonlRepository(paths, lock, must_not_render)  # type: ignore[arg-type]

    assert repo.read_dashboard_bytes() == current_bytes


def test_rebuild_dashboard_replaces_only_html_without_incrementing_revision(
    repo: JsonlRepository, sample_job: JobRecord
) -> None:
    changed = repo.mutate(lambda old: old.with_job(sample_job))
    jsonl_before = repo.paths.jobs_jsonl.read_bytes()
    repo.paths.dashboard_html.write_text("obsolete", encoding="utf-8")

    repo.rebuild_dashboard()

    assert repo.paths.jobs_jsonl.read_bytes() == jsonl_before
    assert repo.load().meta.data_revision == changed.meta.data_revision
    assert extract_html_revision(repo.paths.dashboard_html) == changed.meta.data_revision
    assert b"job-scan review" in repo.paths.dashboard_html.read_bytes()


def test_clear_removes_only_live_results(repo: JsonlRepository, sample_job: JobRecord) -> None:
    repo.mutate(lambda old: old.with_job(sample_job))

    repo.clear()

    assert repo.load().jobs == []
    assert not repo.paths.jobs_jsonl.exists()
    assert not repo.paths.dashboard_html.exists()
