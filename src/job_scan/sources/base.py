from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date
from typing import Literal, Protocol, runtime_checkable

import httpx
from pydantic import BaseModel, Field, HttpUrl, ValidationError, computed_field

from job_scan.domain import (
    CompanyIndustryEvidence,
    CompanyIndustrySource,
    CompanySizeEvidence,
    CompanySizeSource,
    SourceKind,
)
from job_scan.http_client import BlockedResponse, InvalidResponse, ResponseTooLarge
from job_scan.job_snapshot import JobSnapshotReference
from job_scan.normalization import content_hash

ErrorCategory = Literal["http", "blocked", "contract", "incomplete", "browser"]
ClosureReason = Literal["http_404", "http_410", "page_closed_marker"]


class JobReference(BaseModel):
    source: SourceKind
    source_instance: str
    external_id: str
    detail_url: HttpUrl
    platform_url: HttpUrl | None = None
    listing_title: str
    listing_company: str
    listing_location: str
    listing_posted_at: date | None = None
    listing_application_url: HttpUrl | None = None
    listing_company_size_source: CompanySizeSource | None = None
    listing_company_industry_source: CompanyIndustrySource | None = None

    def with_current_identity(
        self,
        *,
        title: str,
        posted_at: date | None,
    ) -> JobReference:
        """Return the reference with identity facts confirmed by its detail page."""
        return self.model_copy(
            update={"listing_title": title, "listing_posted_at": posted_at}
        )


class SourceError(BaseModel):
    category: ErrorCategory
    source: SourceKind
    source_instance: str
    item_key: str | None = None
    status_code: int | None = None
    error_code: str | None = Field(default=None, pattern=r"^[a-z0-9_]{1,64}$")
    message: str


class ExplicitlyClosed(Exception):
    def __init__(self, source_job_key: str, reason: ClosureReason):
        super().__init__(f"{source_job_key} is closed: {reason}")
        self.source_job_key = source_job_key
        self.reason = reason


class ListingFilteredOut(Exception):
    """Skip one listing when a detail-only filter proves it is outside scope."""


class BrowserSourceError(RuntimeError):
    """Report a browser operation failure without importing a browser package."""

    def __init__(self, message: str, *, error_code: str = "browser_error") -> None:
        super().__init__(message)
        self.error_code = error_code


class FetchedOccurrence(BaseModel):
    source: SourceKind
    source_instance: str
    external_id: str
    url: HttpUrl
    company: str
    title: str
    location: str
    description: str
    posted_at: date | None
    content_hash: str
    detail_complete: bool
    fetch_error_code: str | None = None
    job_snapshot: JobSnapshotReference | None = None
    job_snapshot_error_code: str | None = None
    job_snapshot_html: str | None = Field(default=None, exclude=True)
    company_size_source: CompanySizeSource | None = None
    company_industry_source: CompanyIndustrySource | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def source_job_key(self) -> str:
        return f"{self.source.value}:{self.source_instance}:{self.external_id}"


class CompanyProfileFacts(BaseModel):
    """Return source-native size and industry read from one company profile visit."""

    company_size: CompanySizeEvidence | None = None
    company_industry: CompanyIndustryEvidence | None = None


class SourceAdapter(Protocol):
    source: SourceKind
    source_instance: str

    def discover(self) -> list[JobReference]: ...

    def fetch_detail(self, reference: JobReference) -> FetchedOccurrence: ...


@runtime_checkable
class _DiscoveryErrorProvider(Protocol):
    def drain_discovery_errors(self) -> list[SourceError]: ...


@runtime_checkable
class _ListingCompletionProvider(Protocol):
    @property
    def completed_listing(self) -> bool: ...


class SourceRunResult(BaseModel):
    source: SourceKind
    source_instance: str
    occurrences: list[FetchedOccurrence]
    discovered_source_job_keys: set[str]
    explicitly_closed_source_job_keys: set[str]
    errors: list[SourceError]
    completed_listing: bool


def partial_from_reference(reference: JobReference, error_code: str) -> FetchedOccurrence:
    """Build an incomplete occurrence without discarding listing data."""
    description = ""
    return FetchedOccurrence(
        source=reference.source,
        source_instance=reference.source_instance,
        external_id=reference.external_id,
        url=reference.platform_url or reference.detail_url,
        company=reference.listing_company,
        title=reference.listing_title,
        location=reference.listing_location,
        description=description,
        posted_at=reference.listing_posted_at,
        content_hash=content_hash(
            reference.listing_company,
            reference.listing_title,
            reference.listing_location,
            description,
        ),
        detail_complete=False,
        fetch_error_code=error_code,
        company_size_source=reference.listing_company_size_source,
        company_industry_source=reference.listing_company_industry_source,
    )


def run_source(
    adapter: SourceAdapter,
    *,
    posted_since: date | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> SourceRunResult:
    """Fetch one source and report cumulative jobs and warnings after each detail."""
    try:
        references = [
            reference
            for reference in adapter.discover()
            if _is_recent_enough(reference.listing_posted_at, posted_since)
        ]
    except Exception as error:  # noqa: BLE001
        # Source boundary: a broken adapter must not change missing-job availability.
        source_error, _ = _classify_error(adapter, error, item_key=None)
        return SourceRunResult(
            source=adapter.source,
            source_instance=adapter.source_instance,
            occurrences=[],
            discovered_source_job_keys=set(),
            explicitly_closed_source_job_keys=set(),
            errors=[source_error],
            completed_listing=False,
        )

    occurrences: list[FetchedOccurrence] = []
    discovered_keys = {_source_job_key(reference) for reference in references}
    explicitly_closed_keys: set[str] = set()
    errors = (
        adapter.drain_discovery_errors()
        if isinstance(adapter, _DiscoveryErrorProvider)
        else []
    )
    discovery_key_prefix = f"{adapter.source.value}:{adapter.source_instance}:"
    discovered_keys.update(
        error.item_key
        for error in errors
        if error.item_key is not None
        and error.item_key.startswith(discovery_key_prefix)
        and error.item_key != discovery_key_prefix
    )

    for reference in references:
        item_key = _source_job_key(reference)
        try:
            occurrence = adapter.fetch_detail(reference)
            if _is_recent_enough(occurrence.posted_at, posted_since):
                occurrences.append(occurrence)
            else:
                discovered_keys.discard(item_key)
        except ListingFilteredOut:
            discovered_keys.discard(item_key)
        except ExplicitlyClosed as error:
            explicitly_closed_keys.add(error.source_job_key)
        except Exception as error:  # noqa: BLE001
            # Detail boundary: retain this listing and continue with remaining jobs.
            source_error, error_code = _classify_error(adapter, error, item_key=item_key)
            errors.append(source_error)
            occurrence = partial_from_reference(reference, error_code)
            if _is_recent_enough(occurrence.posted_at, posted_since):
                occurrences.append(occurrence)
            else:
                discovered_keys.discard(item_key)
        if progress is not None:
            progress(len(occurrences), len(errors))

    return SourceRunResult(
        source=adapter.source,
        source_instance=adapter.source_instance,
        occurrences=occurrences,
        discovered_source_job_keys=discovered_keys,
        explicitly_closed_source_job_keys=explicitly_closed_keys,
        errors=errors,
        completed_listing=(
            adapter.completed_listing
            if isinstance(adapter, _ListingCompletionProvider)
            else True
        ),
    )


def _source_job_key(reference: JobReference) -> str:
    return f"{reference.source.value}:{reference.source_instance}:{reference.external_id}"


def _is_recent_enough(posted_at: date | None, posted_since: date | None) -> bool:
    """Keep unknown dates and dates on or after the configured cutoff."""
    return posted_since is None or posted_at is None or posted_at >= posted_since


def _classify_error(
    adapter: SourceAdapter, error: Exception, item_key: str | None
) -> tuple[SourceError, str]:
    category: ErrorCategory = "contract"
    status_code: int | None = None
    error_code = _error_code_from_name(type(error).__name__)

    if isinstance(error, BrowserSourceError):
        category = "browser"
        error_code = error.error_code
    elif isinstance(error, BlockedResponse):
        category = "blocked"
        error_code = "blocked"
    elif isinstance(error, (TimeoutError, httpx.TimeoutException)):
        category = "http"
        error_code = "timeout"
    elif isinstance(error, httpx.HTTPStatusError):
        category = "http"
        status_code = error.response.status_code
        error_code = f"http_{status_code}"
    elif isinstance(error, httpx.RequestError):
        category = "http"
    elif isinstance(error, ResponseTooLarge):
        category = "incomplete"
        error_code = "response_too_large"
    elif isinstance(error, (InvalidResponse, ValidationError, ValueError)):
        category = "contract"

    message = str(error) or type(error).__name__
    return (
        SourceError(
            category=category,
            source=adapter.source,
            source_instance=adapter.source_instance,
            item_key=item_key,
            status_code=status_code,
            error_code=error_code,
            message=message,
        ),
        error_code,
    )


def _error_code_from_name(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
