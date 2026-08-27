from __future__ import annotations

import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock, Thread
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from job_scan.domain import UserStatus

ManualImportProgress = Callable[[str, str], None]
ManualImportStep = str
ManualTaskKind = Literal["add-job", "re-evaluate"]


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
    task_kind: ManualTaskKind = "add-job"
    task_label: str = "Add job from URL"
    subject_key: str | None = None
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
    "starting": 5,
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

    def __init__(self, *, max_concurrent: int = 3) -> None:
        if max_concurrent <= 0:
            raise ValueError("max_concurrent must be greater than zero")
        self._max_concurrent = max_concurrent
        self._state_lock = Lock()
        self._runs: dict[str, ManualImportState] = {}
        self._active_task_keys: dict[str, str] = {}
        self._task_keys_by_run: dict[str, str] = {}
        self._pending: deque[
            tuple[str, Callable[[ManualImportProgress], ManualImportResult]]
        ] = deque()
        self._active_count = 0

    def start(
        self,
        run: Callable[[ManualImportProgress], ManualImportResult],
        *,
        task_kind: ManualTaskKind = "add-job",
        task_label: str = "Add job from URL",
        task_key: str | None = None,
        subject_key: str | None = None,
    ) -> ManualImportState:
        """Start one manual import run and return its first visible state."""
        run_id = str(uuid.uuid4())
        initial = ManualImportState(
            import_id=run_id,
            task_kind=task_kind,
            task_label=task_label,
            subject_key=subject_key,
            status="running",
            step="queued",
            message="Preparing manual import...",
            progress_percent=_RUN_STEPS["queued"],
        )
        with self._state_lock:
            if task_key is not None and task_key in self._active_task_keys:
                raise ManualImportBusy("A task for this item is already running.")
            self._runs[run_id] = initial
            if task_key is not None:
                self._active_task_keys[task_key] = run_id
                self._task_keys_by_run[run_id] = task_key
            self._pending.append((run_id, run))
        self._launch_ready_tasks(propagate_run_id=run_id)
        return initial.model_copy(deep=True)

    def read_run(self, import_id: str) -> ManualImportState | None:
        """Return the latest in-memory state snapshot for one import."""
        with self._state_lock:
            state = self._runs.get(import_id)
            return state.model_copy(deep=True) if state is not None else None

    def read_active_runs(self) -> list[ManualImportState]:
        """Return queued and running task snapshots in submission order."""
        with self._state_lock:
            return [
                state.model_copy(deep=True)
                for state in self._runs.values()
                if state.status == "running"
            ]

    def is_busy(self) -> bool:
        """Return whether any manual task is queued or running."""
        with self._state_lock:
            return any(state.status == "running" for state in self._runs.values())

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
        self._record_progress(import_id, "starting", "Preparing manual import...")
        try:
            result = run(
                lambda step, message: self._record_progress(import_id, step, message),
            )
            self._set_complete(import_id, result)
        except BaseException as error:  # noqa: BLE001 - background worker must report terminal state
            self._set_failed(
                import_id,
                str(error) if str(error) else "Manual import failed.",
            )
        finally:
            with self._state_lock:
                self._active_count -= 1
                self._release_task_key_locked(import_id)
            self._launch_ready_tasks()

    def _release_task_key_locked(self, import_id: str) -> None:
        """Release one caller-owned deduplication key while the state lock is held."""
        active_key = self._task_keys_by_run.pop(import_id, None)
        if active_key is not None:
            self._active_task_keys.pop(active_key, None)

    def _claim_ready_tasks(
        self,
    ) -> list[tuple[str, Callable[[ManualImportProgress], ManualImportResult]]]:
        """Claim queued work in submission order without exceeding the active limit."""
        with self._state_lock:
            claimed = []
            while self._pending and self._active_count < self._max_concurrent:
                claimed.append(self._pending.popleft())
                self._active_count += 1
            return claimed

    def _launch_ready_tasks(self, *, propagate_run_id: str | None = None) -> None:
        """Start only claimed work, leaving excess submissions in the FIFO queue."""
        propagated_error: RuntimeError | None = None
        while True:
            claimed = self._claim_ready_tasks()
            if not claimed:
                break
            start_failed = False
            for import_id, run in claimed:
                thread = Thread(
                    target=self._run,
                    args=(import_id, run),
                    name=f"job-scan-manual-import-{import_id}",
                    daemon=True,
                )
                try:
                    thread.start()
                except RuntimeError as error:
                    start_failed = True
                    with self._state_lock:
                        self._active_count -= 1
                        if import_id == propagate_run_id:
                            self._runs.pop(import_id, None)
                        else:
                            current = self._runs.get(import_id)
                            if current is not None:
                                self._runs[import_id] = current.model_copy(
                                    update={
                                        "status": "failed",
                                        "step": "failed",
                                        "message": str(error) or "Manual import failed.",
                                        "progress_percent": 100,
                                        "error": str(error),
                                    },
                                    deep=True,
                                )
                        self._release_task_key_locked(import_id)
                    if import_id == propagate_run_id:
                        propagated_error = error
            if not start_failed:
                break
        if propagated_error is not None:
            raise propagated_error
