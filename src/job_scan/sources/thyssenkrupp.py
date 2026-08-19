from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from pydantic import HttpUrl

from job_scan.config import AppConfig
from job_scan.domain import SourceKind
from job_scan.http_client import InvalidResponse, PublicHttpClient
from job_scan.normalization import content_hash
from job_scan.sources.base import (
    ExplicitlyClosed,
    FetchedOccurrence,
    JobReference,
)

_ORIGIN = "https://jobs.thyssenkrupp.com"
_TKMS_ORIGIN = "https://jobs.tkmsgroup.com"
_FILTER_INFO_URL = f"{_ORIGIN}/api/filter/info"
_SEARCH_URL = f"{_ORIGIN}/api/filter/query"
_DETAIL_BASE_URL = f"{_ORIGIN}/en/job/id"
_COMPANY_NAME = "thyssenkrupp"
_SOURCE_INSTANCE = "thyssenkrupp"
_SPACE = re.compile(r"\s+")
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([,.;:!?])")
_CITY_ALIASES = {
    "dusseldorf": "duesseldorf",
    "hannover": "hanover",
    "koeln": "cologne",
    "munchen": "munich",
    "muenchen": "munich",
    "nurnberg": "nuremberg",
    "nuernberg": "nuremberg",
}


class ThyssenkruppAdapter:
    """Read jobs from thyssenkrupp's public search API and detail pages."""

    source = SourceKind.THYSSENKRUPP
    source_instance = _SOURCE_INSTANCE

    def __init__(
        self,
        config: AppConfig,
        http_client: PublicHttpClient,
        *,
        today: Callable[[], date] | None = None,
    ) -> None:
        self._config = config
        self._http_client = http_client
        self._today = today or _utc_today

    def discover(self) -> list[JobReference]:
        """Return unique Germany jobs for each configured Setup search term."""
        city_filters = self._city_filters()
        if self._config.locations and not city_filters:
            return []
        references: list[JobReference] = []
        seen_external_ids: set[str] = set()
        for search_term in _unique_values(self._config.search_terms):
            page = 0
            visited_pages: set[int] = set()
            received_jobs = 0
            while True:
                if page in visited_pages:
                    raise InvalidResponse("thyssenkrupp search pagination repeated a page")
                visited_pages.add(page)
                payload = self._search(search_term, page=page, city_filters=city_filters)
                jobs, next_page, total_hits = _search_result(payload, expected_page=page)
                received_jobs += len(jobs)
                for job in jobs:
                    reference = _parse_listing(job)
                    if reference.external_id not in seen_external_ids:
                        seen_external_ids.add(reference.external_id)
                        references.append(reference)
                if next_page is None:
                    if received_jobs != total_hits:
                        raise InvalidResponse(
                            "thyssenkrupp search result count did not match totalHits"
                        )
                    break
                page = next_page
        return references

    def fetch_detail(self, reference: JobReference) -> FetchedOccurrence:
        """Return one complete posting from its public JobPosting document."""
        self._require_reference(reference)
        source_job_key = (
            f"{self.source.value}:{self.source_instance}:{reference.external_id}"
        )
        try:
            html = self._http_client.get_text_same_origin(
                str(reference.detail_url),
                allowed_origin=_ORIGIN,
                allowed_redirect_origins=(_TKMS_ORIGIN,),
                headers={"Accept": "text/html"},
            )
        except httpx.HTTPStatusError as error:
            if error.response.status_code in {404, 410}:
                raise ExplicitlyClosed(
                    source_job_key,
                    "http_404" if error.response.status_code == 404 else "http_410",
                ) from error
            raise
        payload = _jobposting_payload(html)
        identifier = payload.get("identifier")
        if not isinstance(identifier, Mapping):
            raise InvalidResponse("thyssenkrupp JobPosting missing identifier object")
        identifier_value = _string_value(identifier.get("value"))
        if identifier_value != reference.external_id:
            raise InvalidResponse(
                "thyssenkrupp JobPosting identifier did not match its listing"
            )

        title = _required_string(payload, "title", "thyssenkrupp JobPosting")
        description_html = _required_string(
            payload,
            "description",
            "thyssenkrupp JobPosting",
        )
        description = _plain_html(description_html)
        company = _hiring_organization(payload) or reference.listing_company
        locations = _detail_locations(payload)
        location = "; ".join(locations) or reference.listing_location
        posted_at = _optional_date(
            payload.get("datePosted"),
            "thyssenkrupp JobPosting datePosted",
        )
        detail_complete = bool(description)
        return FetchedOccurrence(
            source=self.source,
            source_instance=self.source_instance,
            external_id=reference.external_id,
            url=reference.platform_url or reference.detail_url,
            company=company,
            title=title,
            location=location,
            description=description,
            posted_at=posted_at,
            content_hash=content_hash(company, title, location, description),
            detail_complete=detail_complete,
            fetch_error_code=None if detail_complete else "missing_full_description",
        )

    def _city_filters(self) -> list[str]:
        configured_cities = _unique_values(self._config.locations)
        if not configured_cities:
            return []
        payload = self._http_client.get_json_same_origin(
            _FILTER_INFO_URL,
            allowed_origin=_ORIGIN,
            params={"locale": "en"},
            headers={"Accept": "application/json"},
        )
        available = _available_city_filters(payload)
        values: list[str] = []
        for configured_city in configured_cities:
            matches = available.get(_normalized_city(configured_city))
            if not matches:
                continue
            values.extend(matches)
        return _unique_values(values)

    def _search(
        self,
        search_term: str,
        *,
        page: int,
        city_filters: list[str],
    ) -> dict[str, Any]:
        filters: dict[str, list[str]] = {
            "data.locations.country": ["data.locations.country:Germany"],
        }
        if city_filters:
            filters["data.locations.cityState"] = city_filters
        posted_since = self._posted_since()
        if posted_since is not None:
            timestamp = int(
                datetime.combine(posted_since, time.min, tzinfo=UTC).timestamp()
            )
            filters["data.postingDate_timestamp"] = [
                f"data.postingDate_timestamp >= {timestamp}"
            ]
        return self._http_client.post_json_same_origin(
            _SEARCH_URL,
            {
                "locale": "en",
                "page": page,
                "searchQuery": search_term,
                "filter": filters,
            },
            allowed_origin=_ORIGIN,
            headers={"Accept": "application/json"},
        )

    def _posted_since(self) -> date | None:
        days = self._config.posted_within_days
        return None if days is None else self._today() - timedelta(days=days)

    def _require_reference(self, reference: JobReference) -> None:
        if reference.source is not self.source:
            raise ValueError("reference source must be thyssenkrupp")
        if reference.source_instance != self.source_instance:
            raise ValueError("reference source_instance must match thyssenkrupp")


def _search_result(
    payload: Mapping[str, object],
    *,
    expected_page: int,
) -> tuple[list[object], int | None, int]:
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        raise InvalidResponse("thyssenkrupp search response missing jobs array")
    page = _required_non_negative_int(payload, "page", "thyssenkrupp search response")
    if page != expected_page:
        raise InvalidResponse("thyssenkrupp search returned an unexpected page")
    page_size = _required_non_negative_int(
        payload,
        "jobsPerPage",
        "thyssenkrupp search response",
    )
    if page_size == 0:
        raise InvalidResponse("thyssenkrupp search jobsPerPage must be positive")
    total_hits = _required_non_negative_int(
        payload,
        "totalHits",
        "thyssenkrupp search response",
    )
    next_page_value = payload.get("nextPage")
    if next_page_value is None:
        next_page = None
    elif (
        isinstance(next_page_value, int)
        and not isinstance(next_page_value, bool)
        and next_page_value >= 0
    ):
        next_page = next_page_value
    else:
        raise InvalidResponse("thyssenkrupp search nextPage must be non-negative or null")
    return jobs, next_page, total_hits


def _parse_listing(value: object) -> JobReference:
    if not isinstance(value, Mapping):
        raise InvalidResponse("thyssenkrupp search item must be an object")
    data = value.get("data")
    if not isinstance(data, Mapping):
        raise InvalidResponse("thyssenkrupp search item missing data object")
    external_id = _required_string(data, "id", "thyssenkrupp search item")
    if _string_value(data.get("idClient")) != external_id or _string_value(
        data.get("idFS")
    ) != external_id:
        raise InvalidResponse("thyssenkrupp search item identity fields did not match")
    title = _required_string(data, "title", "thyssenkrupp search item")
    company = _required_string(data, "company", "thyssenkrupp search item")
    locations = _listing_locations(data)
    detail_url = HttpUrl(f"{_DETAIL_BASE_URL}/{quote(external_id, safe='')}")
    return JobReference(
        source=SourceKind.THYSSENKRUPP,
        source_instance=_SOURCE_INSTANCE,
        external_id=external_id,
        detail_url=detail_url,
        platform_url=detail_url,
        listing_title=title,
        listing_company=company or _COMPANY_NAME,
        listing_location="; ".join(locations),
        listing_posted_at=_optional_date(
            data.get("postingDate"),
            "thyssenkrupp search item postingDate",
        ),
        listing_application_url=_application_url(data.get("applicationUrl")),
    )


def _available_city_filters(payload: Mapping[str, object]) -> dict[str, list[str]]:
    location_tree = payload.get("locationTree")
    if not isinstance(location_tree, list):
        raise InvalidResponse("thyssenkrupp filter info missing locationTree array")
    result: dict[str, list[str]] = {}

    def visit(node: object, *, within_germany: bool) -> None:
        if not isinstance(node, Mapping):
            raise InvalidResponse("thyssenkrupp locationTree item must be an object")
        value = node.get("value")
        label = node.get("label")
        is_germany = value == "data.locations.country:Germany"
        in_scope = within_germany or is_germany
        if (
            in_scope
            and isinstance(value, str)
            and value.startswith("data.locations.cityState:")
            and isinstance(label, str)
            and label.strip()
        ):
            result.setdefault(_normalized_city(label), []).append(value)
        children = node.get("children", [])
        if not isinstance(children, list):
            raise InvalidResponse("thyssenkrupp locationTree children must be an array")
        for child in children:
            visit(child, within_germany=in_scope)

    for root in location_tree:
        visit(root, within_germany=False)
    return result


def _listing_locations(payload: Mapping[str, object]) -> list[str]:
    locations = payload.get("locations")
    if not isinstance(locations, list):
        raise InvalidResponse("thyssenkrupp search item missing locations array")
    result: list[str] = []
    for location in locations:
        if not isinstance(location, Mapping):
            raise InvalidResponse("thyssenkrupp search location must be an object")
        country = _optional_string(location.get("country"))
        city = _optional_string(location.get("city"))
        state = _optional_string(location.get("state"))
        formatted = _format_location(city, state, country)
        if formatted:
            result.append(formatted)
    return _unique_values(result)


def _detail_locations(payload: Mapping[str, object]) -> list[str]:
    raw_locations = payload.get("jobLocation")
    if isinstance(raw_locations, Mapping):
        locations: list[object] = [raw_locations]
    elif isinstance(raw_locations, list):
        locations = raw_locations
    else:
        raise InvalidResponse("thyssenkrupp JobPosting missing jobLocation")
    result: list[str] = []
    for location in locations:
        if not isinstance(location, Mapping):
            raise InvalidResponse("thyssenkrupp JobPosting location must be an object")
        address = location.get("address")
        if not isinstance(address, Mapping):
            raise InvalidResponse("thyssenkrupp JobPosting location missing address")
        country = _optional_string(address.get("addressCountry"))
        city = _optional_string(address.get("addressLocality"))
        state = _optional_string(address.get("addressRegion"))
        formatted = _format_location(city, state, country)
        if formatted:
            result.append(formatted)
    return _unique_values(result)


def _normalized_city(location: str) -> str:
    city = location.split(",", 1)[0].strip().casefold()
    city = city.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
    city = city.replace("ß", "ss")
    return _CITY_ALIASES.get(city, city)


def _jobposting_payload(html: str) -> Mapping[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.select('script[type="application/ld+json"]'):
        value = script.string
        if value is None:
            continue
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as error:
            raise InvalidResponse("thyssenkrupp JobPosting is not valid JSON") from error
        for candidate in _jsonld_objects(payload):
            if candidate.get("@type") == "JobPosting":
                return candidate
    raise InvalidResponse("thyssenkrupp detail missing JobPosting JSON")


def _jsonld_objects(value: object) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        graph = value.get("@graph")
        if isinstance(graph, list):
            return [item for item in graph if isinstance(item, Mapping)]
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, Mapping)]
    return []


def _hiring_organization(payload: Mapping[str, object]) -> str | None:
    organization = payload.get("hiringOrganization")
    if not isinstance(organization, Mapping):
        return None
    return _optional_string(organization.get("name"))


def _application_url(value: object) -> HttpUrl | None:
    raw_url = _optional_string(value)
    if raw_url is None:
        return None
    resolved = urljoin(f"{_ORIGIN}/", raw_url)
    parts = urlsplit(resolved)
    if parts.scheme != "https" or not parts.netloc:
        raise InvalidResponse("thyssenkrupp application URL must use public HTTPS")
    return HttpUrl(resolved)


def _format_location(city: str | None, state: str | None, country: str | None) -> str:
    return ", ".join(part for part in (city, state, country) if part)


def _required_string(payload: Mapping[str, object], key: str, context: str) -> str:
    value = _optional_string(payload.get(key))
    if value is None:
        raise InvalidResponse(f"{context} missing {key}")
    return value


def _required_non_negative_int(
    payload: Mapping[str, object],
    key: str,
    context: str,
) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise InvalidResponse(f"{context} missing non-negative {key}")
    return value


def _string_value(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return ""


def _optional_string(value: object) -> str | None:
    string_value = _string_value(value)
    return string_value or None


def _optional_date(value: object, context: str) -> date | None:
    raw_date = _optional_string(value)
    if raw_date is None:
        return None
    try:
        return datetime.fromisoformat(raw_date).date()
    except ValueError as error:
        raise InvalidResponse(f"{context} must be an ISO date") from error


def _unique_values(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        stripped = value.strip()
        normalized = stripped.casefold()
        if stripped and normalized not in seen:
            seen.add(normalized)
            result.append(stripped)
    return result


def _plain_html(value: str) -> str:
    text = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    text = _SPACE.sub(" ", text)
    return _SPACE_BEFORE_PUNCTUATION.sub(r"\1", text).strip()


def _utc_today() -> date:
    return datetime.now(UTC).date()
