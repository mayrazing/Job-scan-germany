from __future__ import annotations

import re
import unicodedata
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup, Tag
from pydantic import HttpUrl

from job_scan.config import AppConfig
from job_scan.domain import SourceKind
from job_scan.http_client import InvalidResponse, PublicHttpClient
from job_scan.normalization import content_hash, normalize_job_url
from job_scan.sources.base import (
    ExplicitlyClosed,
    FetchedOccurrence,
    JobReference,
    ListingFilteredOut,
)

_ORIGIN = "https://www.dallmeier.com"
_CAREERS_PATH = "/about-us/careers"
_CAREERS_URL = f"{_ORIGIN}{_CAREERS_PATH}"
_SOURCE_INSTANCE = "dallmeier"
_COMPANY_NAME = "Dallmeier"
_SPACE = re.compile(r"\s+")
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([,.;:!?])")
_LOCATION_PREFIX = re.compile(r"^Standort\s*:\s*", re.IGNORECASE)
_COMPANY_SUFFIX = re.compile(r"\s+[–-]\s+Dallmeier\b.*$", re.IGNORECASE)
_CITY_ALIASES = {
    "aschheim bei munchen": "munchen",
    "cologne": "koln",
    "duesseldorf": "dusseldorf",
    "hanover": "hannover",
    "koeln": "koln",
    "muenchen": "munchen",
    "munich": "munchen",
    "nuernberg": "nurnberg",
    "nuremberg": "nurnberg",
}


class DallmeierAdapter:
    """Read ordinary jobs from Dallmeier's official static careers pages."""

    source = SourceKind.DALLMEIER
    source_instance = _SOURCE_INSTANCE

    def __init__(self, config: AppConfig, http_client: PublicHttpClient) -> None:
        self._config = config
        self._http_client = http_client

    def discover(self) -> list[JobReference]:
        """Return each ordinary job linked under Career Opportunities once."""
        html = self._http_client.get_text_same_origin(
            _CAREERS_URL,
            allowed_origin=_ORIGIN,
            headers={"Accept": "text/html"},
        )
        container = _career_opportunities(BeautifulSoup(html, "html.parser"))
        references: list[JobReference] = []
        seen_external_ids: set[str] = set()
        for link in container.select("a[href]"):
            reference = _parse_listing(link)
            if reference is None:
                continue
            if reference.external_id in seen_external_ids:
                continue
            seen_external_ids.add(reference.external_id)
            references.append(reference)
        if not references:
            raise InvalidResponse("Dallmeier careers page contained no ordinary jobs")
        return references

    def fetch_detail(self, reference: JobReference) -> FetchedOccurrence:
        """Return one complete Dallmeier posting and apply Setup city filtering."""
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
        canonical = soup.select_one('link[rel~="canonical"][href]')
        if canonical is None:
            raise InvalidResponse("Dallmeier detail missing canonical URL")
        canonical_value = canonical.get("href")
        if not isinstance(canonical_value, str):
            raise InvalidResponse("Dallmeier detail canonical URL is empty")
        canonical_url = _official_job_url(canonical_value)
        if normalize_job_url(str(canonical_url)) != normalize_job_url(str(reference.detail_url)):
            raise InvalidResponse("Dallmeier detail canonical URL did not match listing")

        title_element = soup.select_one(".page-header h1")
        if title_element is None:
            raise InvalidResponse("Dallmeier detail missing job title")
        title = _plain_text(title_element.get_text(" ", strip=True))
        if not title:
            raise InvalidResponse("Dallmeier detail job title is empty")

        job_frame = _job_description_frame(soup)
        body = job_frame.select_one(".ce-bodytext")
        if body is None:
            raise InvalidResponse("Dallmeier detail missing job description")
        description = _plain_text(body.get_text(" ", strip=True))
        location = _detail_location(job_frame)
        if self._config.locations and not _matches_any_city(
            location,
            self._config.locations,
        ):
            raise ListingFilteredOut
        company = _detail_company(job_frame)
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
            posted_at=None,
            content_hash=content_hash(company, title, location, description),
            detail_complete=detail_complete,
            fetch_error_code=None if detail_complete else "missing_full_description",
        )

    def _require_reference(self, reference: JobReference) -> None:
        if reference.source is not self.source:
            raise ValueError("reference source must be dallmeier")
        if reference.source_instance != self.source_instance:
            raise ValueError("reference source_instance must match Dallmeier")


def _career_opportunities(soup: BeautifulSoup) -> Tag:
    heading = next(
        (
            item
            for item in soup.select("h2")
            if item.get_text(" ", strip=True).casefold() == "career opportunities"
        ),
        None,
    )
    if heading is None:
        raise InvalidResponse("Dallmeier careers page missing Career Opportunities")

    section = heading.find_parent("section")
    if isinstance(section, Tag):
        row = section.select_one(".row.content")
        if isinstance(row, Tag):
            primary_column = next(
                (child for child in row.children if isinstance(child, Tag) and child.name == "div"),
                None,
            )
            if isinstance(primary_column, Tag):
                container = primary_column.select_one(".ce-bodytext")
                if isinstance(container, Tag):
                    return container
        raise InvalidResponse("Dallmeier careers page missing ordinary job list")

    heading_frame = heading.find_parent("div", class_="frame")
    if not isinstance(heading_frame, Tag):
        raise InvalidResponse("Dallmeier careers page missing ordinary job list")
    listing_frame = heading_frame.find_next_sibling("div", class_="frame")
    if not isinstance(listing_frame, Tag):
        raise InvalidResponse("Dallmeier careers page missing ordinary job list")
    container = listing_frame.select_one(".ce-bodytext")
    if not isinstance(container, Tag):
        raise InvalidResponse("Dallmeier careers page missing ordinary job list")
    return container


def _parse_listing(link: Tag) -> JobReference | None:
    href = link.get("href")
    if not isinstance(href, str) or not href.strip():
        raise InvalidResponse("Dallmeier job link is empty")
    detail_url = _official_job_url(href)
    title = link.get_text(" ", strip=True)
    if not title:
        raise InvalidResponse("Dallmeier job title is empty")
    external_id = urlsplit(str(detail_url)).path.removeprefix(f"{_CAREERS_PATH}/")
    if external_id == "online-application" or external_id.startswith("auszubildende/"):
        return None
    return JobReference(
        source=SourceKind.DALLMEIER,
        source_instance=_SOURCE_INSTANCE,
        external_id=external_id,
        detail_url=detail_url,
        platform_url=detail_url,
        listing_title=title,
        listing_company=_COMPANY_NAME,
        listing_location="",
        listing_posted_at=None,
    )


def _official_job_url(value: str) -> HttpUrl:
    url = urljoin(f"{_ORIGIN}/", value.strip())
    expected = urlsplit(_ORIGIN)
    actual = urlsplit(url)
    if (
        actual.scheme.casefold() != expected.scheme.casefold()
        or actual.netloc.casefold() != expected.netloc.casefold()
        or not actual.path.startswith(f"{_CAREERS_PATH}/")
    ):
        raise InvalidResponse("Dallmeier job URL must stay under its careers page")
    return HttpUrl(url)


def _job_description_frame(soup: BeautifulSoup) -> Tag:
    heading = next(
        (
            item
            for item in soup.select("h2")
            if item.get_text(" ", strip=True).casefold() == "job description"
        ),
        None,
    )
    if heading is None:
        raise InvalidResponse("Dallmeier detail missing Job Description section")
    frame = heading.find_next("div", class_="frame-type-textpic")
    if not isinstance(frame, Tag):
        raise InvalidResponse("Dallmeier detail missing job content frame")
    return frame


def _detail_location(frame: Tag) -> str:
    raw_location = ""
    for element in frame.select("h3, h4, p"):
        text = _plain_text(element.get_text(" ", strip=True))
        if _LOCATION_PREFIX.match(text):
            raw_location = _LOCATION_PREFIX.sub("", text, count=1)
            break
    raw_location = _COMPANY_SUFFIX.sub("", raw_location).strip()
    raw_location = raw_location.partition("|")[0].strip()
    if not raw_location:
        raise InvalidResponse("Dallmeier detail missing Standort")
    cities = [city.strip() for city in re.split(r"\s*(?:\+|;)\s*", raw_location) if city.strip()]
    if not cities:
        raise InvalidResponse("Dallmeier detail Standort is empty")
    return "; ".join(
        city if _fold_words(city).endswith("germany") else f"{city}, Germany" for city in cities
    )


def _detail_company(frame: Tag) -> str:
    text = _plain_text(frame.get_text(" ", strip=True)).casefold()
    if "dallmeier components" in text:
        return "Dallmeier Components GmbH"
    if "dallmeier systems" in text:
        return "Dallmeier Systems GmbH"
    return "Dallmeier electronic GmbH & Co. KG"


def _matches_any_city(location: str, configured_locations: list[str]) -> bool:
    job_cities = {
        _normalized_city(part.split(",", 1)[0]) for part in location.split(";") if part.strip()
    }
    return any(
        _normalized_city(configured) in job_cities
        for configured in configured_locations
        if configured.strip()
    )


def _normalized_city(value: str) -> str:
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
