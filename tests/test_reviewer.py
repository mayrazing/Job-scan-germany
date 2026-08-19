from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import HttpUrl

from job_scan.claude_process import (
    ClaudeInvocation,
    ClaudeOutputLimitExceeded,
    ClaudeRequest,
    ClaudeTimeout,
)
from job_scan.config import AppConfig, ClaudeSettings, SchedulerSettings
from job_scan.domain import AvailabilityStatus, CompanyIndustryEvidence, JobRecord
from job_scan.reviewer import ClaudeReviewer, ReviewEnvelope

NOW = datetime(2026, 8, 3, 12, tzinfo=UTC)
PROFILE = "PRIVATE PROFILE: backend engineer who needs visa sponsorship"


class FakeClaude:
    def __init__(self, *responses: bytes | ClaudeInvocation | BaseException) -> None:
        self.responses = list(responses)
        self.requests: list[ClaudeRequest] = []

    def invoke(self, request: ClaudeRequest) -> ClaudeInvocation:
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, ClaudeInvocation):
            return response
        return invocation(response)


def config(*, batch_size: int = 10, thinking_enabled: bool = True) -> AppConfig:
    return AppConfig(
        ai_runtime="api:deepseek",
        ai_model="deepseek-chat",
        resume_path=Path("/private/resume.pdf"),
        resume_sha256="sha256:" + ("a" * 64),
        profile_sha256="sha256:" + ("b" * 64),
        search_terms=["backend engineer"],
        locations=["Berlin"],
        german_level="A2",
        claude=ClaudeSettings(
            model="claude-sonnet-4-5",
            effort="high",
            thinking_enabled=thinking_enabled,
            batch_size=batch_size,
            timeout_seconds=91,
            max_output_bytes=123_456,
        ),
        scheduler=SchedulerSettings(local_time="08:30"),
    )


def job(key: str, *, description: str | None = None) -> JobRecord:
    jd = description or (
        f"Complete JD for {key}. German is optional. "
        "We build reliable Python services."
    )
    return JobRecord(
        canonical_job_key=key,
        source_occurrences=[],
        primary_source_occurrence_key=f"source:{key}@1",
        company=f"Company {key}",
        title=f"Backend Engineer {key}",
        location="Berlin",
        url=HttpUrl(f"https://example.test/jobs/{key}"),
        description=jd,
        posted_at=date(2026, 8, 1),
        content_hash=f"sha256:{key}",
        first_seen=NOW,
        last_seen=NOW,
        availability_status=AvailabilityStatus.ACTIVE,
        user_status_updated_at=NOW,
    )


def review_item(key: str, **updates: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "job_key": key,
        "german_requirement": "optional",
        "visa_sponsorship": "not_mentioned",
        "existing_work_authorization": "not_mentioned",
        "citizenship_requirement": "none",
        "security_clearance": "none",
        "staffing_agency": "no",
        "eligibility_evidence": ["German is optional."],
        "company_industry": None,
        "company_industry_confidence": "low",
        "company_industry_evidence": [],
        "score": 82,
        "reason": "Strong backend match",
        "confidence": "high",
    }
    values.update(updates)
    return values


def stdout(*results: dict[str, Any]) -> bytes:
    return json.dumps({"structured_output": {"results": list(results)}}).encode()


def invocation(
    payload: bytes,
    *,
    exit_code: int = 0,
) -> ClaudeInvocation:
    return ClaudeInvocation(
        argv=["explicit-local-fake"],
        stdout=payload,
        stderr=b"fake stderr",
        exit_code=exit_code,
        duration_seconds=0.01,
    )


def test_request_uses_generated_schema_configured_limits_and_complete_private_prompt() -> None:
    first = job("job-b")
    first.company_industry = CompanyIndustryEvidence(
        company_name=first.company,
        industry="Software Development",
        source_url="https://example.test/company/job-b",
        source_title="Source company profile",
        checked_at=NOW,
        confidence="high",
        lookup_method="native",
        source_name="linkedin",
        evidence=[],
    )
    second = job("job-a", description="FULL JD: Python. German required.")
    fake = FakeClaude(
        stdout(review_item("job-a", eligibility_evidence=[]), review_item("job-b"))
    )

    outcome = ClaudeReviewer(fake).review(
        [first, second], PROFILE, config(thinking_enabled=False)
    )

    assert set(outcome.accepted) == {"job-a", "job-b"}
    assert outcome.accepted["job-b"].company_industry == "Software Development"
    assert outcome.accepted["job-b"].company_industry_confidence == "high"
    assert outcome.accepted["job-b"].company_industry_evidence == []
    request = fake.requests[0]
    assert request.json_schema == ReviewEnvelope.model_json_schema()
    required_review_fields = set(
        request.json_schema["$defs"]["AIReview"]["required"]
    )
    assert {
        "company_industry",
        "company_industry_confidence",
        "company_industry_evidence",
    } <= required_review_fields
    assert request.model == "claude-sonnet-4-5"
    assert request.effort == "high"
    assert request.thinking_enabled is False
    assert request.runtime == "api:deepseek"
    assert request.runtime_model == "deepseek-chat"
    assert request.timeout_seconds == 91
    assert request.max_output_bytes == 123_456
    assert PROFILE in request.prompt
    assert request.prompt.index('"job_key":"job-a"') < request.prompt.index(
        '"job_key":"job-b"'
    )
    for submitted in (first, second):
        assert submitted.canonical_job_key in request.prompt
        assert submitted.title in request.prompt
        assert submitted.company in request.prompt
        assert submitted.location in request.prompt
        assert submitted.description in request.prompt

    prompt = request.prompt.lower()
    assert "hard german requirement" in prompt
    assert "optional or preferred german" in prompt
    assert "german-language job description alone" in prompt
    assert "must not lower the score" in prompt
    assert "job description is written in german" in prompt
    assert "verbatim" in prompt
    assert "byte-for-byte" in prompt
    assert "not_mentioned" in request.prompt
    assert "professional experience" in prompt
    assert "required skills" in prompt
    assert "preferred skills" in prompt
    assert "core job responsibilities" in prompt
    assert "90-100" in prompt
    assert "75-89" in prompt
    assert "60-74" in prompt
    assert "40-59" in prompt
    assert "0-39" in prompt
    assert "must not affect the technical skill score" in prompt
    assert "strongest matching skills" in prompt
    assert "important missing skills" in prompt
    assert '"backend","platform"' not in request.prompt
    assert '"source_company_industry":"Software Development"' in request.prompt
    assert "company industry" in prompt
    assert "job function" in prompt
    assert "exact contiguous substring" in prompt
    assert "return company_industry=null" in prompt


def test_valid_ai_company_industry_requires_exact_jd_evidence() -> None:
    description = "We manufacture industrial robots for automotive factories."
    response = review_item(
        "job-a",
        eligibility_evidence=[],
        company_industry="Industrial Automation",
        company_industry_confidence="medium",
        company_industry_evidence=[f"  {description}  "],
    )

    outcome = ClaudeReviewer(FakeClaude(stdout(response))).review(
        [job("job-a", description=description)], PROFILE, config()
    )

    accepted = outcome.accepted["job-a"]
    assert accepted.company_industry == "Industrial Automation"
    assert accepted.company_industry_confidence == "medium"
    assert accepted.company_industry_evidence == [description]


def test_invalid_ai_company_industry_does_not_discard_valid_job_review() -> None:
    response = review_item(
        "job-a",
        eligibility_evidence=[],
        company_industry="Industrial Automation",
        company_industry_confidence="medium",
        company_industry_evidence=["The company makes factory robots."],
    )

    outcome = ClaudeReviewer(FakeClaude(stdout(response))).review(
        [
            job(
                "job-a",
                description="We manufacture industrial robots for automotive factories.",
            )
        ],
        PROFILE,
        config(),
    )

    assert outcome.failed == {}
    accepted = outcome.accepted["job-a"]
    assert accepted.score == 82
    assert accepted.reason == "Strong backend match"
    assert accepted.company_industry is None
    assert accepted.company_industry_confidence == "low"
    assert accepted.company_industry_evidence == []


def test_orphan_ai_company_industry_evidence_does_not_discard_valid_job_review() -> None:
    response = review_item(
        "job-a",
        eligibility_evidence=[],
        company_industry=None,
        company_industry_confidence="medium",
        company_industry_evidence=["We build reliable Python services."],
    )

    outcome = ClaudeReviewer(FakeClaude(stdout(response))).review(
        [job("job-a")], PROFILE, config()
    )

    assert outcome.failed == {}
    accepted = outcome.accepted["job-a"]
    assert accepted.score == 82
    assert accepted.company_industry is None
    assert accepted.company_industry_confidence == "low"
    assert accepted.company_industry_evidence == []


def test_missing_company_industry_fields_do_not_discard_valid_job_review() -> None:
    response = review_item("job-a")
    for field in (
        "company_industry",
        "company_industry_confidence",
        "company_industry_evidence",
    ):
        response.pop(field)

    outcome = ClaudeReviewer(FakeClaude(stdout(response))).review(
        [job("job-a")], PROFILE, config()
    )

    assert outcome.failed == {}
    accepted = outcome.accepted["job-a"]
    assert accepted.score == 82
    assert accepted.company_industry is None
    assert accepted.company_industry_confidence == "low"
    assert accepted.company_industry_evidence == []


@pytest.mark.parametrize(
    "updates",
    [
        {"company_industry": 123},
        {"company_industry": " "},
        {"company_industry_confidence": "certain"},
        {"company_industry_evidence": "not-a-list"},
        {"company_industry_evidence": [123]},
    ],
    ids=[
        "industry-not-string",
        "industry-blank",
        "confidence-invalid",
        "evidence-not-list",
        "evidence-member-not-string",
    ],
)
def test_malformed_company_industry_fields_do_not_discard_valid_job_review(
    updates: dict[str, Any],
) -> None:
    response = review_item("job-a", **updates)

    outcome = ClaudeReviewer(FakeClaude(stdout(response))).review(
        [job("job-a")], PROFILE, config()
    )

    assert outcome.failed == {}
    accepted = outcome.accepted["job-a"]
    assert accepted.score == 82
    assert accepted.reason == "Strong backend match"
    assert accepted.company_industry is None
    assert accepted.company_industry_confidence == "low"
    assert accepted.company_industry_evidence == []


def test_valid_output_trims_evidence_and_writes_trimmed_values_back() -> None:
    item = job(
        "job-a",
        description="German is required. Existing permit required.",
    )
    response = review_item(
        "job-a",
        german_requirement="required",
        eligibility_evidence=["  German is required.\n", " Existing permit required. "],
    )

    outcome = ClaudeReviewer(FakeClaude(stdout(response))).review(
        [item], PROFILE, config()
    )

    assert outcome.failed == {}
    assert outcome.accepted["job-a"].eligibility_evidence == [
        "German is required.",
        "Existing permit required.",
    ]


@pytest.mark.parametrize(
    ("responses", "category"),
    [
        ((b"{not-json", b"still-not-json"), "json"),
        ((b"[]", b"[]"), "schema"),
        ((json.dumps({}).encode(), json.dumps({}).encode()), "schema"),
        (
            (
                json.dumps({"structured_output": []}).encode(),
                json.dumps({"structured_output": []}).encode(),
            ),
            "schema",
        ),
        (
            (
                json.dumps({"structured_output": {"results": {}}}).encode(),
                json.dumps({"structured_output": {"results": {}}}).encode(),
            ),
            "schema",
        ),
    ],
    ids=[
        "invalid-json",
        "stdout-not-object",
        "missing-structured-output",
        "structured-output-not-object",
        "results-not-list",
    ],
)
def test_invalid_top_level_envelope_retries_once_then_fails_batch(
    responses: tuple[bytes, bytes],
    category: str,
) -> None:
    fake = FakeClaude(*responses)

    outcome = ClaudeReviewer(fake).review([job("job-a")], PROFILE, config())

    assert outcome.accepted == {}
    assert outcome.failed["job-a"].category == category
    assert len(fake.requests) == 2


def test_unknown_key_is_discarded_and_logged_without_creating_a_job(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = FakeClaude(stdout(review_item("unknown-job")))

    with caplog.at_level(logging.WARNING, logger="job_scan.reviewer"):
        outcome = ClaudeReviewer(fake).review([job("job-a")], PROFILE, config())

    assert outcome.accepted == {}
    assert set(outcome.failed) == {"job-a"}
    assert outcome.failed["job-a"].category == "missing"
    assert "unknown-job" in caplog.text


def test_duplicate_key_is_rejected() -> None:
    duplicate = review_item("job-a")

    outcome = ClaudeReviewer(FakeClaude(stdout(duplicate, duplicate))).review(
        [job("job-a")], PROFILE, config()
    )

    assert outcome.accepted == {}
    assert outcome.failed["job-a"].category == "duplicate"


def test_missing_key_is_rejected() -> None:
    outcome = ClaudeReviewer(FakeClaude(stdout())).review(
        [job("job-a")], PROFILE, config()
    )

    assert outcome.accepted == {}
    assert outcome.failed["job-a"].category == "missing"


@pytest.mark.parametrize(
    "updates",
    [
        {"german_requirement": "sometimes"},
        {"score": -1},
        {"score": 101},
    ],
    ids=["invalid-enum", "score-below-range", "score-above-range"],
)
def test_invalid_member_is_schema_failure_without_rejecting_valid_siblings(
    updates: dict[str, Any],
) -> None:
    fake = FakeClaude(
        stdout(review_item("job-a"), review_item("job-b", **updates)),
        stdout(review_item("job-b", **updates)),
    )

    outcome = ClaudeReviewer(fake).review(
        [job("job-b"), job("job-a")], PROFILE, config()
    )

    assert set(outcome.accepted) == {"job-a"}
    assert outcome.failed["job-b"].category == "schema"
    assert len(fake.requests) == 2


@pytest.mark.parametrize(
    "updates",
    [
        {"visa_sponsorship": "not_offered", "eligibility_evidence": []},
        {"existing_work_authorization": "required", "eligibility_evidence": []},
        {"citizenship_requirement": "german_or_eu", "eligibility_evidence": []},
        {"eligibility_evidence": ["  "]},
        {"eligibility_evidence": ["german is optional."]},
        {"eligibility_evidence": ["German  is optional."]},
    ],
    ids=[
        "visa-not-offered-without-evidence",
        "authorization-required-without-evidence",
        "citizenship-required-without-evidence",
        "empty-evidence",
        "case-normalization-forbidden",
        "whitespace-normalization-forbidden",
    ],
)
def test_invalid_eligibility_evidence_rejects_the_member(
    updates: dict[str, Any],
) -> None:
    outcome = ClaudeReviewer(FakeClaude(stdout(review_item("job-a", **updates)))).review(
        [job("job-a")], PROFILE, config()
    )

    assert outcome.accepted == {}
    assert outcome.failed["job-a"].category == "schema"


def test_german_requirement_without_evidence_does_not_discard_valid_job_review() -> None:
    response = review_item(
        "job-a",
        german_requirement="required",
        eligibility_evidence=[],
    )

    outcome = ClaudeReviewer(FakeClaude(stdout(response))).review(
        [job("job-a")], PROFILE, config()
    )

    assert outcome.failed == {}
    assert outcome.accepted["job-a"].score == 82


def test_member_evidence_must_come_from_its_own_job_description() -> None:
    first = job("job-a", description="Unique evidence for A.")
    second = job("job-b", description="Unique evidence for B.")
    fake = FakeClaude(
        stdout(
            review_item("job-a", eligibility_evidence=["Unique evidence for B."]),
            review_item("job-b", eligibility_evidence=["Unique evidence for B."]),
        ),
        stdout(review_item("job-a", eligibility_evidence=["Unique evidence for B."])),
    )

    outcome = ClaudeReviewer(fake).review([first, second], PROFILE, config())

    assert set(outcome.accepted) == {"job-b"}
    assert outcome.failed["job-a"].category == "schema"


def test_member_retry_prompt_includes_previous_validation_failure() -> None:
    first = job("job-a", description="Exact evidence for A.")
    second = job("job-b", description="Exact evidence for B.")
    fake = FakeClaude(
        stdout(
            review_item("job-a", eligibility_evidence=["Paraphrased evidence."]),
            review_item("job-b", eligibility_evidence=["Exact evidence for B."]),
        ),
        stdout(review_item("job-a", eligibility_evidence=["Exact evidence for A."])),
    )

    outcome = ClaudeReviewer(fake).review([first, second], PROFILE, config())

    assert set(outcome.accepted) == {"job-a", "job-b"}
    assert outcome.failed == {}
    retry_prompt = fake.requests[1].prompt
    assert "Previous validation failures" in retry_prompt
    assert 'job_key "job-a"' in retry_prompt
    assert "not an exact contiguous substring of complete_jd" in retry_prompt


def test_valid_envelope_splits_only_failed_subset_until_singletons() -> None:
    jobs = [job("job-d"), job("job-c"), job("job-b"), job("job-a")]
    duplicate = review_item("job-b")
    fake = FakeClaude(
        stdout(
            review_item("job-a"),
            duplicate,
            duplicate,
            review_item("job-c", score=101),
        ),
        stdout(review_item("job-b")),
        stdout(review_item("job-c")),
        stdout(review_item("job-d")),
    )

    outcome = ClaudeReviewer(fake).review(jobs, PROFILE, config(batch_size=3))

    assert set(outcome.accepted) == {"job-a", "job-b", "job-c", "job-d"}
    assert outcome.failed == {}
    assert len(outcome.invocations) == 4
    assert len(fake.requests) == 4
    requested_prompts = [request.prompt for request in fake.requests]
    assert all(key in requested_prompts[0] for key in ("job-a", "job-b", "job-c"))
    assert "job-d" not in requested_prompts[0]
    assert "job-b" in requested_prompts[1] and "job-c" not in requested_prompts[1]
    assert "job-c" in requested_prompts[2] and "job-b" not in requested_prompts[2]
    assert "job-d" in requested_prompts[3]


def test_review_reports_progress_after_each_configured_batch_finishes() -> None:
    jobs = [job(f"job-{index}") for index in range(5)]
    fake = FakeClaude(
        stdout(review_item("job-0"), review_item("job-1")),
        stdout(review_item("job-2"), review_item("job-3")),
        stdout(review_item("job-4")),
    )
    observed: list[tuple[int, int, int, int]] = []

    ClaudeReviewer(fake).review(
        jobs,
        PROFILE,
        config(batch_size=2),
        progress=lambda current: observed.append(
            (
                current.completed_batches,
                current.total_batches,
                current.completed_jobs,
                current.total_jobs,
            )
        ),
    )

    assert observed == [
        (1, 3, 2, 5),
        (2, 3, 4, 5),
        (3, 3, 5, 5),
    ]


def test_nonzero_process_failure_retries_same_batch_once() -> None:
    fake = FakeClaude(
        invocation(b"fake stdout", exit_code=17),
        stdout(review_item("job-a")),
    )

    outcome = ClaudeReviewer(fake).review([job("job-a")], PROFILE, config())

    assert set(outcome.accepted) == {"job-a"}
    assert outcome.failed == {}
    assert len(fake.requests) == 2
    assert len(outcome.invocations) == 2


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (ClaudeTimeout("controlled timeout"), "timeout"),
        (ClaudeOutputLimitExceeded("controlled output cap"), "output_limit"),
    ],
)
def test_typed_process_failure_retries_once_then_uses_specific_category(
    error: BaseException,
    category: str,
) -> None:
    fake = FakeClaude(error, error)

    outcome = ClaudeReviewer(fake).review([job("job-a")], PROFILE, config())

    assert outcome.accepted == {}
    assert outcome.failed["job-a"].category == category
    assert outcome.failed["job-a"].model == "deepseek-chat"
    assert len(fake.requests) == 2
    assert outcome.invocations == []


def test_every_job_appears_exactly_once_after_mixed_final_outcome() -> None:
    fake = FakeClaude(
        stdout(review_item("job-a"), review_item("job-b", score=101)),
        stdout(),
    )

    outcome = ClaudeReviewer(fake).review(
        [job("job-b"), job("job-a")], PROFILE, config()
    )

    assert set(outcome.accepted) == {"job-a"}
    assert set(outcome.failed) == {"job-b"}
    assert not (set(outcome.accepted) & set(outcome.failed))
    assert outcome.failed["job-b"].category == "missing"
