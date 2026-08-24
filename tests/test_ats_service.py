from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from threading import Event, Lock

import pytest
from pydantic import HttpUrl

from job_scan.ats_service import AtsCheckError, AtsCheckInput, AtsCheckService
from job_scan.claude_process import ClaudeInvocation, ClaudeRequest, ClaudeTimeout
from job_scan.config import AppConfig, ClaudeSettings, SchedulerSettings
from job_scan.domain import AvailabilityStatus, JobRecord
from job_scan.prompts import build_ats_job_prompt
from job_scan.resume import ExtractedResume

NOW = datetime(2026, 8, 9, 12, tzinfo=UTC)


def config() -> AppConfig:
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
            thinking_enabled=False,
            timeout_seconds=91,
            max_output_bytes=123_456,
        ),
        scheduler=SchedulerSettings(local_time="08:30"),
    )


def job(key: str, description: str, *, url: str | None = None) -> JobRecord:
    return JobRecord(
        canonical_job_key=key,
        source_occurrences=[],
        primary_source_occurrence_key=f"source:{key}@1",
        company=f"Company {key}",
        title=f"Backend Engineer {key}",
        location="Berlin",
        url=HttpUrl(url or f"https://example.test/jobs/{key}"),
        description=description,
        posted_at=date(2026, 8, 1),
        content_hash=f"sha256:{key}",
        first_seen=NOW,
        last_seen=NOW,
        availability_status=AvailabilityStatus.ACTIVE,
        user_status_updated_at=NOW,
    )


def ats_input(*jobs: JobRecord) -> AtsCheckInput:
    return AtsCheckInput(
        run_id="ats-1",
        search_run_id="search-1",
        resume_id="sha256:" + "a" * 64,
        candidate_name="Ada",
        resume_filename="Ada CV.pdf",
        resume_bytes=b"PRIVATE RESUME FILE",
        jobs=jobs,
    )


def fake_resume_extractor(path: Path) -> ExtractedResume:
    return ExtractedResume(
        path=path,
        sha256="sha256:" + ("c" * 64),
        text="PRIVATE RESUME",
        format="pdf",
    )


def invocation(structured_output: dict[str, object]) -> ClaudeInvocation:
    return ClaudeInvocation(
        argv=["test-invoker"],
        stdout=json.dumps({"structured_output": structured_output}).encode(),
        stderr=b"",
        exit_code=0,
        duration_seconds=0.01,
    )


def resume_assessment() -> dict[str, object]:
    return {
        "readiness_score": 88,
        "verdict": "ready",
        "title": "Resume content is ATS ready",
        "summary": "Core resume content is clear.",
        "findings": [
            {
                "label": "Contact details",
                "status": "pass",
                "detail": "Contact details are present.",
            }
        ],
    }


def job_assessment(key: str) -> dict[str, object]:
    return {
        "job_key": key,
        "match_score": 81,
        "match_label": "strong",
        "required_skills_score": 84,
        "experience_score": 82,
        "keyword_score": 73,
        "matched": ["Python backend delivery"],
        "needs_attention": ["Kubernetes is not shown"],
        "suggestions": ["Mention supported backend outcomes more clearly."],
    }


class RecordingAtsInvoker:
    def __init__(self) -> None:
        self.kinds: list[str] = []

    def invoke(self, request: ClaudeRequest) -> ClaudeInvocation:
        properties = request.json_schema["properties"]
        if "readiness_score" in properties:
            self.kinds.append("resume")
            return invocation(resume_assessment())
        if "job_key" in properties:
            submitted_job = json.loads(
                request.prompt.split("Submitted job (JSON):\n", 1)[1]
            )
            key = submitted_job["job_key"]
            self.kinds.append(key)
            return invocation(job_assessment(key))
        raise AssertionError("unexpected ATS schema")


def _request_job_keys(request: ClaudeRequest) -> list[str]:
    return re.findall(r'"job_key":"([^"]+)"', request.prompt)


class BlockingAtsInvoker:
    def __init__(self, expected_parallel_jobs: int) -> None:
        self.expected_parallel_jobs = expected_parallel_jobs
        self.max_active_jobs = 0
        self.job_keys_per_call: list[list[str]] = []
        self._active_jobs = 0
        self._lock = Lock()
        self._release = Event()

    def invoke(self, request: ClaudeRequest) -> ClaudeInvocation:
        properties = request.json_schema["properties"]
        if "readiness_score" in properties:
            return invocation(resume_assessment())

        keys = _request_job_keys(request)
        with self._lock:
            self.job_keys_per_call.append(keys)
            self._active_jobs += 1
            self.max_active_jobs = max(self.max_active_jobs, self._active_jobs)
            if self._active_jobs == self.expected_parallel_jobs:
                self._release.set()
        try:
            self._release.wait(timeout=0.5)
            return invocation(job_assessment(keys[0]))
        finally:
            with self._lock:
                self._active_jobs -= 1


class OneJobAlwaysInvalidInvoker:
    def __init__(self, failing_key: str) -> None:
        self.failing_key = failing_key
        self.attempts: defaultdict[str, int] = defaultdict(int)
        self._lock = Lock()

    def invoke(self, request: ClaudeRequest) -> ClaudeInvocation:
        properties = request.json_schema["properties"]
        if "readiness_score" in properties:
            return invocation(resume_assessment())

        key = _request_job_keys(request)[0]
        with self._lock:
            self.attempts[key] += 1
        if key == self.failing_key:
            return invocation({"job_key": key})
        return invocation(job_assessment(key))


class ResumeAlwaysTimeoutInvoker:
    def __init__(self) -> None:
        self.kinds: list[str] = []

    def invoke(self, request: ClaudeRequest) -> ClaudeInvocation:
        properties = request.json_schema["properties"]
        if "readiness_score" in properties:
            self.kinds.append("resume")
            raise ClaudeTimeout("PRIVATE timeout detail")
        keys = _request_job_keys(request)
        self.kinds.extend(keys)
        return invocation(job_assessment(keys[0]))


class WrongJobKeyInvoker:
    def __init__(self, returned_keys: list[str]) -> None:
        self.returned_keys = returned_keys
        self.attempts = 0

    def invoke(self, request: ClaudeRequest) -> ClaudeInvocation:
        properties = request.json_schema["properties"]
        if "readiness_score" in properties:
            return invocation(resume_assessment())

        returned_key = self.returned_keys[self.attempts]
        self.attempts += 1
        return invocation(job_assessment(returned_key))


def test_job_prompt_contains_exactly_one_complete_jd() -> None:
    prompt = build_ats_job_prompt("PRIVATE RESUME", job("job-1", "JD ONE"))

    assert "PRIVATE RESUME" in prompt
    assert '"job_key":"job-1"' in prompt
    assert '"complete_jd":"JD ONE"' in prompt
    assert "submitted_jobs" not in prompt


def test_common_check_finishes_before_any_job_request() -> None:
    invoker = RecordingAtsInvoker()
    service = AtsCheckService(invoker, resume_extractor=fake_resume_extractor)

    result = service.check(
        ats_input(job("job-1", "JD ONE"), job("job-2", "JD TWO")),
        config(),
    )

    assert invoker.kinds[0] == "resume"
    assert sorted(invoker.kinds[1:]) == ["job-1", "job-2"]
    assert [item.job_key for item in result.jobs] == ["job-1", "job-2"]


def test_check_keeps_the_input_resume_hash() -> None:
    service = AtsCheckService(
        RecordingAtsInvoker(),
        resume_extractor=fake_resume_extractor,
    )

    result = service.check(ats_input(job("job-1", "JD ONE")), config())

    assert result.resume_id == "sha256:" + "a" * 64


def test_existing_resume_check_is_reused_and_new_job_is_appended() -> None:
    invoker = RecordingAtsInvoker()
    service = AtsCheckService(invoker, resume_extractor=fake_resume_extractor)
    existing = service.check(ats_input(job("job-a", "JD A")), config())
    invoker.kinds.clear()

    updated = service.check(
        ats_input(job("job-c", "JD C")),
        config(),
        previous=existing,
    )

    assert invoker.kinds == ["job-c"]
    assert updated.resume == existing.resume
    assert [item.job_key for item in updated.jobs] == ["job-a", "job-c"]


def test_existing_job_is_reused_when_its_jd_hash_is_unchanged() -> None:
    invoker = RecordingAtsInvoker()
    service = AtsCheckService(invoker, resume_extractor=fake_resume_extractor)
    selected_job = job("job-a", "SAME JD")
    existing = service.check(ats_input(selected_job), config())
    invoker.kinds.clear()

    updated = service.check(
        ats_input(selected_job),
        config(),
        previous=existing,
    )

    assert invoker.kinds == []
    assert updated.jobs == existing.jobs


def test_existing_job_is_checked_again_when_its_jd_hash_changes() -> None:
    invoker = RecordingAtsInvoker()
    service = AtsCheckService(invoker, resume_extractor=fake_resume_extractor)
    existing = service.check(ats_input(job("job-a", "OLD JD")), config())
    invoker.kinds.clear()
    changed = job("job-a", "NEW JD").model_copy(
        update={"content_hash": "sha256:changed-job-a"}
    )

    updated = service.check(ats_input(changed), config(), previous=existing)

    assert invoker.kinds == ["job-a"]
    assert len(updated.jobs) == 1
    assert updated.jobs[0].content_hash == "sha256:changed-job-a"


def test_job_checks_use_at_most_three_parallel_workers() -> None:
    invoker = BlockingAtsInvoker(expected_parallel_jobs=3)
    service = AtsCheckService(invoker, resume_extractor=fake_resume_extractor)

    result = service.check(
        ats_input(*(job(f"job-{index}", f"JD {index}") for index in range(5))),
        config(),
    )

    assert invoker.max_active_jobs == 3
    assert [item.job_key for item in result.jobs] == [f"job-{index}" for index in range(5)]
    assert all(len(keys) == 1 for keys in invoker.job_keys_per_call)


def test_one_invalid_job_response_retries_once_then_does_not_fail_other_jobs() -> None:
    invoker = OneJobAlwaysInvalidInvoker(failing_key="job-2")
    service = AtsCheckService(invoker, resume_extractor=fake_resume_extractor)

    result = service.check(ats_input(job("job-1", "ONE"), job("job-2", "TWO")), config())

    assert result.jobs[0].assessment is not None
    assert result.jobs[1].failure is not None
    assert result.jobs[1].failure.category == "schema"
    assert invoker.attempts["job-2"] == 2


def test_wrong_job_key_retries_once_then_recovers() -> None:
    invoker = WrongJobKeyInvoker(["wrong-job", "job-1"])
    service = AtsCheckService(invoker, resume_extractor=fake_resume_extractor)

    result = service.check(ats_input(job("job-1", "ONE")), config())

    assert result.jobs[0].failure is None
    assert result.jobs[0].assessment is not None
    assert result.jobs[0].assessment.job_key == "job-1"
    assert invoker.attempts == 2


def test_two_wrong_job_keys_end_with_schema_failure() -> None:
    invoker = WrongJobKeyInvoker(["wrong-job", "still-wrong-job"])
    service = AtsCheckService(invoker, resume_extractor=fake_resume_extractor)

    result = service.check(ats_input(job("job-1", "ONE")), config())

    assert result.jobs[0].assessment is None
    assert result.jobs[0].failure is not None
    assert result.jobs[0].failure.category == "schema"
    assert invoker.attempts == 2


def test_resume_ai_failure_retries_twice_and_never_starts_job_requests() -> None:
    invoker = ResumeAlwaysTimeoutInvoker()
    service = AtsCheckService(invoker, resume_extractor=fake_resume_extractor)

    with pytest.raises(AtsCheckError, match="AI resume check timed out"):
        service.check(ats_input(job("job-1", "ONE")), config())

    assert invoker.kinds == ["resume", "resume"]


def test_job_result_converts_real_job_record_http_url_to_plain_string() -> None:
    real_job = job("job-1", "ONE", url="https://example.test/jobs/1")
    result = AtsCheckService(
        RecordingAtsInvoker(),
        resume_extractor=fake_resume_extractor,
    ).check(ats_input(real_job), config())

    assert result.jobs[0].url == "https://example.test/jobs/1"
    assert type(result.jobs[0].url) is str
