from __future__ import annotations

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, TypeVar

from pydantic import BaseModel, ValidationError

from job_scan.ai_runtime import AiInvoker
from job_scan.ats_models import (
    AtsCheckBundle,
    AtsFailure,
    AtsJobAssessment,
    AtsJobResult,
    AtsResumeAssessment,
    AtsResumeFinding,
)
from job_scan.claude_process import (
    ClaudeOutputLimitExceeded,
    ClaudeProcessError,
    ClaudeRequest,
    ClaudeTimeout,
)
from job_scan.config import AppConfig
from job_scan.domain import JobRecord
from job_scan.prompts import build_ats_job_prompt, build_ats_resume_prompt
from job_scan.resume import ExtractedResume, ResumeError, extract_resume

StructuredModel = TypeVar("StructuredModel", bound=BaseModel)


@dataclass(frozen=True)
class AtsCheckInput:
    run_id: str
    search_run_id: str
    resume_id: str
    candidate_name: str
    resume_filename: str
    resume_bytes: bytes
    jobs: tuple[JobRecord, ...]


@dataclass(frozen=True)
class AtsProgressUpdate:
    task_id: str
    status: Literal["running", "complete", "failed"]
    message: str


class AtsCheckError(RuntimeError):
    """Report a safe run-level ATS failure."""


class AtsCheckService:
    """Run one resume assessment before isolated resume-to-job assessments."""

    def __init__(
        self,
        invoker: AiInvoker,
        *,
        resume_extractor: Callable[[Path], ExtractedResume] = extract_resume,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._invoker = invoker
        self._extract_resume = resume_extractor
        self._clock = clock

    def check(
        self,
        inputs: AtsCheckInput,
        config: AppConfig,
        progress: Callable[[AtsProgressUpdate], None] | None = None,
        *,
        previous: AtsCheckBundle | None = None,
    ) -> AtsCheckBundle:
        """Return one complete ATS bundle after all requested checks settle."""
        started_at = self._clock()
        previous_jobs = {item.job_key: item for item in previous.jobs} if previous else {}
        jobs_to_check = tuple(
            job
            for job in inputs.jobs
            if (
                job.canonical_job_key not in previous_jobs
                or previous_jobs[job.canonical_job_key].content_hash != job.content_hash
            )
        )
        extracted = (
            self._extract(inputs.resume_filename, inputs.resume_bytes)
            if previous is None or jobs_to_check
            else None
        )
        if previous is None:
            assert extracted is not None
            self._emit(progress, "resume", "running", "Checking resume content readiness...")
            resume = self._invoke_resume(extracted.text, config)
            resume = AtsResumeAssessment.model_validate(
                {
                    **resume.model_dump(),
                    "findings": [
                        AtsResumeFinding(
                            label="Text extraction",
                            status="pass",
                            detail="Selectable resume text was extracted.",
                        ),
                        *[
                            item
                            for item in resume.findings
                            if item.label != "Text extraction"
                        ][:11],
                    ],
                }
            )
            self._emit(progress, "resume", "complete", "Resume check complete.")
        else:
            resume = previous.resume
            self._emit(progress, "resume", "complete", "Reused existing resume check.")
        for job in inputs.jobs:
            cached = previous_jobs.get(job.canonical_job_key)
            if cached is not None and cached.content_hash == job.content_hash:
                self._emit(
                    progress,
                    job.canonical_job_key,
                    "complete",
                    "Reused existing job check.",
                )
        checked_jobs = self._check_jobs(
            jobs_to_check,
            extracted.text if extracted is not None else "",
            config,
            progress,
        )
        checked_by_key = {item.job_key: item for item in checked_jobs}
        jobs = [
            checked_by_key.get(item.job_key, item)
            for item in (previous.jobs if previous else [])
        ]
        previous_keys = {item.job_key for item in jobs}
        jobs.extend(
            checked_by_key[job.canonical_job_key]
            for job in inputs.jobs
            if job.canonical_job_key not in previous_keys
        )
        return AtsCheckBundle(
            run_id=inputs.run_id,
            search_run_id=inputs.search_run_id,
            resume_id=inputs.resume_id,
            candidate_name=inputs.candidate_name,
            resume_filename=inputs.resume_filename,
            started_at=started_at,
            finished_at=self._clock(),
            ai_runtime=config.ai_runtime,
            ai_model=config.selected_model,
            resume=resume,
            jobs=jobs,
        )

    def _extract(self, filename: str, resume_bytes: bytes) -> ExtractedResume:
        """Extract resume text from an isolated temporary copy."""
        suffix = Path(filename).suffix.lower()
        if suffix not in {".pdf", ".docx"}:
            suffix = ""
        try:
            with TemporaryDirectory(prefix="job-scan-ats-") as directory:
                path = Path(directory) / f"resume{suffix}"
                path.write_bytes(resume_bytes)
                return self._extract_resume(path)
        except (ResumeError, OSError):
            raise AtsCheckError("Could not read the archived resume.") from None

    def _invoke_resume(self, resume_text: str, config: AppConfig) -> AtsResumeAssessment:
        """Return one validated resume assessment or raise one safe run failure."""
        outcome = self._invoke_structured(
            build_ats_resume_prompt(resume_text),
            AtsResumeAssessment,
            config,
        )
        if isinstance(outcome, AtsFailure):
            messages = {
                "timeout": "AI resume check timed out.",
                "output_limit": "AI resume check exceeded the configured output limit.",
                "process": "AI resume check failed.",
                "json": "AI resume check returned invalid output.",
                "schema": "AI resume check returned invalid output.",
            }
            raise AtsCheckError(messages[outcome.category])
        return outcome

    def _invoke_job(
        self,
        resume_text: str,
        job: JobRecord,
        config: AppConfig,
    ) -> AtsJobAssessment | AtsFailure:
        """Return one validated job assessment or one safe terminal failure."""
        return self._invoke_structured(
            build_ats_job_prompt(resume_text, job),
            AtsJobAssessment,
            config,
            expected_job_key=job.canonical_job_key,
        )

    def _invoke_structured(
        self,
        prompt: str,
        schema: type[StructuredModel],
        config: AppConfig,
        *,
        expected_job_key: str | None = None,
    ) -> StructuredModel | AtsFailure:
        """Retry one private structured AI request once and return safe failures."""
        request = ClaudeRequest(
            runtime=config.ai_runtime,
            prompt=prompt,
            json_schema=schema.model_json_schema(),
            model=config.claude.model,
            runtime_model=config.ai_model,
            effort=config.claude.effort,
            thinking_enabled=config.claude.thinking_enabled,
            timeout_seconds=config.claude.timeout_seconds,
            max_output_bytes=config.claude.max_output_bytes,
        )
        failure: AtsFailure | None = None
        for _attempt in range(2):
            try:
                invocation = self._invoker.invoke(request)
            except ClaudeTimeout:
                failure = AtsFailure(category="timeout", message="AI check timed out.")
                continue
            except ClaudeOutputLimitExceeded:
                failure = AtsFailure(
                    category="output_limit",
                    message="AI check exceeded the configured output limit.",
                )
                continue
            except ClaudeProcessError:
                failure = AtsFailure(
                    category="process",
                    message="AI check process failed.",
                )
                continue

            if invocation.exit_code != 0:
                failure = AtsFailure(
                    category="process",
                    message="AI check process exited unsuccessfully.",
                )
                continue
            try:
                envelope = json.loads(invocation.stdout)
            except (UnicodeDecodeError, json.JSONDecodeError):
                failure = AtsFailure(category="json", message="AI check returned invalid JSON.")
                continue
            structured = (
                envelope.get("structured_output") if isinstance(envelope, dict) else None
            )
            if not isinstance(structured, dict):
                failure = AtsFailure(
                    category="schema",
                    message="AI check returned invalid structured output.",
                )
                continue
            try:
                validated = schema.model_validate(structured)
            except ValidationError:
                failure = AtsFailure(
                    category="schema",
                    message="AI check returned invalid structured output.",
                )
                continue
            if expected_job_key is not None and (
                not isinstance(validated, AtsJobAssessment)
                or validated.job_key != expected_job_key
            ):
                failure = AtsFailure(
                    category="schema",
                    message="AI check returned a result for the wrong job.",
                )
                continue
            return validated

        if failure is None:
            raise AssertionError("ATS retry ended without an outcome")
        return failure

    def _check_jobs(
        self,
        jobs: tuple[JobRecord, ...],
        resume_text: str,
        config: AppConfig,
        progress: Callable[[AtsProgressUpdate], None] | None,
    ) -> list[AtsJobResult]:
        """Check up to three jobs concurrently and restore submitted order."""
        if not jobs:
            return []
        results: dict[str, AtsJobResult] = {}
        with ThreadPoolExecutor(
            max_workers=min(3, len(jobs)),
            thread_name_prefix="job-scan-ats",
        ) as executor:
            futures = {
                executor.submit(
                    self._check_one_job,
                    job,
                    resume_text,
                    config,
                    progress,
                ): job
                for job in jobs
            }
            for future in as_completed(futures):
                job = futures[future]
                results[job.canonical_job_key] = future.result()
        return [results[job.canonical_job_key] for job in jobs]

    def _check_one_job(
        self,
        job: JobRecord,
        resume_text: str,
        config: AppConfig,
        progress: Callable[[AtsProgressUpdate], None] | None,
    ) -> AtsJobResult:
        """Convert one isolated AI outcome to a server-owned job snapshot."""
        key = job.canonical_job_key
        self._emit(progress, key, "running", "Checking job match...")
        outcome = self._invoke_job(resume_text, job, config)

        if isinstance(outcome, AtsFailure):
            self._emit(progress, key, "failed", outcome.message)
            return AtsJobResult(
                job_key=key,
                title=job.title,
                company=job.company,
                location=job.location,
                url=str(job.url),
                content_hash=job.content_hash,
                failure=outcome,
            )
        self._emit(progress, key, "complete", "Job check complete.")
        return AtsJobResult(
            job_key=key,
            title=job.title,
            company=job.company,
            location=job.location,
            url=str(job.url),
            content_hash=job.content_hash,
            assessment=outcome,
        )

    @staticmethod
    def _emit(
        progress: Callable[[AtsProgressUpdate], None] | None,
        task_id: str,
        status: Literal["running", "complete", "failed"],
        message: str,
    ) -> None:
        """Send one progress event when a callback was supplied."""
        if progress is not None:
            progress(AtsProgressUpdate(task_id=task_id, status=status, message=message))
