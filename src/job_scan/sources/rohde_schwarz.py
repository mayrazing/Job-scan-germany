from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from datetime import date, datetime
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup, Tag
from pydantic import HttpUrl

from job_scan.config import AppConfig
from job_scan.domain import SourceKind
from job_scan.http_client import InvalidResponse, PublicHttpClient
from job_scan.normalization import content_hash, normalize_job_url
from job_scan.sources.base import (
    BrowserSourceError,
    ExplicitlyClosed,
    FetchedOccurrence,
    JobReference,
)
from job_scan.sources.job_snapshot_capture import (
    browser_snapshot_script,
    capture_browser_snapshot,
)

_ORIGIN = "https://www.rohde-schwarz.com"
_SEARCH_URL = f"{_ORIGIN}/us/career/jobs/career-jobboard_251573.html"
_COMPANY_NAME = "Rohde & Schwarz"
_SOURCE_INSTANCE = "rohdeschwarz"
_PAGE_SIZE = 30
_SPACE = re.compile(r"\s+")
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([,.;:!?])")


def _snapshot_script(external_id: str) -> str:
    expected_id = json.dumps(external_id)
    return browser_snapshot_script(
        rf"""
  const expectedId = {expected_id};
  const metadata = [...document.querySelectorAll("dl")].find((list) =>
    [...list.querySelectorAll("dd")]
      .some((value) => (value.textContent || "").trim() === expectedId)
  );
  if (!metadata) {{
    return {{status: "unavailable", error_code: "job_identity_mismatch"}};
  }}
  const title = document.querySelector(".stage-container h1") ||
    document.querySelector("h1");
  const category = title?.closest(".stage-container")?.querySelector("h2");
  const contentModules = [...document.querySelectorAll(
    ".module.module-generic-content.center"
  )];
  const intro = contentModules.find((module) =>
    !module.id.endsWith("-profile") &&
    /einleitung|aufgaben|your tasks|responsibilities/i.test(module.textContent || "")
  );
  const profile = contentModules.find((module) =>
    module.id.endsWith("-profile") ||
    /qualifikationen|ihr profil|your profile|requirements/i.test(module.textContent || "")
  );
  const benefits = document.querySelector(".module.module-icon-list-benefits");
  return buildJobSnapshot({{
    snapshotKey: `successfactors:rohdeschwarz:${{expectedId}}`,
    title: title?.textContent?.trim() || document.title,
    sourceLabel: "Rohde & Schwarz",
    accent: "#004479",
    roots: [category, title, metadata, intro, profile, benefits],
  }});
"""
    )


class RohdeSchwarzAdapter:
    """Read Rohde & Schwarz jobs from its official parameterized career page."""

    # Keep the existing namespace so the first complete official crawl can mark
    # records from the replaced API as stale instead of leaving them active.
    source = SourceKind.SUCCESSFACTORS
    source_instance = _SOURCE_INSTANCE

    def __init__(
        self,
        config: AppConfig,
        http_client: PublicHttpClient,
        *,
        capture_snapshot: Callable[[JobReference], bool] | None = None,
    ) -> None:
        self._config = config
        self._http_client = http_client
        self._capture_snapshot = capture_snapshot

    def discover(self) -> list[JobReference]:
        """Return unique official jobs for each setup search term and city set."""
        references: list[JobReference] = []
        seen_external_ids: set[str] = set()
        configured_cities = _unique_values(self._config.locations)
        cities = self._official_cities(configured_cities)
        if configured_cities and not cities:
            return []
        for search_term in _unique_values(self._config.search_terms):
            offset = 0
            while True:
                page = self._search(search_term, cities, offset=offset)
                total_jobs, page_references = _parse_search_page(page)
                if not page_references and offset < total_jobs:
                    raise InvalidResponse(
                        "Rohde & Schwarz search returned an empty page before total jobs"
                    )
                for reference in page_references:
                    if reference.external_id not in seen_external_ids:
                        seen_external_ids.add(reference.external_id)
                        references.append(reference)
                offset += _PAGE_SIZE
                if offset >= total_jobs:
                    break
        return references

    def _official_cities(self, configured_cities: list[str]) -> list[str]:
        if not configured_cities:
            return []
        html = self._search("", [], offset=0)
        available = _city_filter_values(html)
        return [
            official
            for city in configured_cities
            if (official := available.get(city.casefold())) is not None
        ]

    def fetch_detail(self, reference: JobReference) -> FetchedOccurrence:
        """Return one complete posting from its official JobPosting data."""
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

        soup = BeautifulSoup(html, "html.parser")
        payload = _jobposting_payload(soup)
        identifier = payload.get("identifier")
        if not isinstance(identifier, Mapping):
            raise InvalidResponse("Rohde & Schwarz JobPosting missing identifier object")
        identifier_value = _required_identifier(identifier)
        if identifier_value != reference.external_id:
            raise InvalidResponse(
                "Rohde & Schwarz JobPosting identifier did not match its listing"
            )

        canonical_url = _canonical_job_url(payload, reference)
        title = _required_string(payload, "title", "Rohde & Schwarz JobPosting")
        primary_description = _plain_html(
            _required_string(
                payload,
                "description",
                "Rohde & Schwarz JobPosting",
            )
        )
        profile_description = _profile_description(soup)
        description = " ".join(
            part for part in (primary_description, profile_description) if part
        )
        company = _hiring_organization(payload) or reference.listing_company
        location = reference.listing_location or _job_locations(payload)
        posted_at = _optional_date(payload.get("datePosted"))
        detail_complete = bool(description)
        snapshot_html: str | None = None
        snapshot_error_code: str | None = None
        if self._capture_snapshot is not None and self._capture_snapshot(
            reference.with_current_identity(title=title, posted_at=posted_at)
        ):
            try:
                snapshot_html = capture_browser_snapshot(
                    url=str(canonical_url),
                    script=_snapshot_script(reference.external_id),
                    source_name="Rohde & Schwarz",
                )
            except (BrowserSourceError, InvalidResponse):
                snapshot_html = None
            if snapshot_html is None:
                snapshot_error_code = "snapshot_capture_failed"
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
            job_snapshot_html=snapshot_html,
            job_snapshot_error_code=snapshot_error_code,
        )

    def _search(self, search_term: str, cities: list[str], *, offset: int) -> str:
        pairs: list[tuple[str, str]] = [
            ("term", search_term),
            ("filter[rsCountry][]", "Germany"),
        ]
        pairs.extend(("filter[rsCity][]", city) for city in cities)
        if offset:
            pairs.append(("offset", str(offset)))
        return self._http_client.get_text_same_origin(
            f"{_SEARCH_URL}?{urlencode(pairs)}",
            allowed_origin=_ORIGIN,
            headers={"Accept": "text/html"},
        )

    def _require_reference(self, reference: JobReference) -> None:
        if reference.source is not self.source:
            raise ValueError("reference source must be successfactors")
        if reference.source_instance != self.source_instance:
            raise ValueError("reference source_instance must match Rohde & Schwarz")


def _parse_search_page(html: str) -> tuple[int, list[JobReference]]:
    soup = BeautifulSoup(html, "html.parser")
    counter = soup.select_one(".module-counter-big .counter")
    if counter is None:
        raise InvalidResponse("Rohde & Schwarz search page missing job counter")
    counter_text = _plain_text(counter.get_text(" ", strip=True))
    if not counter_text.isdigit():
        raise InvalidResponse("Rohde & Schwarz search job counter must be an integer")
    total_jobs = int(counter_text)
    rows = soup.select(".jobboard .results > .module-accordion")
    references = [_parse_listing(row) for row in rows]
    if len(references) > _PAGE_SIZE:
        raise InvalidResponse("Rohde & Schwarz search page exceeded thirty jobs")
    return total_jobs, references


def _city_filter_values(html: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    values: dict[str, str] = {}
    for field in soup.select('input[name="filter[rsCity][]"][value]'):
        value = field.get("value")
        if isinstance(value, str) and value.strip():
            values.setdefault(value.strip().casefold(), value.strip())
    return values


def _parse_listing(row: Tag) -> JobReference:
    id_element = row.select_one("a.favorite[data-job-id]")
    title_element = row.select_one(":scope > .title")
    link_element = row.select_one("a.accordion-table-list-item-title-link[href]")
    if id_element is None:
        raise InvalidResponse("Rohde & Schwarz listing missing job id")
    external_id = id_element.get("data-job-id")
    if not isinstance(external_id, str) or not external_id.strip():
        raise InvalidResponse("Rohde & Schwarz listing job id is empty")
    if title_element is None:
        raise InvalidResponse("Rohde & Schwarz listing missing title")
    title = _plain_text(title_element.get_text(" ", strip=True))
    if not title:
        raise InvalidResponse("Rohde & Schwarz listing title is empty")
    if link_element is None:
        raise InvalidResponse("Rohde & Schwarz listing missing detail link")
    href = link_element.get("href")
    if not isinstance(href, str) or not href.strip():
        raise InvalidResponse("Rohde & Schwarz listing detail link is empty")
    detail_url = _official_url(href)
    country = _column_text(row, "column-5")
    city = _column_text(row, "column-6")
    location = ", ".join(part for part in (city, country) if part)
    return JobReference(
        source=SourceKind.SUCCESSFACTORS,
        source_instance=_SOURCE_INSTANCE,
        external_id=external_id.strip(),
        detail_url=detail_url,
        platform_url=detail_url,
        listing_title=title,
        listing_company=_COMPANY_NAME,
        listing_location=location,
        listing_posted_at=None,
    )


def _column_text(row: Tag, class_name: str) -> str:
    element = row.select_one(f".{class_name} .accordion-table-list-item-info")
    return "" if element is None else _plain_text(element.get_text(" ", strip=True))


def _jobposting_payload(soup: BeautifulSoup) -> Mapping[str, Any]:
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string
        if raw is None:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, Mapping) and payload.get("@type") == "JobPosting":
            return payload
    raise InvalidResponse("Rohde & Schwarz detail missing JobPosting JSON")


def _required_identifier(identifier: Mapping[str, Any]) -> str:
    value = identifier.get("value")
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise InvalidResponse("Rohde & Schwarz JobPosting identifier value is empty")


def _canonical_job_url(
    payload: Mapping[str, Any], reference: JobReference
) -> HttpUrl:
    value = _required_string(payload, "url", "Rohde & Schwarz JobPosting")
    canonical_url = _official_url(value)
    if normalize_job_url(str(canonical_url)) != normalize_job_url(
        str(reference.detail_url)
    ):
        raise InvalidResponse(
            "Rohde & Schwarz JobPosting URL did not match its listing"
        )
    return canonical_url


def _official_url(value: str) -> HttpUrl:
    url = urljoin(f"{_ORIGIN}/", value.strip())
    expected = urlsplit(_ORIGIN)
    actual = urlsplit(url)
    if (
        actual.scheme.casefold() != expected.scheme.casefold()
        or actual.netloc.casefold() != expected.netloc.casefold()
    ):
        raise InvalidResponse("Rohde & Schwarz URL must stay on its official origin")
    return HttpUrl(url)


def _profile_description(soup: BeautifulSoup) -> str:
    parts = [
        _plain_text(element.get_text(" ", strip=True))
        for element in soup.select(
            '.module-generic-content[id$="-profile"] .content-container > .text'
        )
    ]
    return " ".join(part for part in parts if part)


def _hiring_organization(payload: Mapping[str, Any]) -> str | None:
    organization = payload.get("hiringOrganization")
    if not isinstance(organization, Mapping):
        return None
    name = organization.get("name")
    return name.strip() if isinstance(name, str) and name.strip() else None


def _job_locations(payload: Mapping[str, Any]) -> str:
    value = payload.get("jobLocation")
    locations = value if isinstance(value, list) else [value]
    results: list[str] = []
    seen: set[str] = set()
    for location in locations:
        if not isinstance(location, Mapping):
            continue
        address = location.get("address")
        if not isinstance(address, Mapping):
            continue
        locality = address.get("addressLocality")
        country = address.get("addressCountry")
        parts = [
            part.strip()
            for part in (locality, _country_name(country))
            if isinstance(part, str) and part.strip()
        ]
        result = ", ".join(parts)
        if result and result.casefold() not in seen:
            seen.add(result.casefold())
            results.append(result)
    return "; ".join(results)


def _country_name(value: object) -> object:
    if isinstance(value, str) and value.strip().casefold() == "de":
        return "Germany"
    return value


def _optional_date(value: object) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InvalidResponse("Rohde & Schwarz datePosted must be an ISO timestamp")
    try:
        return datetime.fromisoformat(value.strip()).date()
    except ValueError as error:
        raise InvalidResponse(
            "Rohde & Schwarz datePosted must be an ISO timestamp"
        ) from error


def _required_string(payload: Mapping[str, Any], key: str, context: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InvalidResponse(f"{context} missing {key}")
    return value.strip()


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


def _plain_html(value: str) -> str:
    return _plain_text(BeautifulSoup(value, "html.parser").get_text(" ", strip=True))


def _plain_text(value: str) -> str:
    text = _SPACE.sub(" ", value).strip()
    return _SPACE_BEFORE_PUNCTUATION.sub(r"\1", text)
