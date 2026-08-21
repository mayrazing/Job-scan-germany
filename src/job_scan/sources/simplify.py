from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from bs4 import BeautifulSoup
from pydantic import HttpUrl, ValidationError

from job_scan.company_size import native_company_size_evidence
from job_scan.config import AppConfig
from job_scan.domain import CompanySizeEvidence, CompanySizeSource, SourceKind
from job_scan.http_client import InvalidResponse
from job_scan.normalization import content_hash
from job_scan.sources.base import (
    BrowserSourceError,
    ClosureReason,
    ExplicitlyClosed,
    FetchedOccurrence,
    JobReference,
    SourceError,
)
from job_scan.sources.job_snapshot_capture import browser_snapshot_script
from job_scan.sources.opencli_challenge import (
    DEFAULT_CHALLENGE_WAIT_SECONDS,
    is_challenge_error,
    is_challenge_payload,
    wait_for_challenge_clearance,
)

_DEFAULT_TIMEOUT_SECONDS = 90
_MAX_OUTPUT_BYTES = 5_000_000
_SEARCH_PAGE_URL = "https://simplify.jobs/jobs?state=Germany&country=Germany"
_DETAIL_API = "https://api.simplify.jobs/v2/job-posting/:id/{job_id}/company"
_DETAIL_PAGE = (
    "https://simplify.jobs/jobs?query={query}&state=Germany&country=Germany&jobId={job_id}"
)
_APPLICATION_URL = "https://simplify.jobs/jobs/click/{job_id}"

_SNAPSHOT_PAGE_JS_TEMPLATE = browser_snapshot_script(
    r"""
  const expectedJobId = __EXPECTED_JOB_ID__;
  const isChallenge = () => /Just a moment|Access Denied/i.test(document.title || "") ||
    /Cloudflare Ray ID|Verify you are human/i.test(document.body?.innerText || "") ||
    !!document.querySelector('[id^="cf-"], iframe[src*="captcha" i]');
  if (isChallenge()) return {status: "challenge"};

  let details = document.querySelector('[data-testid="details-view"]');
  for (let index = 0; index < 30 && !details && !isChallenge(); index += 1) {
    await new Promise((resolve) => setTimeout(resolve, 500));
    details = document.querySelector('[data-testid="details-view"]');
  }
  if (isChallenge()) return {status: "challenge"};
  const currentJobId = new URLSearchParams(location.search).get("jobId") ||
    details?.id.replace(/^details-card-/, "") || "";
  if (currentJobId !== expectedJobId || !details) {
    return {status: "unavailable", error_code: "structure_mismatch"};
  }

  const normalize = (value) => (value || "").replace(/\s+/g, " ").trim();
  const title = [...details.querySelectorAll("h1")].find(
    (heading) => !/^Add a resume/i.test(normalize(heading.innerText))
  );
  const leftColumn = title?.parentElement?.parentElement;
  const leftRoots = [...(leftColumn?.children || [])].filter((element) => {
    const text = normalize(element.innerText);
    return text && !/^(Get referrals|History)\b/i.test(text);
  });
  const allowedSectionHeadings = new Set([
    "Job Description",
    "Job Responsibilities",
    "Job Requirements",
    "Requirements",
    "Responsibilities",
    "Desired Qualifications",
    "Benefits",
  ]);
  const jobRoots = [...details.querySelectorAll("h3, div.mt-3")]
    .filter((heading) => allowedSectionHeadings.has(normalize(heading.innerText)))
    .map((heading) => heading.parentElement)
    .filter(Boolean);
  if (!title || leftRoots.length < 2 || jobRoots.length === 0) {
    return {status: "unavailable", error_code: "structure_mismatch"};
  }
  return buildJobSnapshot({
    snapshotKey: `simplify:de:${expectedJobId}`,
    title: normalize(title.innerText || title.textContent),
    sourceLabel: "Simplify",
    accent: "#7c3aed",
    roots: [...leftRoots, ...jobRoots],
  });
""".strip()
)


def lookup_company_size(
    source: CompanySizeSource,
    company: str,
    checked_at: datetime,
) -> CompanySizeEvidence | None:
    """Convert the readable employee range returned by Simplify search."""
    if source.source_name != "simplify":
        raise ValueError("company-size source must be simplify")
    if source.reported_size is None:
        return None
    return native_company_size_evidence(
        company=company,
        reported_size=source.reported_size,
        source_url=str(source.public_url),
        source_title=source.source_title,
        source_name="simplify",
        checked_at=checked_at,
    )


class SimplifyDeAdapter:
    """Read public German Simplify listings through one OpenCLI browser session."""

    source = SourceKind.SIMPLIFY
    source_instance = "de"

    def __init__(
        self,
        config: AppConfig,
        *,
        opencli_executable: str | Path | None = None,
        limit: int | None = None,
        timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
        challenge_wait_seconds: float = DEFAULT_CHALLENGE_WAIT_SECONDS,
        capture_snapshot: Callable[[JobReference], bool] | None = None,
    ) -> None:
        resolved_limit = config.simplify_de_limit if limit is None else limit
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
        self._capture_snapshot = capture_snapshot
        self._session = f"job-scan-simplify-de-{os.getpid()}-{id(self):x}"
        self._discovery_errors: list[SourceError] = []
        self._completed_listing = True
        self._details: dict[str, FetchedOccurrence | Exception] = {}

    def discover(self) -> list[JobReference]:
        """Return unique Simplify jobs for every configured term and location."""
        self._discovery_errors.clear()
        self._completed_listing = True
        self._details.clear()
        references: list[JobReference] = []
        references_by_id: dict[str, JobReference] = {}
        locations = self._config.locations or [""]

        try:
            self._open_page(_SEARCH_PAGE_URL)
            for search_term in self._config.search_terms:
                for location in locations:
                    try:
                        rows = self._search(search_term, location)
                    except (BrowserSourceError, InvalidResponse) as error:
                        self._completed_listing = False
                        self._discovery_errors.append(_source_error(error))
                        if is_challenge_error(error):
                            return []
                        continue
                    for row in rows:
                        try:
                            reference = _parse_search_row(row)
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
                        existing = references_by_id.get(reference.external_id)
                        if existing is not None:
                            if (
                                existing.listing_company_size_source is None
                                and reference.listing_company_size_source is not None
                            ):
                                existing.listing_company_size_source = (
                                    reference.listing_company_size_source
                                )
                            continue
                        references_by_id[reference.external_id] = reference
                        references.append(reference)
            for index, reference in enumerate(references):
                try:
                    self._details[reference.external_id] = self._fetch_detail(reference)
                except (BrowserSourceError, ExplicitlyClosed, InvalidResponse) as error:
                    self._details[reference.external_id] = error
                    if is_challenge_error(error):
                        return references[: index + 1]
            return references
        finally:
            self._close_session()

    @property
    def completed_listing(self) -> bool:
        """Return whether every configured Simplify query completed."""
        return self._completed_listing

    def drain_discovery_errors(self) -> list[SourceError]:
        """Return and clear malformed-query errors from the latest search."""
        errors = self._discovery_errors
        self._discovery_errors = []
        return errors

    def fetch_detail(self, reference: JobReference) -> FetchedOccurrence:
        """Return detail data collected before the OpenCLI session was closed."""
        if reference.source is not self.source:
            raise ValueError("reference source must be simplify")
        try:
            detail = self._details[reference.external_id]
        except KeyError:
            raise ValueError(
                f"Simplify detail was not discovered for {reference.external_id}"
            ) from None
        if isinstance(detail, Exception):
            raise detail
        return detail

    def _search(self, search_term: str, location: str) -> list[object]:
        payload = self._evaluate(_search_script(self._config, search_term, location, self._limit))
        status = _page_status(payload, "search")
        if status != "ok":
            _raise_page_failure(status)
        rows = payload.get("rows") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise InvalidResponse("OpenCLI Simplify search must contain a rows array")
        return [row for row in rows]

    def _fetch_detail(self, reference: JobReference) -> FetchedOccurrence:
        payload = self._evaluate(_detail_script(reference.external_id))
        status = _page_status(payload, "detail")
        if status == "closed":
            raise ExplicitlyClosed(_source_job_key(reference), _closure_reason(payload))
        if status != "ok":
            _raise_page_failure(status)
        if not isinstance(payload, dict) or not isinstance(payload.get("job"), dict):
            raise InvalidResponse("OpenCLI Simplify detail must contain a job object")
        job = payload["job"]
        job_id = _required_job_id(job.get("id"))
        if job_id != reference.external_id:
            raise InvalidResponse("OpenCLI Simplify detail did not match its job ID")
        if _is_closed_job(job):
            raise ExplicitlyClosed(_source_job_key(reference), "page_closed_marker")

        company = _detail_company(job) or reference.listing_company
        title = _optional_string(job.get("title")) or reference.listing_title
        location = _locations_text(job.get("locations")) or reference.listing_location
        description = _full_description(job)
        posted_at = _posted_date(job.get("start_date")) or reference.listing_posted_at
        detail_complete = bool(description)
        snapshot_html: str | None = None
        snapshot_error_code: str | None = None
        if self._capture_snapshot is not None and self._capture_snapshot(
            reference.with_current_identity(title=title, posted_at=posted_at)
        ):
            try:
                self._open_page(str(reference.detail_url))
                snapshot_payload = self._evaluate(
                    _snapshot_script(reference.external_id)
                )
            except (BrowserSourceError, InvalidResponse):
                snapshot_error_code = "snapshot_capture_failed"
            else:
                if (
                    isinstance(snapshot_payload, dict)
                    and snapshot_payload.get("status") == "ok"
                    and isinstance(snapshot_payload.get("html"), str)
                    and snapshot_payload["html"].strip()
                ):
                    snapshot_html = snapshot_payload["html"]
                else:
                    snapshot_error_code = "snapshot_capture_failed"
        return FetchedOccurrence(
            source=self.source,
            source_instance=self.source_instance,
            external_id=reference.external_id,
            url=reference.detail_url,
            company=company,
            title=title,
            location=location,
            description=description,
            posted_at=posted_at,
            content_hash=content_hash(company, title, location, description),
            detail_complete=detail_complete,
            fetch_error_code=None if detail_complete else "missing_full_description",
            job_snapshot_error_code=snapshot_error_code,
            job_snapshot_html=snapshot_html,
            company_size_source=reference.listing_company_size_source,
        )

    def _open_page(self, url: str) -> None:
        self._run(["browser", self._session, "open", url, "--window", "background"])

    def _evaluate(self, script: str) -> object:
        def poll(remaining: float) -> object:
            try:
                return self._evaluate_once(script, timeout_seconds=remaining)
            except BrowserSourceError as error:
                if error.error_code == "opencli_timeout":
                    return {"status": "challenge"}
                raise

        return wait_for_challenge_clearance(
            lambda: self._evaluate_once(script),
            is_challenge_payload,
            wait_seconds=self._challenge_wait_seconds,
            read_with_timeout=poll,
        )

    def _evaluate_once(
        self,
        script: str,
        *,
        timeout_seconds: float | None = None,
    ) -> object:
        effective_timeout = (
            self._timeout_seconds
            if timeout_seconds is None
            else min(self._timeout_seconds, timeout_seconds)
        )
        stdout = _run_opencli(
            self._opencli_executable,
            ["browser", self._session, "eval", script],
            effective_timeout,
        )
        try:
            return json.loads(stdout)
        except json.JSONDecodeError:
            raise InvalidResponse("OpenCLI Simplify output was not valid JSON") from None

    def _run(self, arguments: list[str]) -> str:
        return _run_opencli(self._opencli_executable, arguments, self._timeout_seconds)

    def _close_session(self) -> None:
        _close_opencli_session(
            self._opencli_executable,
            self._session,
            self._timeout_seconds,
        )


def _search_script(
    config: AppConfig,
    search_term: str,
    location: str,
    limit: int,
) -> str:
    cutoff = _posted_cutoff(config)
    arguments = json.dumps(
        {
            "searchTerm": search_term,
            "location": location,
            "limit": limit,
            "cutoff": cutoff,
        }
    )
    return rf"""
(async () => {{
  const input = {arguments};
  const isChallenge = () => /Just a moment|Access Denied/i.test(document.title || "") ||
    /Cloudflare Ray ID|Verify you are human/i.test(document.body?.innerText || "") ||
    !!document.querySelector('[id^="cf-"], iframe[src*="captcha" i]');
  if (isChallenge()) return {{status: "challenge", rows: []}};
  const findContract = () => {{
    const resources = performance.getEntriesByType("resource")
      .map((entry) => entry.name)
      .filter((name) => typeof name === "string");
    for (const name of resources) {{
      try {{
        const candidate = new URL(name);
        if (candidate.hostname === "js-ha.simplify.jobs" &&
            candidate.pathname === "/collections/jobs/documents/search" &&
            candidate.searchParams.get("x-typesense-api-key")) {{
          return candidate;
        }}
      }} catch (_error) {{
        // Ignore non-URL performance entries.
      }}
    }}
    return null;
  }};
  let contract = findContract();
  for (let index = 0; index < 30 && !contract; index += 1) {{
    await new Promise((resolve) => setTimeout(resolve, 500));
    if (isChallenge()) return {{status: "challenge", rows: []}};
    contract = findContract();
  }}
  if (!contract) return {{status: "missing_search_contract", rows: []}};

  const escapeFilter = (value) => String(value)
    .replaceAll("\\", "\\\\")
    .replaceAll("`", "\\`");
  const filters = ["countries:=[`Germany`]"];
  if (input.location) filters.push(`locations:\`${{escapeFilter(input.location)}}\``);
  if (input.cutoff !== null) filters.push(`start_date:>=${{input.cutoff}}`);
  const parameters = new URLSearchParams({{
    q: input.searchTerm,
    query_by: "title,company_name,functions,locations",
    filter_by: filters.join(" && "),
    per_page: String(input.limit),
    page: "1",
    sort_by: "_text_match:desc,start_date:desc,posting_id:desc",
    include_fields: "id,title,company_name,company_size,locations,start_date,updated_date",
    "x-typesense-api-key": contract.searchParams.get("x-typesense-api-key"),
  }});
  try {{
    const response = await fetch(`${{contract.origin}}/collections/jobs/documents/search?${{parameters}}`, {{
      credentials: "omit",
      headers: {{Accept: "application/json"}},
    }});
    if (!response.ok) return {{status: "request_failed", rows: []}};
    const payload = await response.json();
    if (!Array.isArray(payload?.hits)) return {{status: "invalid_response", rows: []}};
    return {{
      status: "ok",
      rows: payload.hits
        .map((hit) => hit?.document)
        .filter(Boolean),
    }};
  }} catch (_error) {{
    return {{status: "request_failed", rows: []}};
  }}
}})()
""".strip()


def _detail_script(job_id: str) -> str:
    endpoint = _DETAIL_API.format(job_id=job_id)
    return f"""
(async () => {{
  const isChallenge = () => /Just a moment|Access Denied/i.test(document.title || "") ||
    /Cloudflare Ray ID|Verify you are human/i.test(document.body?.innerText || "") ||
    !!document.querySelector('[id^="cf-"], iframe[src*="captcha" i]');
  if (isChallenge()) return {{status: "challenge"}};
  try {{
    const response = await fetch({json.dumps(endpoint)}, {{
      credentials: "omit",
      headers: {{Accept: "application/json"}},
    }});
    if (response.status === 404 || response.status === 410) {{
      return {{status: "closed", reason: `http_${{response.status}}`}};
    }}
    if (!response.ok) return {{status: "request_failed"}};
    return {{status: "ok", job: await response.json()}};
  }} catch (_error) {{
    return {{status: "request_failed"}};
  }}
}})()
""".strip()


def _snapshot_script(job_id: str) -> str:
    """Bind one expected job ID to the browser snapshot reader."""
    return _SNAPSHOT_PAGE_JS_TEMPLATE.replace(
        "__EXPECTED_JOB_ID__",
        json.dumps(job_id),
    )


def _posted_cutoff(config: AppConfig) -> int | None:
    if config.posted_within_days is None:
        return None
    cutoff_date = datetime.now(UTC).date() - timedelta(days=config.posted_within_days)
    return int(datetime.combine(cutoff_date, datetime.min.time(), tzinfo=UTC).timestamp())


def _parse_search_row(value: object) -> JobReference:
    if not isinstance(value, dict):
        raise InvalidResponse("OpenCLI Simplify search result must be an object")
    external_id = _required_job_id(value.get("id"))
    title = _required_string(value, "title")
    company = _required_string(value, "company_name")
    location = _locations_text(value.get("locations"))
    if not location:
        raise InvalidResponse("OpenCLI Simplify result missing locations")
    try:
        reported_size = _reported_company_size(value.get("company_size"))
        return JobReference(
            source=SourceKind.SIMPLIFY,
            source_instance="de",
            external_id=external_id,
            detail_url=_detail_page(external_id, company),
            listing_title=title,
            listing_company=company,
            listing_location=location,
            listing_posted_at=_posted_date(value.get("start_date")),
            listing_application_url=HttpUrl(_APPLICATION_URL.format(job_id=external_id)),
            listing_company_size_source=(
                _company_size_source(external_id, reported_size, company)
                if reported_size is not None
                else None
            ),
        )
    except ValidationError:
        raise InvalidResponse("OpenCLI Simplify result contained an invalid URL") from None


def _detail_page(job_id: str, company: str) -> HttpUrl:
    return HttpUrl(
        _DETAIL_PAGE.format(query=quote(company, safe=""), job_id=job_id)
    )


def _company_size_source(
    job_id: str,
    reported_size: str,
    company: str,
) -> CompanySizeSource:
    return CompanySizeSource(
        source_name="simplify",
        lookup_url=HttpUrl(_DETAIL_API.format(job_id=job_id)),
        public_url=_detail_page(job_id, company),
        source_title="Simplify job posting",
        reported_size=reported_size,
    )


def _reported_company_size(value: object) -> str | None:
    reported_size = _optional_string(value)
    if reported_size is None or len(reported_size) > 100:
        return None
    return reported_size


def _full_description(job: dict[object, object]) -> str:
    parts: list[str] = []
    html = _optional_string(job.get("description"))
    if html is not None:
        lines = BeautifulSoup(html, "html.parser").get_text("\n", strip=True).splitlines()
        text = "\n".join(line.strip() for line in lines if line.strip())
        if text:
            parts.append(text)
    for field, heading in (
        ("responsibilities", "Responsibilities"),
        ("requirements", "Requirements"),
        ("desirable", "Desirable"),
        ("additional_requirements", "Additional requirements"),
    ):
        lines = _section_lines(job.get(field))
        if lines:
            parts.extend((heading, *lines))
    return "\n".join(parts)


def _section_lines(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    lines: list[str] = []
    for item in value:
        if isinstance(item, dict):
            item = item.get("description") or item.get("text") or item.get("value")
        text = _optional_string(item)
        if text is None:
            continue
        plain = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
        if plain and plain not in lines:
            lines.append(plain)
    return lines


def _detail_company(job: dict[object, object]) -> str | None:
    candidates = [job.get("company")]
    nested_job = job.get("job")
    if isinstance(nested_job, dict):
        candidates.append(nested_job.get("company"))
    for candidate in candidates:
        if isinstance(candidate, dict):
            name = _optional_string(candidate.get("name"))
            if name is not None:
                return name
    return None


def _locations_text(value: object) -> str:
    if not isinstance(value, list):
        return ""
    names: list[str] = []
    for item in value:
        if isinstance(item, dict):
            item = item.get("name") or item.get("location") or item.get("city")
        name = _optional_string(item)
        if name is not None and name not in names:
            names.append(name)
    return ", ".join(names)


def _posted_date(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise InvalidResponse("OpenCLI Simplify result contained an invalid start_date")
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=UTC).date()
        except (OverflowError, OSError, ValueError):
            raise InvalidResponse(
                "OpenCLI Simplify result contained an invalid start_date"
            ) from None
    raw = _optional_string(value)
    if raw is None:
        raise InvalidResponse("OpenCLI Simplify result contained an invalid start_date")
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        raise InvalidResponse("OpenCLI Simplify result contained an invalid start_date") from None


def _is_closed_job(job: dict[object, object]) -> bool:
    return job.get("active") is False or job.get("visible") is False or job.get("archive") is True


def _page_status(payload: object, page_name: str) -> str:
    if not isinstance(payload, dict):
        raise InvalidResponse(f"OpenCLI Simplify {page_name} output must be an object")
    status = _optional_string(payload.get("status"))
    if status is None:
        raise InvalidResponse(f"OpenCLI Simplify {page_name} output missing status")
    return status


def _closure_reason(payload: object) -> ClosureReason:
    if isinstance(payload, dict) and payload.get("reason") == "http_410":
        return "http_410"
    return "http_404"


def _raise_page_failure(status: str) -> None:
    if status == "challenge":
        raise BrowserSourceError(
            "Simplify served a browser challenge; open the site in the connected "
            "Chrome profile and complete it.",
            error_code="simplify_challenge",
        )
    if status == "missing_search_contract":
        raise BrowserSourceError(
            "Simplify's public search contract was not available in the browser page.",
            error_code="simplify_search_contract",
        )
    if status == "request_failed":
        raise BrowserSourceError(
            "Simplify's public job request failed through OpenCLI.",
            error_code="simplify_request_failed",
        )
    if status == "invalid_response":
        raise InvalidResponse("OpenCLI Simplify returned an invalid public API response")
    raise InvalidResponse(f"OpenCLI Simplify returned unknown page status {status!r}")


def _source_error(error: BrowserSourceError | InvalidResponse) -> SourceError:
    if isinstance(error, BrowserSourceError):
        return SourceError(
            category="browser",
            source=SourceKind.SIMPLIFY,
            source_instance="de",
            error_code=error.error_code,
            message=str(error),
        )
    return SourceError(
        category="contract",
        source=SourceKind.SIMPLIFY,
        source_instance="de",
        error_code="invalid_response",
        message=str(error),
    )


def _run_opencli(executable: str, arguments: list[str], timeout_seconds: float) -> str:
    """Run one bounded Simplify OpenCLI command and return its text output."""
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
            "Simplify through OpenCLI timed out.", error_code="opencli_timeout"
        ) from None
    except OSError:
        raise BrowserSourceError(
            "OpenCLI could not be started.", error_code="opencli_start_failed"
        ) from None
    if result.returncode != 0:
        raise _opencli_failure(result.returncode)
    if len(result.stdout.encode("utf-8")) > _MAX_OUTPUT_BYTES:
        raise InvalidResponse("OpenCLI Simplify output exceeded the size limit")
    return result.stdout


def _close_opencli_session(executable: str, session: str, timeout_seconds: int) -> None:
    """Release one optional OpenCLI browser tab without masking scan results."""
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


def _opencli_failure(exit_code: int) -> BrowserSourceError:
    if exit_code == 69:
        return BrowserSourceError(
            "OpenCLI Browser Bridge is not connected.",
            error_code="opencli_bridge_unavailable",
        )
    if exit_code == 75:
        return BrowserSourceError(
            "Simplify through OpenCLI timed out.", error_code="opencli_timeout"
        )
    return BrowserSourceError(
        f"OpenCLI Simplify failed with exit code {exit_code}.",
        error_code="opencli_failed",
    )


def _find_opencli() -> str:
    scheduled = os.environ.get("JOB_SCAN_OPENCLI", "").strip()
    if scheduled:
        return scheduled
    executable = shutil.which("opencli")
    if executable is not None:
        return executable
    return str(Path(sys.executable).with_name("opencli"))


def _required_string(row: dict[object, object], field: str) -> str:
    value = _optional_string(row.get(field))
    if value is None:
        raise InvalidResponse(f"OpenCLI Simplify result missing {field}")
    return value


def _optional_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _required_job_id(value: object) -> str:
    job_id = _optional_string(value)
    if job_id is None:
        raise InvalidResponse("OpenCLI Simplify result contained an invalid job ID")
    try:
        parsed = uuid.UUID(job_id)
    except ValueError:
        raise InvalidResponse("OpenCLI Simplify result contained an invalid job ID") from None
    if str(parsed) != job_id.casefold():
        raise InvalidResponse("OpenCLI Simplify result contained an invalid job ID")
    return str(parsed)


def _row_item_key(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    try:
        job_id = _required_job_id(value.get("id"))
    except InvalidResponse:
        return None
    return f"simplify:de:{job_id}"


def _source_job_key(reference: JobReference) -> str:
    return f"{reference.source.value}:{reference.source_instance}:{reference.external_id}"
