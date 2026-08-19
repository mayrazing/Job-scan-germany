from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from pydantic import HttpUrl, ValidationError

from job_scan.company_industry import (
    COMPANY_INDUSTRY_PAGE_JS,
    native_company_industry_evidence,
)
from job_scan.company_size import native_company_size_evidence
from job_scan.config import AppConfig
from job_scan.domain import CompanyIndustrySource, CompanySizeEvidence, SourceKind
from job_scan.http_client import InvalidResponse
from job_scan.normalization import content_hash
from job_scan.sources.base import (
    BrowserSourceError,
    CompanyProfileFacts,
    FetchedOccurrence,
    JobReference,
    SourceError,
)
from job_scan.sources.opencli_challenge import (
    DEFAULT_CHALLENGE_WAIT_SECONDS,
    is_challenge_error,
    is_challenge_payload,
    wait_for_challenge_clearance,
)

_DEFAULT_TIMEOUT_SECONDS = 300
_MAX_OUTPUT_BYTES = 5_000_000
_LINKEDIN_JOB_PATH = re.compile(r"^/jobs/view/(\d+)(?:/|$)")
_DATE_POSTED_FILTERS = {
    0: "24h",
    1: "24h",
    3: "week",
    7: "week",
    14: "month",
}

_LINKEDIN_HUMAN_GATE_JS = r"""
(() => {
  const text = [
    location.href || "",
    document.title || "",
    document.body?.innerText || "",
  ].join("\n");
  const challenge = /linkedin\.com\/(login|checkpoint|authwall)/i.test(text) ||
    /captcha|verification required|verify you are human|安全验证|登录领英/i.test(text) ||
    /\b(sign in|log in|join linkedin)\b/i.test(text);
  return {status: challenge ? "challenge" : "ok"};
})()
""".strip()

_COMPANY_PAGE_JS = r"""
(async () => {
  const isChallenge = () => /Just a moment|Access Denied/i.test(document.title || "") ||
    /captcha|verification required|verify you are human|安全验证|登录领英/i.test(document.body?.innerText || "") ||
    /linkedin\.com\/(login|checkpoint|authwall)/i.test(location.href) ||
    /\b(sign in|log in|join linkedin)\b/i.test(`${document.title || ""}\n${document.body?.innerText || ""}`) ||
    !!document.querySelector('iframe[src*="captcha" i]');
  if (isChallenge()) return {status: "challenge", reported_size: ""};
  const readSize = () => {
    const lines = (document.body?.innerText || "")
      .split(/\n+/)
      .map((value) => value.replace(/\s+/g, " ").trim())
      .filter(Boolean);
    const labelIndex = lines.findIndex((line) =>
      /^(Company size|Unternehmensgröße|Employees)$/i.test(line)
    );
    if (labelIndex < 0) return "";
    for (const line of lines.slice(labelIndex + 1, labelIndex + 4)) {
      if (/\d[\d., ]*(?:\s*(?:to|bis|[-–])\s*\d[\d., ]*|\+)/i.test(line)) {
        return line;
      }
    }
    return "";
  };
  let reportedSize = readSize();
  for (let index = 0; index < 20 && !reportedSize; index += 1) {
    await new Promise((resolve) => setTimeout(resolve, 500));
    if (isChallenge()) return {status: "challenge", reported_size: ""};
    reportedSize = readSize();
  }
  return {status: "ok", reported_size: reportedSize};
})()
""".strip()


def lookup_company_size(
    external_id: str,
    company: str,
    checked_at: datetime,
    *,
    opencli_executable: str | Path | None = None,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
) -> CompanySizeEvidence | None:
    """Read Company size from one LinkedIn company About page through OpenCLI."""
    return _lookup_company_facts(
        external_id,
        company,
        checked_at,
        opencli_executable=opencli_executable,
        timeout_seconds=timeout_seconds,
        include_industry=False,
    ).company_size


def lookup_company_facts(
    external_id: str,
    company: str,
    checked_at: datetime,
    *,
    opencli_executable: str | Path | None = None,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
) -> CompanyProfileFacts:
    """Read size and industry during one LinkedIn company-page visit."""
    return _lookup_company_facts(
        external_id,
        company,
        checked_at,
        opencli_executable=opencli_executable,
        timeout_seconds=timeout_seconds,
        include_industry=True,
    )


def _lookup_company_facts(
    external_id: str,
    company: str,
    checked_at: datetime,
    *,
    opencli_executable: str | Path | None,
    timeout_seconds: int,
    include_industry: bool,
) -> CompanyProfileFacts:
    """Read requested company facts without opening the profile more than once."""
    if not external_id.isdigit():
        raise ValueError("LinkedIn external_id must contain digits")
    executable = str(opencli_executable or _find_opencli())
    job_url = f"https://www.linkedin.com/jobs/view/{external_id}"
    detail_result = _run_persistent_linkedin_command(
        executable,
        ["linkedin", "job-detail", job_url, "-f", "json"],
        timeout_seconds,
        DEFAULT_CHALLENGE_WAIT_SECONDS,
        timeout_message="LinkedIn company lookup through OpenCLI timed out.",
    )
    if detail_result.returncode != 0:
        raise _opencli_failure(detail_result.returncode)
    detail_stdout = detail_result.stdout
    try:
        detail_payload = json.loads(detail_stdout)
    except json.JSONDecodeError:
        raise InvalidResponse("OpenCLI LinkedIn company detail was not valid JSON") from None
    detail = (
        detail_payload[0]
        if isinstance(detail_payload, list) and detail_payload
        else detail_payload
    )
    if not isinstance(detail, dict):
        return CompanyProfileFacts()
    about_url = _linkedin_company_about_url(detail.get("company_url"))
    if about_url is None:
        return CompanyProfileFacts()

    session = f"job-scan-linkedin-size-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    try:
        _run_company_opencli(
            executable,
            ["browser", session, "open", about_url, "--window", "background"],
            timeout_seconds,
        )

        def read_company_page(timeout_override: float | None = None) -> object:
            effective_timeout = (
                timeout_seconds
                if timeout_override is None
                else min(timeout_seconds, timeout_override)
            )
            stdout = _run_company_opencli(
                executable,
                ["browser", session, "eval", _COMPANY_PAGE_JS],
                effective_timeout,
            )
            try:
                return json.loads(stdout)
            except json.JSONDecodeError:
                raise InvalidResponse(
                    "OpenCLI LinkedIn company output was not valid JSON"
                ) from None

        page_payload = wait_for_challenge_clearance(
            read_company_page,
            is_challenge_payload,
            read_with_timeout=read_company_page,
        )
        if is_challenge_payload(page_payload):
            return CompanyProfileFacts()
        industry_stdout = (
            _run_company_opencli(
                executable,
                ["browser", session, "eval", COMPANY_INDUSTRY_PAGE_JS],
                timeout_seconds,
            )
            if include_industry
            else None
        )
        try:
            industry_payload = json.loads(industry_stdout) if industry_stdout is not None else None
        except json.JSONDecodeError:
            raise InvalidResponse("OpenCLI LinkedIn company output was not valid JSON") from None
    finally:
        _close_company_session(executable, session, timeout_seconds)
    if not isinstance(page_payload, dict) or page_payload.get("status") != "ok":
        return CompanyProfileFacts()
    reported_size = _optional_string(page_payload.get("reported_size"))
    reported_industry = (
        _optional_string(industry_payload.get("reported_industry"))
        if isinstance(industry_payload, dict) and industry_payload.get("status") == "ok"
        else None
    )
    return CompanyProfileFacts(
        company_size=(
            native_company_size_evidence(
                company=company,
                reported_size=reported_size,
                source_url=about_url,
                source_title="LinkedIn company profile",
                source_name="linkedin",
                checked_at=checked_at,
            )
            if reported_size is not None
            else None
        ),
        company_industry=(
            native_company_industry_evidence(
                company=company,
                reported_industry=reported_industry,
                source_url=about_url,
                source_title="LinkedIn company profile",
                source_name="linkedin",
                checked_at=checked_at,
            )
            if reported_industry is not None
            else None
        ),
    )


class LinkedinAdapter:
    """Read LinkedIn search results through the user's connected OpenCLI browser."""

    source = SourceKind.LINKEDIN
    source_instance = "default"

    def __init__(
        self,
        config: AppConfig,
        *,
        opencli_executable: str | Path | None = None,
        limit: int | None = None,
        timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
        challenge_wait_seconds: float = DEFAULT_CHALLENGE_WAIT_SECONDS,
    ) -> None:
        resolved_limit = config.linkedin_limit if limit is None else limit
        if not 1 <= resolved_limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        if challenge_wait_seconds < 0:
            raise ValueError("challenge_wait_seconds must not be negative")
        self._config = config
        self._opencli_executable = str(
            opencli_executable if opencli_executable is not None else _find_opencli()
        )
        self._limit = resolved_limit
        self._timeout_seconds = timeout_seconds
        self._challenge_wait_seconds = challenge_wait_seconds
        self._occurrences: dict[str, FetchedOccurrence] = {}
        self._discovery_errors: list[SourceError] = []
        self._completed_listing = True

    def discover(self) -> list[JobReference]:
        """Return deduplicated LinkedIn jobs for every configured term and location."""
        self._occurrences.clear()
        self._discovery_errors.clear()
        self._completed_listing = True
        references: list[JobReference] = []
        reference_indexes: dict[str, int] = {}
        locations = self._config.locations or ["Germany"]

        for search_term in self._config.search_terms:
            for location in locations:
                try:
                    rows = self._search(search_term, location)
                except (BrowserSourceError, InvalidResponse) as error:
                    self._completed_listing = False
                    source_error = (
                        SourceError(
                            category="browser",
                            source=self.source,
                            source_instance=self.source_instance,
                            error_code=error.error_code,
                            message=str(error),
                        )
                        if isinstance(error, BrowserSourceError)
                        else SourceError(
                            category="contract",
                            source=self.source,
                            source_instance=self.source_instance,
                            error_code="invalid_response",
                            message=str(error),
                        )
                    )
                    self._discovery_errors.append(source_error)
                    if is_challenge_error(error):
                        return []
                    continue
                for row in rows:
                    try:
                        reference, occurrence = self._parse_row(row)
                    except InvalidResponse as error:
                        self._discovery_errors.append(
                            SourceError(
                                category="contract",
                                source=self.source,
                                source_instance=self.source_instance,
                                item_key=_row_item_key(row),
                                error_code="invalid_response",
                                message=str(error),
                            )
                        )
                        continue
                    existing_index = reference_indexes.get(reference.external_id)
                    if existing_index is not None:
                        existing_reference = references[existing_index]
                        existing_occurrence = self._occurrences[reference.external_id]
                        if _detail_quality(reference, occurrence) > _detail_quality(
                            existing_reference, existing_occurrence
                        ):
                            references[existing_index] = reference
                            self._occurrences[reference.external_id] = occurrence
                        continue
                    reference_indexes[reference.external_id] = len(references)
                    references.append(reference)
                    self._occurrences[reference.external_id] = occurrence
        return references

    @property
    def completed_listing(self) -> bool:
        """Return whether every configured LinkedIn query completed."""
        return self._completed_listing

    def drain_discovery_errors(self) -> list[SourceError]:
        """Return and clear malformed-row errors from the latest search."""
        errors = self._discovery_errors
        self._discovery_errors = []
        return errors

    def fetch_detail(self, reference: JobReference) -> FetchedOccurrence:
        """Return detail data collected by the matching OpenCLI search command."""
        if reference.source is not self.source:
            raise ValueError("reference source must be linkedin")
        try:
            return self._occurrences[reference.external_id]
        except KeyError:
            raise ValueError(
                f"LinkedIn detail was not discovered for {reference.external_id}"
            ) from None

    def _search(self, search_term: str, location: str) -> list[object]:
        arguments = [
            "linkedin",
            "search",
            search_term,
            "--location",
            location,
            "--limit",
            str(self._limit),
            "--details",
        ]
        posted_within_days = self._config.posted_within_days
        if posted_within_days is not None:
            date_posted_filter = _DATE_POSTED_FILTERS[posted_within_days]
            arguments.extend(["--date-posted", date_posted_filter])
        arguments.extend(["-f", "json"])

        result = _run_persistent_linkedin_command(
            self._opencli_executable,
            arguments,
            self._timeout_seconds,
            self._challenge_wait_seconds,
            timeout_message="LinkedIn search through OpenCLI timed out.",
        )

        if result.returncode == 66:
            return []
        if result.returncode != 0:
            raise _opencli_failure(result.returncode)
        if len(result.stdout.encode("utf-8")) > _MAX_OUTPUT_BYTES:
            raise InvalidResponse("OpenCLI LinkedIn output exceeded the size limit")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            raise InvalidResponse("OpenCLI LinkedIn output was not valid JSON") from None
        if not isinstance(payload, list):
            raise InvalidResponse("OpenCLI LinkedIn output must be a JSON array")
        return payload

    def _parse_row(self, value: object) -> tuple[JobReference, FetchedOccurrence]:
        if not isinstance(value, dict):
            raise InvalidResponse("OpenCLI LinkedIn result must be an object")
        title = _required_string(value, "title")
        company = _required_string(value, "company")
        location = _required_string(value, "location")
        job_url = _required_string(value, "url")
        external_id = _linkedin_job_id(job_url)
        posted_at = _optional_date(value.get("listed"))
        description = _optional_string(value.get("description"))
        apply_url = _application_url(value.get("apply_url"))
        detail_complete = bool(description)
        fetch_error_code = None
        if not detail_complete:
            fetch_error_code = (
                "linkedin_detail_failed"
                if _optional_string(value.get("detail_error"))
                else "missing_full_description"
            )
        try:
            detail_url = HttpUrl(job_url)
        except ValidationError:
            raise InvalidResponse("OpenCLI LinkedIn result contained an invalid job URL") from None
        industry_source = CompanyIndustrySource(
            source_name="linkedin",
            lookup_url=detail_url,
            public_url=detail_url,
            source_title="LinkedIn company profile",
        )
        reference = JobReference(
            source=self.source,
            source_instance=self.source_instance,
            external_id=external_id,
            detail_url=detail_url,
            listing_title=title,
            listing_company=company,
            listing_location=location,
            listing_posted_at=posted_at,
            listing_application_url=apply_url,
            listing_company_industry_source=industry_source,
        )
        occurrence = FetchedOccurrence(
            source=self.source,
            source_instance=self.source_instance,
            external_id=external_id,
            url=detail_url,
            company=company,
            title=title,
            location=location,
            description=description or "",
            posted_at=posted_at,
            content_hash=content_hash(company, title, location, description or ""),
            detail_complete=detail_complete,
            fetch_error_code=fetch_error_code,
            company_industry_source=industry_source,
        )
        return reference, occurrence


def _find_opencli() -> str:
    scheduled = os.environ.get("JOB_SCAN_OPENCLI", "").strip()
    if scheduled:
        return scheduled
    executable = shutil.which("opencli")
    if executable is not None:
        return executable
    sibling = Path(sys.executable).with_name("opencli")
    return str(sibling)


def _run_persistent_linkedin_command(
    executable: str,
    arguments: list[str],
    timeout_seconds: int,
    challenge_wait_seconds: float,
    *,
    timeout_message: str,
) -> subprocess.CompletedProcess[str]:
    """Keep one LinkedIn tab available while a user clears an auth challenge."""
    argv = [executable, *arguments, "--site-session", "persistent"]

    def run() -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except FileNotFoundError:
            raise BrowserSourceError(
                "OpenCLI executable was not found.", error_code="opencli_missing"
            ) from None
        except subprocess.TimeoutExpired:
            raise BrowserSourceError(
                timeout_message,
                error_code="opencli_timeout",
            ) from None
        except OSError:
            raise BrowserSourceError(
                "OpenCLI could not be started.", error_code="opencli_start_failed"
            ) from None

    try:
        result = run()
        if result.returncode != 77 or challenge_wait_seconds == 0:
            return result

        def read_human_gate(timeout_override: float | None = None) -> object:
            effective_timeout = (
                timeout_seconds
                if timeout_override is None
                else min(timeout_seconds, timeout_override)
            )
            stdout = _run_company_opencli(
                executable,
                ["browser", "site:linkedin", "eval", _LINKEDIN_HUMAN_GATE_JS],
                effective_timeout,
            )
            try:
                return json.loads(stdout)
            except json.JSONDecodeError:
                raise InvalidResponse(
                    "OpenCLI LinkedIn challenge output was not valid JSON"
                ) from None

        try:
            gate = wait_for_challenge_clearance(
                read_human_gate,
                is_challenge_payload,
                wait_seconds=challenge_wait_seconds,
                read_with_timeout=read_human_gate,
            )
        except BrowserSourceError as error:
            if error.error_code == "opencli_timeout":
                raise BrowserSourceError(
                    "LinkedIn human verification was not completed before the wait expired.",
                    error_code="linkedin_challenge",
                ) from None
            return result
        except InvalidResponse:
            return result
        if is_challenge_payload(gate):
            raise BrowserSourceError(
                "LinkedIn human verification was not completed before the wait expired.",
                error_code="linkedin_challenge",
            )
        return run()
    finally:
        _close_company_session(executable, "site:linkedin", timeout_seconds)


def _run_company_opencli(
    executable: str,
    arguments: list[str],
    timeout_seconds: float,
) -> str:
    """Run one bounded LinkedIn company-size OpenCLI command."""
    try:
        result = subprocess.run(
            [executable, *arguments],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError:
        raise BrowserSourceError(
            "OpenCLI executable was not found.", error_code="opencli_missing"
        ) from None
    except subprocess.TimeoutExpired:
        raise BrowserSourceError(
            "LinkedIn company lookup through OpenCLI timed out.",
            error_code="opencli_timeout",
        ) from None
    except OSError:
        raise BrowserSourceError(
            "OpenCLI could not be started.", error_code="opencli_start_failed"
        ) from None
    if result.returncode != 0:
        raise _opencli_failure(result.returncode)
    if len(result.stdout.encode("utf-8")) > _MAX_OUTPUT_BYTES:
        raise InvalidResponse("OpenCLI LinkedIn output exceeded the size limit")
    return result.stdout


def _close_company_session(
    executable: str,
    session: str,
    timeout_seconds: int,
) -> None:
    """Release one LinkedIn company browser tab without masking scan results."""
    try:
        subprocess.run(
            [executable, "browser", session, "close"],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _linkedin_company_about_url(value: object) -> str | None:
    company_url = _optional_string(value)
    if company_url is None:
        return None
    parsed = urlsplit(company_url)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or not (
        host == "linkedin.com" or host.endswith(".linkedin.com")
    ):
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[0].casefold() != "company":
        return None
    return f"https://www.linkedin.com/company/{parts[1]}/about/"


def _opencli_failure(exit_code: int) -> BrowserSourceError:
    if exit_code == 69:
        return BrowserSourceError(
            "OpenCLI Browser Bridge is not connected.",
            error_code="opencli_bridge_unavailable",
        )
    if exit_code == 77:
        return BrowserSourceError(
            "LinkedIn login is required in the connected Chrome profile.",
            error_code="linkedin_auth_required",
        )
    if exit_code == 75:
        return BrowserSourceError(
            "OpenCLI timed out while reading LinkedIn.",
            error_code="opencli_timeout",
        )
    return BrowserSourceError(
        f"OpenCLI LinkedIn search failed with exit code {exit_code}.",
        error_code="opencli_failed",
    )


def _required_string(row: dict[object, object], field: str) -> str:
    value = _optional_string(row.get(field))
    if value is None:
        raise InvalidResponse(f"OpenCLI LinkedIn result missing {field}")
    return value


def _optional_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _optional_date(value: object) -> date | None:
    text = _optional_string(value)
    if text is None:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _application_url(value: object) -> HttpUrl | None:
    text = _optional_string(value)
    if text is None:
        return None
    parsed = urlsplit(text)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme == "https"
        and (host == "linkedin.com" or host.endswith(".linkedin.com"))
        and parsed.path in {"/safety/go", "/safety/go/", "/redir/redirect/"}
    ):
        targets = parse_qs(parsed.query).get("url", [])
        if len(targets) == 1:
            text = targets[0]
    try:
        return HttpUrl(text)
    except ValidationError:
        return None


def _linkedin_job_id(url: str) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (
        host == "linkedin.com" or host.endswith(".linkedin.com")
    ):
        raise InvalidResponse("OpenCLI LinkedIn result contained a non-LinkedIn job URL")
    path_match = _LINKEDIN_JOB_PATH.match(parsed.path)
    if path_match is not None:
        return path_match.group(1)
    current_job_ids = parse_qs(parsed.query).get("currentJobId", [])
    if len(current_job_ids) == 1 and current_job_ids[0].isdigit():
        return current_job_ids[0]
    raise InvalidResponse("OpenCLI LinkedIn result URL did not contain a job ID")


def _row_item_key(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    url = _optional_string(value.get("url"))
    if url is None:
        return None
    try:
        external_id = _linkedin_job_id(url)
    except InvalidResponse:
        return None
    return f"{SourceKind.LINKEDIN.value}:default:{external_id}"


def _detail_quality(
    reference: JobReference, occurrence: FetchedOccurrence
) -> tuple[bool, bool, int, bool]:
    return (
        occurrence.detail_complete,
        reference.listing_application_url is not None,
        len(occurrence.description),
        occurrence.posted_at is not None,
    )
