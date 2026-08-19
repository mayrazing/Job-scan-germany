from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from job_scan.ats_models import (
    AtsCheckBundle,
    AtsJobAssessment,
    AtsJobResult,
    AtsResumeAssessment,
    AtsResumeFinding,
)


def test_completed_bundle_keeps_resume_check_and_ordered_job_snapshots() -> None:
    resume = AtsResumeAssessment(
        readiness_score=88,
        verdict="needs_attention",
        title="Resume content is readable with minor gaps",
        summary="Core content was extracted.",
        findings=[
            AtsResumeFinding(
                label="Text extraction",
                status="pass",
                detail="Selectable text was extracted.",
            )
        ],
    )
    assessment = AtsJobAssessment(
        job_key="job-1",
        match_score=81,
        match_label="strong",
        required_skills_score=84,
        experience_score=82,
        keyword_score=73,
        matched=["Python backend delivery"],
        needs_attention=["Kubernetes is not shown"],
        suggestions=["Add Kubernetes only if it is real experience."],
    )
    bundle = AtsCheckBundle(
        run_id="ats-1",
        search_run_id="search-1",
        candidate_name="Ada",
        resume_filename="Ada CV.pdf",
        started_at=datetime(2026, 8, 9, 10, tzinfo=UTC),
        finished_at=datetime(2026, 8, 9, 10, 1, tzinfo=UTC),
        ai_runtime="claude-code",
        ai_model="sonnet",
        resume=resume,
        jobs=[
            AtsJobResult(
                job_key="job-1",
                title="Backend Engineer",
                company="Example GmbH",
                location="Berlin",
                url="https://example.test/jobs/1",
                content_hash="sha256:job-1",
                assessment=assessment,
            )
        ],
    )

    assert bundle.jobs[0].assessment == assessment
    assert bundle.failed_job_count == 0


def test_ats_scores_reject_values_outside_zero_to_one_hundred() -> None:
    with pytest.raises(ValidationError):
        AtsJobAssessment(
            job_key="job-1",
            match_score=101,
            match_label="strong",
            required_skills_score=80,
            experience_score=80,
            keyword_score=80,
            matched=[],
            needs_attention=[],
            suggestions=[],
        )
