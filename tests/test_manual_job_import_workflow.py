from __future__ import annotations

import time
from collections.abc import Callable
from threading import Event
from threading import enumerate as enumerate_threads

import pytest

from job_scan.domain import UserStatus
from job_scan.manual_job_import_workflow import (
    ManualImportBusy,
    ManualImportProgress,
    ManualImportResult,
    ManualJobImportWorkflow,
)


def _wait_for_completion(
    workflow: ManualJobImportWorkflow,
    import_id: str,
) -> None:
    for _ in range(200):
        state = workflow.read_run(import_id)
        if state is not None and state.status != "running":
            return
        time.sleep(0.005)
    raise AssertionError("manual import did not finish")


def test_different_manual_tasks_run_at_the_same_time() -> None:
    workflow = ManualJobImportWorkflow(max_concurrent=3)
    first_started = Event()
    second_started = Event()
    release = Event()

    def run_first(_progress: object) -> ManualImportResult:
        first_started.set()
        if not release.wait(timeout=5):
            raise AssertionError("test did not release first task")
        return ManualImportResult("first", UserStatus.SAVED)

    def run_second(_progress: object) -> ManualImportResult:
        second_started.set()
        if not release.wait(timeout=5):
            raise AssertionError("test did not release second task")
        return ManualImportResult("second", UserStatus.SAVED)

    first = workflow.start(
        run_first,
        task_kind="re-evaluate",
        task_label="Backend Engineer",
        task_key="re-evaluate:first",
    )
    try:
        second = workflow.start(
            run_second,
            task_kind="re-evaluate",
            task_label="Platform Engineer",
            task_key="re-evaluate:second",
        )
        assert first_started.wait(timeout=1)
        assert second_started.wait(timeout=1)
    finally:
        release.set()

    _wait_for_completion(workflow, first.import_id)
    _wait_for_completion(workflow, second.import_id)


def test_duplicate_task_key_is_rejected_until_the_first_task_finishes() -> None:
    workflow = ManualJobImportWorkflow(max_concurrent=3)
    started = Event()
    release = Event()

    def run(_progress: object) -> ManualImportResult:
        started.set()
        if not release.wait(timeout=5):
            raise AssertionError("test did not release task")
        return ManualImportResult("tracked", UserStatus.SAVED)

    first = workflow.start(
        run,
        task_kind="re-evaluate",
        task_label="Backend Engineer",
        task_key="re-evaluate:tracked",
    )
    try:
        assert started.wait(timeout=1)
        with pytest.raises(ManualImportBusy, match="already running"):
            workflow.start(
                run,
                task_kind="re-evaluate",
                task_label="Backend Engineer",
                task_key="re-evaluate:tracked",
            )
    finally:
        release.set()

    _wait_for_completion(workflow, first.import_id)


def test_tasks_above_the_concurrency_limit_wait_for_a_slot() -> None:
    workflow = ManualJobImportWorkflow(max_concurrent=2)
    first_started = Event()
    second_started = Event()
    third_started = Event()
    release_first = Event()
    release_second = Event()

    def blocking_run(
        started: Event,
        release: Event,
        job_key: str,
    ) -> Callable[[ManualImportProgress], ManualImportResult]:
        def run(_progress: object) -> ManualImportResult:
            started.set()
            if not release.wait(timeout=5):
                raise AssertionError(f"test did not release {job_key}")
            return ManualImportResult(job_key, UserStatus.SAVED)

        return run

    first = workflow.start(
        blocking_run(first_started, release_first, "first"),
        task_kind="re-evaluate",
        task_label="First",
        task_key="re-evaluate:first",
    )
    second = workflow.start(
        blocking_run(second_started, release_second, "second"),
        task_kind="re-evaluate",
        task_label="Second",
        task_key="re-evaluate:second",
    )
    third = workflow.start(
        lambda _progress: (
            third_started.set()
            or ManualImportResult("third", UserStatus.SAVED)
        ),
        task_kind="re-evaluate",
        task_label="Third",
        task_key="re-evaluate:third",
    )
    try:
        assert first_started.wait(timeout=1)
        assert second_started.wait(timeout=1)
        assert not third_started.wait(timeout=0.1)
        waiting = workflow.read_run(third.import_id)
        assert waiting is not None
        assert waiting.step == "queued"
        thread_names = {thread.name for thread in enumerate_threads()}
        assert f"job-scan-manual-import-{third.import_id}" not in thread_names

        release_first.set()
        assert third_started.wait(timeout=1)
    finally:
        release_first.set()
        release_second.set()

    _wait_for_completion(workflow, first.import_id)
    _wait_for_completion(workflow, second.import_id)
    _wait_for_completion(workflow, third.import_id)


def test_active_runs_include_running_and_waiting_tasks_until_they_finish() -> None:
    workflow = ManualJobImportWorkflow(max_concurrent=1)
    first_started = Event()
    second_started = Event()
    release = Event()

    def run_first(_progress: object) -> ManualImportResult:
        first_started.set()
        if not release.wait(timeout=5):
            raise AssertionError("test did not release first task")
        return ManualImportResult("first", UserStatus.SAVED)

    first = workflow.start(
        run_first,
        task_kind="add-job",
        task_label="https://jobs.example/first",
        task_key="add-job",
    )
    second = workflow.start(
        lambda _progress: (
            second_started.set()
            or ManualImportResult("second", UserStatus.SAVED)
        ),
        task_kind="re-evaluate",
        task_label="Backend Engineer",
        task_key="re-evaluate:second",
    )
    try:
        assert first_started.wait(timeout=1)
        active = workflow.read_active_runs()
        assert [state.import_id for state in active] == [
            first.import_id,
            second.import_id,
        ]
        assert active[0].task_label == "https://jobs.example/first"
        assert active[0].step == "starting"
        assert active[1].task_label == "Backend Engineer"
        assert active[1].step == "queued"
        assert workflow.is_busy()
    finally:
        release.set()

    assert second_started.wait(timeout=1)
    _wait_for_completion(workflow, first.import_id)
    _wait_for_completion(workflow, second.import_id)
    assert workflow.read_active_runs() == []
    assert not workflow.is_busy()
