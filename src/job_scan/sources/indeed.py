from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from pydantic import HttpUrl, ValidationError

from job_scan.company_industry import (
    COMPANY_INDUSTRY_PAGE_JS,
    native_company_industry_evidence,
)
from job_scan.company_size import native_company_size_evidence
from job_scan.config import AppConfig
from job_scan.domain import (
    CompanyIndustrySource,
    CompanySizeEvidence,
    CompanySizeSource,
    SourceKind,
)
from job_scan.http_client import InvalidResponse
from job_scan.normalization import content_hash
from job_scan.sources.base import (
    BrowserSourceError,
    CompanyProfileFacts,
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
_PAGE_STEP = 10
_ORIGIN = "https://de.indeed.com"
_JOB_KEY = re.compile(r"^[a-f0-9]{16}$")
_FROMAGE_DAYS = {0: 1, 1: 1, 3: 3, 7: 7, 14: 14}

_SEARCH_PAGE_JS = r"""
(async () => {
  const searchHeading = () => document.querySelector("main h1") ||
    document.querySelector("h1");
  const hasResults = () => !!searchHeading() &&
    !!document.querySelector(".job_seen_beacon");
  const hasEmptyState = () => {
    const heading = (searchHeading()?.textContent || "")
      .replace(/\s+/g, " ")
      .trim();
    return !!document.querySelector(
      '[data-testid="noResultsMessage"], [data-testid="empty-serp-result"]'
    ) || /es wurden keine jobs für die suche .+ gefunden\.?/i.test(heading) ||
      /keine jobs gefunden|(?:^|[^\d])0 jobs\b|did not match any jobs|no jobs found/i.test(heading);
  };
  const isChallenge = () => (document.title || "").includes("Just a moment") ||
    !!document.querySelector('[id^="cf-"]');
  let ready = hasResults() || hasEmptyState() || isChallenge();
  for (let index = 0; index < 30 && !ready; index += 1) {
    await new Promise((resolve) => setTimeout(resolve, 500));
    ready = hasResults() || hasEmptyState() || isChallenge();
  }
  if (isChallenge()) return {status: "challenge", rows: []};
  if (!ready) return {status: "not_ready", rows: []};
  if (hasEmptyState()) return {status: "ok", rows: []};

  const rows = [];
  const seen = new Set();
  for (const block of document.querySelectorAll(".job_seen_beacon")) {
    const anchor = block.querySelector(
      'a[data-jk], h2.jobTitle a, [class*="jcs-JobTitle"]'
    );
    const id = anchor?.getAttribute("data-jk") || "";
    if (!id || seen.has(id)) continue;
    seen.add(id);
    rows.push({
      id,
      title: (anchor?.innerText || anchor?.textContent || "").trim(),
      company: (
        block.querySelector('[data-testid="company-name"]')?.textContent || ""
      ).trim(),
      location: (
        block.querySelector('[data-testid="text-location"]')?.textContent || ""
      ).trim(),
      url: `https://de.indeed.com/viewjob?jk=${encodeURIComponent(id)}`,
      navigation_url: anchor.href,
    });
  }
  return {status: "ok", rows};
})()
""".strip()

_DETAIL_PAGE_JS_TEMPLATE = r"""
(async () => {
  const expectedTitle = __EXPECTED_TITLE__;
  const normalize = (value) => (value || "").replace(/\s+/g, " ").trim();
  const state = () => {
    const heading = document.querySelector(
      '[data-testid="jobsearch-JobInfoHeader-title"], h1'
    )?.textContent?.trim() || "";
    return {
      challenge: (document.title || "").includes("Just a moment") ||
        !!document.querySelector('[id^="cf-"]'),
      heading,
      notFound: !!document.querySelector('[data-testid="error-page"]') ||
        /Page Not Found|nicht gefunden|not found/i.test(heading),
      ready: normalize(heading).startsWith(normalize(expectedTitle)),
    };
  };
  let current = state();
  for (
    let index = 0;
    index < 30 && !current.challenge && !current.notFound && !current.ready;
    index += 1
  ) {
    await new Promise((resolve) => setTimeout(resolve, 500));
    current = state();
  }
  if (current.challenge) return {status: "challenge"};
  if (current.notFound) return {status: "closed"};
  if (!current.ready) return {status: "not_ready"};

  const companyLink = document.querySelector(
    '[data-testid="inlineHeader-companyName"] a, a[data-company-name="true"]'
  );

  return {
    status: "ok",
    job: {
      title: expectedTitle,
      company: (
        document.querySelector(
          '[data-testid="inlineHeader-companyName"] a, ' +
          '[data-testid="inlineHeader-companyName"], [data-company-name="true"]'
        )?.textContent || ""
      ).trim(),
      location: (
        document.querySelector(
          '[data-testid="jobsearch-JobInfoHeader-companyLocation"] div, ' +
          '[data-testid="inlineHeader-companyLocation"]'
        )?.textContent || ""
      ).trim(),
      description: (
        document.querySelector("#jobDescriptionText")?.innerText || ""
      ).trim(),
      company_url: companyLink?.href || "",
    },
  };
})()
""".strip()

_SNAPSHOT_PAGE_JS = browser_snapshot_script(
    r"""
  const jobId = new URLSearchParams(location.search).get("jk") ||
    document.querySelector("[data-jk]")?.getAttribute("data-jk") || "";
  const title = document.querySelector(
    '[data-testid="jobsearch-JobInfoHeader-title"], h1.jobsearch-JobInfoHeader-title'
  );
  const header = document.querySelector(".jobsearch-InfoHeaderContainer");
  const description = document.querySelector("#jobDescriptionText");
  if (!/^[a-f0-9]{16}$/.test(jobId) || !title || !header || !description) {
    return {status: "unavailable", error_code: "structure_mismatch"};
  }
  return buildJobSnapshot({
    snapshotKey: `indeed:de:${jobId}`,
    title: (title.innerText || title.textContent || "").trim(),
    sourceLabel: "Indeed",
    accent: "#2557a7",
    roots: [
      header,
      document.querySelector("#jobDetailsSection"),
      document.querySelector("#jobLocationSectionWrapper"),
      document.querySelector("#benefits"),
      document.querySelector("#jobDescriptionTitle"),
      description,
    ],
  });
""".strip()
)

_COMPANY_PAGE_JS = r"""
(async () => {
  const isChallenge = () => /Just a moment|Access Denied/i.test(document.title || "") ||
    /Cloudflare Ray ID|Verify you are human/i.test(document.body?.innerText || "") ||
    !!document.querySelector('[id^="cf-"], iframe[src*="captcha" i]');
  if (isChallenge()) return {status: "challenge", reported_size: ""};
  const readSize = () => {
    const lines = (document.body?.innerText || "")
      .split(/\n+/)
      .map((value) => value.replace(/\s+/g, " ").trim())
      .filter(Boolean);
    const labelIndex = lines.findIndex((line) =>
      /^(Mitarbeiter|Company size|Employees)$/i.test(line)
    );
    if (labelIndex < 0) return "";
    for (const line of lines.slice(labelIndex + 1, labelIndex + 4)) {
      if (/\d[\d., ]*(?:\s*(?:bis|to|[-–])\s*\d[\d., ]*|\+)/i.test(line)) {
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
    source: CompanySizeSource,
    company: str,
    checked_at: datetime,
    *,
    opencli_executable: str | Path | None = None,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
) -> CompanySizeEvidence | None:
    """Read Mitarbeiter from one Indeed company About page through OpenCLI."""
    return _lookup_company_facts(
        source,
        company,
        checked_at,
        opencli_executable=opencli_executable,
        timeout_seconds=timeout_seconds,
        include_industry=False,
    ).company_size


def lookup_company_facts(
    source: CompanySizeSource,
    company: str,
    checked_at: datetime,
    *,
    opencli_executable: str | Path | None = None,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
) -> CompanyProfileFacts:
    """Read size and industry during one Indeed company-page visit."""
    return _lookup_company_facts(
        source,
        company,
        checked_at,
        opencli_executable=opencli_executable,
        timeout_seconds=timeout_seconds,
        include_industry=True,
    )


def _lookup_company_facts(
    source: CompanySizeSource,
    company: str,
    checked_at: datetime,
    *,
    opencli_executable: str | Path | None,
    timeout_seconds: int,
    include_industry: bool,
) -> CompanyProfileFacts:
    """Read requested company facts without opening the profile more than once."""
    if source.source_name != "indeed":
        raise ValueError("company-size source must be indeed")
    executable = str(opencli_executable or _find_opencli())
    session = f"job-scan-indeed-size-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    try:
        _run_opencli(
            executable,
            ["browser", session, "open", str(source.lookup_url), "--window", "background"],
            timeout_seconds,
        )

        def read_company_page(timeout_override: float | None = None) -> object:
            effective_timeout = (
                timeout_seconds
                if timeout_override is None
                else min(timeout_seconds, timeout_override)
            )
            stdout = _run_opencli(
                executable,
                ["browser", session, "eval", _COMPANY_PAGE_JS],
                effective_timeout,
            )
            try:
                return json.loads(stdout)
            except json.JSONDecodeError:
                raise InvalidResponse("OpenCLI Indeed company output was not valid JSON") from None

        payload = wait_for_challenge_clearance(
            read_company_page,
            is_challenge_payload,
            read_with_timeout=read_company_page,
        )
        if is_challenge_payload(payload):
            return CompanyProfileFacts()
        industry_stdout = (
            _run_opencli(
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
            raise InvalidResponse("OpenCLI Indeed company output was not valid JSON") from None
    finally:
        _close_opencli_session(executable, session, timeout_seconds)
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return CompanyProfileFacts()
    reported_size = _optional_string(payload.get("reported_size"))
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
                source_url=str(source.public_url),
                source_title=source.source_title,
                source_name="indeed",
                checked_at=checked_at,
            )
            if reported_size is not None
            else None
        ),
        company_industry=(
            native_company_industry_evidence(
                company=company,
                reported_industry=reported_industry,
                source_url=str(source.public_url),
                source_title=source.source_title,
                source_name="indeed",
                checked_at=checked_at,
            )
            if reported_industry is not None
            else None
        ),
    )


class IndeedDeAdapter:
    """Read Indeed Deutschland through OpenCLI Browser Bridge primitives."""

    source = SourceKind.INDEED
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
        resolved_limit = config.indeed_de_limit if limit is None else limit
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
        self._session = f"job-scan-indeed-de-{os.getpid()}-{id(self):x}"
        self._discovery_errors: list[SourceError] = []
        self._completed_listing = True
        self._details: dict[str, FetchedOccurrence | Exception] = {}
        self._detail_navigation_urls: dict[str, str] = {}

    def discover(self) -> list[JobReference]:
        """Return deduplicated Indeed jobs for every configured term and location."""
        self._discovery_errors.clear()
        self._completed_listing = True
        self._details.clear()
        self._detail_navigation_urls.clear()
        references: list[JobReference] = []
        seen_ids: set[str] = set()
        locations = self._config.locations or ["Deutschland"]

        try:
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
                            navigation_url = _detail_navigation_url(row)
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
                        if reference.external_id in seen_ids:
                            continue
                        seen_ids.add(reference.external_id)
                        references.append(reference)
                        self._detail_navigation_urls[reference.external_id] = navigation_url
            for index, reference in enumerate(references):
                try:
                    self._details[reference.external_id] = self._fetch_detail(
                        reference,
                        self._detail_navigation_urls[reference.external_id],
                    )
                except (BrowserSourceError, ExplicitlyClosed, InvalidResponse) as error:
                    self._details[reference.external_id] = error
                    if is_challenge_error(error):
                        return references[: index + 1]
            return references
        finally:
            self._close_session()

    @property
    def completed_listing(self) -> bool:
        """Return whether every configured Indeed query completed."""
        return self._completed_listing

    def drain_discovery_errors(self) -> list[SourceError]:
        """Return and clear malformed-row errors from the latest search."""
        errors = self._discovery_errors
        self._discovery_errors = []
        return errors

    def fetch_detail(self, reference: JobReference) -> FetchedOccurrence:
        """Return detail data collected in the matching OpenCLI browser session."""
        if reference.source is not self.source:
            raise ValueError("reference source must be indeed")
        try:
            detail = self._details[reference.external_id]
        except KeyError:
            raise ValueError(
                f"Indeed detail was not discovered for {reference.external_id}"
            ) from None
        if isinstance(detail, Exception):
            raise detail
        return detail

    def _fetch_detail(
        self,
        reference: JobReference,
        navigation_url: str,
    ) -> FetchedOccurrence:
        """Read one complete Indeed Deutschland job description."""
        self._open_page(navigation_url)
        payload = self._evaluate(_detail_page_js(reference.listing_title))
        status = _page_status(payload, "detail")
        if status == "closed":
            raise ExplicitlyClosed(
                f"{self.source.value}:{self.source_instance}:{reference.external_id}",
                "page_closed_marker",
            )
        if status != "ok":
            _raise_page_failure(status)
        if not isinstance(payload, dict) or not isinstance(payload.get("job"), dict):
            raise InvalidResponse("OpenCLI Indeed detail must contain a job object")
        job = payload["job"]
        title = _optional_string(job.get("title")) or reference.listing_title
        company = _optional_string(job.get("company")) or reference.listing_company
        location = _optional_string(job.get("location")) or reference.listing_location
        description = _optional_string(job.get("description")) or ""
        company_size_source = _company_size_source(job.get("company_url"))
        company_industry_source = _company_industry_source(company_size_source)
        detail_complete = bool(description)
        snapshot_html: str | None = None
        snapshot_error_code: str | None = None
        if self._capture_snapshot is not None and self._capture_snapshot(
            reference.with_current_identity(
                title=title,
                posted_at=reference.listing_posted_at,
            )
        ):
            try:
                snapshot_payload = self._evaluate(_SNAPSHOT_PAGE_JS)
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
            posted_at=None,
            content_hash=content_hash(company, title, location, description),
            detail_complete=detail_complete,
            fetch_error_code=None if detail_complete else "missing_full_description",
            job_snapshot_error_code=snapshot_error_code,
            job_snapshot_html=snapshot_html,
            company_size_source=company_size_source,
            company_industry_source=company_industry_source,
        )

    def _search(self, search_term: str, location: str) -> list[object]:
        keyed_rows: dict[str, object] = {}
        unkeyed_rows: list[object] = []
        expected_pages = (self._limit + _PAGE_STEP - 1) // _PAGE_STEP
        max_pages = expected_pages * 2 + 1
        for page_index in range(max_pages):
            start = page_index * _PAGE_STEP
            page_url = _search_url(self._config, search_term, location, start)
            payload = self._read_page(page_url, _SEARCH_PAGE_JS)
            status = _page_status(payload, "search")
            if status != "ok":
                _raise_page_failure(status)
            if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
                raise InvalidResponse("OpenCLI Indeed search must contain a rows array")
            page_rows = payload["rows"]
            for original_row in page_rows:
                row = original_row
                row_id = _row_id(row)
                if row_id is None:
                    unkeyed_rows.append(row)
                    continue
                existing = keyed_rows.get(row_id)
                if existing is None or (
                    _is_valid_search_row(row) and not _is_valid_search_row(existing)
                ):
                    keyed_rows[row_id] = row
            if _valid_search_row_count(keyed_rows.values()) >= self._limit:
                break
            if len(page_rows) < _PAGE_STEP:
                break

        rows: list[object] = []
        valid_count = 0
        for row in keyed_rows.values():
            if _is_valid_search_row(row):
                if valid_count == self._limit:
                    continue
                valid_count += 1
            rows.append(row)
        rows.extend(unkeyed_rows)
        return rows

    def _read_page(self, url: str, script: str) -> object:
        self._open_page(url)
        return self._evaluate(script)

    def _open_page(self, url: str) -> None:
        self._run(
            [
                "browser",
                self._session,
                "open",
                url,
                "--window",
                "background",
            ]
        )

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
            raise InvalidResponse("OpenCLI Indeed output was not valid JSON") from None

    def _run(self, arguments: list[str]) -> str:
        return _run_opencli(
            self._opencli_executable,
            arguments,
            self._timeout_seconds,
        )

    def _close_session(self) -> None:
        _close_opencli_session(
            self._opencli_executable,
            self._session,
            self._timeout_seconds,
        )


def _run_opencli(
    executable: str,
    arguments: list[str],
    timeout_seconds: float,
) -> str:
    """Run one bounded Indeed OpenCLI command and return its text output."""
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
            "Indeed Deutschland through OpenCLI timed out.",
            error_code="opencli_timeout",
        ) from None
    except OSError:
        raise BrowserSourceError(
            "OpenCLI could not be started.", error_code="opencli_start_failed"
        ) from None
    if result.returncode != 0:
        raise _opencli_failure(result.returncode)
    if len(result.stdout.encode("utf-8")) > _MAX_OUTPUT_BYTES:
        raise InvalidResponse("OpenCLI Indeed output exceeded the size limit")
    return result.stdout


def _close_opencli_session(
    executable: str,
    session: str,
    timeout_seconds: int,
) -> None:
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


def _find_opencli() -> str:
    scheduled = os.environ.get("JOB_SCAN_OPENCLI", "").strip()
    if scheduled:
        return scheduled
    executable = shutil.which("opencli")
    if executable is not None:
        return executable
    return str(Path(sys.executable).with_name("opencli"))


def _search_url(
    config: AppConfig,
    search_term: str,
    location: str,
    start: int,
) -> str:
    parameters: list[tuple[str, str]] = [("q", search_term), ("l", location)]
    if config.posted_within_days is not None:
        parameters.append(("fromage", str(_FROMAGE_DAYS[config.posted_within_days])))
    parameters.append(("sort", "date"))
    if start:
        parameters.append(("start", str(start)))
    return f"{_ORIGIN}/jobs?{urlencode(parameters)}"


def _detail_page_js(expected_title: str) -> str:
    return _DETAIL_PAGE_JS_TEMPLATE.replace(
        "__EXPECTED_TITLE__",
        json.dumps(expected_title),
    )


def _parse_search_row(value: object) -> JobReference:
    if not isinstance(value, dict):
        raise InvalidResponse("OpenCLI Indeed search result must be an object")
    external_id = _required_job_id(value.get("id"))
    title = _required_string(value, "title")
    company = _required_string(value, "company")
    location = _required_string(value, "location")
    job_url = _required_string(value, "url")
    if _indeed_job_id(job_url) != external_id:
        raise InvalidResponse("OpenCLI Indeed result URL did not match its job ID")
    try:
        detail_url = HttpUrl(f"{_ORIGIN}/viewjob?jk={external_id}")
    except ValidationError:
        raise InvalidResponse("OpenCLI Indeed result contained an invalid URL") from None
    return JobReference(
        source=SourceKind.INDEED,
        source_instance="de",
        external_id=external_id,
        detail_url=detail_url,
        listing_title=title,
        listing_company=company,
        listing_location=location,
    )


def _company_size_source(value: object) -> CompanySizeSource | None:
    company_url = _optional_string(value)
    if company_url is None:
        return None
    parsed = urlsplit(company_url)
    if parsed.scheme != "https" or parsed.netloc.lower() != "de.indeed.com":
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[0].casefold() != "cmp":
        return None
    about_url = f"{_ORIGIN}/cmp/{parts[1]}/about"
    return CompanySizeSource(
        source_name="indeed",
        lookup_url=HttpUrl(about_url),
        public_url=HttpUrl(about_url),
        source_title="Indeed company profile",
    )


def _company_industry_source(
    source: CompanySizeSource | None,
) -> CompanyIndustrySource | None:
    if source is None:
        return None
    return CompanyIndustrySource(
        source_name="indeed",
        lookup_url=source.lookup_url,
        public_url=source.public_url,
        source_title="Indeed company profile",
    )


def _page_status(payload: object, page_name: str) -> str:
    if not isinstance(payload, dict):
        raise InvalidResponse(f"OpenCLI Indeed {page_name} output must be an object")
    status = _optional_string(payload.get("status"))
    if status is None:
        raise InvalidResponse(f"OpenCLI Indeed {page_name} output missing status")
    return status


def _raise_page_failure(status: str) -> None:
    if status == "challenge":
        raise BrowserSourceError(
            "Indeed Deutschland served a Cloudflare challenge; open the site in the "
            "connected Chrome profile and complete it.",
            error_code="indeed_challenge",
        )
    if status == "not_ready":
        raise BrowserSourceError(
            "Indeed Deutschland did not finish loading through OpenCLI.",
            error_code="indeed_page_not_ready",
        )
    raise InvalidResponse(f"OpenCLI Indeed returned unknown page status {status!r}")


def _source_error(error: BrowserSourceError | InvalidResponse) -> SourceError:
    if isinstance(error, BrowserSourceError):
        return SourceError(
            category="browser",
            source=SourceKind.INDEED,
            source_instance="de",
            error_code=error.error_code,
            message=str(error),
        )
    return SourceError(
        category="contract",
        source=SourceKind.INDEED,
        source_instance="de",
        error_code="invalid_response",
        message=str(error),
    )


def _opencli_failure(exit_code: int) -> BrowserSourceError:
    if exit_code == 69:
        return BrowserSourceError(
            "OpenCLI Browser Bridge is not connected.",
            error_code="opencli_bridge_unavailable",
        )
    if exit_code == 75:
        return BrowserSourceError(
            "Indeed Deutschland through OpenCLI timed out.",
            error_code="opencli_timeout",
        )
    if exit_code == 77:
        return BrowserSourceError(
            "Indeed Deutschland login is required in the connected Chrome profile.",
            error_code="indeed_auth_required",
        )
    return BrowserSourceError(
        f"OpenCLI Indeed Deutschland failed with exit code {exit_code}.",
        error_code="opencli_failed",
    )


def _required_string(row: dict[object, object], field: str) -> str:
    value = _optional_string(row.get(field))
    if value is None:
        raise InvalidResponse(f"OpenCLI Indeed result missing {field}")
    return value


def _optional_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _required_job_id(value: object) -> str:
    job_id = _optional_string(value)
    if job_id is None or _JOB_KEY.fullmatch(job_id) is None:
        raise InvalidResponse("OpenCLI Indeed result contained an invalid job ID")
    return job_id


def _indeed_job_id(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc.lower() != "de.indeed.com":
        raise InvalidResponse("OpenCLI Indeed result contained a non-German Indeed URL")
    if parsed.path not in {"/viewjob", "/rc/clk"}:
        raise InvalidResponse("OpenCLI Indeed result contained an unsupported job URL")
    job_ids = parse_qs(parsed.query).get("jk", [])
    if len(job_ids) != 1:
        raise InvalidResponse("OpenCLI Indeed result URL did not contain a job ID")
    return _required_job_id(job_ids[0])


def _row_id(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    job_id = _optional_string(value.get("id"))
    return job_id if job_id is not None and _JOB_KEY.fullmatch(job_id) else None


def _is_valid_search_row(value: object) -> bool:
    try:
        _parse_search_row(value)
        _detail_navigation_url(value)
    except InvalidResponse:
        return False
    return True


def _valid_search_row_count(rows: Iterable[object]) -> int:
    return sum(_is_valid_search_row(row) for row in rows)


def _row_item_key(value: object) -> str | None:
    job_id = _row_id(value)
    return f"indeed:de:{job_id}" if job_id is not None else None


def _detail_navigation_url(value: object) -> str:
    if not isinstance(value, dict):
        raise InvalidResponse("OpenCLI Indeed search result must be an object")
    external_id = _required_job_id(value.get("id"))
    navigation_url = _required_string(value, "navigation_url")
    parsed = urlsplit(navigation_url)
    if parsed.scheme != "https" or parsed.netloc.lower() != "de.indeed.com":
        raise InvalidResponse("OpenCLI Indeed navigation URL must stay on de.indeed.com")
    if parsed.path == "/pagead/clk":
        return navigation_url
    if _indeed_job_id(navigation_url) != external_id:
        raise InvalidResponse("OpenCLI Indeed navigation URL did not match its job ID")
    return navigation_url
