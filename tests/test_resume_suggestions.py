from __future__ import annotations

import json
from pathlib import Path

import pytest

from job_scan.claude_process import ClaudeInvocation, ClaudeRequest
from job_scan.config import ClaudeSettings
from job_scan.resume import ExtractedResume
from job_scan.resume_suggestions import (
    ResumeSuggestionError,
    ResumeSuggestions,
    ResumeSuggestionService,
    ResumeSuggestionSettings,
)

PRIVATE_RESUME = "PRIVATE-RESUME-CONTENT-MUST-STAY-IN-THE-AI-PROMPT"


class RecordingInvoker:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.requests: list[ClaudeRequest] = []

    def invoke(self, request: ClaudeRequest) -> ClaudeInvocation:
        self.requests.append(request)
        return ClaudeInvocation(
            argv=["fake-ai"],
            stdout=json.dumps({"structured_output": self.payload}).encode(),
            stderr=b"",
            exit_code=0,
            duration_seconds=0.01,
        )


def settings() -> ResumeSuggestionSettings:
    return ResumeSuggestionSettings(
        ai_runtime="api:deepseek",
        claude=ClaudeSettings(
            model="claude-sonnet-4-5",
            effort="high",
            thinking_enabled=False,
            batch_size=10,
            timeout_seconds=91,
            max_output_bytes=123_456,
        ),
    )


def fake_extract(path: Path) -> ExtractedResume:
    assert path.name == "resume.pdf"
    assert path.read_bytes() == b"resume bytes"
    return ExtractedResume(
        path=path,
        sha256="sha256:" + "a" * 64,
        text=PRIVATE_RESUME,
        format="pdf",
    )


def test_suggest_uses_selected_runtime_and_returns_strict_search_inputs() -> None:
    invoker = RecordingInvoker(
        {
            "search_terms": ["Java Backend Engineer", "Platform Engineer"],
        }
    )
    service = ResumeSuggestionService(invoker, resume_extractor=fake_extract)

    result = service.suggest("candidate.pdf", b"resume bytes", settings())

    assert result == ResumeSuggestions(
        search_terms=["Java Backend Engineer", "Platform Engineer"],
    )
    request = invoker.requests[0]
    assert request.runtime == "api:deepseek"
    assert request.model == "claude-sonnet-4-5"
    assert request.effort == "high"
    assert request.thinking_enabled is False
    assert request.timeout_seconds == 91
    assert request.max_output_bytes == 123_456
    assert "commonly used on German job boards" in request.prompt
    assert "strongest technical background, seniority" in request.prompt
    assert "career directions" not in request.prompt
    assert PRIVATE_RESUME in request.prompt
    assert request.allow_web_search is False


def test_suggest_rejects_invalid_structured_output() -> None:
    invoker = RecordingInvoker({"search_terms": [], "extra": True})
    service = ResumeSuggestionService(invoker, resume_extractor=fake_extract)

    with pytest.raises(ResumeSuggestionError, match="invalid suggestions"):
        service.suggest("candidate.pdf", b"resume bytes", settings())


@pytest.mark.parametrize(
    ("filename", "payload", "message"),
    [
        ("candidate.txt", b"resume bytes", "PDF or DOCX"),
        ("candidate.pdf", b"", "empty"),
    ],
)
def test_suggest_rejects_unusable_uploads(
    filename: str,
    payload: bytes,
    message: str,
) -> None:
    service = ResumeSuggestionService(
        RecordingInvoker({}),
        resume_extractor=fake_extract,
    )

    with pytest.raises(ResumeSuggestionError, match=message):
        service.suggest(filename, payload, settings())
