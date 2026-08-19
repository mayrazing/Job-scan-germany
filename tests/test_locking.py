import errno
import multiprocessing
import os
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

import pytest

from job_scan import locking
from job_scan.locking import FileRWLock, LockUnavailable

WAIT_SECONDS = 5.0


def _hold_lock(
    path: str,
    kind: Literal["shared", "exclusive"],
    ready: Any,
    release: Any,
) -> None:
    try:
        lock = FileRWLock(Path(path))
        acquire = lock.shared if kind == "shared" else lock.exclusive
        with acquire():
            ready.put(("ready", None))
            if not release.wait(WAIT_SECONDS):
                raise TimeoutError("parent did not release child lock")
    except BaseException as exc:
        ready.put(("error", repr(exc)))
        raise


@contextmanager
def _child_holding(
    path: Path, kind: Literal["shared", "exclusive"]
) -> Iterator[Any]:
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    release = context.Event()
    process = context.Process(target=_hold_lock, args=(str(path), kind, ready, release))
    process.start()
    body_failed = False
    try:
        status, detail = ready.get(timeout=WAIT_SECONDS)
        assert status == "ready", detail
        yield release
    except BaseException:
        body_failed = True
        raise
    finally:
        release.set()
        process.join(timeout=WAIT_SECONDS)
        was_alive = process.is_alive()
        if was_alive:
            process.terminate()
            process.join(timeout=WAIT_SECONDS)
        ready.close()
        ready.join_thread()
        if not body_failed:
            assert not was_alive, "child lock holder did not stop"
            assert process.exitcode == 0


def test_exclusive_holder_blocks_shared_holder_until_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / "data.lock"
    nonblocking_finished = threading.Event()
    nonblocking_errors: list[BaseException] = []
    blocking_flock_started = threading.Event()
    acquired = threading.Event()
    errors: list[BaseException] = []

    def try_shared_nonblocking() -> None:
        try:
            with FileRWLock(lock_path).shared(blocking=False):
                pass
        except BaseException as exc:  # noqa: BLE001 - relay every thread failure
            nonblocking_errors.append(exc)
        finally:
            nonblocking_finished.set()

    def acquire_shared() -> None:
        try:
            with FileRWLock(lock_path).shared():
                acquired.set()
        except BaseException as exc:  # noqa: BLE001 - relay every thread failure
            errors.append(exc)

    with _child_holding(lock_path, "exclusive") as release:
        nonblocking_thread = threading.Thread(
            target=try_shared_nonblocking, daemon=True
        )
        blocking_thread = threading.Thread(target=acquire_shared, daemon=True)
        try:
            nonblocking_thread.start()
            assert nonblocking_finished.wait(
                WAIT_SECONDS
            ), "non-blocking shared lock attempt blocked"
            nonblocking_thread.join(timeout=WAIT_SECONDS)
            assert not nonblocking_thread.is_alive()
            assert len(nonblocking_errors) == 1
            assert isinstance(nonblocking_errors[0], LockUnavailable)

            real_flock = locking.fcntl.flock

            def observed_flock(fd: int, operation: int) -> None:
                if operation == locking.fcntl.LOCK_SH:
                    blocking_flock_started.set()
                real_flock(fd, operation)

            monkeypatch.setattr(locking.fcntl, "flock", observed_flock)
            blocking_thread.start()
            assert blocking_flock_started.wait(WAIT_SECONDS)
            assert not acquired.is_set()

            release.set()
            assert acquired.wait(WAIT_SECONDS)
        finally:
            release.set()
            nonblocking_thread.join(timeout=WAIT_SECONDS)
            if blocking_thread.ident is not None:
                blocking_thread.join(timeout=WAIT_SECONDS)

    assert not nonblocking_thread.is_alive()
    assert not blocking_thread.is_alive()
    assert errors == []


def test_shared_holders_can_coexist_across_processes(tmp_path: Path) -> None:
    lock_path = tmp_path / "data.lock"
    acquired = threading.Event()
    errors: list[BaseException] = []

    def acquire_shared() -> None:
        try:
            with FileRWLock(lock_path).shared():
                acquired.set()
        except BaseException as exc:  # noqa: BLE001 - relay every thread failure
            errors.append(exc)

    with _child_holding(lock_path, "shared"):
        thread = threading.Thread(target=acquire_shared, daemon=True)
        thread.start()
        assert acquired.wait(WAIT_SECONDS)
        thread.join(timeout=WAIT_SECONDS)

    assert not thread.is_alive()
    assert errors == []


def test_nonblocking_exclusive_raises_while_shared_lock_is_held(tmp_path: Path) -> None:
    lock_path = tmp_path / "data.lock"
    finished = threading.Event()
    errors: list[BaseException] = []

    def try_exclusive() -> None:
        try:
            with FileRWLock(lock_path).exclusive(blocking=False):
                pass
        except BaseException as exc:  # noqa: BLE001 - relay every thread failure
            errors.append(exc)
        finally:
            finished.set()

    with _child_holding(lock_path, "shared"):
        thread = threading.Thread(target=try_exclusive, daemon=True)
        thread.start()
        assert finished.wait(WAIT_SECONDS), "non-blocking lock attempt blocked"
        thread.join(timeout=WAIT_SECONDS)

    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], LockUnavailable)


def test_context_exit_unlocks_and_closes_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / "data.lock"
    opened_fds: list[int] = []
    flock_operations: list[int] = []
    real_open = os.open
    real_flock = locking.fcntl.flock

    def tracked_open(path: str | bytes | os.PathLike[str], flags: int, mode: int) -> int:
        fd = real_open(path, flags, mode)
        opened_fds.append(fd)
        return fd

    def tracked_flock(fd: int, operation: int) -> None:
        flock_operations.append(operation)
        real_flock(fd, operation)

    monkeypatch.setattr(locking.os, "open", tracked_open)
    monkeypatch.setattr(locking.fcntl, "flock", tracked_flock)

    with FileRWLock(lock_path).shared():
        assert len(opened_fds) == 1

    assert flock_operations == [locking.fcntl.LOCK_SH, locking.fcntl.LOCK_UN]
    with pytest.raises(OSError) as exc_info:
        os.fstat(opened_fds[0])
    assert exc_info.value.errno == errno.EBADF


def test_context_exception_still_releases_lock(tmp_path: Path) -> None:
    lock = FileRWLock(tmp_path / "data.lock")

    with pytest.raises(ValueError, match="test failure"), lock.exclusive():
        raise ValueError("test failure")

    with lock.exclusive(blocking=False):
        pass


@pytest.mark.parametrize("failure_errno", [errno.EACCES, errno.EAGAIN])
def test_failed_nonblocking_acquisition_closes_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_errno: int
) -> None:
    lock_path = tmp_path / "data.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)

    def return_fd(path: str | bytes | os.PathLike[str], flags: int, mode: int) -> int:
        return fd

    def unavailable(fd: int, operation: int) -> None:
        raise OSError(failure_errno, "lock unavailable")

    monkeypatch.setattr(locking.os, "open", return_fd)
    monkeypatch.setattr(locking.fcntl, "flock", unavailable)

    with pytest.raises(LockUnavailable), FileRWLock(lock_path).exclusive(blocking=False):
        pass

    with pytest.raises(OSError) as exc_info:
        os.fstat(fd)
    assert exc_info.value.errno == errno.EBADF


def test_lock_creates_parent_and_keeps_private_lock_file(tmp_path: Path) -> None:
    lock_path = tmp_path / "nested" / "locks" / "data.lock"

    with FileRWLock(lock_path).exclusive():
        assert lock_path.exists()

    assert lock_path.exists()
    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
    assert list(lock_path.parent.iterdir()) == [lock_path]


def test_missing_fcntl_reports_posix_requirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / "nested" / "data.lock"
    monkeypatch.setattr(locking, "fcntl", None)

    with (
        pytest.raises(RuntimeError, match="^job-scan requires POSIX flock$"),
        FileRWLock(lock_path).shared(),
    ):
        pass

    assert not lock_path.parent.exists()
