from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AtsResumeFinding(_FrozenModel):
    label: str = Field(min_length=1, max_length=120)
    status: Literal["pass", "warning"]
    detail: str = Field(min_length=1, max_length=500)


class AtsResumeAssessment(_FrozenModel):
    readiness_score: int = Field(ge=0, le=100)
    verdict: Literal["ready", "needs_attention"]
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=1000)
    findings: list[AtsResumeFinding] = Field(min_length=1, max_length=12)


class AtsJobAssessment(_FrozenModel):
    job_key: str = Field(min_length=1, max_length=500)
    match_score: int = Field(ge=0, le=100)
    match_label: Literal["strong", "possible", "weak"]
    required_skills_score: int = Field(ge=0, le=100)
    experience_score: int = Field(ge=0, le=100)
    keyword_score: int = Field(ge=0, le=100)
    matched: list[str] = Field(max_length=12)
    needs_attention: list[str] = Field(max_length=12)
    suggestions: list[str] = Field(max_length=12)


class AtsFailure(_FrozenModel):
    category: Literal["process", "timeout", "output_limit", "json", "schema"]
    message: str = Field(min_length=1, max_length=300)


class AtsJobResult(_FrozenModel):
    job_key: str
    title: str
    company: str
    location: str
    url: str
    content_hash: str
    assessment: AtsJobAssessment | None = None
    failure: AtsFailure | None = None

    @model_validator(mode="after")
    def require_exactly_one_outcome(self) -> AtsJobResult:
        if (self.assessment is None) == (self.failure is None):
            raise ValueError("job result requires exactly one assessment or failure")
        if self.assessment is not None and self.assessment.job_key != self.job_key:
            raise ValueError("assessment job key must match result job key")
        return self


class AtsCheckBundle(_FrozenModel):
    run_id: str = Field(min_length=1, max_length=100)
    search_run_id: str = Field(min_length=1, max_length=100)
    candidate_name: str = Field(min_length=1, max_length=200)
    resume_filename: str = Field(min_length=1, max_length=255)
    started_at: datetime
    finished_at: datetime
    ai_runtime: str
    ai_model: str
    resume: AtsResumeAssessment
    jobs: list[AtsJobResult]

    @property
    def failed_job_count(self) -> int:
        return sum(item.failure is not None for item in self.jobs)


class AtsHistoryEntry(_FrozenModel):
    run_id: str
    search_run_id: str
    candidate_name: str
    resume_filename: str
    finished_at: datetime
    readiness_score: int = Field(ge=0, le=100)
    job_count: int = Field(ge=0)
    failed_job_count: int = Field(ge=0)


class AtsTaskState(_FrozenModel):
    task_id: str
    kind: Literal["resume", "job"]
    label: str
    status: Literal["waiting", "running", "complete", "failed", "skipped"]
    message: str


class AtsRunState(_FrozenModel):
    run_id: str
    search_run_id: str
    status: Literal["running", "complete", "failed"]
    stage: Literal["resume", "jobs", "archive"]
    message: str
    progress_percent: float = Field(ge=0, le=100)
    tasks: list[AtsTaskState]
    error: str | None = None
