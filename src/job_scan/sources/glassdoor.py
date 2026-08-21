from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup
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
_PAGE_SIZE = 30
_ORIGIN = "https://www.glassdoor.de"
_LOCATION_PAGE = f"{_ORIGIN}/Job/index.htm"
_JOB_ID = re.compile(r"^\d{10,20}$")
_JOB_TITLE_ID = re.compile(r"^job-title-(?P<job_id>\d{10,20})$")
_AGE_FILTERS = {0: 1, 1: 1, 3: 3, 7: 7, 14: 14}
_CITY_ALIASES = {
    "frankfurt": "frankfurt am main",
    "hannover": "hanover",
    "koeln": "cologne",
    "muenchen": "munich",
    "munchen": "munich",
    "nuernberg": "nuremberg",
    "nurnberg": "nuremberg",
}

_LOCATION_PAGE_JS_TEMPLATE = r"""
(async () => {
  const location = __LOCATION__;
  const isChallenge = () => /Just a moment|Access Denied/i.test(document.title || "") ||
    /Cloudflare Ray ID|Reference #[\d.]+/i.test(document.body?.innerText || "");
  if (isChallenge()) return {status: "challenge", locations: []};
  try {
    const parameters = new URLSearchParams({
      locationTypeFilters: "CITY,STATE,COUNTRY",
      caller: "jobs",
      term: location,
    });
    const response = await fetch(`/autocomplete/location?${parameters}`);
    if (!response.ok) return {status: "not_ready", locations: []};
    const locations = await response.json();
    return {
      status: "ok",
      locations: Array.isArray(locations) ? locations : [],
    };
  } catch (_error) {
    return {status: "not_ready", locations: []};
  }
})()
""".strip()

_SEARCH_PAGE_JS = r"""
(async () => {
  const resultHeading = () => document.querySelector("h1")?.textContent || "";
  const hasResults = () => /[1-9][\d.,]*\s+Jobs\b/i.test(resultHeading()) &&
    !!document.querySelector('li[data-test="jobListing"]');
  const hasEmptyState = () => {
    const primaryText = `${document.title || ""}\n${resultHeading()}`;
    return /(?:^|[^\d])0 Jobs\b|keine Jobs gefunden|no jobs found/i.test(primaryText);
  };
  const isChallenge = () => /Just a moment|Access Denied/i.test(document.title || "") ||
    /Cloudflare Ray ID|Reference #[\d.]+/i.test(document.body?.innerText || "");
  let ready = hasResults() || hasEmptyState() || isChallenge();
  for (let index = 0; index < 30 && !ready; index += 1) {
    await new Promise((resolve) => setTimeout(resolve, 500));
    ready = hasResults() || hasEmptyState() || isChallenge();
  }
  if (isChallenge()) return {status: "challenge", page_url: location.href, rows: []};
  if (!ready) return {status: "not_ready", page_url: location.href, rows: []};
  if (hasEmptyState()) return {status: "ok", page_url: location.href, rows: []};

  const rows = [];
  const seen = new Set();
  for (const card of document.querySelectorAll('li[data-test="jobListing"]')) {
    const titleLink = card.querySelector('a[data-test="job-title"]');
    const match = (titleLink?.id || "").match(/^job-title-(\d{10,20})$/);
    const id = match?.[1] || "";
    if (!id || seen.has(id)) continue;
    seen.add(id);
    rows.push({
      id,
      title: (titleLink?.innerText || titleLink?.textContent || "").trim(),
      company: (
        card.querySelector('[id^="job-employer-"] span')?.textContent || ""
      ).trim(),
      location: (
        card.querySelector('[data-test="emp-location"]')?.textContent || ""
      ).trim(),
      url: titleLink?.href || "",
    });
  }
  return {status: "ok", page_url: location.href, rows};
})()
""".strip()

_DETAIL_PAGE_JS = r"""
(async () => {
  const isChallenge = () => /Just a moment|Access Denied/i.test(document.title || "") ||
    /Cloudflare Ray ID|Reference #[\d.]+/i.test(document.body?.innerText || "");
  const isClosed = () => /Job ist nicht mehr verfügbar|Stelle ist nicht mehr verfügbar|This job is no longer available/i.test(
    document.body?.innerText || ""
  );
  const findPosting = () => {
    for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
      try {
        const parsed = JSON.parse(script.textContent || "null");
        const values = Array.isArray(parsed) ? parsed : [parsed];
        for (const value of values) {
          if (value?.["@type"] === "JobPosting") return value;
          if (Array.isArray(value?.["@graph"])) {
            const posting = value["@graph"].find(
              (item) => item?.["@type"] === "JobPosting"
            );
            if (posting) return posting;
          }
        }
      } catch (_error) {
        // Ignore unrelated malformed structured data and keep looking.
      }
    }
    return null;
  };
  let job = findPosting();
  for (let index = 0; index < 30 && !job && !isChallenge() && !isClosed(); index += 1) {
    await new Promise((resolve) => setTimeout(resolve, 500));
    job = findPosting();
  }
  if (isChallenge()) return {status: "challenge", page_url: location.href};
  if (job) return {status: "ok", page_url: location.href, job};
  if (isClosed()) return {status: "closed", page_url: location.href};
  return {status: "not_ready", page_url: location.href};
})()
""".strip()

_SNAPSHOT_PAGE_JS = browser_snapshot_script(
    r"""
  const isChallenge = () => /Just a moment|Access Denied/i.test(document.title || "") ||
    /Cloudflare Ray ID|Reference #[\d.]+|Verify you are human/i.test(
      document.body?.innerText || ""
    );
  if (isChallenge()) return {status: "challenge"};
  const jobId = new URLSearchParams(location.search).get("jl") ||
    document.querySelector('h1[id^="jd-job-title-"]')?.id.replace("jd-job-title-", "") ||
    "";
  const header = document.querySelector('[data-test="job-details-header"]');
  const title = document.querySelector(`#jd-job-title-${jobId}`) || header?.querySelector("h1");
  const description = document.querySelector('[class*="JobDetails_jobDescription__"]');
  if (!/^\d{10,20}$/.test(jobId) || !header || !title || !description) {
    return {status: "unavailable", error_code: "structure_mismatch"};
  }
  return buildJobSnapshot({
    snapshotKey: `glassdoor:de:${jobId}`,
    title: (title.innerText || title.textContent || "").trim(),
    sourceLabel: "Glassdoor",
    accent: "#007663",
    roots: [header, description],
  });
""".strip()
)

_COMPANY_PAGE_JS = r"""
(async () => {
  const isChallenge = () => /Just a moment|Access Denied/i.test(document.title || "") ||
    /Cloudflare Ray ID|Reference #[\d.]+|Verify you are human/i.test(document.body?.innerText || "") ||
    !!document.querySelector('[id^="cf-"], iframe[src*="captcha" i]');
  if (isChallenge()) return {status: "challenge", reported_size: ""};
  const normalizeNumber = (value) => value.replace(/[^\d]/g, "");
  const readSize = () => {
    const lines = (document.body?.innerText || "")
      .split(/\n+/)
      .map((value) => value.replace(/\s+/g, " ").trim())
      .filter(Boolean);
    for (const line of lines) {
      const match = line.match(
        /\b(Mehr als\s+)?(\d[\d. ]*)(?:\s*(?:bis|[-–])\s*(\d[\d. ]*))?\s+Mitarbeiter\b/i
      );
      if (!match) continue;
      const minimum = normalizeNumber(match[2]);
      if (!minimum) continue;
      if (match[1]) return `${minimum}+ Mitarbeiter`;
      const maximum = normalizeNumber(match[3] || "");
      return maximum ? `${minimum}-${maximum} Mitarbeiter` : `${minimum} Mitarbeiter`;
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


@dataclass(frozen=True)
class _LocationTarget:
    location_type: str
    location_id: int
    name: str


def lookup_company_size(
    source: CompanySizeSource,
    company: str,
    checked_at: datetime,
    *,
    opencli_executable: str | Path | None = None,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
) -> CompanySizeEvidence | None:
    """Read employee count from one Glassdoor company overview page."""
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
    """Read size and industry during one Glassdoor company-page visit."""
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
    if source.source_name != "glassdoor":
        raise ValueError("company-size source must be glassdoor")
    executable = str(opencli_executable or _find_opencli())
    session = f"job-scan-glassdoor-size-{os.getpid()}-{uuid.uuid4().hex[:8]}"
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
                raise InvalidResponse(
                    "OpenCLI Glassdoor company output was not valid JSON"
                ) from None

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
            raise InvalidResponse("OpenCLI Glassdoor company output was not valid JSON") from None
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
                source_name="glassdoor",
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
                source_name="glassdoor",
                checked_at=checked_at,
            )
            if reported_industry is not None
            else None
        ),
    )


class GlassdoorDeAdapter:
    """Read Glassdoor Deutschland through OpenCLI Browser Bridge primitives."""

    source = SourceKind.GLASSDOOR
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
        resolved_limit = getattr(config, "glassdoor_de_limit", 10) if limit is None else limit
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
        self._session = f"job-scan-glassdoor-de-{os.getpid()}-{id(self):x}"
        self._discovery_errors: list[SourceError] = []
        self._completed_listing = True
        self._details: dict[str, FetchedOccurrence | Exception] = {}
        self._locations: dict[str, _LocationTarget | None] = {}

    def discover(self) -> list[JobReference]:
        """Return deduplicated Glassdoor jobs for every configured query."""
        self._discovery_errors.clear()
        self._completed_listing = True
        self._details.clear()
        self._locations.clear()
        references: list[JobReference] = []
        seen_ids: set[str] = set()
        locations = self._config.locations or ["Deutschland"]

        try:
            for search_term in self._config.search_terms:
                for location in locations:
                    try:
                        target = self._resolve_location(location)
                        if target is None:
                            continue
                        rows = self._search(search_term, target)
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
                        if reference.external_id in seen_ids:
                            continue
                        seen_ids.add(reference.external_id)
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
        """Return whether every configured Glassdoor query completed."""
        return self._completed_listing

    def drain_discovery_errors(self) -> list[SourceError]:
        """Return and clear errors collected during the latest search."""
        errors = self._discovery_errors
        self._discovery_errors = []
        return errors

    def fetch_detail(self, reference: JobReference) -> FetchedOccurrence:
        """Return detail data collected in the matching browser session."""
        if reference.source is not self.source:
            raise ValueError("reference source must be glassdoor")
        try:
            detail = self._details[reference.external_id]
        except KeyError:
            raise ValueError(
                f"Glassdoor detail was not discovered for {reference.external_id}"
            ) from None
        if isinstance(detail, Exception):
            raise detail
        return detail

    def _resolve_location(self, location: str) -> _LocationTarget | None:
        """Resolve one configured place to Glassdoor's German location identity."""
        cache_key = location.strip().casefold()
        if cache_key in self._locations:
            return self._locations[cache_key]
        target: _LocationTarget | None
        if cache_key in {"deutschland", "germany"}:
            target = _LocationTarget("N", 96, "Deutschland")
        else:
            self._open_page(_LOCATION_PAGE)
            script = _LOCATION_PAGE_JS_TEMPLATE.replace("__LOCATION__", json.dumps(location))
            payload = self._evaluate(script)
            status = _page_status(payload, "location")
            if status != "ok":
                _raise_page_failure(status)
            if not isinstance(payload, dict) or not isinstance(payload.get("locations"), list):
                raise InvalidResponse(
                    "OpenCLI Glassdoor location output must contain a locations array"
                )
            target = _select_location(payload["locations"], location)
        self._locations[cache_key] = target
        return target

    def _fetch_detail(self, reference: JobReference) -> FetchedOccurrence:
        """Read one complete Glassdoor Deutschland JobPosting document."""
        self._open_page(str(reference.detail_url))
        payload = self._evaluate(_DETAIL_PAGE_JS)
        status = _page_status(payload, "detail")
        if status == "closed":
            raise ExplicitlyClosed(
                f"{self.source.value}:{self.source_instance}:{reference.external_id}",
                "page_closed_marker",
            )
        if status != "ok":
            _raise_page_failure(status)
        if not isinstance(payload, dict) or not isinstance(payload.get("job"), dict):
            raise InvalidResponse("OpenCLI Glassdoor detail must contain a job object")
        page_url = _required_string(payload, "page_url")
        if _glassdoor_job_id(page_url) != reference.external_id:
            raise InvalidResponse("OpenCLI Glassdoor detail URL did not match its job ID")
        job = payload["job"]
        if _optional_string(job.get("@type")) != "JobPosting":
            raise InvalidResponse("OpenCLI Glassdoor detail was not a JobPosting")
        organization = job.get("hiringOrganization")
        organization = organization if isinstance(organization, dict) else {}
        title = _required_string(job, "title")
        company = _optional_string(organization.get("name")) or reference.listing_company
        location = _job_location(job.get("jobLocation")) or reference.listing_location
        description = _description_text(job.get("description"))
        posted_at = _posted_date(job.get("datePosted"))
        company_size_source = _company_size_source(organization.get("sameAs"))
        company_industry_source = _company_industry_source(company_size_source)
        detail_complete = bool(description)
        snapshot_html: str | None = None
        snapshot_error_code: str | None = None
        if self._capture_snapshot is not None and self._capture_snapshot(
            reference.with_current_identity(title=title, posted_at=posted_at)
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
            posted_at=posted_at,
            content_hash=content_hash(company, title, location, description),
            detail_complete=detail_complete,
            fetch_error_code=None if detail_complete else "missing_full_description",
            job_snapshot_error_code=snapshot_error_code,
            job_snapshot_html=snapshot_html,
            company_size_source=company_size_source,
            company_industry_source=company_industry_source,
        )

    def _search(self, search_term: str, target: _LocationTarget) -> list[object]:
        keyed_rows: dict[str, object] = {}
        unkeyed_rows: list[object] = []
        expected_pages = (self._limit + _PAGE_SIZE - 1) // _PAGE_SIZE
        max_pages = expected_pages * 2 + 1
        first_page_url: str | None = None
        for page in range(1, max_pages + 1):
            if page == 1:
                page_url = _search_url(self._config, search_term, target)
            else:
                if first_page_url is None:
                    raise InvalidResponse("OpenCLI Glassdoor search output missing page URL")
                page_url = _pagination_url(first_page_url, page)
            payload = self._read_page(page_url, _SEARCH_PAGE_JS)
            status = _page_status(payload, "search")
            if status != "ok":
                _raise_page_failure(status)
            if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
                raise InvalidResponse("OpenCLI Glassdoor search must contain a rows array")
            if page == 1:
                first_page_url = _required_string(payload, "page_url")
            page_rows = payload["rows"]
            for row in page_rows:
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
            if len(page_rows) < _PAGE_SIZE:
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
            raise InvalidResponse("OpenCLI Glassdoor output was not valid JSON") from None

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


def _run_opencli(executable: str, arguments: list[str], timeout_seconds: float) -> str:
    """Run one bounded Glassdoor OpenCLI command and return its output."""
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
            "Glassdoor Deutschland through OpenCLI timed out.",
            error_code="opencli_timeout",
        ) from None
    except OSError:
        raise BrowserSourceError(
            "OpenCLI could not be started.", error_code="opencli_start_failed"
        ) from None
    if result.returncode != 0:
        raise _opencli_failure(result.returncode)
    if len(result.stdout.encode("utf-8")) > _MAX_OUTPUT_BYTES:
        raise InvalidResponse("OpenCLI Glassdoor output exceeded the size limit")
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


def _find_opencli() -> str:
    scheduled = os.environ.get("JOB_SCAN_OPENCLI", "").strip()
    if scheduled:
        return scheduled
    executable = shutil.which("opencli")
    if executable is not None:
        return executable
    return str(Path(sys.executable).with_name("opencli"))


def _select_location(values: list[object], requested: str) -> _LocationTarget | None:
    requested_key = _normalized_city(requested)
    german: list[dict[object, object]] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        if _optional_string(value.get("country2LetterIso")) != "DE":
            continue
        german.append(value)
    if not german:
        return None
    matched = next(
        (
            value
            for value in german
            if requested_key
            in {
                _normalized_city(_optional_string(value.get(field)) or "")
                for field in ("locationName", "cityName", "label")
            }
        ),
        None,
    )
    if matched is None:
        return None
    location_type = _required_string(matched, "locationType")
    location_id = matched.get("locationId")
    if isinstance(location_id, bool) or not isinstance(location_id, int) or location_id <= 0:
        raise InvalidResponse("Glassdoor location contained an invalid locationId")
    name = _required_string(matched, "locationName")
    return _LocationTarget(location_type, location_id, name)


def _normalized_city(value: str) -> str:
    city = value.split(",", 1)[0].strip().casefold()
    city = (
        city.replace("ä", "ae")
        .replace("ö", "oe")
        .replace("ü", "ue")
        .replace("ß", "ss")
    )
    return _CITY_ALIASES.get(city, city)


def _search_url(
    config: AppConfig,
    search_term: str,
    target: _LocationTarget,
) -> str:
    place_slug = _path_slug(target.name)
    query_slug = _path_slug(search_term)
    query_start = len(place_slug) + 1
    query_end = query_start + len(query_slug)
    location_code = f"I{target.location_type.upper()}{target.location_id}"
    path = (
        f"/Job/{quote(place_slug, safe='-')}-{quote(query_slug, safe='-')}-jobs-"
        f"SRCH_IL.0,{len(place_slug)}_{location_code}_KO{query_start},{query_end}.htm"
    )
    parameters: list[tuple[str, str]] = []
    if config.posted_within_days is not None:
        parameters.append(("fromAge", str(_AGE_FILTERS[config.posted_within_days])))
    parameters.append(("sortBy", "date_desc"))
    return urlunsplit(("https", "www.glassdoor.de", path, urlencode(parameters), ""))


def _path_slug(value: str) -> str:
    slug = re.sub(r"[^\w]+", "-", value.strip().casefold()).strip("-")
    if not slug:
        raise InvalidResponse("Glassdoor search value could not form a URL path")
    return slug.replace("_", "-")


def _pagination_url(first_page_url: str, page: int) -> str:
    parsed = urlsplit(first_page_url)
    if (
        parsed.scheme != "https"
        or parsed.netloc.casefold() != "www.glassdoor.de"
        or "-SRCH_" not in parsed.path
        or not parsed.path.endswith(".htm")
    ):
        raise InvalidResponse("OpenCLI Glassdoor search returned an unsupported page URL")
    path = re.sub(r"(?:_IP\d+)?\.htm$", f"_IP{page}.htm", parsed.path)
    return urlunsplit(("https", "www.glassdoor.de", path, parsed.query, ""))


def _parse_search_row(value: object) -> JobReference:
    if not isinstance(value, dict):
        raise InvalidResponse("OpenCLI Glassdoor search result must be an object")
    external_id = _required_job_id(value.get("id"))
    title = _required_string(value, "title")
    company = _required_string(value, "company")
    location = _required_string(value, "location")
    job_url = _required_string(value, "url")
    if _glassdoor_job_id(job_url) != external_id:
        raise InvalidResponse("OpenCLI Glassdoor result URL did not match its job ID")
    try:
        detail_url = HttpUrl(job_url)
    except ValidationError:
        raise InvalidResponse("OpenCLI Glassdoor result contained an invalid URL") from None
    return JobReference(
        source=SourceKind.GLASSDOOR,
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
    if parsed.scheme != "https" or parsed.netloc.casefold() != "www.glassdoor.de":
        return None
    if re.search(r"-EI_IE\d+(?:\.\d+,\d+)?\.htm$", parsed.path, re.IGNORECASE) is None:
        return None
    public_url = urlunsplit(("https", "www.glassdoor.de", parsed.path, "", ""))
    return CompanySizeSource(
        source_name="glassdoor",
        lookup_url=HttpUrl(public_url),
        public_url=HttpUrl(public_url),
        source_title="Glassdoor company profile",
    )


def _company_industry_source(
    source: CompanySizeSource | None,
) -> CompanyIndustrySource | None:
    if source is None:
        return None
    return CompanyIndustrySource(
        source_name="glassdoor",
        lookup_url=source.lookup_url,
        public_url=source.public_url,
        source_title="Glassdoor company profile",
    )


def _description_text(value: object) -> str:
    html = _optional_string(value)
    if html is None:
        return ""
    lines = BeautifulSoup(html, "html.parser").get_text("\n", strip=True).splitlines()
    return "\n".join(line.strip() for line in lines if line.strip())


def _job_location(value: object) -> str | None:
    locations = value if isinstance(value, list) else [value]
    names: list[str] = []
    for location in locations:
        if not isinstance(location, dict):
            continue
        address = location.get("address")
        if not isinstance(address, dict):
            continue
        name = _optional_string(address.get("addressLocality"))
        if name is not None and name not in names:
            names.append(name)
    return ", ".join(names) or None


def _posted_date(value: object) -> date | None:
    raw = _optional_string(value)
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        raise InvalidResponse("OpenCLI Glassdoor detail contained an invalid datePosted") from None


def _glassdoor_job_id(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc.casefold() != "www.glassdoor.de":
        raise InvalidResponse("OpenCLI Glassdoor result contained a non-Glassdoor URL")
    parameters = parse_qs(parsed.query)
    values = parameters.get("jl") or parameters.get("jobListingId") or []
    if len(values) != 1:
        raise InvalidResponse("OpenCLI Glassdoor result contained an unsupported job URL")
    return _required_job_id(values[0])


def _page_status(payload: object, page_name: str) -> str:
    if not isinstance(payload, dict):
        raise InvalidResponse(f"OpenCLI Glassdoor {page_name} output must be an object")
    status = _optional_string(payload.get("status"))
    if status is None:
        raise InvalidResponse(f"OpenCLI Glassdoor {page_name} output missing status")
    return status


def _raise_page_failure(status: str) -> None:
    if status == "challenge":
        raise BrowserSourceError(
            "Glassdoor Deutschland served a browser challenge; open the site in the "
            "connected Chrome profile and complete it.",
            error_code="glassdoor_challenge",
        )
    if status == "not_ready":
        raise BrowserSourceError(
            "Glassdoor Deutschland did not finish loading through OpenCLI.",
            error_code="glassdoor_page_not_ready",
        )
    raise InvalidResponse(f"OpenCLI Glassdoor returned unknown page status {status!r}")


def _source_error(error: BrowserSourceError | InvalidResponse) -> SourceError:
    if isinstance(error, BrowserSourceError):
        return SourceError(
            category="browser",
            source=SourceKind.GLASSDOOR,
            source_instance="de",
            error_code=error.error_code,
            message=str(error),
        )
    return SourceError(
        category="contract",
        source=SourceKind.GLASSDOOR,
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
            "Glassdoor Deutschland through OpenCLI timed out.",
            error_code="opencli_timeout",
        )
    if exit_code == 77:
        return BrowserSourceError(
            "Glassdoor Deutschland requires browser interaction in the connected Chrome profile.",
            error_code="glassdoor_auth_required",
        )
    return BrowserSourceError(
        f"OpenCLI Glassdoor Deutschland failed with exit code {exit_code}.",
        error_code="opencli_failed",
    )


def _required_string(row: dict[object, object], field: str) -> str:
    value = _optional_string(row.get(field))
    if value is None:
        raise InvalidResponse(f"OpenCLI Glassdoor result missing {field}")
    return value


def _optional_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _required_job_id(value: object) -> str:
    job_id = _optional_string(value)
    if job_id is None or _JOB_ID.fullmatch(job_id) is None:
        raise InvalidResponse("OpenCLI Glassdoor result contained an invalid job ID")
    return job_id


def _row_id(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    job_id = _optional_string(value.get("id"))
    return job_id if job_id is not None and _JOB_ID.fullmatch(job_id) else None


def _is_valid_search_row(value: object) -> bool:
    try:
        _parse_search_row(value)
    except InvalidResponse:
        return False
    return True


def _valid_search_row_count(rows: Iterable[object]) -> int:
    return sum(_is_valid_search_row(row) for row in rows)


def _row_item_key(value: object) -> str | None:
    job_id = _row_id(value)
    return f"glassdoor:de:{job_id}" if job_id is not None else None
