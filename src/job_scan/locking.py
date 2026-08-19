import errno
import os
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised through injected absence
    fcntl = None  # type: ignore[assignment]


class LockUnavailable(RuntimeError):
    """Report that a non-blocking file lock could not be acquired."""


class FileRWLock:
    """Coordinate readers and writers through one POSIX lock file."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def shared(self, blocking: bool = True) -> AbstractContextManager[None]:
        """Acquire a shared lock until the returned context exits."""
        return self._acquire("shared", blocking)

    def exclusive(self, blocking: bool = True) -> AbstractContextManager[None]:
        """Acquire an exclusive lock until the returned context exits."""
        return self._acquire("exclusive", blocking)

    @contextmanager
    def _acquire(self, kind: str, blocking: bool) -> Iterator[None]:
        if fcntl is None:
            raise RuntimeError("job-scan requires POSIX flock")

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        operation = fcntl.LOCK_SH if kind == "shared" else fcntl.LOCK_EX
        if not blocking:
            operation |= fcntl.LOCK_NB

        try:
            try:
                fcntl.flock(fd, operation)
            except OSError as exc:
                if not blocking and exc.errno in {errno.EACCES, errno.EAGAIN}:
                    raise LockUnavailable(str(self.path)) from exc
                raise

            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
