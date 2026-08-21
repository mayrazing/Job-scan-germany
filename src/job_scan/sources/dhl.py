from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime
from typing import Any
from urllib.parse import quote, unquote, urlsplit

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
)
from job_scan.sources.job_snapshot_capture import (
    browser_snapshot_script,
    capture_browser_snapshot,
)

_ORIGIN = "https://careers.dhl.com"
_SEARCH_URL = f"{_ORIGIN}/amer/en/search-results"
_WIDGETS_URL = f"{_ORIGIN}/widgets"
_DETAIL_BASE_URL = f"{_ORIGIN}/amer/en/job"
_GERMANY_PLACE_ID = "ChIJa76xwh5ymkcRW-WRjmtd6HU"
_COMPANY_NAME = "DHL Group"
_SOURCE_INSTANCE = "dhl"
_DDO_MARKER = "phApp.ddo = "
_SPACE = re.compile(r"\s+")
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([,.;:!?])")
_CITY_ALIASES = {
    "hannover": "hanover",
    "koeln": "cologne",
    "muenchen": "munich",
    "nuernberg": "nuremberg",
}

def _snapshot_page_js(expected_external_id: str) -> str:
    return browser_snapshot_script(
        r"""
  const expectedJobId = __EXPECTED_JOB_ID__;
  const isChallenge = () => /Just a moment|Access Denied/i.test(document.title || "") ||
    /Verify you are human|Cloudflare Ray ID/i.test(document.body?.innerText || "");
  if (isChallenge()) return {status: "challenge"};
  const embeddedJob = globalThis.phApp?.ddo?.jobDetail?.data?.job;
  const pageJobId = location.pathname.match(/\/job\/([^/?#]+)/)?.[1] ||
    document.querySelector("[data-job-id]")?.getAttribute("data-job-id") ||
    embeddedJob?.jobSeqNo || "";
  if (!pageJobId) {
    return {status: "unavailable", error_code: "structure_mismatch"};
  }
  if (pageJobId !== expectedJobId) {
    return {status: "unavailable", error_code: "job_identity_mismatch"};
  }

  let header = document.querySelector(".job-info");
  let title = header?.querySelector(".job-title, h1");
  let description = document.querySelector(".job-description");
  let roots;
  if (!header || !title || !description) {
    if (
      embeddedJob?.jobSeqNo !== expectedJobId ||
      typeof embeddedJob.title !== "string" ||
      typeof embeddedJob.description !== "string"
    ) {
      return {status: "unavailable", error_code: "structure_mismatch"};
    }
    header = document.createElement("section");
    header.className = "job-info";
    title = document.createElement("h1");
    title.className = "job-title";
    title.textContent = embeddedJob.title;
    header.append(title);
    for (const [className, value] of [
      ["job-company", embeddedJob.companyName || embeddedJob.jobCompany],
      ["job-location", embeddedJob.location],
      ["job-posted-date", embeddedJob.datePosted],
    ]) {
      if (typeof value !== "string" || !value.trim()) continue;
      const detail = document.createElement("div");
      detail.className = className;
      detail.textContent = value.trim();
      header.append(detail);
    }
    description = document.createElement("section");
    description.className = "job-description";
    description.innerHTML = embeddedJob.description;
    for (const action of description.querySelectorAll(
      "a, button, form, input, select, textarea"
    )) {
      action.remove();
    }
    roots = [header, description];
  } else {
    const headerRoots = [...header.children].filter((element) =>
      !/^(Copy job link|Share)\b/i.test(
        (element.innerText || "").replace(/\s+/g, " ").trim()
      )
    );
    roots = [...headerRoots, description];
  }
  return buildJobSnapshot({
    snapshotKey: `dhl:dhl:${expectedJobId}`,
    title: (title.innerText || title.textContent || "").trim(),
    sourceLabel: "DHL",
    accent: "#d40511",
    roots,
  });
""".strip().replace("__EXPECTED_JOB_ID__", json.dumps(expected_external_id))
    )


class DhlAdapter:
    """Read DHL jobs returned by the public Phenom career search."""

    source = SourceKind.DHL
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
        """Return unique DHL jobs matching each setup search term and city."""
        references: list[JobReference] = []
        seen_external_ids: set[str] = set()
        configured_locations = _unique_values(self._config.locations)
        official_locations = self._official_locations(configured_locations)
        if configured_locations and not official_locations:
            return []
        if not official_locations:
            official_locations = [("Germany", _GERMANY_PLACE_ID)]
        for search_term in _unique_values(self._config.search_terms):
            for location, place_id in official_locations:
                offset = 0
                while True:
                    jobs, total_hits = self._search(
                        search_term,
                        location=location,
                        place_id=place_id,
                        offset=offset,
                    )
                    if not jobs:
                        if offset < total_hits:
                            raise InvalidResponse(
                                "DHL search returned an empty page before total jobs"
                            )
                        break
                    for job in jobs:
                        reference = _parse_listing(job)
                        if reference is None or reference.external_id in seen_external_ids:
                            continue
                        seen_external_ids.add(reference.external_id)
                        references.append(reference)
                    offset += len(jobs)
                    if offset >= total_hits:
                        break
        return references

    def fetch_detail(self, reference: JobReference) -> FetchedOccurrence:
        """Return one complete DHL posting from the detail-page job data."""
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

        job = _detail_job(_ddo_payload(html))
        detail_external_id = _required_string(job, "jobSeqNo", "DHL job detail")
        if detail_external_id != reference.external_id:
            raise InvalidResponse("DHL detail jobSeqNo did not match its listing")

        title = _required_string(job, "title", "DHL job detail")
        company = _company_name(job)
        locations = _detail_locations(job)
        location = "; ".join(locations)
        description = _plain_html(_required_string(job, "description", "DHL job detail"))
        posted_at = _required_date(job.get("datePosted"))
        detail_complete = bool(description)
        snapshot_html: str | None = None
        snapshot_error_code: str | None = None
        if self._capture_snapshot is not None and self._capture_snapshot(
            reference.with_current_identity(title=title, posted_at=posted_at)
        ):
            try:
                snapshot_html = capture_browser_snapshot(
                    url=str(reference.detail_url),
                    script=_snapshot_page_js(reference.external_id),
                    source_name="dhl",
                )
            except (BrowserSourceError, InvalidResponse):
                snapshot_error_code = "snapshot_capture_failed"
            else:
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
            job_snapshot_error_code=snapshot_error_code,
            job_snapshot_html=snapshot_html,
        )

    def _search(
        self,
        search_term: str,
        *,
        location: str,
        place_id: str,
        offset: int,
    ) -> tuple[list[Mapping[str, Any]], int]:
        html = self._http_client.get_text_same_origin(
            _SEARCH_URL,
            allowed_origin=_ORIGIN,
            params={
                "keywords": search_term,
                "from": offset,
                "p": place_id,
                "location": location,
            },
            headers={"Accept": "text/html"},
        )
        payload = _ddo_payload(html)
        search = payload.get("eagerLoadRefineSearch")
        if not isinstance(search, Mapping):
            raise InvalidResponse("DHL search page missing eagerLoadRefineSearch data")
        if _required_non_negative_int(search, "status", "DHL search") != 200:
            raise InvalidResponse("DHL search response reported failure")
        hits = _required_non_negative_int(search, "hits", "DHL search")
        total_hits = _required_non_negative_int(search, "totalHits", "DHL search")
        data = search.get("data")
        if not isinstance(data, Mapping):
            raise InvalidResponse("DHL search response missing data object")
        raw_jobs = data.get("jobs")
        if not isinstance(raw_jobs, list):
            raise InvalidResponse("DHL search response missing jobs array")
        if hits != len(raw_jobs) or hits > total_hits:
            raise InvalidResponse("DHL search hit counts did not match its jobs array")
        jobs: list[Mapping[str, Any]] = []
        for job in raw_jobs:
            if not isinstance(job, Mapping):
                raise InvalidResponse("DHL search item must be an object")
            jobs.append(job)
        return jobs, total_hits

    def _official_locations(self, configured_locations: list[str]) -> list[tuple[str, str]]:
        locations: list[tuple[str, str]] = []
        seen_place_ids: set[str] = set()
        for configured_location in configured_locations:
            payload = self._http_client.post_json_same_origin(
                _WIDGETS_URL,
                {
                    "lang": "en_amer",
                    "deviceType": "desktop",
                    "country": "amer",
                    "pageName": "home",
                    "pageId": "page1",
                    "keywords": configured_location,
                    "ddoKey": "placeAutoComplete",
                },
                allowed_origin=_ORIGIN,
                headers={"Accept": "application/json"},
            )
            location = _official_location(payload, configured_location)
            if location is None or location[1] in seen_place_ids:
                continue
            seen_place_ids.add(location[1])
            locations.append(location)
        return locations

    def _require_reference(self, reference: JobReference) -> None:
        if reference.source is not self.source:
            raise ValueError("reference source must be dhl")
        if reference.source_instance != self.source_instance:
            raise ValueError("reference source_instance must match DHL")
        parts = urlsplit(str(reference.detail_url))
        expected_path = f"/amer/en/job/{quote(reference.external_id, safe='')}"
        if (
            parts.scheme != "https"
            or parts.netloc.casefold() != "careers.dhl.com"
            or unquote(parts.path) != unquote(expected_path)
        ):
            raise ValueError("reference detail_url must match the DHL job identity")


def _parse_listing(
    value: Mapping[str, Any],
) -> JobReference:
    # DHL city searches are radius searches. Keep every result returned by the
    # official query instead of forcing another address match in the project.
    external_id = _required_string(value, "jobSeqNo", "DHL search item")
    title = _required_string(value, "title", "DHL search item")
    locations = _listing_locations(value)
    if not locations:
        raise InvalidResponse("DHL search item missing location")
    detail_url = HttpUrl(f"{_DETAIL_BASE_URL}/{quote(external_id, safe='')}")
    apply_url = value.get("applyUrl")
    return JobReference(
        source=SourceKind.DHL,
        source_instance=_SOURCE_INSTANCE,
        external_id=external_id,
        detail_url=detail_url,
        platform_url=detail_url,
        listing_title=title,
        listing_company=_COMPANY_NAME,
        listing_location="; ".join(locations),
        # DHL postedDate is a listing update timestamp. Detail datePosted owns
        # the Setup "Posted within" decision, so discovery must not prefilter it.
        listing_posted_at=None,
        listing_application_url=(
            HttpUrl(apply_url) if isinstance(apply_url, str) and apply_url.strip() else None
        ),
    )


def _ddo_payload(html: str) -> Mapping[str, Any]:
    marker_index = html.find(_DDO_MARKER)
    if marker_index < 0:
        raise InvalidResponse("DHL page missing phApp.ddo data")
    value = html[marker_index + len(_DDO_MARKER) :].lstrip()
    try:
        payload, _ = json.JSONDecoder().raw_decode(value)
    except json.JSONDecodeError as error:
        raise InvalidResponse("DHL phApp.ddo data is not valid JSON") from error
    if not isinstance(payload, Mapping):
        raise InvalidResponse("DHL phApp.ddo data must be an object")
    return payload


def _detail_job(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    detail = payload.get("jobDetail")
    if not isinstance(detail, Mapping):
        raise InvalidResponse("DHL detail page missing jobDetail data")
    if _required_non_negative_int(detail, "status", "DHL job detail") != 200:
        raise InvalidResponse("DHL job detail reported failure")
    data = detail.get("data")
    if not isinstance(data, Mapping):
        raise InvalidResponse("DHL job detail missing data object")
    job = data.get("job")
    if not isinstance(job, Mapping):
        raise InvalidResponse("DHL job detail missing job object")
    return job


def _listing_locations(value: Mapping[str, Any]) -> list[str]:
    raw_locations = value.get("multi_location")
    locations = _unique_values(raw_locations if isinstance(raw_locations, list) else [])
    if locations:
        return locations
    return [_required_string(value, "location", "DHL search item")]


def _detail_locations(job: Mapping[str, Any]) -> list[str]:
    locations: list[str] = []
    raw_locations = job.get("multi_location")
    if isinstance(raw_locations, list) and raw_locations:
        for item in raw_locations:
            if not isinstance(item, Mapping):
                raise InvalidResponse("DHL job detail multi_location item must be an object")
            location = _required_string(item, "location", "DHL job location")
            if location.casefold() not in {value.casefold() for value in locations}:
                locations.append(location)
        return locations
    return [_required_string(job, "location", "DHL job detail")]


def _official_location(
    payload: Mapping[str, Any],
    configured_location: str,
) -> tuple[str, str] | None:
    autocomplete = payload.get("placeAutoComplete")
    if not isinstance(autocomplete, Mapping):
        raise InvalidResponse("DHL location lookup missing placeAutoComplete data")
    predictions = autocomplete.get("predictions")
    if not isinstance(predictions, list):
        raise InvalidResponse("DHL location lookup missing predictions array")
    matches: list[tuple[str, str]] = []
    for prediction in predictions:
        if not isinstance(prediction, Mapping):
            raise InvalidResponse("DHL location prediction must be an object")
        description = _required_string(prediction, "description", "DHL location prediction")
        place_id = _required_string(prediction, "place_id", "DHL location prediction")
        location_type = _required_string(
            prediction,
            "locationType",
            "DHL location prediction",
        )
        if (
            location_type.casefold() == "city"
            and description.rsplit(",", 1)[-1].strip().casefold() == "germany"
            and _normalized_city(description) == _normalized_city(configured_location)
        ):
            matches.append((description, place_id))
    if not matches:
        return None
    # The autocomplete may emit a duplicate city/state candidate with a generic
    # place ID. Prefer the concise "City, Germany" candidate used by the search UI.
    return next((match for match in matches if match[0].count(",") == 1), matches[0])


def _normalized_city(location: str) -> str:
    city = location.split(",", 1)[0].strip().casefold()
    city = city.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    return _CITY_ALIASES.get(city, city)


def _company_name(job: Mapping[str, Any]) -> str:
    for key in ("companyName", "jobCompany"):
        value = job.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise InvalidResponse("DHL job detail missing company name")


def _required_date(value: object) -> date:
    if not isinstance(value, str) or not value.strip():
        raise InvalidResponse("DHL job detail missing datePosted")
    try:
        return datetime.fromisoformat(value.strip()).date()
    except ValueError as error:
        raise InvalidResponse("DHL datePosted must be an ISO timestamp") from error


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


def _unique_values(values: Sequence[object]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        stripped = value.strip()
        folded = stripped.casefold()
        if stripped and folded not in seen:
            seen.add(folded)
            result.append(stripped)
    return result


def _plain_html(value: str) -> str:
    text = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    text = _SPACE.sub(" ", text)
    return _SPACE_BEFORE_PUNCTUATION.sub(r"\1", text).strip()
