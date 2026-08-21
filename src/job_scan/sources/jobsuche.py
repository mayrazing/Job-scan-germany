from __future__ import annotations

import base64
import json
from collections.abc import Callable, Mapping
from datetime import date, datetime
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import HttpUrl

from job_scan.company_size import native_company_size_evidence
from job_scan.config import AppConfig
from job_scan.domain import CompanySizeEvidence, CompanySizeSource, SourceKind
from job_scan.http_client import JOBSUCHE_API_KEY, InvalidResponse, PublicHttpClient
from job_scan.normalization import content_hash, normalize_text
from job_scan.sources.base import (
    BrowserSourceError,
    ExplicitlyClosed,
    FetchedOccurrence,
    JobReference,
    SourceError,
)
from job_scan.sources.job_snapshot_capture import (
    browser_snapshot_script,
    capture_browser_snapshot,
)

_BASE_URL = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4"
_SEARCH_BASE_URL = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6"
_EMPLOYER_PROFILE_BASE_URL = (
    "https://rest.arbeitsagentur.de/vermittlung/"
    "ag-darstellung-service/pc/v1/arbeitgeberdarstellung"
)
_HEADERS = {"X-API-Key": JOBSUCHE_API_KEY}
_DEFAULT_PAGE_SIZE = 100
_CLOSED_STATUSES = {"closed", "inactive", "unavailable", "nicht verfügbar"}

def _snapshot_page_js(expected_external_id: str) -> str:
    return browser_snapshot_script(
        r"""
  const expectedJobId = __EXPECTED_JOB_ID__;
  const isChallenge = () => /Just a moment|Access Denied/i.test(document.title || "") ||
    /Verify you are human|Sicherheitsüberprüfung/i.test(document.body?.innerText || "");
  if (isChallenge()) return {status: "challenge"};
  const pageJobId = location.pathname.match(/\/jobsuche\/jobdetail\/([^/?#]+)/)?.[1] ||
    document.querySelector("[data-job-id]")?.getAttribute("data-job-id") || "";
  const header = document.querySelector("#detail-kopfbereich-container");
  const title = document.querySelector("#detail-h1-heading");
  const description = document.querySelector("#detail-beschreibung-container");
  const employer = document.querySelector("#detail-agdarstellung-container");
  if (!pageJobId || !header || !title || !description) {
    return {status: "unavailable", error_code: "structure_mismatch"};
  }
  if (pageJobId !== expectedJobId) {
    return {status: "unavailable", error_code: "job_identity_mismatch"};
  }
  return buildJobSnapshot({
    snapshotKey: `arbeitsagentur:default:${expectedJobId}`,
    title: (title.innerText || title.textContent || "").trim(),
    sourceLabel: "Arbeitsagentur",
    accent: "#005f87",
    roots: [header, description, employer],
  });
""".strip().replace("__EXPECTED_JOB_ID__", json.dumps(expected_external_id))
    )


def lookup_company_size(
    source: CompanySizeSource,
    company: str,
    checked_at: datetime,
    http_client: PublicHttpClient,
) -> CompanySizeEvidence | None:
    """Read Betriebsgröße from the official employer-profile API."""
    if source.source_name != "arbeitsagentur":
        raise ValueError("company-size source must be arbeitsagentur")
    payload = http_client.get_json(str(source.lookup_url), headers=_HEADERS)
    reported_size = _nonempty_string(payload.get("betriebsgroesse"))
    if reported_size is None:
        return None
    return native_company_size_evidence(
        company=company,
        reported_size=reported_size,
        source_url=str(source.public_url),
        source_title=source.source_title,
        source_name="arbeitsagentur",
        checked_at=checked_at,
    )


class JobsucheAdapter:
    """Read public Bundesagentur listings and details."""

    source = SourceKind.ARBEITSAGENTUR
    source_instance = "default"

    def __init__(
        self,
        config: AppConfig,
        http_client: PublicHttpClient,
        *,
        page_size: int = _DEFAULT_PAGE_SIZE,
        request_base_url: str = _SEARCH_BASE_URL,
        capture_snapshot: Callable[[JobReference], bool] | None = None,
    ) -> None:
        if page_size <= 0:
            raise ValueError("page_size must be greater than zero")
        self._config = config
        self._http_client = http_client
        self._page_size = page_size
        self._request_base_url = request_base_url.rstrip("/")
        self._capture_snapshot = capture_snapshot
        if not self._request_base_url:
            raise ValueError("request_base_url must not be empty")
        self._detail_base_url = (
            _BASE_URL
            if self._request_base_url == _SEARCH_BASE_URL
            else self._request_base_url
        )
        self._discovery_errors: list[SourceError] = []

    def discover(self) -> list[JobReference]:
        """Return valid references from every configured term and location search."""
        self._discovery_errors.clear()
        references: list[JobReference] = []
        seen_external_ids: set[str] = set()
        locations: list[str | None] = (
            list(self._config.locations) if self._config.locations else [None]
        )

        for search_term in self._config.search_terms:
            for location in locations:
                page = 1
                while True:
                    params: dict[str, str | int] = {
                        "was": search_term,
                        "page": page,
                        "size": self._page_size,
                    }
                    if location is None:
                        params["wo"] = "Deutschland"
                    else:
                        params["wo"] = location
                    if self._config.posted_within_days is not None:
                        params["veroeffentlichtseit"] = self._config.posted_within_days
                    payload = self._http_client.get_json(
                        f"{self._request_base_url}/jobs",
                        params=params,
                        headers=_HEADERS,
                    )
                    listings = payload.get("ergebnisliste")
                    current = True
                    if listings is None:
                        listings = payload.get("stellenangebote")
                        current = False
                    if not isinstance(listings, list):
                        raise InvalidResponse(
                            "Jobsuche listing missing ergebnisliste array"
                        )

                    for listing in listings:
                        reference = self._parse_listing(listing, current=current)
                        if (
                            reference is not None
                            and reference.external_id not in seen_external_ids
                        ):
                            seen_external_ids.add(reference.external_id)
                            references.append(reference)

                    if self._is_last_page(payload, len(listings), page):
                        break
                    page += 1

        return references

    def drain_discovery_errors(self) -> list[SourceError]:
        """Return and clear item errors collected by the latest discovery."""
        errors = self._discovery_errors
        self._discovery_errors = []
        return errors

    def fetch_detail(self, reference: JobReference) -> FetchedOccurrence:
        """Return one complete detail or a pending-source-ready incomplete occurrence."""
        source_job_key = (
            f"{self.source.value}:{self.source_instance}:{reference.external_id}"
        )
        try:
            payload = self._http_client.get_json(str(reference.detail_url), headers=_HEADERS)
        except httpx.HTTPStatusError as error:
            if error.response.status_code in {404, 410}:
                raise ExplicitlyClosed(
                    source_job_key,
                    "http_404" if error.response.status_code == 404 else "http_410",
                ) from error
            raise

        if _has_closed_marker(payload):
            raise ExplicitlyClosed(source_job_key, "page_closed_marker")

        detail_refnr = _optional_string(payload, "refnr") or _required_string(
            payload, "referenznummer", "Jobsuche detail"
        )
        if detail_refnr != reference.external_id:
            raise InvalidResponse(
                f"Jobsuche detail refnr mismatch: expected {reference.external_id}, got {detail_refnr}"
            )

        company = (
            _optional_string(payload, "arbeitgeber")
            or _optional_string(payload, "firma")
            or reference.listing_company
        )
        title = _optional_string(payload, "stellenangebotsTitel") or reference.listing_title
        location = (
            _detail_location(payload)
            or _current_locations(payload)
            or reference.listing_location
        )
        posted_at = _optional_date(
            payload, "aktuelleVeroeffentlichungsdatum"
        ) or _optional_date(payload, "datumErsteVeroeffentlichung")
        if posted_at is None:
            posted_at = reference.listing_posted_at
        raw_description = payload.get("stellenangebotsBeschreibung")
        if raw_description is None:
            raw_description = ""
        elif not isinstance(raw_description, str):
            raise InvalidResponse(
                "Jobsuche field stellenangebotsBeschreibung must be a string"
            )
        detail_complete = bool(normalize_text(raw_description))
        description = raw_description.strip() if detail_complete else ""
        company_size_source = _company_size_source(payload, reference.external_id)
        snapshot_html: str | None = None
        snapshot_error_code: str | None = None
        if self._capture_snapshot is not None and self._capture_snapshot(
            reference.with_current_identity(title=title, posted_at=posted_at)
        ):
            try:
                snapshot_html = capture_browser_snapshot(
                    url=str(reference.platform_url or _public_detail_url(reference.external_id)),
                    script=_snapshot_page_js(reference.external_id),
                    source_name="arbeitsagentur",
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
            company_size_source=company_size_source,
        )

    def _parse_listing(
        self, listing: object, *, current: bool = False
    ) -> JobReference | None:
        if not isinstance(listing, Mapping):
            self._record_listing_error(None, "listing item must be an object")
            return None

        refnr_field = "referenznummer" if current else "refnr"
        company_field = "firma" if current else "arbeitgeber"
        title_field = "stellenangebotsTitel" if current else "titel"
        refnr = _nonempty_string(listing.get(refnr_field))
        company = _nonempty_string(listing.get(company_field))
        title = _nonempty_string(listing.get(title_field))
        required = (
            (refnr_field, refnr),
            (company_field, company),
            (title_field, title),
        )
        for field, value in required:
            if value is None:
                self._record_listing_error(
                    refnr,
                    f"listing item missing required field: {field}",
                )
                return None

        assert refnr is not None and company is not None and title is not None
        try:
            return JobReference(
                source=self.source,
                source_instance=self.source_instance,
                external_id=refnr,
                detail_url=HttpUrl(
                    _current_detail_url(refnr)
                    if current
                    else f"{self._detail_base_url}/jobdetails/{quote(refnr, safe='')}"
                ),
                platform_url=HttpUrl(_public_detail_url(refnr)),
                listing_title=title,
                listing_company=company,
                listing_location=(
                    _current_locations(listing)
                    if current
                    else _listing_location(listing)
                ),
                listing_posted_at=_optional_date(
                    listing,
                    (
                        "datumErsteVeroeffentlichung"
                        if current
                        else "aktuelleVeroeffentlichungsdatum"
                    ),
                ),
                listing_application_url=_optional_http_url(
                    listing, "externeURL" if current else "externeUrl"
                ),
            )
        except (InvalidResponse, ValueError) as error:
            self._record_listing_error(refnr, str(error))
            return None

    def _record_listing_error(self, refnr: str | None, message: str) -> None:
        item_key = (
            f"{self.source.value}:{self.source_instance}:{refnr}"
            if refnr is not None
            else None
        )
        self._discovery_errors.append(
            SourceError(
                category="contract",
                source=self.source,
                source_instance=self.source_instance,
                item_key=item_key,
                message=message,
            )
        )

    def _is_last_page(
        self, payload: Mapping[str, Any], listing_count: int, page: int
    ) -> bool:
        total = payload.get("maxErgebnisse")
        if total is None:
            return listing_count < self._page_size
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise InvalidResponse("Jobsuche listing has invalid maxErgebnisse")
        return page * self._page_size >= int(total)


def _required_string(payload: Mapping[str, Any], key: str, context: str) -> str:
    value = _nonempty_string(payload.get(key))
    if value is None:
        raise InvalidResponse(f"{context} missing {key}")
    return value


def _optional_string(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    parsed = _nonempty_string(value)
    if parsed is None:
        raise InvalidResponse(f"Jobsuche field {key} must be a non-empty string")
    return parsed


def _optional_http_url(payload: Mapping[str, Any], key: str) -> HttpUrl | None:
    value = _optional_string(payload, key)
    return HttpUrl(value) if value is not None else None


def _nonempty_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _optional_date(payload: Mapping[str, Any], key: str) -> date | None:
    raw = payload.get(key)
    if raw is None:
        return None
    value = _nonempty_string(raw)
    if value is None:
        raise InvalidResponse(f"Jobsuche field {key} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise InvalidResponse(f"Jobsuche field {key} must be an ISO date") from error


def _listing_location(listing: Mapping[str, Any]) -> str:
    workplace = listing.get("arbeitsort")
    return _format_workplace(workplace) if isinstance(workplace, Mapping) else ""


def _detail_location(payload: Mapping[str, Any]) -> str:
    workplaces = payload.get("arbeitsorte")
    if not isinstance(workplaces, list):
        return ""
    locations = [
        _format_workplace(workplace)
        for workplace in workplaces
        if isinstance(workplace, Mapping)
    ]
    return "; ".join(location for location in locations if location)


def _current_locations(payload: Mapping[str, Any]) -> str:
    workplaces = payload.get("stellenlokationen")
    if not isinstance(workplaces, list):
        return ""
    locations = []
    for workplace in workplaces:
        if not isinstance(workplace, Mapping):
            continue
        address = workplace.get("adresse")
        if isinstance(address, Mapping):
            location = _format_workplace(address)
            if location:
                locations.append(location)
    return "; ".join(locations)


def _current_detail_url(refnr: str) -> str:
    encoded = base64.b64encode(refnr.encode("utf-8")).decode("ascii")
    return f"{_BASE_URL}/jobdetails/{quote(encoded, safe='')}"


def _public_detail_url(refnr: str) -> str:
    return (
        "https://www.arbeitsagentur.de/jobsuche/jobdetail/"
        f"{quote(refnr, safe='')}"
    )


def _company_size_source(
    payload: Mapping[str, Any],
    refnr: str,
) -> CompanySizeSource | None:
    employer_hash = _nonempty_string(payload.get("arbeitgeberKundennummerHash"))
    if employer_hash is None:
        return None
    return CompanySizeSource(
        source_name="arbeitsagentur",
        lookup_url=HttpUrl(
            f"{_EMPLOYER_PROFILE_BASE_URL}/{quote(employer_hash, safe='')}"
        ),
        public_url=HttpUrl(_public_detail_url(refnr)),
        source_title="Arbeitsagentur · Betriebsgröße",
    )


def _format_workplace(workplace: Mapping[str, Any]) -> str:
    postcode = _nonempty_string(workplace.get("plz"))
    city = _nonempty_string(workplace.get("ort"))
    if postcode or city:
        return " ".join(value for value in (postcode, city) if value)
    return (
        _nonempty_string(workplace.get("region"))
        or _nonempty_string(workplace.get("land"))
        or ""
    )


def _has_closed_marker(payload: Mapping[str, Any]) -> bool:
    for key in ("status", "stellenangebotsStatus", "verfuegbarkeit"):
        status = _nonempty_string(payload.get(key))
        if status is not None and status.casefold() in _CLOSED_STATUSES:
            return True
    for key, value in (
        ("istStelleOnline", False),
        ("stellenangebotIstNichtMehrVerfuegbar", True),
        ("isClosed", True),
    ):
        if payload.get(key) is value:
            return True
    return False
