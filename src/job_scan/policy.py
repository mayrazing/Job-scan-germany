from __future__ import annotations

from datetime import datetime

from job_scan.config import STAFFING_AGENCY_PENALTY, AppConfig
from job_scan.domain import (
    AIReview,
    CompanyIndustryEvidence,
    JobRecord,
    MachineStatus,
    ReviewHistoryEntry,
)
from job_scan.reviewer import ReviewFailure

_POSSIBLE_DUPLICATE_LABEL = "Possible duplicate"
_MIN_RECOMMENDED_SCORE = 60


def apply_review(
    job: JobRecord,
    review: AIReview,
    config: AppConfig,
    profile_hash: str,
    now: datetime,
) -> JobRecord:
    """Apply one valid semantic review and record its successful attempt."""
    _clear_stale_manual_override(job, profile_hash)
    _apply_review_facts(job, review, config)
    _apply_company_industry(job, review, now)
    job.ai_review = review
    job.review_model = config.selected_model
    job.reviewed_at = now

    job.last_review_attempt_content_hash = job.content_hash
    job.last_review_attempt_profile_hash = profile_hash
    job.last_review_attempt_at = now
    job.last_successful_review_content_hash = job.content_hash
    job.last_successful_review_profile_hash = profile_hash
    job.last_error = None
    job.review_history.append(
        ReviewHistoryEntry(
            attempted_at=now,
            content_hash=job.content_hash,
            profile_hash=profile_hash,
            model=config.selected_model,
            outcome="accepted",
            review=review,
        )
    )
    return job


def refresh_review_decision(job: JobRecord, config: AppConfig) -> JobRecord:
    """Reapply current local policy to one saved AI review without calling AI."""
    if job.ai_review is not None:
        _apply_review_facts(job, job.ai_review, config)
    return job


def review_decision(
    review: AIReview,
    config: AppConfig,
) -> tuple[MachineStatus, list[str]]:
    """Return the deterministic status for one AI review."""
    exclusion_reasons = _exclusion_reasons(review)
    if exclusion_reasons:
        return MachineStatus.EXCLUDED, exclusion_reasons

    score_reasons = _score_reasons(review, config)
    if score_reasons:
        return MachineStatus.EXCLUDED, score_reasons

    critical_uncertainty = any(
        (
            review.visa_sponsorship == "uncertain",
            review.existing_work_authorization == "uncertain",
            review.citizenship_requirement == "uncertain",
        )
    )
    if critical_uncertainty:
        return MachineStatus.UNCERTAIN, []
    return MachineStatus.ELIGIBLE, []


def _apply_review_facts(
    job: JobRecord,
    review: AIReview,
    config: AppConfig,
) -> None:
    """Copy policy-derived fields from one accepted semantic review onto a job."""
    job.machine_status, job.exclusion_reasons = review_decision(review, config)
    job.labels = _retained_labels(job) + _review_labels(review)
    penalty = STAFFING_AGENCY_PENALTY if review.staffing_agency == "yes" else 0
    job.score = max(0, review.score - penalty)
    job.reason = review.reason


def _apply_company_industry(
    job: JobRecord,
    review: AIReview,
    now: datetime,
) -> None:
    """Persist JD-grounded AI industry only when no source industry exists."""
    if job.company_industry is not None:
        return
    if review.company_industry is None:
        return
    job.company_industry = CompanyIndustryEvidence(
        company_name=job.company,
        industry=review.company_industry,
        source_url=job.url,
        source_title="AI inference from complete job description",
        checked_at=now,
        confidence=review.company_industry_confidence,
        lookup_method="ai",
        source_name="ai",
        evidence=review.company_industry_evidence,
    )


def apply_review_failure(
    job: JobRecord,
    failure: ReviewFailure,
    profile_hash: str,
    now: datetime,
) -> JobRecord:
    """Apply one final failed scan-run outcome without scheduling another attempt."""
    _clear_stale_manual_override(job, profile_hash)

    job.machine_status = MachineStatus.PENDING
    job.ai_review = None
    job.score = None
    job.reason = ""
    job.review_model = None
    job.reviewed_at = None
    job.exclusion_reasons = []
    job.labels = _retained_labels(job)
    job.last_review_attempt_content_hash = job.content_hash
    job.last_review_attempt_profile_hash = profile_hash
    job.last_review_attempt_at = now
    job.last_error = failure.message

    job.review_history.append(
        ReviewHistoryEntry(
            attempted_at=now,
            content_hash=job.content_hash,
            profile_hash=profile_hash,
            model=failure.model,
            outcome="failed",
            failure_category=failure.category,
        )
    )
    return job


def _clear_stale_manual_override(job: JobRecord, profile_hash: str) -> None:
    """Clear an override whose captured content or profile no longer matches."""
    if (
        job.manual_override_content_hash == job.content_hash
        and job.manual_override_profile_hash == profile_hash
    ):
        return
    job.manual_override = None
    job.manual_override_content_hash = None
    job.manual_override_profile_hash = None


def _exclusion_reasons(review: AIReview) -> list[str]:
    """Return every deterministic hard-exclusion reason in policy order."""
    reasons: list[str] = []
    if review.visa_sponsorship == "not_offered":
        reasons.append("no_visa_sponsorship")
    if review.existing_work_authorization == "required":
        reasons.append("work_authorization_required")
    if review.citizenship_requirement == "german_or_eu":
        reasons.append("citizenship_required")
    return reasons


def _score_reasons(review: AIReview, config: AppConfig) -> list[str]:
    """Reject reviews whose adjusted technical score is below the threshold."""
    reasons: list[str] = []
    penalty = STAFFING_AGENCY_PENALTY if review.staffing_agency == "yes" else 0
    if max(0, review.score - penalty) < _MIN_RECOMMENDED_SCORE:
        reasons.append("score_below_minimum")
    return reasons


def _review_labels(review: AIReview) -> list[str]:
    """Return visible labels derived from accepted semantic facts."""
    labels: list[str] = []
    if review.visa_sponsorship == "offered":
        labels.append("Visa support")
    elif review.visa_sponsorship == "not_mentioned":
        labels.append("Visa details to verify")
    if review.existing_work_authorization == "not_mentioned":
        labels.append("Work authorization to verify")
    if review.security_clearance == "required":
        labels.append("Security clearance")
    if review.staffing_agency == "yes":
        labels.append("Recruiter")
    if review.german_requirement == "required":
        labels.append("German required")
    return labels


def _retained_labels(job: JobRecord) -> list[str]:
    """Keep duplicate evidence labels owned by canonical deduplication."""
    return [label for label in job.labels if label == _POSSIBLE_DUPLICATE_LABEL]
