from __future__ import annotations

import json
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from job_scan.claude_process import (
    ClaudeInvocation,
    ClaudeProcessError,
    ClaudeRequest,
)
from job_scan.config import ClaudeSettings
from job_scan.resume import ExtractedResume, ResumeError, extract_resume

_MAX_RESUME_BYTES = 20 * 1024 * 1024


class ResumeSuggestionError(RuntimeError):
    """Report a safe resume-suggestion failure to the Setup page."""


class ResumeSuggestionSettings(BaseModel):
    """Select the configured AI runtime used for one suggestion request."""

    model_config = ConfigDict(extra="forbid")

    ai_runtime: str = Field(
        default="claude-code",
        pattern=r"^(?:claude-code|api:[a-z0-9]+(?:-[a-z0-9]+)*)$",
    )
    claude: ClaudeSettings


class ResumeSuggestions(BaseModel):
    """Return concise search inputs inferred only from one resume."""

    model_config = ConfigDict(extra="forbid")

    search_terms: list[str] = Field(min_length=1, max_length=6)

    @field_validator("search_terms")
    @classmethod
    def normalize_unique_values(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            item = " ".join(value.split())
            if not item or len(item) > 80:
                raise ValueError("suggestions must contain 1 to 80 characters")
            key = item.casefold()
            if key not in seen:
                normalized.append(item)
                seen.add(key)
        if not normalized:
            raise ValueError("at least one unique suggestion is required")
        return normalized


class SuggestionInvoker(Protocol):
    def invoke(self, request: ClaudeRequest) -> ClaudeInvocation: ...


class ResumeSuggestionService:
    """Suggest job-search inputs from one temporary uploaded resume."""

    def __init__(
        self,
        invoker: SuggestionInvoker,
        *,
        resume_extractor: Callable[[Path], ExtractedResume] = extract_resume,
    ) -> None:
        self._invoker = invoker
        self._extract_resume = resume_extractor

    def suggest(
        self,
        filename: str,
        payload: bytes,
        settings: ResumeSuggestionSettings,
    ) -> ResumeSuggestions:
        """Read one upload and return strict AI-generated search suggestions."""
        suffix = Path(filename).suffix.lower()
        if suffix not in {".pdf", ".docx"}:
            raise ResumeSuggestionError("Use a PDF or DOCX resume.")
        if not payload:
            raise ResumeSuggestionError("Uploaded resume is empty.")
        if len(payload) > _MAX_RESUME_BYTES:
            raise ResumeSuggestionError("Uploaded resume exceeds the 20 MB limit.")

        try:
            with tempfile.TemporaryDirectory(
                prefix="job-scan-resume-suggestion-"
            ) as directory:
                resume_path = Path(directory) / f"resume{suffix}"
                resume_path.write_bytes(payload)
                extracted = self._extract_resume(resume_path)
        except ResumeError:
            raise ResumeSuggestionError(
                "Could not read this resume. Use a text-based PDF or DOCX file."
            ) from None
        except OSError:
            raise ResumeSuggestionError(
                "Could not prepare this resume for analysis; retry."
            ) from None

        request = ClaudeRequest(
            runtime=settings.ai_runtime,
            prompt=_suggestion_prompt(extracted.text),
            json_schema=ResumeSuggestions.model_json_schema(),
            model=settings.claude.model,
            effort=settings.claude.effort,
            thinking_enabled=settings.claude.thinking_enabled,
            timeout_seconds=settings.claude.timeout_seconds,
            max_output_bytes=settings.claude.max_output_bytes,
        )
        try:
            invocation = self._invoker.invoke(request)
        except ClaudeProcessError as error:
            raise ResumeSuggestionError(str(error)) from None
        return _validated_suggestions(invocation)


def _suggestion_prompt(resume_text: str) -> str:
    """Build one resume-only prompt for German job-board search inputs."""
    return (
        "Analyze this resume for a job search in Germany. Return only structured "
        "suggestions grounded in the resume.\n\n"
        "search_terms must contain 3 to 6 concise English job titles commonly used on "
        "German job boards, ordered by strongest fit to the candidate's actual "
        "experience first.\n\n"
        "Prioritize titles that directly reflect the candidate's strongest technical "
        "background, seniority, and demonstrated production experience. Prefer titles "
        "whose typical job requirements substantially overlap with the resume's core "
        "technologies and system responsibilities. Avoid overly broad titles when a "
        "more specific, commonly used title better represents the candidate.\n\n"
        "Do not add locations, employers, explanations, technologies, skills, "
        "industries, or responsibilities as job titles. Do not invent titles that are "
        "not supported by the resume.\n\n"
        "Resume:\n"
        f"{resume_text}"
    )


def _validated_suggestions(invocation: ClaudeInvocation) -> ResumeSuggestions:
    """Validate one successful structured AI response without exposing resume text."""
    if invocation.exit_code != 0:
        raise ResumeSuggestionError("AI could not analyze this resume; retry.")
    try:
        envelope = json.loads(invocation.stdout)
        structured = envelope.get("structured_output")
        return ResumeSuggestions.model_validate(structured)
    except (AttributeError, UnicodeDecodeError, json.JSONDecodeError, ValidationError):
        raise ResumeSuggestionError(
            "AI returned invalid suggestions; retry."
        ) from None
