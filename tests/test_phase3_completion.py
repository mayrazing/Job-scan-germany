from __future__ import annotations

import json
import os
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import HttpUrl

from job_scan.claude_process import ClaudeInvocation, ClaudeRequest
from job_scan.config import AppConfig, ClaudeSettings, SchedulerSettings
from job_scan.domain import MachineStatus, SourceKind
from job_scan.locking import FileRWLock
from job_scan.normalization import content_hash
from job_scan.paths import AppPaths
from job_scan.repository import JsonlRepository
from job_scan.reviewer import ClaudeReviewer
from job_scan.scan_service import ScanService
from job_scan.setup_service import SetupAnswers, SetupService
from job_scan.sources.base import FetchedOccurrence, JobReference

NOW = datetime(2026, 8, 3, 9, tzinfo=UTC)
RESUME = Path(__file__).parent / "fixtures" / "resume" / "sample.docx"
PROFILE = """# Target roles
Backend Engineer

# Technical skills
Python, SQL

# Experience
Backend delivery

# Languages
English, German B1

# Work authorization and visa
Needs visa sponsorship

# Preferences
Berlin or remote
"""
REVIEW_PROMPT_MARKER = (
    "Submitted jobs (JSON; complete_jd is the complete plain-text JD):\n"
)


class RoutingFakeClaude:
    """Return a profile for setup and title-routed results for semantic review."""

    def __init__(self) -> None:
        self.requests: list[ClaudeRequest] = []
        self.review_call_count = 0

    def invoke(self, request: ClaudeRequest) -> ClaudeInvocation:
        self.requests.append(request)
        if REVIEW_PROMPT_MARKER not in request.prompt:
            payload: dict[str, object] = {
                "structured_output": {"profile_markdown": PROFILE}
            }
        else:
            self.review_call_count += 1
            submitted, _ = json.JSONDecoder().raw_decode(
                request.prompt.split(REVIEW_PROMPT_MARKER, 1)[1]
            )
            results = [
                self._review(item)
                for item in submitted
                if item["title"] != "Needs Retry"
            ]
            payload = {"structured_output": {"results": results}}
        return ClaudeInvocation(
            argv=["fixture-claude"],
            stdout=json.dumps(payload).encode(),
            stderr=b"",
            exit_code=0,
            duration_seconds=0.01,
            budget_usd=Decimal("0.01"),
        )

    @staticmethod
    def _review(item: dict[str, str]) -> dict[str, object]:
        hard_german = item["title"] == "Hard German Role"
        return {
            "job_key": item["job_key"],
            "german_requirement": "required" if hard_german else "optional",
            "visa_sponsorship": "not_mentioned",
            "existing_work_authorization": "not_mentioned",
            "citizenship_requirement": "none",
            "security_clearance": "none",
            "staffing_agency": "no",
            "eligibility_evidence": (
                ["Fließende Deutschkenntnisse sind zwingend erforderlich."]
                if hard_german
                else []
            ),
            "score": 71 if hard_german else 92,
            "reason": "Fixture semantic review.",
            "confidence": "high",
        }


class FixtureAdapter:
    source = SourceKind.LINKEDIN
    source_instance = "fixture/jobs"

    def __init__(self, occurrences: Sequence[FetchedOccurrence]) -> None:
        self._occurrences = {item.external_id: item for item in occurrences}

    def discover(self) -> list[JobReference]:
        return [
            JobReference(
                source=item.source,
                source_instance=item.source_instance,
                external_id=item.external_id,
                detail_url=item.url,
                listing_title=item.title,
                listing_company=item.company,
                listing_location=item.location,
                listing_posted_at=item.posted_at,
            )
            for item in self._occurrences.values()
        ]

    def fetch_detail(self, reference: JobReference) -> FetchedOccurrence:
        return self._occurrences[reference.external_id]


def _answers() -> SetupAnswers:
    return SetupAnswers(
        search_terms=["backend engineer"],
        locations=["Berlin"],
        german_level="B1",
        staffing_penalty=10,
        claude=ClaudeSettings(
            model="claude-sonnet-4-5",
            effort="high",
            batch_size=10,
        ),
        scheduler=SchedulerSettings(local_time="09:00"),
    )


def _occurrence(external_id: str, title: str, description: str) -> FetchedOccurrence:
    company = "Fixture GmbH"
    location = "Berlin"
    return FetchedOccurrence(
        source=SourceKind.LINKEDIN,
        source_instance="fixture/jobs",
        external_id=external_id,
        url=HttpUrl(f"https://fixture.example/jobs/{external_id}"),
        company=company,
        title=title,
        location=location,
        description=description,
        posted_at=date(2026, 8, 1),
        content_hash=content_hash(company, title, location, description),
        detail_complete=True,
    )


def test_phase3_completion_gate_reuses_persisted_setup_and_reviews_every_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_scan_home = tmp_path / "job-scan-home"
    monkeypatch.setenv("JOB_SCAN_HOME", str(job_scan_home))
    paths = AppPaths.from_environment(os.environ)
    fake_claude = RoutingFakeClaude()

    setup = SetupService(paths, fake_claude).run(RESUME, _answers())

    assert paths.root == job_scan_home
    assert paths.config_toml.exists()
    assert paths.profile_md.read_text(encoding="utf-8") == PROFILE
    assert fake_claude.review_call_count == 0

    adapter = FixtureAdapter(
        [
            _occurrence(
                "OPTIONAL",
                "Preferred German Backend",
                "Wir suchen Python-Erfahrung. Deutschkenntnisse sind von Vorteil.",
            ),
            _occurrence(
                "REQUIRED",
                "Hard German Role",
                "Fließende Deutschkenntnisse sind zwingend erforderlich.",
            ),
            _occurrence(
                "RETRY",
                "Needs Retry",
                "Python backend role with visa details not mentioned.",
            ),
        ]
    )
    loaded_configs: list[AppConfig] = []

    def fixture_sources(config: AppConfig) -> Sequence[FixtureAdapter]:
        loaded_configs.append(config)
        return [adapter]

    service = ScanService(
        paths,
        reviewer=ClaudeReviewer(fake_claude),
        source_factory=fixture_sources,
        clock=lambda: NOW,
    )

    first = service.run()
    review_calls_after_first = fake_claude.review_call_count
    repository = JsonlRepository(paths, FileRWLock(paths.lock_file))
    jobs_by_title = {item.title: item for item in repository.load().jobs}

    assert loaded_configs == [setup.config]
    assert first.reviewed_count == 2
    assert first.eligible_count == 2
    assert first.excluded_count == 0
    assert first.pending_count == 1
    assert first.claude_failure_count == 1
    assert review_calls_after_first == 2
    assert jobs_by_title["Preferred German Backend"].machine_status is MachineStatus.ELIGIBLE
    assert jobs_by_title["Preferred German Backend"].score == 92
    assert jobs_by_title["Hard German Role"].machine_status is MachineStatus.ELIGIBLE
    assert jobs_by_title["Hard German Role"].score == 71
    pending = jobs_by_title["Needs Retry"]
    assert pending.machine_status is MachineStatus.PENDING
    assert pending.score is None

    second = service.run()

    assert loaded_configs == [setup.config, setup.config]
    assert second.reviewed_count == 2
    assert second.claude_failure_count == 1
    assert fake_claude.review_call_count == review_calls_after_first + 2
