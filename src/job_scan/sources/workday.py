from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup
from pydantic import HttpUrl

from job_scan.config import AppConfig
from job_scan.domain import SourceKind
from job_scan.http_client import InvalidResponse, PublicHttpClient
from job_scan.normalization import content_hash
from job_scan.sources.base import (
    BrowserSourceError,
    ExplicitlyClosed,
    FetchedOccurrence,
    JobReference,
    ListingFilteredOut,
)
from job_scan.sources.job_snapshot_capture import (
    browser_snapshot_script,
    capture_browser_snapshot,
)

_PAGE_SIZE = 20
_SPACE = re.compile(r"\s+")
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([,.;:!?])")
_POSTED_ON = re.compile(
    r"^Posted\s+(?:(\d+)\+?\s+Da(?:y|ys)\s+Ago|Today|Yesterday)$",
    re.IGNORECASE,
)
_CITY_ALIASES = {
    "cologne": "koln",
    "hanover": "hannover",
    "muenchen": "munchen",
    "munich": "munchen",
    "nuernberg": "nurnberg",
    "nuremberg": "nurnberg",
}


def snapshot_script(external_id: str) -> str:
    expected_id = json.dumps(external_id)
    return browser_snapshot_script(
        rf"""
  const expectedId = {expected_id};
  if (!location.pathname.endsWith("_" + expectedId) &&
      !location.pathname.endsWith("/" + expectedId)) {{
    return {{status: "unavailable", error_code: "job_identity_mismatch"}};
  }}
  const title = document.querySelector('h1[data-automation-id="jobPostingHeader"]');
  const description = document.querySelector('[data-automation-id="jobDescription"]');
  return buildJobSnapshot({{
    snapshotKey: null,
    title: title?.textContent?.trim() || document.title,
    sourceLabel: "Workday",
    accent: "#003087",
    roots: [title, description],
  }});
"""
    )


class WorkdaySiteAdapter:
    """Read one tenant's public postings from its Workday career site."""

    source: SourceKind
    source_instance: str
    _tenant: str
    _site: str
    _company_name: str

    def __init__(
        self,
        config: AppConfig,
        http_client: PublicHttpClient,
        *,
        capture_snapshot: Callable[[JobReference], bool] | None = None,
        today: Callable[[], date] | None = None,
    ) -> None:
        self._config = config
        self._http_client = http_client
        self._capture_snapshot = capture_snapshot
        self._today = today or _utc_today

    def discover(self) -> list[JobReference]:
        """Return unique Workday postings found by each configured search term."""
        references: list[JobReference] = []
        seen_external_ids: set[str] = set()
        for search_term in _unique_values(self._config.search_terms):
            for reference in self._discover_search_term(search_term):
                if reference.external_id not in seen_external_ids:
                    seen_external_ids.add(reference.external_id)
                    references.append(reference)
        return references

    def fetch_detail(self, reference: JobReference) -> FetchedOccurrence:
        """Return one complete Workday posting from its cxs detail API."""
        self._require_reference(reference)
        source_job_key = (
            f"{self.source.value}:{self.source_instance}:{reference.external_id}"
        )
        detail_api_url = self._detail_api_url(reference)
        try:
            payload = self._http_client.get_json_same_origin(
                detail_api_url,
                allowed_origin=self._origin(),
            )
        except httpx.HTTPStatusError as error:
            if error.response.status_code in {404, 410}:
                raise ExplicitlyClosed(
                    source_job_key,
                    "http_404" if error.response.status_code == 404 else "http_410",
                ) from error
            raise

        posting = payload.get("jobPostingInfo")
        if not isinstance(posting, Mapping):
            raise InvalidResponse("Workday detail missing jobPostingInfo")
        external_url = _required_text(posting, "externalUrl")
        if reference.external_id not in external_url:
            raise InvalidResponse("Workday detail identity did not match listing")

        title = _required_text(posting, "title")
        description = _description_text(posting)
        location = _detail_location(posting)
        if self._config.locations and not _matches_any_city(
            location,
            self._config.locations,
        ):
            raise ListingFilteredOut
        posted_at = _detail_posted_at(posting)
        detail_complete = bool(description)
        snapshot_html: str | None = None
        snapshot_error_code: str | None = None
        if self._capture_snapshot is not None and self._capture_snapshot(
            reference.with_current_identity(title=title, posted_at=posted_at)
        ):
            try:
                snapshot_html = capture_browser_snapshot(
                    url=str(reference.detail_url),
                    script=snapshot_script(reference.external_id),
                    source_name=self._company_name,
                )
            except (BrowserSourceError, InvalidResponse):
                snapshot_html = None
            if snapshot_html is None:
                snapshot_error_code = "snapshot_capture_failed"
        return FetchedOccurrence(
            source=self.source,
            source_instance=self.source_instance,
            external_id=reference.external_id,
            url=HttpUrl(external_url),
            company=self._company_name,
            title=title,
            location=location,
            description=description,
            posted_at=posted_at,
            content_hash=content_hash(
                self._company_name,
                title,
                location,
                description,
            ),
            detail_complete=detail_complete,
            fetch_error_code=None if detail_complete else "missing_full_description",
            job_snapshot_html=snapshot_html,
            job_snapshot_error_code=snapshot_error_code,
        )

    def _discover_search_term(self, search_term: str) -> list[JobReference]:
        references: list[JobReference] = []
        offset = 0
        while True:
            payload = self._http_client.post_json_same_origin(
                self._jobs_api_url(),
                {
                    "appliedFacets": {},
                    "limit": _PAGE_SIZE,
                    "offset": offset,
                    "searchText": search_term,
                },
                allowed_origin=self._origin(),
            )
            total = _total_jobs(payload)
            postings = _job_postings(payload)
            if not postings and offset < total:
                raise InvalidResponse(
                    "Workday search returned an empty page before total jobs"
                )
            references.extend(
                reference
                for posting in postings
                if (reference := self._parse_posting(posting)) is not None
            )
            offset += _PAGE_SIZE
            if offset >= total or not postings:
                break
        return references

    def _parse_posting(self, posting: Any) -> JobReference:
        if not isinstance(posting, Mapping):
            raise InvalidResponse("Workday posting is not an object")
        title = _required_text(posting, "title")
        external_path = _required_text(posting, "externalPath")
        if not external_path.startswith("/job/"):
            raise InvalidResponse("Workday posting externalPath is not a job path")
        external_id = _requisition_id(posting)
        detail_url = f"{self._origin()}/{self._site}{external_path}"
        return JobReference(
            source=self.source,
            source_instance=self.source_instance,
            external_id=external_id,
            detail_url=HttpUrl(detail_url),
            platform_url=HttpUrl(detail_url),
            listing_title=title,
            listing_company=self._company_name,
            listing_location=_required_text(posting, "locationsText"),
            listing_posted_at=_posted_on_date(
                _required_text(posting, "postedOn"),
                self._today,
            ),
        )

    def _require_reference(self, reference: JobReference) -> None:
        if reference.source is not self.source:
            raise ValueError("reference source must match this Workday adapter")
        if reference.source_instance != self.source_instance:
            raise ValueError("reference source_instance must match this Workday adapter")

    def _origin(self) -> str:
        return f"https://{self._tenant}.wd3.myworkdayjobs.com"

    def _jobs_api_url(self) -> str:
        return f"{self._origin()}/wday/cxs/{self._tenant}/{self._site}/jobs"

    def _detail_api_url(self, reference: JobReference) -> str:
        detail_path = urlsplit(str(reference.detail_url)).path
        site_prefix = f"/{self._site}/"
        if not detail_path.startswith(site_prefix):
            raise InvalidResponse("Workday detail URL is outside its career site")
        external_path = detail_path.removeprefix(f"/{self._site}")
        return f"{self._origin()}/wday/cxs/{self._tenant}/{self._site}{external_path}"


class HaierAdapter(WorkdaySiteAdapter):
    """Read Haier Europe jobs from Haier's Workday career site."""

    source = SourceKind.HAIER
    source_instance = "haier"
    _tenant = "haier"
    _site = "HaierEurope_Professional_Careers"
    _company_name = "Haier"


class NexperiaAdapter(WorkdaySiteAdapter):
    """Read Nexperia jobs from Nexperia's Workday career site."""

    source = SourceKind.NEXPERIA
    source_instance = "nexperia"
    _tenant = "nexperia"
    _site = "careers"
    _company_name = "Nexperia"


class VosslohAdapter(WorkdaySiteAdapter):
    """Read Vossloh Rolling Stock jobs from Vossloh's Workday career site."""

    source = SourceKind.VOSSLOH
    source_instance = "vossloh"
    _tenant = "vossloh"
    _site = "Vossloh_External_Careers"
    _company_name = "Vossloh"


class JohnsonElectricAdapter(WorkdaySiteAdapter):
    """Read Johnson Electric jobs from its Workday career site."""

    source = SourceKind.JOHNSON_ELECTRIC
    source_instance = "johnson-electric"
    _tenant = "johnsonelectric"
    _site = "Career_JE"
    _company_name = "Johnson Electric"


class AdvantechAdapter(WorkdaySiteAdapter):
    """Read Advantech jobs from Advantech's Workday career site."""

    source = SourceKind.ADVANTECH
    source_instance = "advantech"
    _tenant = "advantech"
    _site = "External"
    _company_name = "Advantech"


def _utc_today() -> date:
    return datetime.now(UTC).date()


def _unique_values(values: list[str]) -> list[str]:
    unique: list[str] = []
    for value in values:
        if value.strip() and value not in unique:
            unique.append(value)
    return unique


def _total_jobs(payload: Mapping[str, Any]) -> int:
    total = payload.get("total")
    if not isinstance(total, int) or total < 0:
        raise InvalidResponse("Workday search missing total job count")
    return total


def _job_postings(payload: Mapping[str, Any]) -> list[Any]:
    postings = payload.get("jobPostings")
    if not isinstance(postings, list):
        raise InvalidResponse("Workday search missing jobPostings")
    return postings


def _requisition_id(posting: Mapping[str, Any]) -> str:
    bullets = posting.get("bulletFields")
    external_id = (
        bullets[0].strip()
        if isinstance(bullets, list) and bullets and isinstance(bullets[0], str)
        else ""
    )
    if not external_id:
        raise InvalidResponse("Workday posting missing requisition id")
    return external_id


def _required_text(posting: Mapping[str, Any], field: str) -> str:
    value = posting.get(field)
    if not isinstance(value, str) or not value.strip():
        raise InvalidResponse(f"Workday posting missing {field}")
    return value.strip()


def _posted_on_date(posted_on: str, today: Callable[[], date]) -> date | None:
    """Convert one relative Workday posting label into an approximate date."""
    match = _POSTED_ON.match(posted_on)
    if match is None:
        return None
    if match.group(1) is not None:
        return today() - timedelta(days=int(match.group(1)))
    if posted_on.casefold() == "posted yesterday":
        return today() - timedelta(days=1)
    return today()


def _detail_posted_at(posting: Mapping[str, Any]) -> date | None:
    start_date = posting.get("startDate")
    if not isinstance(start_date, str) or not start_date.strip():
        return None
    try:
        return date.fromisoformat(start_date.strip())
    except ValueError:
        return None


def _detail_location(posting: Mapping[str, Any]) -> str:
    parts = [_required_text(posting, "location")]
    additional = posting.get("additionalLocations")
    if isinstance(additional, list):
        parts.extend(
            value.strip() for value in additional if isinstance(value, str) and value.strip()
        )
    return "; ".join(parts)


def _description_text(posting: Mapping[str, Any]) -> str:
    raw_description = posting.get("jobDescription")
    if not isinstance(raw_description, str) or not raw_description.strip():
        return ""
    text = BeautifulSoup(raw_description, "html.parser").get_text(" ", strip=True)
    return _plain_text(text)


def _matches_any_city(location: str, configured_locations: list[str]) -> bool:
    folded_location = f" {_fold_words(location)} "
    return any(
        f" {_fold_words(_city_alias(configured))} " in folded_location
        for configured in configured_locations
        if configured.strip()
    )


def _city_alias(value: str) -> str:
    folded = _fold_words(value)
    return _CITY_ALIASES.get(folded, folded)


def _fold_words(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_value = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    return " ".join(re.findall(r"[a-z0-9]+", ascii_value.casefold()))


def _plain_text(value: str) -> str:
    text = _SPACE.sub(" ", value).strip()
    return _SPACE_BEFORE_PUNCTUATION.sub(r"\1", text)
