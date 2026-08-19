from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup
from pydantic import HttpUrl

from job_scan.config import AppConfig
from job_scan.domain import CompanyIndustrySource, SourceKind
from job_scan.http_client import InvalidResponse, PublicHttpClient
from job_scan.normalization import content_hash, normalize_text
from job_scan.sources.base import ExplicitlyClosed, FetchedOccurrence, JobReference

_API_BASE_URL = "https://api.smartrecruiters.com/v1/companies"
_PUBLIC_BASE_URL = "https://jobs.smartrecruiters.com"
_DEFAULT_PAGE_SIZE = 100
_SPACE = re.compile(r"\s+")
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([,.;:!?])")
_STANDARD_SECTIONS = (
    "companyDescription",
    "jobDescription",
    "qualifications",
    "additionalInformation",
)
_LOCATION_ALIASES = {
    "cologne": ("cologne", "koln"),
    "hanover": ("hanover", "hannover"),
    "munich": ("munich", "munchen"),
    "nuremberg": ("nuremberg", "nurnberg"),
}
_CITY_QUERY_NAMES = {
    "cologne": "Köln",
    "hanover": "Hannover",
    "munich": "München",
    "nuremberg": "Nürnberg",
}

class SmartRecruitersAdapter:
    """Read one SmartRecruiters company's public postings and details."""

    source = SourceKind.SMARTRECRUITERS

    def __init__(
        self,
        config: AppConfig,
        http_client: PublicHttpClient,
        *,
        company_identifier: str,
        company_name: str,
        page_size: int = _DEFAULT_PAGE_SIZE,
        today: Callable[[], date] | None = None,
    ) -> None:
        company_identifier = company_identifier.strip()
        company_name = company_name.strip()
        if not company_identifier:
            raise ValueError("company_identifier must not be empty")
        if not company_name:
            raise ValueError("company_name must not be empty")
        if not 1 <= page_size <= 100:
            raise ValueError("page_size must be between 1 and 100")
        self._config = config
        self._http_client = http_client
        self._company_identifier = company_identifier
        self._company_name = company_name
        self._page_size = page_size
        self._today = today or _utc_today
        self._details: dict[str, FetchedOccurrence | Exception] = {}
        self.source_instance = company_identifier.casefold()
        encoded_identifier = quote(company_identifier, safe="")
        self._list_url = f"{_API_BASE_URL}/{encoded_identifier}/postings"
        self._public_company_url = f"{_PUBLIC_BASE_URL}/{encoded_identifier}"

    def discover(self) -> list[JobReference]:
        """Return unique recent postings found by each setup term and city."""
        self._details.clear()
        references: list[JobReference] = []
        seen_external_ids: set[str] = set()
        posted_since = self._posted_since()
        for search_term in self._config.search_terms:
            for city in _query_cities(self._config.locations):
                offset = 0
                while True:
                    params: dict[str, str | int] = {
                        "country": "de",
                        "q": search_term,
                        "limit": self._page_size,
                        "offset": offset,
                    }
                    if city is not None:
                        params["city"] = city
                    payload = self._http_client.get_json(
                        self._list_url,
                        params=params,
                    )
                    listings = payload.get("content")
                    total_found = payload.get("totalFound")
                    if not isinstance(listings, list):
                        raise InvalidResponse(
                            "SmartRecruiters listing missing content array"
                        )
                    if (
                        not isinstance(total_found, int)
                        or isinstance(total_found, bool)
                        or total_found < 0
                    ):
                        raise InvalidResponse(
                            "SmartRecruiters listing missing non-negative totalFound"
                        )

                    for listing in listings:
                        reference = self._parse_listing(listing, posted_since)
                        if (
                            reference is not None
                            and reference.external_id not in seen_external_ids
                        ):
                            seen_external_ids.add(reference.external_id)
                            references.append(reference)

                    if offset + len(listings) >= total_found:
                        break
                    if not listings:
                        raise InvalidResponse(
                            "SmartRecruiters pagination ended before totalFound"
                        )
                    offset += len(listings)

        return references

    def fetch_detail(self, reference: JobReference) -> FetchedOccurrence:
        """Return one complete SmartRecruiters posting as plain text."""
        self._require_reference(reference)
        if reference.external_id in self._details:
            detail = self._details[reference.external_id]
            if isinstance(detail, Exception):
                raise detail
            return detail
        source_job_key = (
            f"{self.source.value}:{self.source_instance}:{reference.external_id}"
        )
        try:
            payload = self._http_client.get_json(str(reference.detail_url))
        except httpx.HTTPStatusError as error:
            if error.response.status_code in {404, 410}:
                raise ExplicitlyClosed(
                    source_job_key,
                    "http_404" if error.response.status_code == 404 else "http_410",
                ) from error
            raise

        posting_id = _required_string(payload, "id", "SmartRecruiters detail")
        if posting_id != reference.external_id:
            raise InvalidResponse(
                "SmartRecruiters detail posting ID did not match its listing"
            )
        if payload.get("active") is False:
            raise ExplicitlyClosed(source_job_key, "page_closed_marker")

        company = _company_name(payload) or reference.listing_company
        title = _optional_string(payload, "name") or reference.listing_title
        location = _location_text(payload.get("location")) or reference.listing_location
        posted_at = _optional_date(payload.get("releasedDate"))
        if posted_at is None:
            posted_at = reference.listing_posted_at
        description = _job_description(payload)
        detail_complete = bool(normalize_text(description))
        posting_url = (
            _optional_string(payload, "postingUrl")
            or _optional_string(payload, "applyUrl")
            or str(reference.platform_url or reference.detail_url)
        )
        company_industry_source = _company_industry_source(
            payload.get("industry"),
            reference.detail_url,
            reference.platform_url or HttpUrl(posting_url),
        ) or reference.listing_company_industry_source
        return FetchedOccurrence(
            source=self.source,
            source_instance=self.source_instance,
            external_id=reference.external_id,
            url=HttpUrl(posting_url),
            company=company,
            title=title,
            location=location,
            description=description,
            posted_at=posted_at,
            content_hash=content_hash(company, title, location, description),
            detail_complete=detail_complete,
            fetch_error_code=None if detail_complete else "missing_full_description",
            company_industry_source=company_industry_source,
        )

    def _parse_listing(
        self,
        listing: object,
        posted_since: date | None,
    ) -> JobReference | None:
        if not isinstance(listing, Mapping):
            raise InvalidResponse("SmartRecruiters listing item must be an object")
        title = _required_string(listing, "name", "SmartRecruiters listing")
        location = _location_text(listing.get("location"))
        if not _matches_locations(location, self._config.locations):
            return None
        external_id = _required_string(listing, "id", "SmartRecruiters listing")
        company = _company_name(listing) or self._company_name
        posted_at = _optional_date(listing.get("releasedDate"))
        if posted_since is not None and posted_at is not None and posted_at < posted_since:
            return None
        detail_url = HttpUrl(f"{self._list_url}/{quote(external_id, safe='')}")
        public_url = HttpUrl(
            f"{self._public_company_url}/{quote(external_id, safe='')}"
        )
        return JobReference(
            source=self.source,
            source_instance=self.source_instance,
            external_id=external_id,
            detail_url=detail_url,
            platform_url=public_url,
            listing_title=title,
            listing_company=company,
            listing_location=location,
            listing_posted_at=posted_at,
            listing_company_industry_source=_company_industry_source(
                listing.get("industry"), detail_url, public_url
            ),
        )

    def _posted_since(self) -> date | None:
        days = self._config.posted_within_days
        return None if days is None else self._today() - timedelta(days=days)

    def _require_reference(self, reference: JobReference) -> None:
        if reference.source is not self.source:
            raise ValueError("reference source must be smartrecruiters")
        if reference.source_instance != self.source_instance:
            raise ValueError("reference source_instance must match this company")


def _matches_locations(location: str, configured_locations: list[str]) -> bool:
    """Match setup city names, including common English and German spellings."""
    if not configured_locations:
        return True
    candidate = f" {_fold_words(location)} "
    for configured in configured_locations:
        folded = _fold_words(configured)
        aliases = _LOCATION_ALIASES.get(folded, (folded,))
        if any(alias and f" {alias} " in candidate for alias in aliases):
            return True
    return False


def _company_industry_source(
    value: object,
    lookup_url: HttpUrl,
    public_url: HttpUrl,
) -> CompanyIndustrySource | None:
    if not isinstance(value, Mapping):
        return None
    label = value.get("label")
    if not isinstance(label, str) or not label.strip():
        return None
    return CompanyIndustrySource(
        source_name="smartrecruiters",
        lookup_url=lookup_url,
        public_url=public_url,
        source_title="SmartRecruiters job posting",
        reported_industry=label.strip(),
    )


def _query_cities(configured_locations: list[str]) -> list[str | None]:
    """Return deduplicated SmartRecruiters city parameters for setup locations."""
    if not configured_locations:
        return [None]
    cities: list[str | None] = []
    seen: set[str] = set()
    for location in configured_locations:
        value = _CITY_QUERY_NAMES.get(_fold_words(location), location.strip())
        if value and value.casefold() not in seen:
            seen.add(value.casefold())
            cities.append(value)
    return cities


def _fold_words(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value).casefold()
    without_marks = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )
    return " ".join(re.findall(r"[a-z0-9]+", without_marks))


def _company_name(payload: Mapping[str, Any]) -> str | None:
    company = payload.get("company")
    if not isinstance(company, Mapping):
        return None
    value = company.get("name")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _location_text(value: object) -> str:
    if not isinstance(value, Mapping):
        return ""
    full_location = value.get("fullLocation")
    if isinstance(full_location, str) and full_location.strip():
        return full_location.strip()
    parts = [value.get("city"), value.get("region"), value.get("country")]
    return ", ".join(
        part.strip() for part in parts if isinstance(part, str) and part.strip()
    )


def _job_description(payload: Mapping[str, Any]) -> str:
    job_ad = payload.get("jobAd")
    if not isinstance(job_ad, Mapping):
        return ""
    sections = job_ad.get("sections")
    if not isinstance(sections, Mapping):
        return ""
    blocks: list[str] = []
    for key in _STANDARD_SECTIONS:
        section = sections.get(key)
        if not isinstance(section, Mapping):
            continue
        title = section.get("title")
        text = section.get("text")
        plain_text = _plain_html(text) if isinstance(text, str) else ""
        plain_title = title.strip() if isinstance(title, str) else ""
        block = "\n".join(part for part in (plain_title, plain_text) if part)
        if block:
            blocks.append(block)
    return "\n\n".join(blocks)


def _plain_html(value: str) -> str:
    text = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    text = _SPACE.sub(" ", text)
    return _SPACE_BEFORE_PUNCTUATION.sub(r"\1", text).strip()


def _required_string(payload: Mapping[str, Any], key: str, context: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InvalidResponse(f"{context} field {key} must be a non-empty string")
    return value.strip()


def _optional_string(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _optional_date(value: object) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip()).date()
    except ValueError as error:
        raise InvalidResponse(
            "SmartRecruiters releasedDate must be an ISO timestamp"
        ) from error


def _utc_today() -> date:
    """Return the UTC calendar date used by the scan-wide posted-date cutoff."""
    return datetime.now(UTC).date()
