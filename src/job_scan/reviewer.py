from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ValidationError

from job_scan.claude_process import (
    ClaudeInvocation,
    ClaudeOutputLimitExceeded,
    ClaudeProcess,
    ClaudeProcessError,
    ClaudeRequest,
    ClaudeTimeout,
)
from job_scan.config import AppConfig
from job_scan.domain import AIReview, JobRecord
from job_scan.prompts import build_review_prompt

logger = logging.getLogger(__name__)


class ReviewEnvelope(BaseModel):
    results: list[AIReview]


class ReviewFailure(BaseModel):
    job_key: str
    category: Literal[
        "process",
        "timeout",
        "output_limit",
        "json",
        "schema",
        "missing",
        "duplicate",
    ]
    message: str
    model: str


class ReviewBatchOutcome(BaseModel):
    accepted: dict[str, AIReview]
    failed: dict[str, ReviewFailure]
    invocations: list[ClaudeInvocation]


@dataclass(frozen=True)
class ReviewBatchProgress:
    """Report completed configured batches and their covered jobs."""

    completed_batches: int
    total_batches: int
    completed_jobs: int
    total_jobs: int


class ClaudeInvoker(Protocol):
    def invoke(self, request: ClaudeRequest) -> ClaudeInvocation: ...


class _EnvelopeResult(BaseModel):
    results: list[Any]


class _ParsedMembers(BaseModel):
    accepted: dict[str, AIReview]
    failed: dict[str, ReviewFailure]


class ClaudeReviewer:
    """Run bounded Claude review batches and isolate invalid result subsets."""

    def __init__(self, claude: ClaudeInvoker | None = None) -> None:
        self._claude = claude if claude is not None else ClaudeProcess()

    def review(
        self,
        jobs: Sequence[JobRecord],
        profile: str,
        config: AppConfig,
        *,
        progress: Callable[[ReviewBatchProgress], None] | None = None,
    ) -> ReviewBatchOutcome:
        """Return one accepted or failed semantic-review outcome per canonical key."""
        jobs_by_key = {item.canonical_job_key: item for item in jobs}
        sorted_keys = sorted(jobs_by_key)
        accepted: dict[str, AIReview] = {}
        failed: dict[str, ReviewFailure] = {}
        invocations: list[ClaudeInvocation] = []

        batch_size = config.claude.batch_size
        total_jobs = len(sorted_keys)
        total_batches = (total_jobs + batch_size - 1) // batch_size
        for batch_index, offset in enumerate(
            range(0, total_jobs, batch_size),
            start=1,
        ):
            keys = sorted_keys[offset : offset + batch_size]
            self._review_subset(
                keys,
                jobs_by_key,
                profile,
                config,
                accepted,
                failed,
                invocations,
            )
            if progress is not None:
                progress(
                    ReviewBatchProgress(
                        completed_batches=batch_index,
                        total_batches=total_batches,
                        completed_jobs=min(offset + len(keys), total_jobs),
                        total_jobs=total_jobs,
                    )
                )

        return ReviewBatchOutcome(
            accepted=accepted,
            failed=failed,
            invocations=invocations,
        )

    def _review_subset(
        self,
        keys: list[str],
        jobs_by_key: dict[str, JobRecord],
        profile: str,
        config: AppConfig,
        accepted: dict[str, AIReview],
        failed: dict[str, ReviewFailure],
        invocations: list[ClaudeInvocation],
        retry_failures: dict[str, ReviewFailure] | None = None,
    ) -> None:
        """Review one subset, accepting valid members before retrying only failures."""
        members, batch_failure = self._invoke_valid_envelope(
            keys,
            jobs_by_key,
            profile,
            config,
            invocations,
            retry_failures,
        )
        if batch_failure is not None:
            for key in keys:
                failed[key] = _failure(
                    key,
                    batch_failure[0],
                    batch_failure[1],
                    config,
                )
            return

        if members is None:
            raise AssertionError("valid review envelope did not produce members")
        parsed = _parse_members(members.results, keys, jobs_by_key, config)
        accepted.update(parsed.accepted)
        unresolved = [key for key in keys if key not in parsed.accepted]
        if not unresolved:
            return
        if len(keys) == 1:
            key = keys[0]
            failure = parsed.failed[key]
            if retry_failures is None and failure.category == "schema":
                self._review_subset(
                    keys,
                    jobs_by_key,
                    profile,
                    config,
                    accepted,
                    failed,
                    invocations,
                    {key: failure},
                )
            else:
                failed[key] = failure
            return

        midpoint = max(1, len(unresolved) // 2)
        for subset in (unresolved[:midpoint], unresolved[midpoint:]):
            if subset:
                self._review_subset(
                    subset,
                    jobs_by_key,
                    profile,
                    config,
                    accepted,
                    failed,
                    invocations,
                    {key: parsed.failed[key] for key in subset},
                )

    def _invoke_valid_envelope(
        self,
        keys: list[str],
        jobs_by_key: dict[str, JobRecord],
        profile: str,
        config: AppConfig,
        invocations: list[ClaudeInvocation],
        retry_failures: dict[str, ReviewFailure] | None,
    ) -> tuple[
        _EnvelopeResult | None,
        tuple[Literal["process", "timeout", "output_limit", "json", "schema"], str]
        | None,
    ]:
        """Retry one process or top-level-envelope failure once for the same subset."""
        request = ClaudeRequest(
            runtime=config.ai_runtime,
            prompt=_build_attempt_prompt(
                keys,
                jobs_by_key,
                profile,
                retry_failures,
            ),
            json_schema=ReviewEnvelope.model_json_schema(),
            model=config.claude.model,
            runtime_model=config.ai_model,
            effort=config.claude.effort,
            thinking_enabled=config.claude.thinking_enabled,
            timeout_seconds=config.claude.timeout_seconds,
            max_output_bytes=config.claude.max_output_bytes,
        )
        last_failure: tuple[
            Literal["process", "timeout", "output_limit", "json", "schema"], str
        ] | None = None
        for _attempt in range(2):
            try:
                invocation = self._claude.invoke(request)
            except ClaudeTimeout:
                last_failure = ("timeout", "AI review timed out.")
                continue
            except ClaudeOutputLimitExceeded:
                last_failure = (
                    "output_limit",
                    "AI review exceeded the configured output limit.",
                )
                continue
            except ClaudeProcessError:
                last_failure = ("process", "AI review process failed.")
                continue

            invocations.append(invocation)
            if invocation.exit_code != 0:
                last_failure = ("process", "AI review process exited unsuccessfully.")
                continue
            envelope, last_failure = _parse_top_level(invocation.stdout)
            if envelope is not None:
                return envelope, None

        if last_failure is None:
            raise AssertionError("review retry ended without an outcome")
        return None, last_failure


def _build_attempt_prompt(
    keys: list[str],
    jobs_by_key: dict[str, JobRecord],
    profile: str,
    retry_failures: dict[str, ReviewFailure] | None,
) -> str:
    """Add prior member-validation failures to a retry prompt."""
    prompt = build_review_prompt([jobs_by_key[key] for key in keys], profile)
    if not retry_failures:
        return prompt
    corrections = "\n".join(
        f'- job_key {json.dumps(key)}: {retry_failures[key].message}'
        for key in keys
    )
    marker = "Submitted jobs (JSON; complete_jd is the complete plain-text JD):\n"
    instructions, submitted_jobs = prompt.rsplit(marker, 1)
    return (
        f"{instructions}Previous validation failures:\n{corrections}\n"
        "Correct every listed failure and return a valid result for each submitted job."
        f"\n\n{marker}{submitted_jobs}"
    )


def _parse_top_level(
    stdout: bytes,
) -> tuple[
    _EnvelopeResult | None,
    tuple[Literal["json", "schema"], str] | None,
]:
    """Validate only the three required top-level review envelope shapes."""
    try:
        payload = json.loads(stdout)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, ("json", "AI returned invalid review JSON.")
    if not isinstance(payload, dict):
        return None, ("schema", "AI review output was not a JSON object.")
    structured_output = payload.get("structured_output")
    if not isinstance(structured_output, dict):
        return None, ("schema", "AI review output lacked structured output.")
    results = structured_output.get("results")
    if not isinstance(results, list):
        return None, ("schema", "AI structured output lacked a results list.")
    return _EnvelopeResult(results=results), None


def _parse_members(
    raw_results: list[Any],
    keys: list[str],
    jobs_by_key: dict[str, JobRecord],
    config: AppConfig,
) -> _ParsedMembers:
    """Validate each member independently against its matching input job."""
    input_keys = set(keys)
    returned_known_keys: list[str] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        raw_key = item.get("job_key")
        if not isinstance(raw_key, str):
            continue
        if raw_key not in input_keys:
            logger.warning("Discarding Claude review for unknown job key %r.", raw_key)
            continue
        returned_known_keys.append(raw_key)

    duplicate_keys = {
        key for key, count in Counter(returned_known_keys).items() if count > 1
    }
    accepted: dict[str, AIReview] = {}
    failed: dict[str, ReviewFailure] = {
        key: _failure(
            key,
            "duplicate",
            "AI returned duplicate results for this job.",
            config,
        )
        for key in duplicate_keys
    }

    for item in raw_results:
        if not isinstance(item, dict):
            continue
        raw_key = item.get("job_key")
        if not isinstance(raw_key, str):
            continue
        if raw_key not in input_keys or raw_key in duplicate_keys:
            continue
        try:
            review = AIReview.model_validate(_sanitize_company_industry(item))
        except ValidationError as error:
            failed[raw_key] = _failure(
                raw_key,
                "schema",
                _validation_failure_message(error),
                config,
            )
            continue
        validated, validation_error = _validated_review(
            review,
            jobs_by_key[raw_key],
        )
        if validated is None:
            failed[raw_key] = _failure(
                raw_key,
                "schema",
                validation_error
                or "AI returned invalid eligibility evidence for this job.",
                config,
            )
            continue
        accepted[raw_key] = validated

    for key in keys:
        if key not in accepted and key not in failed:
            failed[key] = _failure(
                key,
                "missing",
                "AI returned no result for this job.",
                config,
            )
    return _ParsedMembers(accepted=accepted, failed=failed)


def _validation_failure_message(error: ValidationError) -> str:
    """Describe invalid review fields without retaining model-returned values."""
    details = "; ".join(
        f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}"
        for issue in error.errors(
            include_url=False,
            include_context=False,
            include_input=False,
        )
    )
    return f"AI returned an invalid result for this job. {details}"


def _sanitize_company_industry(item: dict[str, Any]) -> dict[str, Any]:
    """Discard malformed optional industry fields before validating core review facts."""
    industry = item.get("company_industry")
    confidence = item.get("company_industry_confidence")
    evidence = item.get("company_industry_evidence")
    valid_industry = industry is None or (
        isinstance(industry, str) and 0 < len(industry.strip()) <= 300
    )
    valid_confidence = isinstance(confidence, str) and confidence in (
        "high",
        "medium",
        "low",
    )
    valid_evidence = isinstance(evidence, list) and all(
        isinstance(value, str) for value in evidence
    )
    if valid_industry and valid_confidence and valid_evidence:
        return item
    return {
        **item,
        "company_industry": None,
        "company_industry_confidence": "low",
        "company_industry_evidence": [],
    }


def _validated_review(
    review: AIReview,
    job: JobRecord,
) -> tuple[AIReview | None, str | None]:
    """Return a review with exact-JD evidence or its validation failure."""
    evidence = [item.strip() for item in review.eligibility_evidence]
    if any(not item for item in evidence):
        return None, "eligibility_evidence contains an empty string."
    if any(item not in job.description for item in evidence):
        return None, (
            "eligibility_evidence contains text that is not an exact contiguous "
            "substring of complete_jd. Copy evidence byte-for-byte without paraphrasing."
        )
    exclusion_fact = (
        review.visa_sponsorship == "not_offered"
        or review.existing_work_authorization == "required"
        or review.citizenship_requirement == "german_or_eu"
    )
    if exclusion_fact and not evidence:
        return None, (
            "An exclusion fact was returned without eligibility_evidence. Copy at least "
            "one exact supporting substring from complete_jd."
        )

    industry: str | None
    if job.company_industry is not None:
        industry = job.company_industry.industry
        industry_confidence = "high"
        industry_evidence: list[str] = []
    else:
        industry = review.company_industry.strip() if review.company_industry else None
        industry_confidence = review.company_industry_confidence
        industry_evidence = [item.strip() for item in review.company_industry_evidence]
        invalid_industry = (
            (review.company_industry is not None and not industry)
            or any(not item for item in industry_evidence)
            or any(item not in job.description for item in industry_evidence)
            or (industry is not None and not industry_evidence)
            or (industry is None and bool(industry_evidence))
        )
        if invalid_industry:
            industry = None
            industry_confidence = "low"
            industry_evidence = []

    return review.model_copy(
        update={
            "eligibility_evidence": evidence,
            "company_industry": industry,
            "company_industry_confidence": industry_confidence,
            "company_industry_evidence": industry_evidence,
        }
    ), None


def _failure(
    job_key: str,
    category: Literal[
        "process",
        "timeout",
        "output_limit",
        "json",
        "schema",
        "missing",
        "duplicate",
    ],
    message: str,
    config: AppConfig,
) -> ReviewFailure:
    """Build one stable failure record for the configured review model."""
    return ReviewFailure(
        job_key=job_key,
        category=category,
        message=message,
        model=config.selected_model,
    )
