from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from urllib.parse import quote, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup, Tag
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
)
from job_scan.sources.job_snapshot_capture import (
    browser_snapshot_script,
    capture_browser_snapshot,
)

_CAREER_ORIGIN = "https://jobs.bosch.com"
_CAREER_URL = f"{_CAREER_ORIGIN}/en/"
_SEARCH_API_ORIGIN = "https://bosch-i3-caas-api.e-spirit.cloud"
_COMPANY_NAME = "Bosch"
_SOURCE_INSTANCE = "bosch"
_DEFAULT_PAGE_SIZE = 100
_SPACE = re.compile(r"\s+")
_POSTED_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_JOBS_API_CONFIG = re.compile(
    r"jobsApi\s*:\s*\{\s*"
    r'baseUrl\s*:\s*"(?P<base_url>[^"]+)"\s*,\s*'
    r'tenant\s*:\s*"(?P<tenant>[^"]+)"\s*,\s*'
    r'project\s*:\s*"(?P<project>[^"]+)"\s*,\s*'
    r'collection\s*:\s*"(?P<collection>[^"]+)"\s*,\s*'
    r'apiKey\s*:\s*"(?P<api_key>[A-Za-z0-9-]+)"\s*\}',
    re.DOTALL,
)
_CITY_QUERY_NAMES = {
    "cologne": "Köln",
    "frankfurt am main": "Frankfurt",
    "hanover": "Hannover",
    "munich": "München",
    "nuremberg": "Nürnberg",
}


def _snapshot_script(external_id: str) -> str:
    expected_id = json.dumps(external_id)
    return browser_snapshot_script(
        rf"""
  const expectedId = {expected_id};
  const metadata = document.querySelector(".ApplyButton[data-job-reference]");
  const rawReference = metadata?.getAttribute("data-job-reference") || "";
  const actualId = rawReference.trim().split(/\s+/).at(-1) || "";
  if (actualId !== expectedId) {{
    return {{status: "unavailable", error_code: "job_identity_mismatch"}};
  }}
  const titleStage = [...document.querySelectorAll(".M-Stage-Two")]
    .find((element) => element.querySelector("h1"));
  const facts = document.querySelector(".Stage-Two__facts");
  const sections = [...document.querySelectorAll("section")];
  const sectionByHeading = (pattern) => sections.find((section) =>
    [...section.querySelectorAll("h1, h2, h3")]
      .some((heading) => pattern.test((heading.textContent || "").trim()))
  );
  const tasks = sectionByHeading(/^(Your tasks|Your profile|Deine Aufgaben|Ihr Aufgabenbereich|Aufgaben)$/i);
  const contact = sectionByHeading(/^(Contact & additional information|Kontakt(?: & weitere Informationen)?)$/i);
  const benefits = sections.filter((section) =>
    section.matches(".M-Text-StagedTypography") &&
    /benefits|vorteile|was wir bieten/i.test(section.textContent || "")
  );
  return buildJobSnapshot({{
    snapshotKey: `bosch:bosch:${{expectedId}}`,
    title: titleStage?.querySelector("h1")?.textContent?.trim() || document.title,
    sourceLabel: "Bosch",
    accent: "#ea0016",
    roots: [titleStage, facts, tasks, contact, ...benefits],
  }});
"""
    )


@dataclass(frozen=True)
class _SearchConfig:
    endpoint: str
    cities_endpoint: str
    api_key: str


class BoschAdapter:
    """Read Bosch jobs from its official career search and detail pages."""

    source = SourceKind.BOSCH
    source_instance = _SOURCE_INSTANCE

    def __init__(
        self,
        config: AppConfig,
        http_client: PublicHttpClient,
        *,
        page_size: int = _DEFAULT_PAGE_SIZE,
        today: Callable[[], date] | None = None,
        capture_snapshot: Callable[[JobReference], bool] | None = None,
    ) -> None:
        if not 1 <= page_size <= 100:
            raise ValueError("page_size must be between 1 and 100")
        self._config = config
        self._http_client = http_client
        self._page_size = page_size
        self._today = today or _utc_today
        self._capture_snapshot = capture_snapshot
        self._search_config: _SearchConfig | None = None

    def discover(self) -> list[JobReference]:
        """Return unique Bosch jobs found by the official search semantics."""
        search_config = self._official_search_config()
        configured_locations = _unique_values(self._config.locations)
        cities = self._official_cities(search_config, configured_locations)
        if configured_locations and not cities:
            return []
        posted_since = self._posted_since()
        references: list[JobReference] = []
        seen_external_ids: set[str] = set()
        query_cities: list[str | None] = [*cities] if cities else [None]
        for search_term in _unique_values(self._config.search_terms):
            for city in query_cities:
                page = 1
                while True:
                    payload = self._search(
                        search_config,
                        search_term=search_term,
                        city=city,
                        page=page,
                    )
                    total, listings = _search_result(payload)
                    if not listings and (page - 1) * self._page_size < total:
                        raise InvalidResponse(
                            "Bosch official search returned an empty page before total jobs"
                        )
                    for listing in listings:
                        reference = _parse_listing(listing, posted_since)
                        if (
                            reference is not None
                            and reference.external_id not in seen_external_ids
                        ):
                            seen_external_ids.add(reference.external_id)
                            references.append(reference)
                    if page * self._page_size >= total:
                        break
                    page += 1
        return references

    def fetch_detail(self, reference: JobReference) -> FetchedOccurrence:
        """Return one complete Bosch posting from its official detail page."""
        self._require_reference(reference)
        source_job_key = (
            f"{self.source.value}:{self.source_instance}:{reference.external_id}"
        )
        try:
            html = self._http_client.get_text_same_origin(
                str(reference.detail_url),
                allowed_origin=_CAREER_ORIGIN,
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
        metadata = soup.select_one(".ApplyButton[data-job-reference]")
        if metadata is None:
            raise InvalidResponse("Bosch official detail missing job metadata")
        detail_reference = _metadata_reference(metadata)
        if detail_reference != reference.external_id:
            raise InvalidResponse("Bosch official detail reference did not match listing")

        title = _attribute_text(metadata, "data-job-name") or reference.listing_title
        posted_at = _metadata_date(metadata) or reference.listing_posted_at
        location = reference.listing_location or _detail_location(soup)
        company = reference.listing_company or _COMPANY_NAME
        description = _detail_description(soup)
        detail_complete = bool(description)
        snapshot_html: str | None = None
        snapshot_error_code: str | None = None
        if self._capture_snapshot is not None and self._capture_snapshot(
            reference.with_current_identity(title=title, posted_at=posted_at)
        ):
            try:
                snapshot_html = capture_browser_snapshot(
                    url=str(reference.detail_url),
                    script=_snapshot_script(reference.external_id),
                    source_name="Bosch",
                )
            except (BrowserSourceError, InvalidResponse):
                snapshot_html = None
            if snapshot_html is None:
                snapshot_error_code = "snapshot_capture_failed"
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
            job_snapshot_html=snapshot_html,
            job_snapshot_error_code=snapshot_error_code,
        )

    def _official_search_config(self) -> _SearchConfig:
        if self._search_config is not None:
            return self._search_config
        html = self._http_client.get_text_same_origin(
            _CAREER_URL,
            allowed_origin=_CAREER_ORIGIN,
            headers={"Accept": "text/html"},
        )
        match = _JOBS_API_CONFIG.search(html)
        if match is None:
            raise InvalidResponse("Bosch career page missing official jobs API config")
        base_url = match.group("base_url").rstrip("/")
        if base_url != _SEARCH_API_ORIGIN:
            raise InvalidResponse("Bosch jobs API config used an unexpected origin")
        tenant = quote(match.group("tenant"), safe="")
        project = quote(match.group("project"), safe="")
        collection = quote(match.group("collection"), safe="")
        self._search_config = _SearchConfig(
            endpoint=(
                f"{base_url}/{tenant}/{project}.{collection}.content/_aggrs/get_jobs"
            ),
            cities_endpoint=(
                f"{base_url}/{tenant}/{project}.{collection}.content/_aggrs/cities"
            ),
            api_key=match.group("api_key"),
        )
        return self._search_config

    def _official_cities(
        self,
        search_config: _SearchConfig,
        configured_locations: list[str],
    ) -> list[str]:
        if not configured_locations:
            return []
        raw_payload = self._http_client.get_text_same_origin(
            search_config.cities_endpoint,
            allowed_origin=_SEARCH_API_ORIGIN,
            params={
                "np": "",
                "rep": "pj",
                "avars": json.dumps({"country": "de"}, separators=(",", ":")),
            },
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {search_config.api_key}",
            },
        )
        try:
            payload = json.loads(raw_payload)
        except json.JSONDecodeError as error:
            raise InvalidResponse("Bosch official cities response is not valid JSON") from error
        available = _city_options(payload)
        result: list[str] = []
        for location in configured_locations:
            query_name = _CITY_QUERY_NAMES.get(_fold_words(location), location)
            query_key = _fold_words(query_name)
            for official_key, official in available.items():
                if (
                    official_key == query_key
                    or official_key.startswith(f"{query_key} ")
                    and not official_key.startswith(f"{query_key} oder")
                ) and official not in result:
                    result.append(official)
        return result

    def _search(
        self,
        search_config: _SearchConfig,
        *,
        search_term: str,
        city: str | None,
        page: int,
    ) -> dict[str, object]:
        variables: dict[str, object] = {
            "country": ["de"],
            "page_language": "en",
            "search_term": search_term,
            "sort": {"releasedDate": -1},
        }
        if city is not None:
            variables["city"] = city
        return self._http_client.get_json_same_origin(
            search_config.endpoint,
            allowed_origin=_SEARCH_API_ORIGIN,
            params={
                "pagesize": self._page_size,
                "page": page,
                "avars": json.dumps(
                    variables,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {search_config.api_key}",
            },
        )

    def _posted_since(self) -> date | None:
        days = self._config.posted_within_days
        return None if days is None else self._today() - timedelta(days=days)

    def _require_reference(self, reference: JobReference) -> None:
        if reference.source is not self.source:
            raise ValueError("reference source must be bosch")
        if reference.source_instance != self.source_instance:
            raise ValueError("reference source_instance must match Bosch")


def _search_result(payload: Mapping[str, object]) -> tuple[int, list[object]]:
    embedded = payload.get("_embedded")
    if not isinstance(embedded, Mapping):
        raise InvalidResponse("Bosch official search missing embedded results")
    results = embedded.get("rh:result")
    if not isinstance(results, list) or not results or not isinstance(results[0], Mapping):
        raise InvalidResponse("Bosch official search missing result object")
    result = results[0]
    listings = result.get("data")
    meta = result.get("meta")
    if not isinstance(listings, list):
        raise InvalidResponse("Bosch official search missing data array")
    # Bosch uses empty data and meta arrays for a valid query with zero jobs.
    if not listings and meta == []:
        return 0, []
    if not isinstance(meta, list) or not meta or not isinstance(meta[0], Mapping):
        raise InvalidResponse("Bosch official search missing result count")
    total = meta[0].get("count")
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        raise InvalidResponse("Bosch official search count must be non-negative")
    return total, listings


def _parse_listing(listing: object, posted_since: date | None) -> JobReference | None:
    if not isinstance(listing, Mapping):
        raise InvalidResponse("Bosch official listing must be an object")
    external_id = _required_string(listing, "refNumber", "Bosch official listing")
    internal_id = _required_string(listing, "_id", "Bosch official listing")
    if internal_id != external_id:
        raise InvalidResponse("Bosch official listing reference fields did not match")
    title = _required_string(listing, "name", "Bosch official listing")
    posted_at = _required_date(listing.get("releasedDate"), "Bosch official listing")
    if posted_since is not None and posted_at < posted_since:
        return None
    job_url = _required_string(listing, "jobUrl", "Bosch official listing")
    detail_url = _official_url(f"/en/job/{job_url}")
    company = _custom_field_value(listing.get("legal_entity")) or _COMPANY_NAME
    location = _listing_location(listing)
    return JobReference(
        source=SourceKind.BOSCH,
        source_instance=_SOURCE_INSTANCE,
        external_id=external_id,
        detail_url=detail_url,
        platform_url=detail_url,
        listing_title=title,
        listing_company=company,
        listing_location=location,
        listing_posted_at=posted_at,
    )


def _listing_location(listing: Mapping[str, object]) -> str:
    location = listing.get("location")
    if isinstance(location, Mapping):
        place = _first_string(location, "workLocation", "city")
        country = _country_name(location.get("country"))
    else:
        place = None
        country = None
    place = place or _custom_field_value(listing.get("working_location"))
    return ", ".join(part for part in (place, country) if part)


def _detail_description(soup: BeautifulSoup) -> str:
    section = soup.select_one('section[aria-label="Your tasks-Your profile"]')
    if section is None:
        return ""
    return _plain_text(section.get_text(" ", strip=True))


def _detail_location(soup: BeautifulSoup) -> str:
    for wrapper in soup.select(".M-JobKeyFacts__termWrapper"):
        term = wrapper.select_one(".M-JobKeyFacts__term")
        fact = wrapper.select_one(".M-JobKeyFacts__fact")
        if term is None or fact is None:
            continue
        if _plain_text(term.get_text(" ", strip=True)).casefold() == "bosch location":
            place = _plain_text(fact.get_text(" ", strip=True))
            return f"{place}, Germany" if place else ""
    return ""


def _metadata_reference(metadata: Tag) -> str:
    value = _attribute_text(metadata, "data-job-reference")
    parts = value.split()
    if not parts:
        raise InvalidResponse("Bosch official detail reference is empty")
    return parts[-1]


def _metadata_date(metadata: Tag) -> date | None:
    value = _attribute_text(metadata, "data-release-date")
    match = _POSTED_DATE.search(value)
    if match is None:
        return None
    return _required_date(match.group(0), "Bosch official detail")


def _attribute_text(element: Tag, attribute: str) -> str:
    value = element.get(attribute)
    return value.strip() if isinstance(value, str) else ""


def _custom_field_value(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    label = value.get("valueLabel")
    return label.strip() if isinstance(label, str) and label.strip() else None


def _first_string(value: Mapping[str, object], *keys: str) -> str | None:
    for key in keys:
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            return item.strip()
    return None


def _country_name(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return "Germany" if value.strip().casefold() == "de" else value.strip()


def _official_url(value: str) -> HttpUrl:
    url = urljoin(f"{_CAREER_ORIGIN}/", value.strip())
    expected = urlsplit(_CAREER_ORIGIN)
    actual = urlsplit(url)
    if (
        actual.scheme.casefold() != expected.scheme.casefold()
        or actual.netloc.casefold() != expected.netloc.casefold()
    ):
        raise InvalidResponse("Bosch job URL must stay on its official origin")
    return HttpUrl(url)


def _required_string(
    payload: Mapping[str, object],
    key: str,
    context: str,
) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InvalidResponse(f"{context} missing {key}")
    return value.strip()


def _required_date(value: object, context: str) -> date:
    if not isinstance(value, str) or not value.strip():
        raise InvalidResponse(f"{context} date must be an ISO timestamp")
    try:
        return datetime.fromisoformat(value.strip()).date()
    except ValueError as error:
        raise InvalidResponse(f"{context} date must be an ISO timestamp") from error


def _city_options(payload: object) -> dict[str, str]:
    if not isinstance(payload, list):
        raise InvalidResponse("Bosch official cities response must be an array")
    options: dict[str, str] = {}
    for option in payload:
        if not isinstance(option, Mapping):
            raise InvalidResponse("Bosch official city option must be an object")
        value = _required_string(option, "_id", "Bosch official city option")
        options.setdefault(_fold_words(value), value)
    return options


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


def _fold_words(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _plain_text(value: str) -> str:
    return _SPACE.sub(" ", value).strip()


def _utc_today() -> date:
    return datetime.now(UTC).date()
