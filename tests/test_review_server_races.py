from __future__ import annotations

import os
import re
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import TypeVar

import pytest
from fastapi.testclient import TestClient

from job_scan.domain import Snapshot
from job_scan.locking import FileRWLock
from job_scan.paths import AppPaths
from job_scan.repository import JsonlRepository
from job_scan.review_server import create_review_app

WAIT_SECONDS = 3.0
ORIGIN = "http://127.0.0.1:8765"
REVISION_PATTERN = re.compile(rb'<meta name="job-scan-revision" content="(\d+)">')
T = TypeVar("T")


def _revision_html(snapshot: Snapshot, marker: str = "complete") -> str:
    return (
        '<!doctype html><html><head><meta name="job-scan-revision" '
        f'content="{snapshot.meta.data_revision}"></head>'
        f"<body>{marker}</body></html>"
    )


@pytest.fixture
def repository(tmp_path: Path) -> JsonlRepository:
    paths = AppPaths.from_root(tmp_path / "home")
    value = JsonlRepository(paths, FileRWLock(paths.lock_file), _revision_html)
    value.rebuild_dashboard()
    return value


def _client(repository: JsonlRepository) -> TestClient:
    app = create_review_app(repository, "token", frozenset({ORIGIN}))
    return TestClient(app, base_url=ORIGIN)


def _revision(content: bytes) -> int:
    match = REVISION_PATTERN.search(content)
    assert match is not None
    return int(match.group(1))


def _identity(snapshot: Snapshot) -> Snapshot:
    return snapshot


def _future_result(future: Future[T]) -> T:
    return future.result(timeout=WAIT_SECONDS)


def test_get_holds_shared_lock_until_all_response_bytes_are_copied(
    repository: JsonlRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bytes_copied = Event()
    release_get = Event()
    writer_done = Event()
    real_read_bytes = Path.read_bytes
    pause_once = True

    def pause_after_copy(path: Path) -> bytes:
        nonlocal pause_once
        content = real_read_bytes(path)
        if path == repository.paths.dashboard_html and pause_once:
            pause_once = False
            bytes_copied.set()
            assert release_get.wait(WAIT_SECONDS)
        return content

    def write_new_revision() -> None:
        repository.mutate(_identity)
        writer_done.set()

    monkeypatch.setattr(Path, "read_bytes", pause_after_copy)
    with _client(repository) as client, ThreadPoolExecutor(max_workers=2) as executor:
        get_future = executor.submit(client.get, "/")
        assert bytes_copied.wait(WAIT_SECONDS)
        writer_future = executor.submit(write_new_revision)
        try:
            assert not writer_done.wait(0.1)
        finally:
            release_get.set()
        response = _future_result(get_future)
        _future_result(writer_future)

    assert response.status_code == 200
    assert _revision(response.content) == 0
    assert _revision(real_read_bytes(repository.paths.dashboard_html)) == 1


def test_get_blocks_between_jsonl_and_html_replace_then_returns_new_revision(
    repository: JsonlRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer_between_replaces = Event()
    release_writer = Event()
    writer_done = Event()
    real_replace = os.replace
    pause_once = True

    def pause_before_html_replace(
        source: str | os.PathLike[str], destination: str | os.PathLike[str]
    ) -> None:
        nonlocal pause_once
        if Path(destination) == repository.paths.dashboard_html and pause_once:
            pause_once = False
            writer_between_replaces.set()
            assert release_writer.wait(WAIT_SECONDS)
        real_replace(source, destination)

    def write_new_revision() -> None:
        repository.mutate(_identity)
        writer_done.set()

    monkeypatch.setattr(os, "replace", pause_before_html_replace)
    with _client(repository) as client, ThreadPoolExecutor(max_workers=2) as executor:
        writer_future = executor.submit(write_new_revision)
        assert writer_between_replaces.wait(WAIT_SECONDS)
        get_future = executor.submit(client.get, "/")
        try:
            assert not get_future.done()
            assert not writer_done.wait(0.1)
        finally:
            release_writer.set()
        _future_result(writer_future)
        response = _future_result(get_future)

    assert response.status_code == 200
    assert _revision(response.content) == 1
    assert response.content == repository.paths.dashboard_html.read_bytes()


def test_get_rebuilds_current_dashboard_after_html_replace_failure(
    repository: JsonlRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_replace = os.replace
    fail_once = True

    def fail_html_replace(
        source: str | os.PathLike[str], destination: str | os.PathLike[str]
    ) -> None:
        nonlocal fail_once
        if Path(destination) == repository.paths.dashboard_html and fail_once:
            fail_once = False
            raise OSError("injected HTML replace failure")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_html_replace)
    with pytest.raises(OSError, match="injected HTML replace failure"):
        repository.mutate(_identity)
    assert repository.load().meta.data_revision == 1
    assert _revision(repository.paths.dashboard_html.read_bytes()) == 0
    monkeypatch.setattr(os, "replace", real_replace)

    with _client(repository) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert _revision(response.content) == 1
    assert response.content == repository.paths.dashboard_html.read_bytes()
