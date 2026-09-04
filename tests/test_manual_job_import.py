from __future__ import annotations

import hashlib
import json
import socket
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from job_scan import manual_job_import
from job_scan.claude_process import ClaudeInvocation, ClaudeRequest
from job_scan.config import AppConfig, ClaudeSettings, SchedulerSettings
from job_scan.domain import AIReview, MachineStatus
from job_scan.job_snapshot import JobSnapshotStore
from job_scan.reviewer import ReviewBatchOutcome, ReviewFailure

_MANUAL_SNAPSHOT_HTML = (
    "<!doctype html>"
    '<html data-job-scan-snapshot="manual:careers.example:'
    '012a125eb63d741726a6e7b75a5afffa53c6725e5d231bb652fa377998b48a83">'
    "<head><style>h1{color:#2557a7}</style></head>"
    "<body><main><h1>Backend Engineer</h1></main></body></html>"
)


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


def test_ai_job_extractor_accepts_visible_text_from_rendered_body_html(
    tmp_path: Path,
) -> None:
    title = "Senior Platform Engineer"
    description = (
        "Responsibilities:\n"
        "- Build reliable backend services for international banking customers.\n"
        "- Operate production systems, improve observability, and automate delivery."
    )
    page_html = (
        "<section><h1>Senior <span>Platform</span> Engineer</h1>"
        "<p>Acme <strong>GmbH</strong></p><p>Berlin, Germany</p>"
        "<section><h2><strong>Responsibilities</strong>:</h2><ul>"
        "<li>Build reliable <strong>backend services</strong> for international banking "
        "customers.</li></ul><button>Apply now</button><ul>"
        "<li>Operate production systems, improve observability, and "
        "automate delivery.</li></ul></section></section>"
    )
    invoker = RecordingInvoker(
        {
            "is_job_detail": True,
            "title": title,
            "company": "Acme GmbH",
            "location": "Berlin, Germany",
            "description_sections": [description],
            "posted_at": None,
            "posted_at_evidence": None,
        }
    )

    result = manual_job_import.AiJobExtractor(invoker).extract(
        "https://careers.example/jobs/42",
        "Career page",
        page_html,
        _config(tmp_path),
    )

    assert result.title == title
    assert result.company == "Acme GmbH"
    assert result.description == description
    assert "rendered body HTML" in invoker.requests[0].prompt
    assert "visible text" in invoker.requests[0].prompt


def test_ai_job_extractor_ignores_only_layout_unicode_characters(
    tmp_path: Path,
) -> None:
    description = (
        "Develop C++ and C# services with JavaEE and PostgreSQL. Preserve German "
        "Fähigkeiten, salaries, dates, and visible-hyphens in production systems."
    )
    page_description = (
        description.replace("C++", "C+\u200b+")
        .replace("JavaEE", "Java\u2060EE")
        .replace("PostgreSQL", "Post\ufeffgreSQL")
    )
    page_html = (
        "<main><h1>Java Backend Devel\u00adoper (m/w/d)</h1>"
        "<p>FERCHAU GmbH</p><p>Stutt\u00adgart, Deutschland</p>"
        f"<section>{page_description}</section></main>"
    )
    invoker = RecordingInvoker(
        {
            "is_job_detail": True,
            "title": "Java Backend Developer (m/w/d)",
            "company": "FERCHAU GmbH",
            "location": "Stuttgart, Deutschland",
            "description_sections": [description],
            "posted_at": None,
            "posted_at_evidence": None,
        }
    )

    result = manual_job_import.AiJobExtractor(invoker).extract(
        "https://touch.ferchau.com/de/de/job/351172/java-backend-developer",
        "FERCHAU job page",
        page_html,
        _config(tmp_path),
    )

    assert result.title == "Java Backend Developer (m/w/d)"
    assert result.location == "Stuttgart, Deutschland"
    assert result.description == description
    assert result.source_validation_errors == ()


@pytest.mark.parametrize(
    ("source_title", "extracted_title"),
    [
        ("C# Devel\u00adoper", "C Developer"),
        ("C++ Devel\u00adoper", "C+ Developer"),
        ("Fähige Devel\u00adoper", "Fahige Developer"),
        ("Backend-Devel\u00adoper", "Backend Developer"),
        ("Java\u200dDevel\u00adoper", "JavaDeveloper"),
    ],
)
def test_ai_job_extractor_keeps_meaningful_unicode_and_symbols_strict(
    tmp_path: Path,
    source_title: str,
    extracted_title: str,
) -> None:
    description = (
        "Develop reliable backend services and maintain production observability "
        "for international customers using stable engineering practices."
    )
    page_html = (
        f"<main><h1>{source_title}</h1><p>FERCHAU GmbH</p>"
        f"<p>Stuttgart, Deutschland</p><section>{description}</section></main>"
    )
    invoker = RecordingInvoker(
        {
            "is_job_detail": True,
            "title": extracted_title,
            "company": "FERCHAU GmbH",
            "location": "Stuttgart, Deutschland",
            "description_sections": [description],
            "posted_at": None,
            "posted_at_evidence": None,
        }
    )

    result = manual_job_import.AiJobExtractor(invoker).extract(
        "https://touch.ferchau.com/de/de/job/351172/java-backend-developer",
        "FERCHAU job page",
        page_html,
        _config(tmp_path),
    )

    assert len(result.source_validation_errors) == 1
    assert result.source_validation_errors[0].startswith(
        f'Job title: AI read "{extracted_title}".'
    )
    assert "Check that this information is correct." in result.source_validation_errors[0]


def test_ai_job_extractor_records_facts_not_copied_from_page(tmp_path: Path) -> None:
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

    result = manual_job_import.AiJobExtractor(invoker).extract(
        "https://careers.example/jobs/42",
        "Backend Engineer | Acme",
        page_text,
        _config(tmp_path),
    )

    expected_error = (
        f'Job description: AI read "{invented_description}". The same wording was not '
        "found on the original page. Check that this information is correct."
    )
    assert result.source_validation_errors == (expected_error,)


def test_ai_job_extractor_records_description_stitched_across_page_regions(
    tmp_path: Path,
) -> None:
    first_job_text = (
        "Build reliable payment APIs and maintain production observability for "
        "international banking customers."
    )
    unrelated_job_text = (
        "Operate Kubernetes clusters and design data pipelines for a separate "
        "analytics vacancy."
    )
    page_html = (
        "<main><article><h1>Backend Engineer</h1><p>Acme GmbH</p><p>Berlin</p>"
        f"<section>{first_job_text}</section></article>"
        "<aside><h2>Other vacancy</h2>"
        f"<section>{unrelated_job_text}</section></aside></main>"
    )
    invoker = RecordingInvoker(
        {
            "is_job_detail": True,
            "title": "Backend Engineer",
            "company": "Acme GmbH",
            "location": "Berlin",
            "description_sections": [f"{first_job_text} {unrelated_job_text}"],
            "posted_at": None,
            "posted_at_evidence": None,
        }
    )

    result = manual_job_import.AiJobExtractor(invoker).extract(
        "https://careers.example/jobs/42",
        "Backend Engineer | Acme GmbH",
        page_html,
        _config(tmp_path),
    )

    assert len(result.source_validation_errors) == 1
    assert result.source_validation_errors[0].startswith(
        f'Job description: AI read "{first_job_text} {unrelated_job_text}".'
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
    content = "first chunk\nsecond chunk"
    content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    first_chunk = {
        "url": "https://careers.example/jobs/42",
        "title": "Backend Engineer",
        "total_chars": 24,
        "content_sha256": content_sha256,
        "start": 0,
        "end": 12,
        "next_start_char": 12,
        "content": "first chunk\n",
    }
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
            first_chunk,
            first_chunk,
            {
                "url": "https://careers.example/jobs/42",
                "title": "Backend Engineer",
                "total_chars": 24,
                "content_sha256": content_sha256,
                "start": 12,
                "end": 24,
                "next_start_char": None,
                "content": "second chunk",
            },
            {"status": "ok", "html": _MANUAL_SNAPSHOT_HTML},
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
    assert page.content == content
    assert page.snapshot_html == _MANUAL_SNAPSHOT_HTML
    assert runner.commands[:2] == [
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
    ]
    assert runner.commands[2][:4] == [
        "opencli",
        "browser",
        "manual-session",
        "eval",
    ]
    assert runner.commands[3][:4] == [
        "opencli",
        "browser",
        "manual-session",
        "eval",
    ]
    first_script = runner.commands[2][4]
    assert "document.body" in first_script
    assert "clone.querySelectorAll('img')" in first_script
    assert "image.remove()" in first_script
    assert "image.replaceWith" not in first_script
    assert "script, style, noscript, template" in first_script
    assert "nav, header, footer, aside" not in first_script
    assert "form, dialog" not in first_script
    assert runner.commands[4][:4] == [
        "opencli",
        "browser",
        "manual-session",
        "eval",
    ]
    assert runner.commands[5][:4] == [
        "opencli",
        "browser",
        "manual-session",
        "eval",
    ]
    assert runner.commands[6:] == [
        [
            "opencli",
            "browser",
            "manual-session",
            "network",
            "--all",
        ],
        ["opencli", "browser", "manual-session", "close"],
    ]


def test_opencli_page_reader_captures_ai_html_and_snapshot_in_one_session() -> None:
    content = "Backend Engineer content"
    page_payload = {
        "url": "https://careers.example/jobs/42",
        "title": "Backend Engineer",
        "total_chars": 24,
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "start": 0,
        "end": 24,
        "next_start_char": None,
        "content": content,
    }
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
            page_payload,
            page_payload,
            {"status": "ok", "html": _MANUAL_SNAPSHOT_HTML},
            {
                "session": "manual-session",
                "count": 1,
                "entries": [{"url": "https://careers.example/jobs/42"}],
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

    assert page.content == content
    assert page.snapshot_html == _MANUAL_SNAPSHOT_HTML
    assert [command[3] for command in runner.commands if len(command) > 3].count("open") == 1
    eval_commands = [command for command in runner.commands if command[3] == "eval"]
    assert len(eval_commands) == 3
    assert "manual:careers.example:012a125e" in eval_commands[2][4]
    assert runner.commands[-1] == ["opencli", "browser", "manual-session", "close"]


def test_opencli_page_reader_records_diagnostics_on_invalid_content(tmp_path) -> None:
    diagnostics_dir = tmp_path / "logs"
    diagnostics_dir.mkdir()
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
                "title": "Career Site",
                "total_chars": len(content),
                "start": 0,
                "end": len(content),
                "next_start_char": None,
                "content": content,
            },
            "Browser session tab lease released",
        ]
    )

    with pytest.raises(
        manual_job_import.ManualJobImportError,
        match="OpenCLI returned invalid page content.",
    ):
        manual_job_import.OpenCliPageReader(
            opencli_executable="opencli",
            runner=runner,
            session_factory=lambda: "manual-session",
            address_resolver=lambda _host: ["93.184.216.34"],
            timeout_seconds=10,
            diagnostics_dir=diagnostics_dir,
        ).read("https://careers.example/jobs/42")

    log_path = diagnostics_dir / "manual-import.jsonl"
    record = json.loads(log_path.read_text(encoding="utf-8"))
    assert record["kind"] == "read_failed"
    assert record["error"] == "OpenCLI returned invalid page content."
    assert record["url"] == "https://careers.example/jobs/42"
    assert record["last_payload"]["content"] == f"str[{len(content)}]"
    assert content not in log_path.read_text(encoding="utf-8")


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
                "content_sha256": hashlib.sha256(partial_content.encode("utf-8")).hexdigest(),
                "start": 0,
                "end": len(partial_content),
                "next_start_char": None,
                "content": partial_content,
            },
            {
                "url": "https://careers.example/jobs/42",
                "title": "Career Site",
                "total_chars": len(content),
                "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "start": 0,
                "end": len(content),
                "next_start_char": None,
                "content": content,
            },
            {
                "url": "https://careers.example/jobs/42",
                "title": "Career Site",
                "total_chars": len(content),
                "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "start": 0,
                "end": len(content),
                "next_start_char": None,
                "content": content,
            },
            {"status": "ok", "html": _MANUAL_SNAPSHOT_HTML},
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


def test_opencli_page_reader_waits_for_nonempty_page_content_to_stabilize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    partial_content = "Employer Privacy Policy"
    content = "Backend Engineer Detail"
    assert len(partial_content) == len(content)

    def page_payload(body: str) -> dict[str, object]:
        return {
            "url": "https://careers.example/jobs/42",
            "title": "Career Site",
            "total_chars": len(body),
            "content_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "start": 0,
            "end": len(body),
            "next_start_char": None,
            "content": body,
        }

    runner = RecordingRunner(
        [
            {"url": "https://careers.example/jobs/42", "page": "PAGE-42"},
            {
                "session": "manual-session",
                "count": 1,
                "entries": [{"url": "https://careers.example/jobs/42"}],
            },
            page_payload(partial_content),
            page_payload(content),
            page_payload(content),
            {"status": "ok", "html": _MANUAL_SNAPSHOT_HTML},
            {"session": "manual-session", "count": 0, "entries": []},
            "Browser session tab lease released",
        ]
    )
    monkeypatch.setattr(manual_job_import.time, "sleep", lambda _seconds: None)

    page = manual_job_import.OpenCliPageReader(
        opencli_executable="opencli",
        runner=runner,
        session_factory=lambda: "manual-session",
        address_resolver=lambda _host: ["93.184.216.34"],
        timeout_seconds=10,
    ).read("https://careers.example/jobs/42")

    assert page.content == content


def test_opencli_page_reader_restarts_when_page_changes_during_chunking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_version = "Backend role first|old second section"
    stable_version = "Backend role first|new second section"
    assert len(first_version) == len(stable_version)
    split_at = first_version.index("|") + 1

    def page_payload(body: str, start: int) -> dict[str, object]:
        end = min(len(body), start + split_at)
        return {
            "url": "https://careers.example/jobs/42",
            "title": "Career Site",
            "total_chars": len(body),
            "content_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "start": start,
            "end": end,
            "next_start_char": end if end < len(body) else None,
            "content": body[start:end],
        }

    runner = RecordingRunner(
        [
            {"url": "https://careers.example/jobs/42", "page": "PAGE-42"},
            {
                "session": "manual-session",
                "count": 1,
                "entries": [{"url": "https://careers.example/jobs/42"}],
            },
            page_payload(first_version, 0),
            page_payload(first_version, 0),
            page_payload(stable_version, split_at),
            page_payload(stable_version, 0),
            page_payload(stable_version, split_at),
            {"status": "ok", "html": _MANUAL_SNAPSHOT_HTML},
            {"session": "manual-session", "count": 0, "entries": []},
            "Browser session tab lease released",
        ]
    )
    monkeypatch.setattr(manual_job_import.time, "sleep", lambda _seconds: None)

    page = manual_job_import.OpenCliPageReader(
        opencli_executable="opencli",
        runner=runner,
        session_factory=lambda: "manual-session",
        address_resolver=lambda _host: ["93.184.216.34"],
        timeout_seconds=10,
    ).read("https://careers.example/jobs/42")

    assert page.content == stable_version


def test_rendered_body_chunk_filters_iframes_before_measuring_content() -> None:
    script = manual_job_import._rendered_body_chunk_script(0)

    assert "'iframe'" in script


def test_rendered_body_chunk_does_not_split_utf16_surrogate_pairs() -> None:
    script = manual_job_import._rendered_body_chunk_script(0)

    assert "html.charCodeAt(end - 1)" in script
    assert "html.charCodeAt(end)" in script


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
                "content_sha256": "a" * 64,
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
                "content_sha256": "a" * 64,
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


def test_network_capture_resolves_each_hostname_once() -> None:
    resolved_hosts: list[str] = []

    def resolver(host: str) -> list[str]:
        resolved_hosts.append(host)
        return ["93.184.216.34"]

    manual_job_import._validate_network_capture(
        {
            "count": 3,
            "entries": [
                {"url": "https://cdn.example/app.js"},
                {"url": "https://cdn.example/styles.css"},
                {"url": "https://analytics.example/event"},
            ],
        },
        resolver,
    )

    assert resolved_hosts == ["cdn.example", "analytics.example"]


def test_network_capture_ignores_browser_extension_requests() -> None:
    resolved_hosts: list[str] = []

    def resolver(host: str) -> list[str]:
        resolved_hosts.append(host)
        return ["93.184.216.34"]

    manual_job_import._validate_network_capture(
        {
            "count": 2,
            "entries": [
                {"url": "https://careers.example/jobs/42"},
                {"status": 0, "url": "chrome-extension://invalid/"},
            ],
        },
        resolver,
    )

    assert resolved_hosts == ["careers.example"]


def test_network_capture_does_not_resolve_a_failed_subresource() -> None:
    resolved_hosts: list[str] = []

    def resolver(host: str) -> list[str]:
        resolved_hosts.append(host)
        return ["93.184.216.34"]

    manual_job_import._validate_network_capture(
        {
            "count": 2,
            "entries": [
                {"status": 200, "url": "https://careers.example/jobs/42"},
                {"status": 0, "url": "https://broken-tracker.example/event"},
            ],
        },
        resolver,
    )

    assert resolved_hosts == ["careers.example"]


def test_host_resolution_retries_a_temporary_dns_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    def flaky_getaddrinfo(
        _host: str,
        _port: int,
        *,
        type: socket.SocketKind,
    ) -> list[tuple[object, object, object, str, tuple[str, int]]]:
        nonlocal attempts
        attempts += 1
        assert type == socket.SOCK_STREAM
        if attempts < 3:
            raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(manual_job_import.socket, "getaddrinfo", flaky_getaddrinfo)

    assert manual_job_import._resolve_host_addresses("cdn.example") == (
        "93.184.216.34",
    )
    assert attempts == 3


def test_host_resolution_error_identifies_the_failed_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failed_getaddrinfo(
        _host: str,
        _port: int,
        *,
        type: socket.SocketKind,
    ) -> list[tuple[object, object, object, str, tuple[str, int]]]:
        assert type == socket.SOCK_STREAM
        raise socket.gaierror(socket.EAI_NONAME, "Name not known")

    monkeypatch.setattr(manual_job_import.socket, "getaddrinfo", failed_getaddrinfo)

    with pytest.raises(
        manual_job_import.ManualJobImportError,
        match=r"cdn\.example",
    ):
        manual_job_import._resolve_host_addresses("cdn.example")


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
    page_payload = {
        "url": "https://careers.example/jobs/42",
        "title": "Backend Engineer",
        "total_chars": len(content),
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "start": 0,
        "end": len(content),
        "next_start_char": None,
        "content": content,
    }
    runner = RecordingRunner(
        [
            {"url": "https://careers.example/jobs/42", "page": "PAGE-42"},
            {
                "session": "manual-session",
                "count": 1,
                "entries": [{"url": "https://careers.example/jobs/42"}],
            },
            page_payload,
            page_payload,
            {"status": "ok", "html": _MANUAL_SNAPSHOT_HTML},
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


def test_manual_job_import_service_builds_job_and_keeps_source_validation_errors(
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
        snapshot_html=_MANUAL_SNAPSHOT_HTML,
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
    source_validation_error = (
        'Job title: AI read "Backend Engineer". The same wording was not found on the '
        "original page. Check that this information is correct."
    )
    facts._source_validation_errors.append(source_validation_error)
    extracted_titles: list[str] = []
    import_events: list[str] = []

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
            import_events.append("review")
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

    snapshot_store = JobSnapshotStore(tmp_path / "job-snapshots")
    service = service_type(PageReader(), Extractor(), Reviewer(), snapshot_store)
    imported_at = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)

    first = service.import_url(
        "https://careers.example/jobs/42?utm_source=test",
        _config(tmp_path),
        "# Candidate profile",
        imported_at,
        on_job_extracted=lambda _job: import_events.append("extracted"),
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
    assert first.manual_import_errors == [source_validation_error]
    snapshot_reference = first.source_occurrences[0].job_snapshot
    assert snapshot_reference is not None
    assert snapshot_reference.captured_at == imported_at
    assert snapshot_store.read(snapshot_reference.snapshot_id) == page.snapshot_html.encode("utf-8")
    assert extracted_titles == ["Backend Engineer", "Backend Engineer"]
    assert import_events == ["extracted", "review", "review"]


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
        snapshot_html=_MANUAL_SNAPSHOT_HTML,
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
        JobSnapshotStore(tmp_path / "job-snapshots"),
    ).import_url(
        "https://careers.example/jobs/42",
        _config(tmp_path),
        "# Candidate profile",
        imported_at,
    )

    assert job.machine_status is MachineStatus.PENDING
    assert job.score is None
    assert job.last_error == "AI review timed out."
