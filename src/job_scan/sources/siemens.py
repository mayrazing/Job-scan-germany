from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, date, datetime
from urllib.parse import quote, urlencode, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup, Tag
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

_ORIGIN = "https://jobs.siemens.com"
_SEARCH_BASE_URL = f"{_ORIGIN}/en_US/externaljobs/SearchJobs"
_COMPANY_NAME = "Siemens"
_SOURCE_INSTANCE = "siemens"
_COUNTRY_FILTER = "[812132]"
_COUNTRY_FILTER_FORMAT = "17546"
_STATE_FILTER_FORMAT = "17547"
_PAGE_SIZE = 6
_DETAIL_PATH = re.compile(r"^/en_US/externaljobs/JobDetail/(?P<job_id>[0-9]+)$")
_JOB_ID = re.compile(r"^Job ID:\s*(?P<job_id>[0-9]+)$", re.IGNORECASE)
_SPACE = re.compile(r"\s+")
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([,.;:!?])")
# dogtail: Siemens exposes state/city filters as internal option IDs. Suggested
# cities use stable state IDs. Unknown custom cities must not remove the valid
# state filters or turn a city search into a Germany-wide search.
_STATE_ID_BY_CITY = {
    "berlin": "812528",
    "bremen": "813357",
    "cologne": "813008",
    "dresden": "813055",
    "duesseldorf": "813008",
    "frankfurt am main": "813046",
    "hamburg": "815198",
    "hanover": "815300",
    "leipzig": "813055",
    "munich": "813141",
    "nuremberg": "813141",
    "stuttgart": "814103",
}


class SiemensAdapter:
    """Read Siemens jobs from its official career search and detail pages."""

    source = SourceKind.SIEMENS
    source_instance = _SOURCE_INSTANCE

    def __init__(self, config: AppConfig, http_client: PublicHttpClient) -> None:
        self._config = config
        self._http_client = http_client

    def discover(self) -> list[JobReference]:
        """Return Siemens jobs matching the configured search terms and locations."""
        references: list[JobReference] = []
        seen_external_ids: set[str] = set()
        locations = _unique_values(self._config.locations)
        state_ids = _official_state_ids(locations)
        if locations and not state_ids:
            return []
        for search_term in _unique_values(self._config.search_terms):
            next_url: HttpUrl | None = _search_url(search_term, state_ids)
            visited_urls: set[str] = set()
            while next_url is not None:
                current_url = str(next_url)
                if current_url in visited_urls:
                    raise InvalidResponse("Siemens search pagination repeated a page")
                visited_urls.add(current_url)
                html = self._http_client.get_text_same_origin(
                    current_url,
                    allowed_origin=_ORIGIN,
                    headers={"Accept": "text/html"},
                )
                page_references, next_url = _parse_search_page(html)
                if next_url is not None and not page_references:
                    raise InvalidResponse(
                        "Siemens search returned an empty page before its next page"
                    )
                for reference in page_references:
                    if reference.external_id in seen_external_ids:
                        continue
                    seen_external_ids.add(reference.external_id)
                    references.append(reference)
        return references

    def fetch_detail(self, reference: JobReference) -> FetchedOccurrence:
        """Return one complete Siemens posting from its official detail page."""
        self._require_reference(reference)
        source_job_key = f"{self.source.value}:{self.source_instance}:{reference.external_id}"
        try:
            html = self._http_client.get_text_same_origin(
                str(reference.detail_url),
                allowed_origin=_ORIGIN,
                headers={"Accept": "text/html"},
            )
        except httpx.HTTPStatusError as error:
            if error.response.status_code in {404, 410}:
                raise ExplicitlyClosed(
                    source_job_key,
                    "http_404" if error.response.status_code == 404 else "http_410",
                ) from error
            raise

        soup = BeautifulSoup(html, "html.parser")
        fields = _detail_fields(soup)
        detail_job_id = _required_field_text(fields, "Job ID")
        if detail_job_id != reference.external_id:
            raise InvalidResponse("Siemens detail Job ID did not match its listing")

        title_element = soup.select_one("main .section__header__text__title")
        title = _element_text(title_element)
        if not title:
            raise InvalidResponse("Siemens detail missing title")
        company = _element_text(fields.get("Company")) or reference.listing_company
        posted_at = _posted_date(_required_field_text(fields, "Posted since"))
        location = _detail_locations(fields.get("Location(s)"))
        if not location:
            location = reference.listing_location
        description_element = soup.select_one(
            "#section1__content .article__content__view__field__value"
        )
        description = _element_text(description_element)
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

    def _require_reference(self, reference: JobReference) -> None:
        if reference.source is not self.source:
            raise ValueError("reference source must be siemens")
        if reference.source_instance != self.source_instance:
            raise ValueError("reference source_instance must match Siemens")
        _detail_job_id(str(reference.detail_url))


def _search_url(query: str, state_ids: list[str]) -> HttpUrl:
    pairs: list[tuple[str, str | int]] = [
        ("42386", _COUNTRY_FILTER),
        ("42386_format", _COUNTRY_FILTER_FORMAT),
    ]
    if state_ids:
        pairs.extend(
            [
                ("42387", f"[{','.join(state_ids)}]"),
                ("42387_format", _STATE_FILTER_FORMAT),
            ]
        )
    pairs.extend(
        [
            ("listFilterMode", 1),
            ("folderRecordsPerPage", _PAGE_SIZE),
        ]
    )
    params = urlencode(pairs)
    return HttpUrl(f"{_SEARCH_BASE_URL}/{quote(query, safe='')}?{params}")


def _official_state_ids(locations: list[str]) -> list[str]:
    """Return the state IDs for configured cities Siemens can map safely."""
    state_ids: list[str] = []
    for location in locations:
        state_id = _STATE_ID_BY_CITY.get(_normalized_city(location))
        if state_id is None:
            continue
        if state_id not in state_ids:
            state_ids.append(state_id)
    return state_ids


def _parse_search_page(html: str) -> tuple[list[JobReference], HttpUrl | None]:
    soup = BeautifulSoup(html, "html.parser")
    references = [_parse_listing(article) for article in soup.select("article.article--result")]
    next_link = soup.select_one('a[aria-label^="Go to Next Page"]')
    if next_link is None:
        return references, None
    href = next_link.get("href")
    if not isinstance(href, str) or not href.strip():
        raise InvalidResponse("Siemens search next page link is empty")
    return references, _official_url(href)


def _parse_listing(article: Tag) -> JobReference:
    link = article.select_one('h3 a[href*="/en_US/externaljobs/JobDetail/"]')
    if link is None:
        raise InvalidResponse("Siemens search listing missing detail link")
    href = link.get("href")
    if not isinstance(href, str) or not href.strip():
        raise InvalidResponse("Siemens search detail link is empty")
    detail_url = _official_url(href)
    external_id = _detail_job_id(str(detail_url))
    title = _element_text(link)
    if not title:
        raise InvalidResponse("Siemens search listing missing title")
    job_id_element = article.select_one(".list-item-jobId")
    job_id_text = _element_text(job_id_element)
    match = _JOB_ID.fullmatch(job_id_text)
    if match is None:
        raise InvalidResponse("Siemens search listing missing Job ID")
    if match.group("job_id") != external_id:
        raise InvalidResponse("Siemens search Job ID did not match its detail link")
    location = _element_text(article.select_one(".list-item-location"))
    if not location:
        raise InvalidResponse("Siemens search listing missing location")
    return JobReference(
        source=SourceKind.SIEMENS,
        source_instance=_SOURCE_INSTANCE,
        external_id=external_id,
        detail_url=detail_url,
        platform_url=detail_url,
        listing_title=title,
        listing_company=_COMPANY_NAME,
        listing_location=location,
        listing_posted_at=None,
    )


def _detail_job_id(url: str) -> str:
    official_url = _official_url(url)
    match = _DETAIL_PATH.fullmatch(urlsplit(str(official_url)).path)
    if match is None:
        raise InvalidResponse("Siemens detail URL did not contain a Job ID")
    return match.group("job_id")


def _official_url(value: str) -> HttpUrl:
    url = urljoin(f"{_ORIGIN}/", value.strip())
    expected = urlsplit(_ORIGIN)
    actual = urlsplit(url)
    if (
        actual.scheme.casefold() != expected.scheme.casefold()
        or actual.netloc.casefold() != expected.netloc.casefold()
    ):
        raise InvalidResponse("Siemens URL must stay on its official origin")
    return HttpUrl(url)


def _detail_fields(soup: BeautifulSoup) -> dict[str, Tag]:
    section = soup.select_one("#section0__content")
    if section is None:
        raise InvalidResponse("Siemens detail missing job fields")
    fields: dict[str, Tag] = {}
    for field in section.select(".article__content__view__field"):
        label = _element_text(field.select_one(".article__content__view__field__label"))
        value = field.select_one(".article__content__view__field__value")
        if label and value is not None:
            fields[label] = value
    return fields


def _required_field_text(fields: Mapping[str, Tag], label: str) -> str:
    value = _element_text(fields.get(label))
    if not value:
        raise InvalidResponse(f"Siemens detail missing {label}")
    return value


def _detail_locations(value: Tag | None) -> str:
    if value is None:
        return ""
    items = value.select("li")
    raw_locations = [_element_text(item) for item in items] or [_element_text(value)]
    locations: list[str] = []
    seen: set[str] = set()
    for raw_location in raw_locations:
        parts = [part.strip() for part in re.split(r"(?<=\s)-(?=\s)", raw_location) if part.strip()]
        location = ", ".join(parts)
        if location and location.casefold() not in seen:
            seen.add(location.casefold())
            locations.append(location)
    return "; ".join(locations)


def _posted_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%d-%b-%Y").replace(tzinfo=UTC).date()
    except ValueError as error:
        raise InvalidResponse("Siemens Posted since must use DD-Mon-YYYY") from error


def _normalized_city(location: str) -> str:
    city = location.split(",", 1)[0].strip().casefold()
    return city.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")


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


def _element_text(element: Tag | None) -> str:
    if element is None:
        return ""
    text = _SPACE.sub(" ", element.get_text(" ", strip=True))
    return _SPACE_BEFORE_PUNCTUATION.sub(r"\1", text).strip()
