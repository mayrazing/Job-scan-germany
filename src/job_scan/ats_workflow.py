from __future__ import annotations

import uuid
from dataclasses import dataclass
from threading import Lock, Thread

from job_scan.ats_history import AtsHistoryStore
from job_scan.ats_models import AtsRunState, AtsTaskState
from job_scan.ats_service import (
    AtsCheckError,
    AtsCheckInput,
    AtsCheckService,
    AtsProgressUpdate,
)
from job_scan.config import AppConfig
from job_scan.domain import JobRecord


class AtsWorkflowBusy(RuntimeError):
    """Report that one ATS run already owns the workflow."""


class AtsInvalidJobSelection(ValueError):
    """Report empty or duplicate jobs supplied for an ATS check."""


class AtsInputError(ValueError):
    """Report missing caller-owned ATS input."""


@dataclass(frozen=True)
class AtsResumeInput:
    """Pair one resume content hash with the jobs that use that resume."""

    resume_id: str
    candidate_name: str
    resume_filename: str
    resume_bytes: bytes
    jobs: tuple[JobRecord, ...]


@dataclass(frozen=True)
class AtsWorkflowInput:
    """Hold one caller-owned ATS check input snapshot."""

    search_run_id: str
    resumes: tuple[AtsResumeInput, ...]
    config: AppConfig


def _validate_input(inputs: AtsWorkflowInput) -> None:
    """Reject incomplete caller input before creating an ATS run."""
    if not inputs.search_run_id.strip() or not inputs.resumes:
        raise AtsInputError("ATS check requires a search ID and one or more resumes.")
    resume_ids = tuple(item.resume_id for item in inputs.resumes)
    if len(resume_ids) != len(set(resume_ids)):
        raise AtsInputError("ATS check requires unique resume groups.")
    for item in inputs.resumes:
        if not all(
            value.strip()
            for value in (item.resume_id, item.candidate_name, item.resume_filename)
        ):
            raise AtsInputError("ATS check requires resume identity, name, and filename.")
        if not item.resume_bytes:
            raise AtsInputError("ATS check requires resume content.")
    keys = tuple(
        job.canonical_job_key
        for resume in inputs.resumes
        for job in resume.jobs
    )
    if not keys or len(keys) != len(set(keys)) or any(not resume.jobs for resume in inputs.resumes):
        raise AtsInvalidJobSelection("Select one or more unique active jobs.")


class AtsWorkflow:
    """Run one background ATS check and publish in-memory progress snapshots."""

    def __init__(
        self,
        service: AtsCheckService,
        history: AtsHistoryStore,
    ) -> None:
        self._service = service
        self._history = history
        self._run_lock = Lock()
        self._state_lock = Lock()
        self._runs: dict[str, AtsRunState] = {}

    def start(self, inputs: AtsWorkflowInput) -> AtsRunState:
        """Validate caller input and start one background ATS run."""
        if not self._run_lock.acquire(blocking=False):
            raise AtsWorkflowBusy("An ATS check is already running.")
        try:
            _validate_input(inputs)
            run_id = str(uuid.uuid4())
            initial = AtsRunState(
                run_id=run_id,
                search_run_id=inputs.search_run_id,
                status="running",
                stage="resume",
                message="Preparing the resume content check...",
                progress_percent=0,
                tasks=[
                    task
                    for resume in inputs.resumes
                    for task in (
                        AtsTaskState(
                            task_id=_resume_task_id(resume.resume_id),
                            kind="resume",
                            label=resume.resume_filename,
                            status="waiting",
                            message="Waiting",
                        ),
                        *(
                            AtsTaskState(
                                task_id=job.canonical_job_key,
                                kind="job",
                                label=job.title,
                                status="waiting",
                                message="Waiting",
                            )
                            for job in resume.jobs
                        ),
                    )
                ],
            )
            self._set_state(initial)
            self._start_thread(run_id, inputs)
            return initial.model_copy(deep=True)
        except BaseException:
            self._run_lock.release()
            raise

    def read_run(self, run_id: str) -> AtsRunState | None:
        """Return one detached run-state snapshot."""
        with self._state_lock:
            state = self._runs.get(run_id)
            return state.model_copy(deep=True) if state is not None else None

    def read_current_run(self) -> AtsRunState | None:
        """Return the active ATS run, excluding finished runs."""
        with self._state_lock:
            current = next(
                (state for state in reversed(self._runs.values()) if state.status == "running"),
                None,
            )
            return current.model_copy(deep=True) if current is not None else None

    def is_busy(self) -> bool:
        """Return whether one ATS run currently owns the workflow."""
        return self._run_lock.locked()

    def delete_history(self, run_id: str) -> None:
        """Delete one result while excluding ATS run start and completion."""
        if not self._run_lock.acquire(blocking=False):
            raise AtsWorkflowBusy("An ATS check is already running.")
        try:
            self._history.delete(run_id)
        finally:
            self._run_lock.release()

    def _set_state(self, state: AtsRunState) -> None:
        """Publish one detached state under the state lock."""
        with self._state_lock:
            self._runs[state.run_id] = state.model_copy(deep=True)

    def _start_thread(
        self,
        run_id: str,
        inputs: AtsWorkflowInput,
    ) -> None:
        """Start one daemon worker using caller-provided input."""
        Thread(
            target=self._run,
            args=(run_id, inputs),
            name=f"job-scan-ats-workflow-{run_id}",
            daemon=True,
        ).start()

    def _run(
        self,
        run_id: str,
        inputs: AtsWorkflowInput,
    ) -> None:
        """Check and archive one caller-provided ATS input."""
        try:
            failed_job_count = 0
            result_ids: list[str] = []
            for resume in inputs.resumes:
                previous = self._history.load_for_resume(resume.resume_id)
                record_id = previous.run_id if previous is not None else str(uuid.uuid4())
                def record_group_progress(
                    update: AtsProgressUpdate,
                    resume_id: str = resume.resume_id,
                ) -> None:
                    self._record_progress(
                        run_id,
                        _group_progress(update, resume_id),
                    )

                bundle = self._service.check(
                    AtsCheckInput(
                        run_id=record_id,
                        search_run_id=inputs.search_run_id,
                        resume_id=resume.resume_id,
                        candidate_name=resume.candidate_name,
                        resume_filename=resume.resume_filename,
                        resume_bytes=resume.resume_bytes,
                        jobs=resume.jobs,
                    ),
                    inputs.config,
                    progress=record_group_progress,
                    previous=previous,
                )
                entry = self._history.archive(bundle, resume.resume_bytes)
                result_ids.append(entry.run_id)
                failed_job_count += bundle.failed_job_count
            self._complete_run(run_id, failed_job_count, result_ids)
        except Exception as error:  # noqa: BLE001 - worker errors must settle observable state
            safe_message = str(error) if isinstance(error, AtsCheckError) else "ATS check failed."
            self._fail_run(run_id, safe_message)
        finally:
            self._run_lock.release()

    def _record_progress(self, run_id: str, update: AtsProgressUpdate) -> None:
        """Replace exactly one task and publish its settled-task progress."""
        with self._state_lock:
            current = self._runs.get(run_id)
            if current is None or current.status != "running":
                return
            tasks = list(current.tasks)
            matched_index = next(
                (index for index, task in enumerate(tasks) if task.task_id == update.task_id),
                None,
            )
            if matched_index is None:
                return
            task = tasks[matched_index]
            tasks[matched_index] = task.model_copy(
                update={"status": update.status, "message": update.message},
                deep=True,
            )
            settled = sum(item.status in {"complete", "failed", "skipped"} for item in tasks)
            stage = current.stage
            if task.kind == "job" or (task.kind == "resume" and update.status == "complete"):
                stage = "jobs"
            self._runs[run_id] = current.model_copy(
                update={
                    "stage": stage,
                    "message": update.message,
                    "progress_percent": settled / len(tasks) * 100,
                    "tasks": tasks,
                },
                deep=True,
            )

    def _complete_run(
        self,
        run_id: str,
        failed_job_count: int,
        result_ids: list[str],
    ) -> None:
        """Publish one archived terminal state."""
        message = (
            f"ATS check complete with {failed_job_count} failed jobs."
            if failed_job_count
            else "ATS check complete."
        )
        with self._state_lock:
            current = self._runs.get(run_id)
            if current is None:
                return
            self._runs[run_id] = current.model_copy(
                update={
                    "status": "complete",
                    "stage": "archive",
                    "message": message,
                    "progress_percent": 100,
                    "result_ids": result_ids,
                    "error": None,
                },
                deep=True,
            )

    def _fail_run(self, run_id: str, safe_message: str) -> None:
        """Settle one run-level failure without exposing private inputs."""
        with self._state_lock:
            current = self._runs.get(run_id)
            if current is None:
                return
            tasks: list[AtsTaskState] = []
            for task in current.tasks:
                if task.status in {"complete", "failed", "skipped"}:
                    tasks.append(task)
                elif task.kind == "resume":
                    tasks.append(
                        task.model_copy(
                            update={"status": "failed", "message": safe_message},
                            deep=True,
                        )
                    )
                else:
                    tasks.append(
                        task.model_copy(
                            update={
                                "status": "skipped",
                                "message": "Skipped because the ATS check could not complete.",
                            },
                            deep=True,
                        )
                    )
            self._runs[run_id] = current.model_copy(
                update={
                    "status": "failed",
                    "message": safe_message,
                    "progress_percent": 100,
                    "tasks": tasks,
                    "error": safe_message,
                },
                deep=True,
            )


def _resume_task_id(resume_id: str) -> str:
    """Return one progress-task ID scoped to a resume content hash."""
    return f"resume:{resume_id}"


def _group_progress(update: AtsProgressUpdate, resume_id: str) -> AtsProgressUpdate:
    """Scope the service's resume progress event to its resume group."""
    if update.task_id != "resume":
        return update
    return AtsProgressUpdate(
        task_id=_resume_task_id(resume_id),
        status=update.status,
        message=update.message,
    )
