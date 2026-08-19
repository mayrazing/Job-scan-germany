from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import httpx
from bs4 import BeautifulSoup
from pydantic import HttpUrl

from job_scan.config import AppConfig
from job_scan.domain import SourceKind
from job_scan.http_client import InvalidResponse, PublicHttpClient
from job_scan.normalization import content_hash
from job_scan.sources.base import ExplicitlyClosed, FetchedOccurrence, JobReference

_ORIGIN = "https://careers.telekom.com"
_SEARCH_URL = f"{_ORIGIN}/api/jobs-proxy/search"
_DETAIL_BASE_URL = f"{_ORIGIN}/en/jobs"
_COMPANY_NAME = "Deutsche Telekom"
_CLOSED_PAGE_MARKER = "unfortunately, we can't find the page you're looking for."
_SPACE = re.compile(r"\s+")
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([,.;:!?])")
_SLUG_SEPARATORS = re.compile(r"[\s/%]+")
_SLUG_UNSAFE = re.compile(r"[^a-z0-9-]", re.IGNORECASE)
_SLUG_HYPHENS = re.compile(r"-+")
_LOCATION_QUERY_NAMES = {
    "frankfurt am main": "Frankfurt",
}


class TelekomAdapter:
    """Read Deutsche Telekom jobs returned by its official career search."""

    source = SourceKind.TELEKOM
    source_instance = "telekom"

    def __init__(self, config: AppConfig, http_client: PublicHttpClient) -> None:
        self._config = config
        self._http_client = http_client

    def discover(self) -> list[JobReference]:
        """Return unique jobs from each configured Telekom search term."""
        references: list[JobReference] = []
        seen_external_ids: set[str] = set()
        configured_locations = _unique_values(self._config.locations)
        location = self._official_location(configured_locations)
        if configured_locations and not location:
            return []
        location = location or "Germany"
        for query in _search_terms(self._config.search_terms):
            page = 1
            while True:
                payload = self._search(query, location, page=page)
                jobs = payload.get("data")
                if not isinstance(jobs, list):
                    raise InvalidResponse("Telekom search response missing data array")
                pagination = payload.get("pagination_info")
                if not isinstance(pagination, Mapping):
                    raise InvalidResponse("Telekom search response missing pagination_info object")
                response_page = _required_non_negative_int(pagination, "page", "Telekom pagination")
                number_of_pages = _required_non_negative_int(
                    pagination, "number_of_pages", "Telekom pagination"
                )
                if response_page != page:
                    raise InvalidResponse("Telekom search returned an unexpected page")
                for job in jobs:
                    reference = _parse_listing(job)
                    if reference.external_id not in seen_external_ids:
                        seen_external_ids.add(reference.external_id)
                        references.append(reference)
                if page >= number_of_pages:
                    break
                page += 1
        return references

    def _official_location(self, configured_locations: list[str]) -> str:
        if not configured_locations:
            return ""
        payload = self._search("", "Germany", page=1)
        available = _location_options(payload)
        values: list[str] = []
        for location in configured_locations:
            query_name = _LOCATION_QUERY_NAMES.get(location.casefold(), location)
            official = available.get(query_name.casefold())
            if official is not None:
                values.append(official)
        return ";".join(values)

    def fetch_detail(self, reference: JobReference) -> FetchedOccurrence:
        """Return one complete Telekom posting from its JobPosting JSON."""
        self._require_reference(reference)
        source_job_key = f"{self.source.value}:{self.source_instance}:{reference.external_id}"
        try:
            html = self._http_client.get_text_same_origin(
                str(reference.detail_url),
                allowed_origin=_ORIGIN,
            )
        except httpx.HTTPStatusError as error:
            if error.response.status_code in {404, 410}:
                raise ExplicitlyClosed(
                    source_job_key,
                    "http_404" if error.response.status_code == 404 else "http_410",
                ) from error
            raise
        if _CLOSED_PAGE_MARKER in html.casefold():
            raise ExplicitlyClosed(source_job_key, "page_closed_marker")
        payload = _jobposting_payload(html)
        identifier = payload.get("identifier")
        if not isinstance(identifier, Mapping):
            raise InvalidResponse("Telekom JobPosting missing identifier object")
        identifier_value = _required_string(identifier, "value", "Telekom JobPosting identifier")
        if not (
            identifier_value == reference.external_id
            or identifier_value.endswith(f"-{reference.external_id}")
        ):
            raise InvalidResponse("Telekom JobPosting identifier did not match its listing")

        title = _required_string(payload, "title", "Telekom JobPosting")
        description_value = _required_string(payload, "description", "Telekom JobPosting")
        description = _plain_html(description_value)
        company = _hiring_organization(payload) or reference.listing_company
        location = _job_location(payload) or reference.listing_location
        posted_at = _optional_date(payload.get("datePosted"))
        canonical_url = _canonical_job_url(payload) or HttpUrl(
            str(reference.platform_url or reference.detail_url)
        )
        detail_complete = bool(description)
        return FetchedOccurrence(
            source=self.source,
            source_instance=self.source_instance,
            external_id=reference.external_id,
            url=canonical_url,
            company=company,
            title=title,
            location=location,
            description=description,
            posted_at=posted_at,
            content_hash=content_hash(company, title, location, description),
            detail_complete=detail_complete,
            fetch_error_code=None if detail_complete else "missing_full_description",
        )

    def _require_reference(self, reference: JobReference) -> None:
        if reference.source is not self.source:
            raise ValueError("reference source must be telekom")
        if reference.source_instance != self.source_instance:
            raise ValueError("reference source_instance must match Telekom")

    def _search(self, search_term: str, location: str, *, page: int) -> dict[str, Any]:
        query = urlencode(
            {
                "location": location,
                "search": search_term,
                # The public jobs page sends this selector while returning
                # both apprenticeship and professional job_source values.
                "job_source": "apprenticeship",
                "page": page,
            }
        )
        payload = self._http_client.post_json_same_origin(
            f"{_SEARCH_URL}?{query}",
            {"locale": "en", "user_query": search_term},
            allowed_origin=_ORIGIN,
        )
        if payload.get("status_code") != 200:
            raise InvalidResponse("Telekom search response reported failure")
        return payload


def _parse_listing(value: object) -> JobReference:
    if not isinstance(value, Mapping):
        raise InvalidResponse("Telekom search item must be an object")
    external_id = _required_string(value, "requisition_id", "Telekom search item")
    title = _required_string(value, "title", "Telekom search item")
    location = _required_string(value, "city", "Telekom search item")
    detail_url = HttpUrl(f"{_DETAIL_BASE_URL}/{_job_slug(title)}-{quote(external_id, safe='')}")
    apply_url = value.get("apply_url")
    return JobReference(
        source=SourceKind.TELEKOM,
        source_instance=TelekomAdapter.source_instance,
        external_id=external_id,
        detail_url=detail_url,
        platform_url=detail_url,
        listing_title=title,
        listing_company=_COMPANY_NAME,
        listing_location=location,
        listing_posted_at=None,
        listing_application_url=(
            HttpUrl(apply_url) if isinstance(apply_url, str) and apply_url.strip() else None
        ),
    )


def _location_options(payload: Mapping[str, Any]) -> dict[str, str]:
    raw_locations = payload.get("locations")
    if not isinstance(raw_locations, list):
        raise InvalidResponse("Telekom search response missing locations array")
    locations: dict[str, str] = {}
    for value in raw_locations:
        if not isinstance(value, str) or not value.strip():
            raise InvalidResponse("Telekom location option must be a non-empty string")
        locations.setdefault(value.strip().casefold(), value.strip())
    return locations


def _unique_values(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        stripped = value.strip()
        folded = stripped.casefold()
        if stripped and folded not in seen:
            seen.add(folded)
            result.append(stripped)
    return result


def _search_terms(search_terms: list[str]) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for search_term in search_terms:
        value = search_term.strip()
        if value and value.casefold() not in seen:
            seen.add(value.casefold())
            values.append(value)
    return values


def _job_slug(title: str) -> str:
    value = _SLUG_SEPARATORS.sub("-", title)
    value = _SLUG_UNSAFE.sub("", value)
    value = _SLUG_HYPHENS.sub("-", value).strip("-").lower()
    return value or "job"


def _required_string(payload: Mapping[str, Any], key: str, context: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InvalidResponse(f"{context} missing {key}")
    return value.strip()


def _required_non_negative_int(payload: Mapping[str, Any], key: str, context: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise InvalidResponse(f"{context} missing non-negative {key}")
    return value


def _jobposting_payload(html: str) -> Mapping[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    script = soup.select_one('script#job-posting-jsonld[type="application/ld+json"]')
    if script is None or script.string is None:
        raise InvalidResponse("Telekom detail missing JobPosting JSON")
    try:
        payload = json.loads(script.string)
    except json.JSONDecodeError as error:
        raise InvalidResponse("Telekom JobPosting is not valid JSON") from error
    if not isinstance(payload, Mapping) or payload.get("@type") != "JobPosting":
        raise InvalidResponse("Telekom detail JSON is not a JobPosting object")
    return payload


def _hiring_organization(payload: Mapping[str, Any]) -> str | None:
    organization = payload.get("hiringOrganization")
    if not isinstance(organization, Mapping):
        return None
    name = organization.get("name")
    return name.strip() if isinstance(name, str) and name.strip() else None


def _job_location(payload: Mapping[str, Any]) -> str:
    location = payload.get("jobLocation")
    if not isinstance(location, Mapping):
        return ""
    address = location.get("address")
    if not isinstance(address, Mapping):
        return ""
    parts = [address.get("addressLocality"), address.get("addressCountry")]
    return ", ".join(part.strip() for part in parts if isinstance(part, str) and part.strip())


def _optional_date(value: object) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip()).date()
    except ValueError as error:
        raise InvalidResponse("Telekom datePosted must be an ISO timestamp") from error


def _canonical_job_url(payload: Mapping[str, Any]) -> HttpUrl | None:
    value = payload.get("url")
    if not isinstance(value, str) or not value.strip():
        return None
    parts = urlsplit(value.strip())
    if parts.scheme != "https" or parts.netloc.casefold() != "careers.telekom.com":
        raise InvalidResponse("Telekom JobPosting URL must stay on the careers site")
    return HttpUrl(value.strip())


def _plain_html(value: str) -> str:
    text = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    text = _SPACE.sub(" ", text)
    return _SPACE_BEFORE_PUNCTUATION.sub(r"\1", text).strip()
