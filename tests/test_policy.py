from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from job_scan.config import AppConfig, ClaudeSettings, SchedulerSettings
from job_scan.domain import (
    AIReview,
    AvailabilityStatus,
    CompanyIndustryEvidence,
    JobRecord,
    MachineStatus,
    ReviewHistoryEntry,
)
from job_scan.policy import apply_review, apply_review_failure
from job_scan.reviewer import ReviewFailure

NOW = datetime(2026, 8, 3, 12, tzinfo=UTC)
CONTENT_HASH = "sha256:content"
PROFILE_HASH = "sha256:profile"


def config(*, staffing_penalty: int = 10, ai_model: str | None = None) -> AppConfig:
    return AppConfig(
        ai_model=ai_model,
        resume_path=Path("/private/resume.pdf"),
        resume_sha256="sha256:" + ("a" * 64),
        profile_sha256="sha256:" + ("b" * 64),
        search_terms=["backend engineer"],
        locations=["Berlin"],
        german_level="A2",
        staffing_penalty=staffing_penalty,
        claude=ClaudeSettings(
            model="claude-sonnet-4-5",
            effort="high",
        ),
        scheduler=SchedulerSettings(local_time="08:30"),
    )


def job(**updates: Any) -> JobRecord:
    values: dict[str, Any] = {
        "canonical_job_key": "canonical-1",
        "source_occurrences": [],
        "primary_source_occurrence_key": "linkedin:acme:1@1",
        "company": "Acme",
        "title": "Backend Engineer",
        "location": "Berlin",
        "url": "https://example.test/jobs/1",
        "description": "Build reliable Python services.",
        "posted_at": date(2026, 8, 1),
        "content_hash": CONTENT_HASH,
        "first_seen": NOW,
        "last_seen": NOW,
        "availability_status": AvailabilityStatus.ACTIVE,
        "user_status_updated_at": NOW,
    }
    values.update(updates)
    return JobRecord(**values)


def review(**updates: Any) -> AIReview:
    values: dict[str, Any] = {
        "job_key": "canonical-1",
        "german_requirement": "none",
        "visa_sponsorship": "offered",
        "existing_work_authorization": "not_required",
        "citizenship_requirement": "none",
        "security_clearance": "none",
        "staffing_agency": "no",
        "eligibility_evidence": [],
        "company_industry": None,
        "company_industry_confidence": "low",
        "company_industry_evidence": [],
        "score": 80,
        "reason": "Strong backend match",
        "confidence": "high",
    }
    values.update(updates)
    return AIReview(**values)


def test_review_records_the_actual_selected_api_model() -> None:
    result = apply_review(
        job(),
        review(),
        config(ai_model="deepseek-chat"),
        PROFILE_HASH,
        NOW,
    )

    assert result.review_model == "deepseek-chat"
    assert result.review_history[-1].model == "deepseek-chat"


def test_ai_industry_is_saved_when_source_did_not_provide_one() -> None:
    evidence = "We manufacture industrial robots for automotive factories."

    result = apply_review(
        job(description=evidence),
        review(
            company_industry="Industrial Automation",
            company_industry_confidence="medium",
            company_industry_evidence=[evidence],
        ),
        config(),
        PROFILE_HASH,
        NOW,
    )

    assert result.company_industry is not None
    assert result.company_industry.industry == "Industrial Automation"
    assert result.company_industry.lookup_method == "ai"
    assert result.company_industry.source_name == "ai"
    assert result.company_industry.source_url == result.url
    assert result.company_industry.evidence == [evidence]


def test_source_industry_is_not_overwritten_by_ai_review() -> None:
    source_industry = CompanyIndustryEvidence(
        company_name="Acme",
        industry="Software Development",
        source_url="https://example.test/companies/acme",
        source_title="Source company profile",
        checked_at=NOW,
        confidence="high",
        lookup_method="native",
        source_name="linkedin",
        evidence=[],
    )

    result = apply_review(
        job(company_industry=source_industry),
        review(
            company_industry="Industrial Automation",
            company_industry_confidence="medium",
            company_industry_evidence=["Build reliable Python services."],
        ),
        config(),
        PROFILE_HASH,
        NOW,
    )

    assert result.company_industry == source_industry


def test_existing_ai_industry_is_not_reprocessed() -> None:
    evidence = "We manufacture industrial robots for automotive factories."
    old_ai_industry = CompanyIndustryEvidence(
        company_name="Acme",
        industry="Old AI guess",
        source_url="https://example.test/jobs/1",
        source_title="AI inference from complete job description",
        checked_at=NOW - timedelta(days=1),
        confidence="low",
        lookup_method="ai",
        source_name="ai",
        evidence=[evidence],
    )

    result = apply_review(
        job(description=evidence, company_industry=old_ai_industry),
        review(
            company_industry="Industrial Automation",
            company_industry_confidence="medium",
            company_industry_evidence=[evidence],
        ),
        config(),
        PROFILE_HASH,
        NOW,
    )

    assert result.company_industry == old_ai_industry


@pytest.mark.parametrize(
    ("facts", "status", "reason", "label", "score"),
    [
        ({"german_requirement": "required"}, "eligible", None, "German required", 80),
        ({"german_requirement": "optional"}, "eligible", None, None, 80),
        (
            {"visa_sponsorship": "not_offered"},
            "excluded",
            "no_visa_sponsorship",
            None,
            80,
        ),
        (
            {"existing_work_authorization": "required"},
            "excluded",
            "work_authorization_required",
            None,
            80,
        ),
        (
            {"citizenship_requirement": "german_or_eu"},
            "excluded",
            "citizenship_required",
            None,
            80,
        ),
        ({"visa_sponsorship": "offered"}, "eligible", None, "Visa support", 80),
        (
            {"security_clearance": "required"},
            "eligible",
            None,
            "Security clearance",
            80,
        ),
        ({"staffing_agency": "yes"}, "eligible", None, "Recruiter", 70),
        ({"german_requirement": "uncertain"}, "eligible", None, None, 80),
        ({"visa_sponsorship": "uncertain"}, "uncertain", None, None, 80),
        (
            {"visa_sponsorship": "not_mentioned"},
            "eligible",
            None,
            "Visa details to verify",
            80,
        ),
        (
            {"existing_work_authorization": "uncertain"},
            "uncertain",
            None,
            None,
            80,
        ),
        (
            {"existing_work_authorization": "not_mentioned"},
            "eligible",
            None,
            "Work authorization to verify",
            80,
        ),
        ({"citizenship_requirement": "uncertain"}, "uncertain", None, None, 80),
        ({"security_clearance": "uncertain"}, "eligible", None, None, 80),
        ({"staffing_agency": "uncertain"}, "eligible", None, None, 80),
    ],
)
def test_apply_review_uses_deterministic_policy_table(
    facts: dict[str, str],
    status: str,
    reason: str | None,
    label: str | None,
    score: int,
) -> None:
    result = apply_review(job(), review(**facts), config(), PROFILE_HASH, NOW)

    assert result.machine_status.value == status
    assert result.exclusion_reasons == ([] if reason is None else [reason])
    expected_labels = [] if facts.get("visa_sponsorship") != "offered" else ["Visa support"]
    if "visa_sponsorship" not in facts:
        expected_labels.append("Visa support")
    if label is not None and label not in expected_labels:
        expected_labels.append(label)
    assert result.labels == expected_labels
    assert result.score == score


def test_hard_exclusion_takes_precedence_over_critical_uncertainty() -> None:
    result = apply_review(
        job(),
        review(visa_sponsorship="not_offered", german_requirement="uncertain"),
        config(),
        PROFILE_HASH,
        NOW,
    )

    assert result.machine_status is MachineStatus.EXCLUDED
    assert result.exclusion_reasons == ["no_visa_sponsorship"]


def test_missing_eligibility_details_remain_eligible_with_verification_labels() -> None:
    result = apply_review(
        job(),
        review(
            visa_sponsorship="not_mentioned",
            existing_work_authorization="not_mentioned",
        ),
        config(),
        PROFILE_HASH,
        NOW,
    )

    assert result.machine_status is MachineStatus.ELIGIBLE
    assert result.labels == [
        "Visa details to verify",
        "Work authorization to verify",
    ]


def test_review_excludes_jobs_below_the_skill_match_threshold() -> None:
    result = apply_review(
        job(),
        review(score=59),
        config(),
        PROFILE_HASH,
        NOW,
    )

    assert result.machine_status is MachineStatus.EXCLUDED
    assert result.exclusion_reasons == ["score_below_minimum"]


def test_high_skill_score_is_eligible_without_a_second_category_filter() -> None:
    result = apply_review(
        job(),
        review(),
        config(),
        PROFILE_HASH,
        NOW,
    )

    assert result.machine_status is MachineStatus.ELIGIBLE
    assert result.exclusion_reasons == []


def test_score_at_recommendation_threshold_remains_eligible() -> None:
    result = apply_review(
        job(),
        review(score=60),
        config(),
        PROFILE_HASH,
        NOW,
    )

    assert result.machine_status is MachineStatus.ELIGIBLE


def test_staffing_penalty_is_fixed_before_recommendation_threshold() -> None:
    result = apply_review(
        job(),
        review(score=65, staffing_agency="yes"),
        config(staffing_penalty=23),
        PROFILE_HASH,
        NOW,
    )

    assert result.score == 55
    assert result.machine_status is MachineStatus.EXCLUDED
    assert result.exclusion_reasons == ["score_below_minimum"]


def test_low_skill_score_takes_precedence_over_critical_uncertainty() -> None:
    result = apply_review(
        job(),
        review(
            score=10,
            existing_work_authorization="uncertain",
            citizenship_requirement="uncertain",
        ),
        config(),
        PROFILE_HASH,
        NOW,
    )

    assert result.machine_status is MachineStatus.EXCLUDED
    assert result.exclusion_reasons == ["score_below_minimum"]


def test_apply_review_collects_every_hard_exclusion_reason() -> None:
    result = apply_review(
        job(),
        review(
            german_requirement="required",
            visa_sponsorship="not_offered",
            existing_work_authorization="required",
            citizenship_requirement="german_or_eu",
            security_clearance="required",
        ),
        config(),
        PROFILE_HASH,
        NOW,
    )

    assert result.machine_status is MachineStatus.EXCLUDED
    assert result.exclusion_reasons == [
        "no_visa_sponsorship",
        "work_authorization_required",
        "citizenship_required",
    ]
    assert result.labels == ["Security clearance", "German required"]


def test_staffing_penalty_is_clamped_at_zero() -> None:
    result = apply_review(
        job(),
        review(score=3, staffing_agency="yes"),
        config(staffing_penalty=10),
        PROFILE_HASH,
        NOW,
    )

    assert result.score == 0


def test_german_language_job_description_does_not_exclude_without_hard_fact() -> None:
    result = apply_review(
        job(description="Wir entwickeln zuverlässige Python-Dienste in Berlin."),
        review(german_requirement="none"),
        config(),
        PROFILE_HASH,
        NOW,
    )

    assert result.machine_status is MachineStatus.ELIGIBLE
    assert result.score == 80


def test_valid_review_updates_metadata_and_appends_history() -> None:
    previous_history = ReviewHistoryEntry(
        attempted_at=NOW - timedelta(days=1),
        content_hash="sha256:old-content",
        profile_hash="sha256:old-profile",
        model="claude-old",
        outcome="failed",
        failure_category="timeout",
    )
    item = job(
        review_history=[previous_history],
        manual_override="show",
        manual_override_content_hash="sha256:old-content",
        manual_override_profile_hash=PROFILE_HASH,
        last_error="Claude review timed out.",
    )
    accepted = review()

    result = apply_review(item, accepted, config(), PROFILE_HASH, NOW)

    assert result.ai_review == accepted
    assert result.reason == accepted.reason
    assert result.review_model == "claude-sonnet-4-5"
    assert result.reviewed_at == NOW
    assert result.last_review_attempt_content_hash == CONTENT_HASH
    assert result.last_review_attempt_profile_hash == PROFILE_HASH
    assert result.last_review_attempt_at == NOW
    assert result.last_successful_review_content_hash == CONTENT_HASH
    assert result.last_successful_review_profile_hash == PROFILE_HASH
    assert result.last_error is None
    assert result.manual_override is None
    assert result.manual_override_content_hash is None
    assert result.manual_override_profile_hash is None
    assert result.review_history[:-1] == [previous_history]
    assert result.review_history[-1] == ReviewHistoryEntry(
        attempted_at=NOW,
        content_hash=CONTENT_HASH,
        profile_hash=PROFILE_HASH,
        model="claude-sonnet-4-5",
        outcome="accepted",
        review=accepted,
    )


@pytest.mark.parametrize(
    ("override_content_hash", "override_profile_hash", "expected_override"),
    [
        (CONTENT_HASH, PROFILE_HASH, "show"),
        ("sha256:old-content", PROFILE_HASH, None),
        (CONTENT_HASH, "sha256:old-profile", None),
    ],
)
def test_review_clears_manual_override_only_when_captured_hashes_are_stale(
    override_content_hash: str,
    override_profile_hash: str,
    expected_override: str | None,
) -> None:
    item = job(
        manual_override="show",
        manual_override_content_hash=override_content_hash,
        manual_override_profile_hash=override_profile_hash,
    )

    result = apply_review(item, review(), config(), PROFILE_HASH, NOW)

    assert result.manual_override == expected_override
    if expected_override is None:
        assert result.manual_override_content_hash is None
        assert result.manual_override_profile_hash is None


def test_final_failure_sets_pending_without_scheduling_retry() -> None:
    previous_history = ReviewHistoryEntry(
        attempted_at=NOW - timedelta(days=1),
        content_hash=CONTENT_HASH,
        profile_hash=PROFILE_HASH,
        model="claude-old",
        outcome="accepted",
        review=review(),
    )
    item = job(
        machine_status=MachineStatus.ELIGIBLE,
        review_history=[previous_history],
        last_review_attempt_content_hash=CONTENT_HASH,
        last_review_attempt_profile_hash=PROFILE_HASH,
        last_successful_review_content_hash="sha256:successful-content",
        last_successful_review_profile_hash="sha256:successful-profile",
    )
    failure = ReviewFailure(
        job_key="canonical-1",
        category="timeout",
        message="Claude review timed out.",
        model="claude-sonnet-4-5",
    )

    result = apply_review_failure(item, failure, PROFILE_HASH, NOW)

    assert result.machine_status is MachineStatus.PENDING
    assert result.last_review_attempt_content_hash == CONTENT_HASH
    assert result.last_review_attempt_profile_hash == PROFILE_HASH
    assert result.last_review_attempt_at == NOW
    assert result.last_successful_review_content_hash == "sha256:successful-content"
    assert result.last_successful_review_profile_hash == "sha256:successful-profile"
    assert "consecutive_failed_review_runs" not in type(result).model_fields
    assert "next_review_at" not in type(result).model_fields
    assert result.last_error == "Claude review timed out."
    assert result.review_history[:-1] == [previous_history]
    assert result.review_history[-1] == ReviewHistoryEntry(
        attempted_at=NOW,
        content_hash=CONTENT_HASH,
        profile_hash=PROFILE_HASH,
        model="claude-sonnet-4-5",
        outcome="failed",
        failure_category="timeout",
    )


def test_failure_on_changed_input_clears_stale_manual_override() -> None:
    item = job(
        last_review_attempt_content_hash="sha256:old-content",
        last_review_attempt_profile_hash=PROFILE_HASH,
        manual_override="show",
        manual_override_content_hash=CONTENT_HASH,
        manual_override_profile_hash="sha256:old-profile",
    )
    failure = ReviewFailure(
        job_key="canonical-1",
        category="missing",
        message="Claude returned no result for this job.",
        model="claude-sonnet-4-5",
    )

    result = apply_review_failure(item, failure, PROFILE_HASH, NOW)

    assert result.manual_override is None
    assert result.manual_override_content_hash is None
    assert result.manual_override_profile_hash is None
