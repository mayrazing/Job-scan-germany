from __future__ import annotations

import importlib
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from job_scan.domain import SourceKind
from job_scan.normalization import content_hash
from job_scan.scan_service import _persist_job_snapshots
from job_scan.sources.base import FetchedOccurrence

SELF_CONTAINED_PAGE = """<!doctype html>
<html data-job-scan-snapshot="stepstone:de:13889830">
<head>
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:">
  <style>body { color: #0c2577; }</style>
</head>
<body><main data-original-page-snapshot>Senior Software Engineer</main></body>
</html>
"""


def occurrence(html: str | None = None) -> FetchedOccurrence:
    description = "Build Java services."
    return FetchedOccurrence(
        source=SourceKind.STEPSTONE,
        source_instance="de",
        external_id="13889830",
        url="https://www.stepstone.de/job/13889830",
        company="IDnow GmbH",
        title="Senior Software Engineer Java",
        location="Berlin",
        description=description,
        posted_at=date(2026, 8, 20),
        content_hash=content_hash(
            "IDnow GmbH",
            "Senior Software Engineer Java",
            "Berlin",
            description,
        ),
        detail_complete=True,
        job_snapshot_html=html,
    )


def test_snapshot_store_saves_one_self_contained_page(tmp_path: Path) -> None:
    module = importlib.import_module("job_scan.job_snapshot")
    store = module.JobSnapshotStore(tmp_path)

    reference = store.save(
        source_job_key="stepstone:de:13889830",
        captured_at=datetime(2026, 8, 20, 9, 30, tzinfo=UTC),
        html=SELF_CONTAINED_PAGE,
    )

    assert reference.snapshot_id.startswith("sha256:")
    assert reference.captured_at == datetime(2026, 8, 20, 9, 30, tzinfo=UTC)
    assert store.read(reference.snapshot_id) == SELF_CONTAINED_PAGE.encode()
    assert list(tmp_path.glob("*.html")) == [
        tmp_path / f"{reference.snapshot_id.removeprefix('sha256:')}.html"
    ]


def test_snapshot_store_rejects_a_page_that_can_contact_the_source(tmp_path: Path) -> None:
    module = importlib.import_module("job_scan.job_snapshot")
    store = module.JobSnapshotStore(tmp_path)

    external_page = SELF_CONTAINED_PAGE.replace(
        "</body>", '<img src="https://www.stepstone.de/tracker.gif"></body>'
    )

    try:
        store.save(
            source_job_key="stepstone:de:13889830",
            captured_at=datetime(2026, 8, 20, 9, 30, tzinfo=UTC),
            html=external_page,
        )
    except ValueError as error:
        assert str(error) == (
            "job snapshot must be a safe self-contained job page: "
            "external src resource"
        )
    else:
        raise AssertionError("external snapshot was accepted")


def test_snapshot_store_does_not_overwrite_an_immutable_snapshot_url(
    tmp_path: Path,
) -> None:
    module = importlib.import_module("job_scan.job_snapshot")
    store = module.JobSnapshotStore(tmp_path)
    changed_page = SELF_CONTAINED_PAGE.replace("#0c2577", "#d40511")

    original = store.save(
        source_job_key="stepstone:de:13889830",
        captured_at=datetime(2026, 8, 20, 9, 30, tzinfo=UTC),
        html=SELF_CONTAINED_PAGE,
    )
    changed = store.save(
        source_job_key="stepstone:de:13889830",
        captured_at=datetime(2026, 8, 20, 9, 31, tzinfo=UTC),
        html=changed_page,
    )

    assert original.snapshot_id != changed.snapshot_id
    assert store.read(original.snapshot_id) == SELF_CONTAINED_PAGE.encode()
    assert store.read(changed.snapshot_id) == changed_page.encode()


@pytest.mark.parametrize(
    "unsafe_fragment",
    [
        "<img src=https://source.example/tracker.gif>",
        "<script>alert('unsafe')</script>",
        '<form action="/apply"><input name="email"></form>',
        '<meta http-equiv="refresh" content="0;url=https://source.example">',
        '<style>@import "https://source.example/site.css"</style>',
        '<a href="data:text/html,active">open</a>',
    ],
)
def test_snapshot_store_rejects_active_or_external_html(
    tmp_path: Path,
    unsafe_fragment: str,
) -> None:
    module = importlib.import_module("job_scan.job_snapshot")
    store = module.JobSnapshotStore(tmp_path)
    unsafe_page = SELF_CONTAINED_PAGE.replace("</body>", f"{unsafe_fragment}</body>")

    with pytest.raises(ValueError, match="safe self-contained job page"):
        store.save(
            source_job_key="stepstone:de:13889830",
            captured_at=datetime(2026, 8, 20, 9, 30, tzinfo=UTC),
            html=unsafe_page,
        )


def test_snapshot_store_rejects_a_page_for_another_job(tmp_path: Path) -> None:
    module = importlib.import_module("job_scan.job_snapshot")
    store = module.JobSnapshotStore(tmp_path)

    with pytest.raises(ValueError, match="snapshot marker"):
        store.save(
            source_job_key="indeed:de:another-job",
            captured_at=datetime(2026, 8, 20, 9, 30, tzinfo=UTC),
            html=SELF_CONTAINED_PAGE,
        )


def test_optional_snapshot_directory_failure_does_not_fail_the_job(
    tmp_path: Path,
) -> None:
    module = importlib.import_module("job_scan.job_snapshot")
    blocked_directory = tmp_path / "job-snapshots"
    blocked_directory.write_text("not a directory")
    item = occurrence(SELF_CONTAINED_PAGE)

    _persist_job_snapshots(
        [item],
        module.JobSnapshotStore(blocked_directory),
        datetime(2026, 8, 20, 9, 30, tzinfo=UTC),
    )

    assert item.job_snapshot is None
    assert item.job_snapshot_error_code == "snapshot_save_failed"
    assert item.job_snapshot_html is None


def test_scan_persists_transient_snapshot_html_without_serializing_it(
    tmp_path: Path,
) -> None:
    module = importlib.import_module("job_scan.job_snapshot")
    item = occurrence(SELF_CONTAINED_PAGE)

    _persist_job_snapshots(
        [item],
        module.JobSnapshotStore(tmp_path),
        datetime(2026, 8, 20, 9, 30, tzinfo=UTC),
    )

    assert item.job_snapshot is not None
    assert item.job_snapshot_error_code is None
    assert item.job_snapshot_html is None
    assert "job_snapshot_html" not in item.model_dump()


def test_invalid_snapshot_does_not_fail_the_job_scan(tmp_path: Path) -> None:
    module = importlib.import_module("job_scan.job_snapshot")
    external_page = SELF_CONTAINED_PAGE.replace(
        "</body>", '<img src="https://www.stepstone.de/tracker.gif"></body>'
    )
    item = occurrence(external_page)

    _persist_job_snapshots(
        [item],
        module.JobSnapshotStore(tmp_path),
        datetime(2026, 8, 20, 9, 30, tzinfo=UTC),
    )

    assert item.job_snapshot is None
    assert item.job_snapshot_error_code == "snapshot_save_failed"
    assert item.job_snapshot_html is None
