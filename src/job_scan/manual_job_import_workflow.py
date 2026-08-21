from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock, Thread
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from job_scan.domain import UserStatus

ManualImportProgress = Callable[[str, str], None]
ManualImportStep = str


class ManualImportBusy(RuntimeError):
    """Report that one manual import already owns the workflow."""


@dataclass(frozen=True)
class ManualImportResult:
    """Return the values needed by UI and upsert calls."""

    job_key: str
    result_status: UserStatus
    resume_id: str | None = None


class ManualImportState(BaseModel):
    """Expose one manual-import workflow state for UI polling."""

    model_config = ConfigDict(extra="forbid")

    import_id: str
    status: Literal["running", "complete", "failed"]
    step: ManualImportStep
    message: str
    progress_percent: float = Field(ge=0, le=100)
    job_key: str | None = None
    result_status: UserStatus | None = None
    resume_id: str | None = None
    error: str | None = None


_RUN_STEPS: dict[str, float] = {
    "queued": 2,
    "validate": 10,
    "read-page": 25,
    "extract": 45,
    "review": 65,
    "save": 85,
    "complete": 100,
    "failed": 100,
}


def _safe_status_message(
    step: str,
    message: str | None,
) -> tuple[str, str]:
    if not message:
        return step, "Manual import still running."
    return step, message


class ManualJobImportWorkflow:
    """Run one background manual-import flow and publish in-memory progress."""

    def __init__(self) -> None:
        self._run_lock = Lock()
        self._state_lock = Lock()
        self._runs: dict[str, ManualImportState] = {}

    def start(
        self,
        run: Callable[[ManualImportProgress], ManualImportResult],
    ) -> ManualImportState:
        """Start one manual import run and return its first visible state."""
        if not self._run_lock.acquire(blocking=False):
            raise ManualImportBusy("A manual job import is already running.")
        run_id = str(uuid.uuid4())
        initial = ManualImportState(
            import_id=run_id,
            status="running",
            step="queued",
            message="Preparing manual import...",
            progress_percent=_RUN_STEPS["queued"],
        )
        with self._state_lock:
            self._runs[run_id] = initial
        thread = Thread(
            target=self._run,
            args=(run_id, run),
            name=f"job-scan-manual-import-{run_id}",
            daemon=True,
        )
        try:
            thread.start()
        except BaseException:
            self._run_lock.release()
            raise
        return initial.model_copy(deep=True)

    def read_run(self, import_id: str) -> ManualImportState | None:
        """Return the latest in-memory state snapshot for one import."""
        with self._state_lock:
            state = self._runs.get(import_id)
            return state.model_copy(deep=True) if state is not None else None

    def _set_state(self, import_id: str, state: ManualImportState) -> None:
        """Publish one immutable snapshot."""
        with self._state_lock:
            self._runs[import_id] = state

    def _record_progress(
        self,
        import_id: str,
        step: str,
        message: str,
    ) -> None:
        """Update one running state if the import is still active."""
        with self._state_lock:
            current = self._runs.get(import_id)
            if current is None or current.status != "running":
                return
            stage, safe_message = _safe_status_message(step, message)
            self._runs[import_id] = current.model_copy(
                update={
                    "step": stage,
                    "message": safe_message,
                    "progress_percent": _RUN_STEPS.get(stage, current.progress_percent),
                },
                deep=True,
            )

    def _set_complete(
        self,
        import_id: str,
        result: ManualImportResult,
    ) -> None:
        """Set one terminal success snapshot."""
        with self._state_lock:
            current = self._runs.get(import_id)
            if current is None or current.status != "running":
                return
            self._runs[import_id] = current.model_copy(
                update={
                    "status": "complete",
                    "step": "complete",
                    "message": "Manual import complete.",
                    "progress_percent": 100,
                    "job_key": result.job_key,
                    "result_status": result.result_status,
                    "resume_id": result.resume_id,
                    "error": None,
                },
                deep=True,
            )

    def _set_failed(
        self,
        import_id: str,
        message: str,
    ) -> None:
        """Set one terminal failed snapshot."""
        with self._state_lock:
            current = self._runs.get(import_id)
            if current is None or current.status != "running":
                return
            self._runs[import_id] = current.model_copy(
                update={
                    "status": "failed",
                    "step": "failed",
                    "message": message or "Manual import failed.",
                    "progress_percent": _RUN_STEPS["failed"],
                    "error": message,
                },
                deep=True,
            )

    def _run(
        self,
        import_id: str,
        run: Callable[[ManualImportProgress], ManualImportResult],
    ) -> None:
        """Call one importer under isolation and record terminal state."""
        try:
            result = run(
                lambda step, message: self._record_progress(import_id, step, message),
            )
            self._set_complete(import_id, result)
        except BaseException as error:  # noqa: BLE001 - background worker must report terminal state
            self._set_failed(import_id, str(error) if str(error) else "Manual import failed.")
        finally:
            self._run_lock.release()
