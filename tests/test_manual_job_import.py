from __future__ import annotations

import json
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from job_scan import manual_job_import
from job_scan.claude_process import ClaudeInvocation, ClaudeRequest
from job_scan.config import AppConfig, ClaudeSettings, SchedulerSettings
from job_scan.domain import AIReview, MachineStatus
from job_scan.reviewer import ReviewBatchOutcome, ReviewFailure


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        candidate_name="Ada",
        ai_runtime="api:deepseek",
        ai_model="deepseek-v4",
        resume_path=tmp_path / "resume.pdf",
        resume_sha256="sha256:" + "a" * 64,
        profile_sha256="sha256:" + "b" * 64,
        search_terms=["backend"],
        locations=["Berlin"],
        german_level="B1",
        claude=ClaudeSettings(
            model="sonnet",
            effort="medium",
            timeout_seconds=91,
            max_output_bytes=123_456,
        ),
        scheduler=SchedulerSettings(),
    )


class RecordingInvoker:
    def __init__(self, structured_output: dict[str, object]) -> None:
        self.structured_output = structured_output
        self.requests: list[ClaudeRequest] = []

    def invoke(self, request: ClaudeRequest) -> ClaudeInvocation:
        self.requests.append(request)
        return ClaudeInvocation(
            argv=["fake-ai"],
            stdout=json.dumps({"structured_output": self.structured_output}).encode("utf-8"),
            stderr=b"",
            exit_code=0,
            duration_seconds=0.1,
        )


class RecordingRunner:
    def __init__(self, outputs: list[dict[str, object] | str]) -> None:
        self.outputs = outputs
        self.commands: list[list[str]] = []

    def __call__(
        self,
        command: list[str],
        _timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        output = self.outputs.pop(0)
        stdout = json.dumps(output) if isinstance(output, dict) else output
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost./job",
        "https://careers.local./job",
        "https://2130706433/job",
        "https://0x7f000001/job",
        "https://0177.0.0.1/job",
    ],
)
def test_public_job_url_rejects_nonstandard_local_hosts(url: str) -> None:
    with pytest.raises(manual_job_import.ManualJobImportError, match="public HTTPS"):
        manual_job_import.require_public_job_url(url)


def test_ai_job_extractor_returns_structured_facts_from_page_text(
    tmp_path: Path,
) -> None:
    extractor_type = getattr(manual_job_import, "AiJobExtractor", None)
    assert extractor_type is not None
    description = (
        "Build and operate backend services in Python. Work with PostgreSQL, "
        "distributed systems, and production observability for customer workloads."
    )
    page_text = (
        "# Senior Backend Engineer\n\n"
        "Acme GmbH\n\nBerlin, Germany\n\n"
        f"{description}\n\nPosted 2026-08-18"
    )
    invoker = RecordingInvoker(
        {
            "is_job_detail": True,
            "title": "Senior Backend Engineer",
            "company": "Acme GmbH",
            "location": "Berlin, Germany",
            "description_sections": [description],
            "posted_at": "2026-08-18",
            "posted_at_evidence": "Posted 2026-08-18",
        }
    )

    result = extractor_type(invoker).extract(
        "https://careers.example/jobs/42",
        "Senior Backend Engineer | Acme GmbH",
        page_text,
        _config(tmp_path),
    )

    assert result.title == "Senior Backend Engineer"
    assert result.company == "Acme GmbH"
    assert result.location == "Berlin, Germany"
    assert result.description == description
    assert result.posted_at.isoformat() == "2026-08-18"
    request = invoker.requests[0]
    assert request.runtime == "api:deepseek"
    assert request.runtime_model == "deepseek-v4"
    assert request.allow_web_search is False
    assert page_text in request.prompt


def test_ai_job_extractor_uses_browser_title_and_assembles_every_job_section(
    tmp_path: Path,
) -> None:
    browser_title = "Orbit Systems GmbH - Senior Platform Engineer"
    overview = (
        "## Deine Rolle\n"
        "Du entwickelst unsere Plattform fuer internationale Energiekunden."
    )
    responsibilities = (
        "## Was du tun wirst\n"
        "Du baust Kotlin-Services und betreibst sie gemeinsam mit dem Team."
    )
    requirements = (
        "## Was du mitbringst\n"
        "Mehrjaehrige Erfahrung mit JVM-Systemen, APIs und relationalen Datenbanken."
    )
    nice_to_have = (
        "## Ein Plus\n"
        "Erfahrung mit Kubernetes und ereignisgetriebenen Architekturen."
    )
    benefits = (
        "## Deine Vorteile\n"
        "Flexible Arbeitszeiten, Weiterbildung und ein Deutschlandticket."
    )
    sections = [overview, responsibilities, requirements, nice_to_have, benefits]
    page_text = (
        "# Senior Platform Engineer\n\n"
        "Berlin, Deutschland\n\n"
        + "\n\n".join(sections)
    )
    invoker = RecordingInvoker(
        {
            "is_job_detail": True,
            "title": "Senior Platform Engineer",
            "company": "Orbit Systems GmbH",
            "location": "Berlin, Deutschland",
            "description_sections": sections,
            "posted_at": None,
            "posted_at_evidence": None,
        }
    )

    result = manual_job_import.AiJobExtractor(invoker).extract(
        "https://jobs.example/stellen/42",
        browser_title,
        page_text,
        _config(tmp_path),
    )

    assert result.company == "Orbit Systems GmbH"
    assert result.description == "\n\n".join(sections)
    request = invoker.requests[0]
    assert browser_title in request.prompt
    assert page_text in request.prompt


def test_ai_job_extractor_accepts_removed_markdown_decoration(tmp_path: Path) -> None:
    source_section = (
        "**Tasks:**\n\n"
        "-   Build reliable services for industrial energy-management workloads.\n\n"
        "-   Improve automated testing, deployment pipelines, and observability."
    )
    extracted_section = (
        "Tasks:\n\n"
        "-   Build reliable services for industrial energy-management workloads.\n\n"
        "-   Improve automated testing, deployment pipelines, and observability."
    )
    page_text = f"# Platform Engineer\n\nAcme GmbH\n\nBerlin\n\n{source_section}"
    invoker = RecordingInvoker(
        {
            "is_job_detail": True,
            "title": "Platform Engineer",
            "company": "Acme GmbH",
            "location": "Berlin",
            "description_sections": [extracted_section],
            "posted_at": None,
            "posted_at_evidence": None,
        }
    )

    result = manual_job_import.AiJobExtractor(invoker).extract(
        "https://jobs.example/stellen/42",
        "Acme GmbH - Platform Engineer",
        page_text,
        _config(tmp_path),
    )

    assert result.description == extracted_section


def test_ai_job_extractor_accepts_markdown_blank_line_variation(
    tmp_path: Path,
) -> None:
    source_working_conditions = (
        "-   Hybrid\n"
        "-   -   Darmstadt, Hessen, Germany\n\n"
        "-   Backend"
    )
    extracted_working_conditions = (
        "-   Hybrid\n\n"
        "-   -   Darmstadt, Hessen, Germany\n\n"
        "-   Backend"
    )
    description = (
        "Build and maintain Spring Boot services for an industrial energy-management "
        "platform while contributing to architecture, automated testing, and delivery."
    )
    page_text = (
        "# Senior Software Developer\n\n"
        f"{source_working_conditions}\n\n"
        f"## Job description\n\n{description}"
    )
    sections = [
        extracted_working_conditions,
        f"## Job description\n\n{description}",
    ]
    invoker = RecordingInvoker(
        {
            "is_job_detail": True,
            "title": "Senior Software Developer",
            "company": "etalytics",
            "location": "Darmstadt, Hessen, Germany",
            "description_sections": sections,
            "posted_at": None,
            "posted_at_evidence": None,
        }
    )

    result = manual_job_import.AiJobExtractor(invoker).extract(
        "https://join.etalytics.com/o/senior-software-developer",
        "etalytics - Senior Software Developer",
        page_text,
        _config(tmp_path),
    )

    assert result.description == "\n\n".join(sections)


def test_ai_job_extractor_rejects_facts_not_copied_from_page(tmp_path: Path) -> None:
    invented_description = (
        "Invented responsibilities that never appeared anywhere in the supplied job "
        "page and therefore must not be accepted as source content."
    )
    invoker = RecordingInvoker(
        {
            "is_job_detail": True,
            "title": "Backend Engineer",
            "company": "Acme",
            "location": "Berlin",
            "description_sections": [invented_description],
            "posted_at": None,
            "posted_at_evidence": None,
        }
    )
    page_text = "# Backend Engineer\n\nAcme\n\nBerlin\n\nReal page text."

    with pytest.raises(
        manual_job_import.ManualJobImportError,
        match="copied exactly",
    ):
        manual_job_import.AiJobExtractor(invoker).extract(
            "https://careers.example/jobs/42",
            "Backend Engineer | Acme",
            page_text,
            _config(tmp_path),
        )


def test_ai_job_extractor_rejects_posted_date_that_disagrees_with_evidence(
    tmp_path: Path,
) -> None:
    description = (
        "Build and operate backend services in Python. Work with PostgreSQL, "
        "distributed systems, and production observability for customer workloads."
    )
    page_text = f"# Backend Engineer\n\nAcme\n\nBerlin\n\n{description}\n\nPosted 2025-01-01"
    invoker = RecordingInvoker(
        {
            "is_job_detail": True,
            "title": "Backend Engineer",
            "company": "Acme",
            "location": "Berlin",
            "description_sections": [description],
            "posted_at": "2026-08-19",
            "posted_at_evidence": "Posted 2025-01-01",
        }
    )

    with pytest.raises(
        manual_job_import.ManualJobImportError,
        match="posted date does not match",
    ):
        manual_job_import.AiJobExtractor(invoker).extract(
            "https://careers.example/jobs/42",
            "Backend Engineer | Acme",
            page_text,
            _config(tmp_path),
        )


def test_manual_job_facts_reject_blank_required_fields() -> None:
    with pytest.raises(ValidationError, match="blank job description section"):
        manual_job_import.ManualJobFacts(
            is_job_detail=True,
            title="Backend Engineer",
            company="Acme",
            location="Berlin",
            description_sections=[" " * 80],
            posted_at=None,
            posted_at_evidence=None,
        )


def test_opencli_page_reader_reads_all_chunks_and_closes_session() -> None:
    reader_type = getattr(manual_job_import, "OpenCliPageReader", None)
    assert reader_type is not None
    runner = RecordingRunner(
        [
            {
                "url": "https://careers.example/jobs/42",
                "page": "PAGE-42",
            },
            {
                "session": "manual-session",
                "count": 1,
                "entries": [{"url": "https://careers.example/jobs/42"}],
            },
            {
                "url": "https://careers.example/jobs/42",
                "title": "Backend Engineer",
                "total_chars": 24,
                "start": 0,
                "end": 12,
                "next_start_char": 12,
                "content": "first chunk\n",
            },
            {
                "url": "https://careers.example/jobs/42",
                "title": "Backend Engineer",
                "total_chars": 24,
                "start": 12,
                "end": 24,
                "next_start_char": None,
                "content": "second chunk",
            },
            {
                "session": "manual-session",
                "count": 0,
                "entries": [],
            },
            "Browser session tab lease released",
        ]
    )

    page = reader_type(
        opencli_executable="opencli",
        runner=runner,
        session_factory=lambda: "manual-session",
        address_resolver=lambda _host: ["93.184.216.34"],
        timeout_seconds=10,
    ).read("https://careers.example/jobs/42")

    assert page.url == "https://careers.example/jobs/42"
    assert page.title == "Backend Engineer"
    assert page.content == "first chunk\nsecond chunk"
    assert runner.commands == [
        [
            "opencli",
            "browser",
            "manual-session",
            "open",
            "https://careers.example/jobs/42",
            "--window",
            "background",
        ],
        [
            "opencli",
            "browser",
            "manual-session",
            "network",
            "--all",
        ],
        [
            "opencli",
            "browser",
            "manual-session",
            "extract",
            "--chunk-size",
            "20000",
            "--start",
            "0",
        ],
        [
            "opencli",
            "browser",
            "manual-session",
            "extract",
            "--chunk-size",
            "20000",
            "--start",
            "12",
        ],
        [
            "opencli",
            "browser",
            "manual-session",
            "network",
            "--all",
        ],
        ["opencli", "browser", "manual-session", "close"],
    ]


def test_opencli_page_reader_waits_for_page_content_to_finish_rendering() -> None:
    partial_content = "Employer Privacy Policy"
    content = "Backend Engineer\nRendered job content"
    runner = RecordingRunner(
        [
            {
                "url": "https://careers.example/jobs/42",
                "page": "PAGE-42",
            },
            {
                "session": "manual-session",
                "count": 1,
                "entries": [{"url": "https://careers.example/jobs/42"}],
            },
            {
                "url": "https://careers.example/jobs/42",
                "title": "",
                "total_chars": 0,
                "start": 0,
                "end": 0,
                "next_start_char": None,
                "content": "",
            },
            {
                "url": "https://careers.example/jobs/42",
                "title": "Career Site",
                "total_chars": len(partial_content),
                "start": 0,
                "end": len(partial_content),
                "next_start_char": None,
                "content": partial_content,
            },
            {
                "url": "https://careers.example/jobs/42",
                "title": "Career Site",
                "total_chars": len(content),
                "start": 0,
                "end": len(content),
                "next_start_char": None,
                "content": content,
            },
            {
                "url": "https://careers.example/jobs/42",
                "title": "Career Site",
                "total_chars": len(content),
                "start": 0,
                "end": len(content),
                "next_start_char": None,
                "content": content,
            },
            {
                "session": "manual-session",
                "count": 0,
                "entries": [],
            },
            "Browser session tab lease released",
        ]
    )

    page = manual_job_import.OpenCliPageReader(
        opencli_executable="opencli",
        runner=runner,
        session_factory=lambda: "manual-session",
        address_resolver=lambda _host: ["93.184.216.34"],
        timeout_seconds=10,
    ).read("https://careers.example/jobs/42")

    assert page.title == "Career Site"
    assert page.content == content


def test_opencli_page_reader_rejects_a_gap_between_chunks() -> None:
    runner = RecordingRunner(
        [
            {"url": "https://careers.example/jobs/42", "page": "PAGE-42"},
            {
                "session": "manual-session",
                "count": 1,
                "entries": [{"url": "https://careers.example/jobs/42"}],
            },
            {
                "url": "https://careers.example/jobs/42",
                "title": "Backend Engineer",
                "total_chars": 100,
                "start": 0,
                "end": 50,
                "next_start_char": 100,
                "content": "partial content",
            },
            "Browser session tab lease released",
        ]
    )
    reader = manual_job_import.OpenCliPageReader(
        opencli_executable="opencli",
        runner=runner,
        session_factory=lambda: "manual-session",
        address_resolver=lambda _host: ["93.184.216.34"],
        timeout_seconds=10,
    )

    with pytest.raises(
        manual_job_import.ManualJobImportError,
        match="invalid page chunks",
    ):
        reader.read("https://careers.example/jobs/42")


def test_opencli_page_reader_rejects_chunk_whose_content_length_is_false() -> None:
    runner = RecordingRunner(
        [
            {"url": "https://careers.example/jobs/42", "page": "PAGE-42"},
            {
                "session": "manual-session",
                "count": 1,
                "entries": [{"url": "https://careers.example/jobs/42"}],
            },
            {
                "url": "https://careers.example/jobs/42",
                "title": "Backend Engineer",
                "total_chars": 50,
                "start": 0,
                "end": 50,
                "next_start_char": None,
                "content": "far too short",
            },
            "Browser session tab lease released",
        ]
    )
    reader = manual_job_import.OpenCliPageReader(
        opencli_executable="opencli",
        runner=runner,
        session_factory=lambda: "manual-session",
        address_resolver=lambda _host: ["93.184.216.34"],
        timeout_seconds=10,
    )

    with pytest.raises(
        manual_job_import.ManualJobImportError,
        match="invalid page chunks",
    ):
        reader.read("https://careers.example/jobs/42")


def test_opencli_page_reader_rejects_hostname_resolving_to_private_address() -> None:
    runner = RecordingRunner([])
    reader = manual_job_import.OpenCliPageReader(
        opencli_executable="opencli",
        runner=runner,
        session_factory=lambda: "manual-session",
        address_resolver=lambda _host: ["127.0.0.1"],
        timeout_seconds=10,
    )

    with pytest.raises(manual_job_import.ManualJobImportError, match="public address"):
        reader.read("https://careers.example/jobs/42")

    assert runner.commands == []


def test_opencli_page_reader_rejects_private_network_request_before_extract() -> None:
    runner = RecordingRunner(
        [
            {"url": "https://careers.example/jobs/42", "page": "PAGE-42"},
            {
                "session": "manual-session",
                "count": 2,
                "entries": [
                    {"url": "https://careers.example/jobs/42"},
                    {"url": "http://127.0.0.1/private"},
                ],
            },
            "Browser session tab lease released",
        ]
    )
    reader = manual_job_import.OpenCliPageReader(
        opencli_executable="opencli",
        runner=runner,
        session_factory=lambda: "manual-session",
        address_resolver=lambda _host: ["93.184.216.34"],
        timeout_seconds=10,
    )

    with pytest.raises(manual_job_import.ManualJobImportError, match="public HTTPS"):
        reader.read("https://careers.example/jobs/42")

    assert all("extract" not in command for command in runner.commands)


def test_opencli_page_reader_rejects_private_request_during_extraction() -> None:
    content = "Complete public job content"
    runner = RecordingRunner(
        [
            {"url": "https://careers.example/jobs/42", "page": "PAGE-42"},
            {
                "session": "manual-session",
                "count": 1,
                "entries": [{"url": "https://careers.example/jobs/42"}],
            },
            {
                "url": "https://careers.example/jobs/42",
                "title": "Backend Engineer",
                "total_chars": len(content),
                "start": 0,
                "end": len(content),
                "next_start_char": None,
                "content": content,
            },
            {
                "session": "manual-session",
                "count": 1,
                "entries": [{"url": "http://127.0.0.1/private"}],
            },
            "Browser session tab lease released",
        ]
    )
    reader = manual_job_import.OpenCliPageReader(
        opencli_executable="opencli",
        runner=runner,
        session_factory=lambda: "manual-session",
        address_resolver=lambda _host: ["93.184.216.34"],
        timeout_seconds=10,
    )

    with pytest.raises(manual_job_import.ManualJobImportError, match="public HTTPS"):
        reader.read("https://careers.example/jobs/42")

    assert runner.commands[-1] == ["opencli", "browser", "manual-session", "close"]


def test_manual_job_import_service_builds_and_reviews_stable_job(
    tmp_path: Path,
) -> None:
    service_type = getattr(manual_job_import, "ManualJobImportService", None)
    assert service_type is not None
    description = (
        "Build and operate backend services in Python. Work with PostgreSQL, "
        "distributed systems, and production observability for customer workloads."
    )
    page = manual_job_import.RenderedJobPage(
        url="https://careers.example/jobs/42",
        title="Backend Engineer",
        content=f"Backend Engineer\nAcme GmbH\nBerlin\n{description}",
    )
    facts = manual_job_import.ManualJobFacts(
        is_job_detail=True,
        title="Backend Engineer",
        company="Acme GmbH",
        location="Berlin",
        description_sections=[description],
        posted_at=date(2026, 8, 18),
        posted_at_evidence="Posted 2026-08-18",
    )
    extracted_titles: list[str] = []

    class PageReader:
        def read(self, _url: str) -> manual_job_import.RenderedJobPage:
            return page

    class Extractor:
        def extract(
            self,
            _url: str,
            page_title: str,
            _content: str,
            _settings: AppConfig,
        ) -> manual_job_import.ManualJobFacts:
            extracted_titles.append(page_title)
            return facts

    class Reviewer:
        def review(
            self,
            jobs: list[object],
            _profile: str,
            _settings: AppConfig,
        ) -> ReviewBatchOutcome:
            key = jobs[0].canonical_job_key
            review = AIReview(
                job_key=key,
                german_requirement="none",
                visa_sponsorship="not_mentioned",
                existing_work_authorization="not_mentioned",
                citizenship_requirement="none",
                security_clearance="none",
                staffing_agency="no",
                eligibility_evidence=[],
                company_industry=None,
                company_industry_confidence="low",
                company_industry_evidence=[],
                score=88,
                reason="Strong backend match.",
                confidence="high",
            )
            return ReviewBatchOutcome(
                accepted={key: review},
                failed={},
                invocations=[],
            )

    service = service_type(PageReader(), Extractor(), Reviewer())
    imported_at = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)

    first = service.import_url(
        "https://careers.example/jobs/42?utm_source=test",
        _config(tmp_path),
        "# Candidate profile",
        imported_at,
    )
    second = service.import_url(
        "https://careers.example/jobs/42",
        _config(tmp_path),
        "# Candidate profile",
        imported_at,
    )

    assert first.canonical_job_key == second.canonical_job_key
    assert first.source_occurrences[0].source.value == "manual"
    assert first.source_occurrences[0].source_instance == "careers.example"
    assert str(first.url) == page.url
    assert first.title == "Backend Engineer"
    assert first.company == "Acme GmbH"
    assert first.location == "Berlin"
    assert first.description == description
    assert first.posted_at == date(2026, 8, 18)
    assert first.machine_status is MachineStatus.ELIGIBLE
    assert first.score == 88
    assert extracted_titles == ["Backend Engineer", "Backend Engineer"]


def test_manual_job_import_service_keeps_failed_review_as_pending_card(
    tmp_path: Path,
) -> None:
    description = (
        "Build and operate backend services in Python. Work with PostgreSQL, "
        "distributed systems, and production observability for customer workloads."
    )
    page = manual_job_import.RenderedJobPage(
        url="https://careers.example/jobs/42",
        title="Backend Engineer",
        content=f"Backend Engineer\nAcme GmbH\nBerlin\n{description}",
    )
    facts = manual_job_import.ManualJobFacts(
        is_job_detail=True,
        title="Backend Engineer",
        company="Acme GmbH",
        location="Berlin",
        description_sections=[description],
    )

    class PageReader:
        def read(self, _url: str) -> manual_job_import.RenderedJobPage:
            return page

    class Extractor:
        def extract(
            self,
            _url: str,
            _page_title: str,
            _content: str,
            _settings: AppConfig,
        ) -> manual_job_import.ManualJobFacts:
            return facts

    class FailedReviewer:
        def review(
            self,
            jobs: list[object],
            _profile: str,
            settings: AppConfig,
        ) -> ReviewBatchOutcome:
            key = jobs[0].canonical_job_key
            failure = ReviewFailure(
                job_key=key,
                category="timeout",
                message="AI review timed out.",
                model=settings.claude.model,
            )
            return ReviewBatchOutcome(
                accepted={},
                failed={key: failure},
                invocations=[],
            )

    imported_at = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
    job = manual_job_import.ManualJobImportService(
        PageReader(),
        Extractor(),
        FailedReviewer(),
    ).import_url(
        "https://careers.example/jobs/42",
        _config(tmp_path),
        "# Candidate profile",
        imported_at,
    )

    assert job.machine_status is MachineStatus.PENDING
    assert job.score is None
    assert job.last_error == "AI review timed out."
