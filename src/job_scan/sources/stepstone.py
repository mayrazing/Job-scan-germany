from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

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
from job_scan.sources.opencli_challenge import (
    DEFAULT_CHALLENGE_WAIT_SECONDS,
    is_challenge_error,
    is_challenge_payload,
    wait_for_challenge_clearance,
)

_DEFAULT_TIMEOUT_SECONDS = 90
_MAX_OUTPUT_BYTES = 5_000_000
_PAGE_SIZE = 25
_ORIGIN = "https://www.stepstone.de"
_JOB_ID = re.compile(r"^\d{5,20}$")
_JOB_PATH = re.compile(r"--(?P<job_id>\d{5,20})(?:-inline)?\.html$")
_AGE_FILTERS = {0: "age_1", 1: "age_1", 3: "age_3", 7: "age_7", 14: "age_14"}

_SEARCH_PAGE_JS = r"""
(async () => {
  const resultItems = () => {
    const items = window.__PRELOADED_STATE__?.["app-unifiedResultlist"]
      ?.searchResults?.items;
    return Array.isArray(items) ? items : null;
  };
  const hasResults = () => !!document.querySelector('[data-testid="job-item"]') &&
    resultItems() !== null;
  const hasResolvedEmptyPage = () => resultItems()?.length === 0 &&
    !!document.querySelector("h1")?.textContent?.trim();
  const hasEmptyState = () => /Es passt gerade kein Job|0 Jobs|no jobs found/i.test(
    document.body?.innerText || ""
  );
  const isChallenge = () => /Access Denied|Just a moment/i.test(document.title || "") ||
    /Reference #[\d.]+|Akamai/i.test(document.body?.innerText || "");
  let ready = hasResults() || hasResolvedEmptyPage() || hasEmptyState() || isChallenge();
  for (let index = 0; index < 30 && !ready; index += 1) {
    await new Promise((resolve) => setTimeout(resolve, 500));
    ready = hasResults() || hasResolvedEmptyPage() || hasEmptyState() || isChallenge();
  }
  if (isChallenge()) return {status: "challenge", rows: []};
  if (!ready) return {status: "not_ready", rows: []};

  const mainIds = new Set(
    (resultItems() || [])
      .filter((item) => item?.section === "main")
      .map((item) => String(item.id || ""))
      .filter(Boolean)
  );
  const cards = [...document.querySelectorAll('[data-testid="job-item"]')];
  const rows = [];
  const seen = new Set();
  for (const card of cards) {
    const id = (card.id || "").replace(/^job-item-/, "");
    if (!id || !mainIds.has(id) || seen.has(id)) continue;
    seen.add(id);
    const titleLink = card.querySelector('[data-testid="job-item-title"]');
    rows.push({
      id,
      title: (titleLink?.innerText || titleLink?.textContent || "").trim(),
      company: (
        card.querySelector('[data-at="job-item-company-name"]')?.textContent || ""
      ).trim(),
      location: (
        card.querySelector('[data-at="job-item-location"]')?.textContent || ""
      ).trim(),
      url: titleLink?.href || "",
      company_url: card.querySelector('a[data-at="company-logo"]')?.href || "",
    });
  }
  return {status: "ok", rows, card_count: cards.length};
})()
""".strip()

_DETAIL_PAGE_JS = r"""
(async () => {
  const isChallenge = () => /Access Denied|Just a moment/i.test(document.title || "") ||
    /Reference #[\d.]+|Akamai/i.test(document.body?.innerText || "");
  const isClosed = () => /Job ist nicht mehr verfügbar|Stelle ist nicht mehr verfügbar|not found/i.test(
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
  if (isChallenge()) return {status: "challenge"};
  if (isClosed()) return {status: "closed"};
  if (!job) return {status: "not_ready"};
  return {status: "ok", job};
})()
""".strip()

_COMPANY_PAGE_JS = r"""
(async () => {
  const isChallenge = () => /Just a moment|Access Denied/i.test(document.title || "") ||
    /Cloudflare Ray ID|Reference #[\d.]+|Verify you are human/i.test(document.body?.innerText || "") ||
    !!document.querySelector('[id^="cf-"], iframe[src*="captcha" i]');
  if (isChallenge()) return {status: "challenge", reported_size: ""};
  const readSize = () => {
    const lines = (document.body?.innerText || "")
      .split(/\n+/)
      .map((value) => value.replace(/\s+/g, " ").trim())
      .filter(Boolean);
    for (const line of lines) {
      const match = line.match(
        /(?:^|•)\s*((?:\d[\d., ]*)\s*(?:(?:bis|to|[-–])\s*\d[\d., ]*|\+))\s+Mitarbeiter\b/i
      );
      if (match) return `${match[1].trim()} Mitarbeiter`;
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
    """Read Mitarbeiter from one StepStone company page through OpenCLI."""
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
    """Read size and industry during one StepStone company-page visit."""
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
    if source.source_name != "stepstone":
        raise ValueError("company-size source must be stepstone")
    executable = str(opencli_executable or _find_opencli())
    session = f"job-scan-stepstone-size-{os.getpid()}-{uuid.uuid4().hex[:8]}"
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
                    "OpenCLI StepStone company output was not valid JSON"
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
            raise InvalidResponse("OpenCLI StepStone company output was not valid JSON") from None
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
                source_name="stepstone",
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
                source_name="stepstone",
                checked_at=checked_at,
            )
            if reported_industry is not None
            else None
        ),
    )


class StepstoneDeAdapter:
    """Read StepStone Deutschland through OpenCLI Browser Bridge primitives."""

    source = SourceKind.STEPSTONE
    source_instance = "de"

    def __init__(
        self,
        config: AppConfig,
        *,
        opencli_executable: str | Path | None = None,
        limit: int | None = None,
        timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
        challenge_wait_seconds: float = DEFAULT_CHALLENGE_WAIT_SECONDS,
    ) -> None:
        resolved_limit = config.stepstone_de_limit if limit is None else limit
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
        self._session = f"job-scan-stepstone-de-{os.getpid()}-{id(self):x}"
        self._discovery_errors: list[SourceError] = []
        self._completed_listing = True
        self._details: dict[str, FetchedOccurrence | Exception] = {}
        self._company_urls: dict[str, str | None] = {}

    def discover(self) -> list[JobReference]:
        """Return deduplicated StepStone jobs for every configured term and location."""
        self._discovery_errors.clear()
        self._completed_listing = True
        self._details.clear()
        self._company_urls.clear()
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
                        self._company_urls[reference.external_id] = _optional_string(
                            row.get("company_url") if isinstance(row, dict) else None
                        )
            for index, reference in enumerate(references):
                try:
                    self._details[reference.external_id] = self._fetch_detail(
                        reference,
                        self._company_urls[reference.external_id],
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
        """Return whether every configured StepStone query completed."""
        return self._completed_listing

    def drain_discovery_errors(self) -> list[SourceError]:
        """Return and clear malformed-row errors from the latest search."""
        errors = self._discovery_errors
        self._discovery_errors = []
        return errors

    def fetch_detail(self, reference: JobReference) -> FetchedOccurrence:
        """Return detail data collected in the matching OpenCLI browser session."""
        if reference.source is not self.source:
            raise ValueError("reference source must be stepstone")
        try:
            detail = self._details[reference.external_id]
        except KeyError:
            raise ValueError(
                f"StepStone detail was not discovered for {reference.external_id}"
            ) from None
        if isinstance(detail, Exception):
            raise detail
        return detail

    def _fetch_detail(
        self,
        reference: JobReference,
        listing_company_url: str | None,
    ) -> FetchedOccurrence:
        """Read one complete StepStone Deutschland JobPosting document."""
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
            raise InvalidResponse("OpenCLI StepStone detail must contain a job object")
        job = payload["job"]
        if _optional_string(job.get("@type")) != "JobPosting":
            raise InvalidResponse("OpenCLI StepStone detail was not a JobPosting")
        detail_url = _required_string(job, "url")
        if _stepstone_job_id(detail_url) != reference.external_id:
            raise InvalidResponse("OpenCLI StepStone detail URL did not match its job ID")
        organization = job.get("hiringOrganization")
        organization = organization if isinstance(organization, dict) else {}
        title = _required_string(job, "title")
        company = _optional_string(organization.get("name")) or reference.listing_company
        location = _job_location(job.get("jobLocation")) or reference.listing_location
        description = _description_text(job.get("description"))
        posted_at = _posted_date(job.get("datePosted"))
        company_size_source = _company_size_source(organization.get("url")) or _company_size_source(
            listing_company_url
        )
        company_industry_source = _company_industry_source(company_size_source)
        detail_complete = bool(description)
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
            company_size_source=company_size_source,
            company_industry_source=company_industry_source,
        )

    def _search(self, search_term: str, location: str) -> list[object]:
        keyed_rows: dict[str, object] = {}
        unkeyed_rows: list[object] = []
        expected_pages = (self._limit + _PAGE_SIZE - 1) // _PAGE_SIZE
        max_pages = expected_pages * 2 + 1
        for page in range(1, max_pages + 1):
            page_url = _search_url(self._config, search_term, location, page)
            payload = self._read_page(page_url, _SEARCH_PAGE_JS)
            status = _page_status(payload, "search")
            if status != "ok":
                _raise_page_failure(status)
            if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
                raise InvalidResponse("OpenCLI StepStone search must contain a rows array")
            page_rows = payload["rows"]
            card_count = payload.get("card_count")
            if type(card_count) is not int or card_count < len(page_rows):
                raise InvalidResponse(
                    "OpenCLI StepStone search card_count must be an integer not smaller than rows"
                )
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
            if card_count < _PAGE_SIZE:
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
            raise InvalidResponse("OpenCLI StepStone output was not valid JSON") from None

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
    """Run one bounded StepStone OpenCLI command and return its text output."""
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
            "StepStone Deutschland through OpenCLI timed out.",
            error_code="opencli_timeout",
        ) from None
    except OSError:
        raise BrowserSourceError(
            "OpenCLI could not be started.", error_code="opencli_start_failed"
        ) from None
    if result.returncode != 0:
        raise _opencli_failure(result.returncode)
    if len(result.stdout.encode("utf-8")) > _MAX_OUTPUT_BYTES:
        raise InvalidResponse("OpenCLI StepStone output exceeded the size limit")
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


def _search_url(config: AppConfig, search_term: str, location: str, page: int) -> str:
    query = quote(_path_slug(search_term), safe="-")
    place = quote(_path_slug(location), safe="-")
    parameters: list[tuple[str, str]] = []
    if config.posted_within_days is not None:
        parameters.append(("ag", _AGE_FILTERS[config.posted_within_days]))
    parameters.append(("sort", "2"))
    if page > 1:
        parameters.append(("page", str(page)))
    return f"{_ORIGIN}/jobs/{query}/in-{place}?{urlencode(parameters)}"


def _path_slug(value: str) -> str:
    return re.sub(r"\s+", "-", value.strip().casefold())


def _parse_search_row(value: object) -> JobReference:
    if not isinstance(value, dict):
        raise InvalidResponse("OpenCLI StepStone search result must be an object")
    external_id = _required_job_id(value.get("id"))
    title = _required_string(value, "title")
    company = _required_string(value, "company")
    location = _required_string(value, "location")
    job_url = _required_string(value, "url")
    if _stepstone_job_id(job_url) != external_id:
        raise InvalidResponse("OpenCLI StepStone result URL did not match its job ID")
    try:
        detail_url = HttpUrl(job_url)
    except ValidationError:
        raise InvalidResponse("OpenCLI StepStone result contained an invalid URL") from None
    company_size_source = _company_size_source(value.get("company_url"))
    return JobReference(
        source=SourceKind.STEPSTONE,
        source_instance="de",
        external_id=external_id,
        detail_url=detail_url,
        listing_title=title,
        listing_company=company,
        listing_location=location,
        listing_company_size_source=company_size_source,
        listing_company_industry_source=_company_industry_source(
            company_size_source
        ),
    )


def _company_size_source(value: object) -> CompanySizeSource | None:
    company_url = _optional_string(value)
    if company_url is None:
        return None
    parsed = urlsplit(company_url)
    if parsed.scheme != "https" or parsed.netloc.casefold() != "www.stepstone.de":
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if (
        len(parts) != 4
        or parts[:2] != ["cmp", "de"]
        or parts[-1].casefold() not in {"jobs", "jobs.html"}
    ):
        return None
    public_url = urlunsplit(("https", "www.stepstone.de", parsed.path, "", ""))
    return CompanySizeSource(
        source_name="stepstone",
        lookup_url=HttpUrl(public_url),
        public_url=HttpUrl(public_url),
        source_title="StepStone company profile",
    )


def _company_industry_source(
    source: CompanySizeSource | None,
) -> CompanyIndustrySource | None:
    if source is None:
        return None
    return CompanyIndustrySource(
        source_name="stepstone",
        lookup_url=source.lookup_url,
        public_url=source.public_url,
        source_title="StepStone company profile",
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
        raise InvalidResponse("OpenCLI StepStone detail contained an invalid datePosted") from None


def _stepstone_job_id(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc.casefold() != "www.stepstone.de":
        raise InvalidResponse("OpenCLI StepStone result contained a non-StepStone URL")
    match = _JOB_PATH.search(parsed.path)
    if match is None:
        raise InvalidResponse("OpenCLI StepStone result contained an unsupported job URL")
    return _required_job_id(match.group("job_id"))


def _page_status(payload: object, page_name: str) -> str:
    if not isinstance(payload, dict):
        raise InvalidResponse(f"OpenCLI StepStone {page_name} output must be an object")
    status = _optional_string(payload.get("status"))
    if status is None:
        raise InvalidResponse(f"OpenCLI StepStone {page_name} output missing status")
    return status


def _raise_page_failure(status: str) -> None:
    if status == "challenge":
        raise BrowserSourceError(
            "StepStone Deutschland served a browser challenge; open the site in the "
            "connected Chrome profile and complete it.",
            error_code="stepstone_challenge",
        )
    if status == "not_ready":
        raise BrowserSourceError(
            "StepStone Deutschland did not finish loading through OpenCLI.",
            error_code="stepstone_page_not_ready",
        )
    raise InvalidResponse(f"OpenCLI StepStone returned unknown page status {status!r}")


def _source_error(error: BrowserSourceError | InvalidResponse) -> SourceError:
    if isinstance(error, BrowserSourceError):
        return SourceError(
            category="browser",
            source=SourceKind.STEPSTONE,
            source_instance="de",
            error_code=error.error_code,
            message=str(error),
        )
    return SourceError(
        category="contract",
        source=SourceKind.STEPSTONE,
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
            "StepStone Deutschland through OpenCLI timed out.",
            error_code="opencli_timeout",
        )
    if exit_code == 77:
        return BrowserSourceError(
            "StepStone Deutschland requires browser interaction in the connected Chrome profile.",
            error_code="stepstone_auth_required",
        )
    return BrowserSourceError(
        f"OpenCLI StepStone Deutschland failed with exit code {exit_code}.",
        error_code="opencli_failed",
    )


def _required_string(row: dict[object, object], field: str) -> str:
    value = _optional_string(row.get(field))
    if value is None:
        raise InvalidResponse(f"OpenCLI StepStone result missing {field}")
    return value


def _optional_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _required_job_id(value: object) -> str:
    job_id = _optional_string(value)
    if job_id is None or _JOB_ID.fullmatch(job_id) is None:
        raise InvalidResponse("OpenCLI StepStone result contained an invalid job ID")
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
    return f"stepstone:de:{job_id}" if job_id is not None else None
